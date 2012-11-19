#!/usr/bin/env python
# coding: utf-8

import argparse
import errno
import logging
import os
import pygit2
import stat
import sys

from collections import namedtuple
from fuse import FuseOSError, FUSE, Operations, LoggingMixIn


Stat = namedtuple(
    'Stat',
    [
        'st_mode',
        'st_ino',
        'st_dev',
        'st_nlink',
        'st_uid',
        'st_gid',
        'st_size',
        'st_atime',
        'st_mtime',
        'st_ctime',
    ]
)


def copy_stat(st, **kwargs):
    result = Stat(*st)

    result = result._replace(**kwargs)
    result = result._replace(
        st_ino=0,
        # Remove any write bits from st_mode
        st_mode=result.st_mode & ~0222,
    )

    return result._asdict()


def git_tree_to_direntries(tree):
    return [entry.name.encode('utf-8') for entry in tree]


def git_tree_find(tree, path):
    parts = path.split('/')

    # Advance through sub-trees until end of path
    tree = reduce(
        lambda t, part: t[part].to_object() if t is not None else None,
        parts[:-1],
        tree,
    )

    # Get the entry for the last part of the path
    try:
        entry = tree[parts[-1]]
    except (TypeError, KeyError):
        # TypeError - reduce returned None
        # KeyError - file not found in tree
        raise FuseOSError(errno.ENOENT)
    return entry


class GitFS(Operations, LoggingMixIn):
    class GitFSError(Exception):
        pass

    def __init__(self, base_path):
        base_path = os.path.abspath(base_path)
        git_path = os.path.join(base_path, '.git')

        if os.path.exists(git_path):
            self.repo = pygit2.Repository(git_path)
        elif os.path.exists(base_path):
            self.repo = pygit2.Repository(base_path)
        else:
            raise self.GitFSError(
                'Path \'{0}\' does not point to a valid repository'.format(base_path)
            )

    @property
    def refs(self):
        """
        Gets a list of refs minus the leading 'refs' string.

        Example:
        >>> gitfs.refs
        ['/remotes/origin/master',
         '/remotes/origin/config-int-types',
         '/remotes/origin/index-open-cleanup',
         '/remotes/origin/attr-export',
         '/remotes/origin/HEAD']
        """
        return [r[4:].encode('utf-8') for r in self.repo.listall_references() if r.startswith('refs/')]

    def get_parent_ref(self, path):
        """
        Finds the parent ref for a path.

        Example:
        >>> gitfs.get_parent_ref('/remotes/origin/master/README.md')
        '/remotes/origin/master'
        """
        matches = filter(lambda r: path.startswith(r + '/'), self.refs)
        if len(matches) != 1:
            raise FuseOSError(errno.ENOENT)
        return matches[0]

    def get_child_refs(self, path):
        """
        Finds the refs under a path.

        Example:
        >>> gitfs.get_child_refs('/remotes')
        ['/remotes/origin/master',
         '/remotes/origin/config-int-types',
         '/remotes/origin/index-open-cleanup',
         '/remotes/origin/attr-export',
         '/remotes/origin/HEAD']
        """
        return filter(lambda r: r.startswith(path), self.refs)

    def get_path_children(self, path):
        """
        Gets the children under a path which is a parent of a ref.

        Example:
        >>> gitfs.get_path_children('/')
        ['remotes']
        """
        path_len = 0 if path == '/' else len(path)
        children = self.get_child_refs(path)
        children = [
            r[path_len:].split('/', 2)[1]
            for r in children
            if len(r) > path_len
        ]
        return list(frozenset(children))

    def get_reference_commit(self, ref_name):
        """
        Gets the commit object for a named reference.

        Example:
        >>> gitfs.get_reference_commit('/remotes/origin/master')
        <_pygit2.Commit object at 0xb741d150>
        """
        ref = self.repo.lookup_reference('refs' + ref_name)
        return self.repo[ref.oid]

    def getattr(self, path, fh=None):
        if path.startswith('/.'):
            raise FuseOSError(errno.ENOENT)

        repo_stat = os.lstat(self.repo.path)
        default_stat = copy_stat(repo_stat)

        # Path is parent of ref or is ref
        if self.get_child_refs(path):
            return default_stat

        # Path is child of ref
        ref = self.get_parent_ref(path)
        commit = self.get_reference_commit(ref)
        entry = git_tree_find(commit.tree, path[len(ref) + 1:])

        # Path is directory
        if entry.filemode & stat.S_IFDIR == stat.S_IFDIR:
            return default_stat

        # Path is stand-alone file
        blob = self.repo[entry.oid]
        size = len(blob.data)
        return copy_stat(repo_stat, st_size=size, st_mode=entry.filemode)

    def readdir(self, path, fh):
        # Path is parent of ref
        children = self.get_path_children(path)
        if children:
            return children

        # Path is ref
        if path in self.refs:
            path_tree = self.get_reference_commit(path).tree
            return git_tree_to_direntries(path_tree)

        # Path is a child of a ref
        ref = self.get_parent_ref(path)
        commit = self.get_reference_commit(ref)
        entry = git_tree_find(commit.tree, path[len(ref) + 1:])

        # Path is directory
        if entry.filemode & stat.S_IFDIR == stat.S_IFDIR:
            subtree = self.repo[entry.oid]
            return git_tree_to_direntries(subtree)

        return []

    def open(self, path, flags):
        if path.startswith('/.'):
            raise FuseOSError(errno.ENOENT)

        if flags & os.O_RDONLY != os.O_RDONLY:
            raise FuseOSError(errno.EACCES)

        return 0

    def read(self, path, size, offset, fh):
        if path.startswith('/.'):
            raise FuseOSError(errno.ENOENT)

        # Path is a child of ref
        ref = self.get_parent_ref(path)
        commit = self.get_reference_commit(ref)
        entry = git_tree_find(commit.tree, path[len(ref) + 1:])

        blob = entry.to_object()

        if offset == 0 and len(blob.data) <= size:
            return blob.data

        return blob.data[offset:offset + size]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Mounts the contents of a git repository in read-only mode using FUSE.'
    )
    parser.add_argument('git_path', metavar='<git_path>', help='Path to git repository.')
    parser.add_argument('mount_path', metavar='<mount_path>', help='Path to mount point.')

    if len(sys.argv) != 3:
        parser.print_help()
        sys.exit(0)

    logging.getLogger().setLevel(logging.DEBUG)

    args = parser.parse_args()
    fuse = FUSE(GitFS(args.git_path), args.mount_path, foreground=True, debug=True)