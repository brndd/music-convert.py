"""Scope of a reconciliation pass.

Inotify only tells us *that* something may have changed. This module captures
the *where*: the set of source directories a pass must re-examine. A full scan
is represented by :attr:`ScanScope.full`; otherwise ``dirs`` are re-read
non-recursively (their own files only) and ``trees`` are walked recursively
(for example when a populated directory is moved into the library).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath


def parent_dir(relpath: str) -> str:
    """Return the directory part of a POSIX-style relative path (``""`` at root)."""
    parent = PurePosixPath(relpath).parent
    return "" if str(parent) == "." else str(parent)


@dataclass
class ScanScope:
    dirs: set[str] = field(default_factory=set)
    trees: set[str] = field(default_factory=set)
    full: bool = False

    def dir_in_scope(self, dir_rel: str) -> bool:
        """Whether outputs/covers in *dir_rel* may be reconciled by this pass."""
        if self.full:
            return True
        if dir_rel in self.dirs:
            return True
        for root in self.trees:
            if root == "" or dir_rel == root or dir_rel.startswith(root + "/"):
                return True
        return False

    def path_in_scope(self, relpath: str) -> bool:
        return self.dir_in_scope(parent_dir(relpath))

    def candidate_prefixes(self) -> list[str] | None:
        """Coarse directory prefixes for a state query, or ``None`` for everything.

        The result only narrows the rows fetched from SQLite; callers must still
        apply :meth:`dir_in_scope` because ``LIKE`` is case-insensitive and a
        non-recursive directory prefix also matches deeper descendants.
        """
        if self.full:
            return None
        prefixes = self.dirs | self.trees
        # The root prefix matches the whole tree, so do not pretend to narrow.
        if "" in prefixes:
            return None
        return sorted(prefixes)
