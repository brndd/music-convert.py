"""inotify-based daemon mode.

The watcher's job is to notice *that* and *where* something may have changed,
and to keep its watches up to date. It records the affected directories so the
reconciler can re-examine just those, but it never decides what is correct:
the reconciler is re-run after every debounced batch of events, and a full scan
is forced periodically, on startup, and whenever a watch is lost.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from inotify_simple import INotify, flags

from .config import Config
from .progress import ProgressReporter
from .reconcile import Reconciler
from .scope import ScanScope
from .state import State
from .subproc import Cancelled

WATCH_FLAGS = (
    flags.CREATE
    | flags.DELETE
    | flags.MODIFY
    | flags.CLOSE_WRITE
    | flags.MOVED_FROM
    | flags.MOVED_TO
    | flags.ATTRIB
    | flags.DELETE_SELF
    | flags.MOVE_SELF
)

CHANGE_FLAGS = (
    flags.CREATE
    | flags.DELETE
    | flags.MODIFY
    | flags.CLOSE_WRITE
    | flags.MOVED_FROM
    | flags.MOVED_TO
    | flags.ATTRIB
)

POLL_MS = 1000


class Watcher:
    def __init__(
        self,
        cfg: Config,
        state: State,
        logger: logging.Logger,
        reporter: ProgressReporter | None = None,
        stop: threading.Event | None = None,
    ):
        self.cfg = cfg
        self.state = state
        self.log = logger
        self.stop = stop if stop is not None else threading.Event()
        self.reconciler = Reconciler(cfg, state, logger, reporter, self.stop)
        self.inotify = INotify()
        self.wd_to_dir: dict[int, Path] = {}
        self.dir_to_wd: dict[Path, int] = {}
        self.dirty = threading.Event()
        self._lock = threading.Lock()
        self._changed_dirs: set[str] = set()
        self._changed_trees: set[str] = set()
        self._full = False

    # -- watches ---------------------------------------------------------

    def add_dir(self, path: Path) -> None:
        if path in self.dir_to_wd:
            return
        try:
            wd = self.inotify.add_watch(path, WATCH_FLAGS)
        except OSError as exc:
            self.log.warning("cannot watch %s: %s", path, exc)
            return
        self.wd_to_dir[wd] = path
        self.dir_to_wd[path] = wd

    def add_tree(self, root: Path) -> None:
        if not root.is_dir():
            return
        self.add_dir(root)
        for dirpath, dirnames, _ in os.walk(root, followlinks=False):
            current = Path(dirpath)
            dirnames[:] = [d for d in dirnames if not (current / d).is_symlink()]
            for name in dirnames:
                self.add_dir(current / name)

    def remove_wd(self, wd: int) -> None:
        path = self.wd_to_dir.pop(wd, None)
        if path is not None:
            self.dir_to_wd.pop(path, None)

    # -- change tracking -------------------------------------------------

    def _rel_dir(self, path: Path) -> str:
        rel = Path(os.path.relpath(path, self.cfg.source)).as_posix()
        return "" if rel == "." else rel

    def _mark_dir(self, dir_rel: str) -> None:
        """Record that one directory's own files need re-checking."""
        with self._lock:
            self._changed_dirs.add(dir_rel)
            self.dirty.set()

    def _mark_tree(self, dir_rel: str) -> None:
        """Record that *dir_rel* and everything below it need re-checking."""
        with self._lock:
            self._changed_trees.add(dir_rel)
            self.dirty.set()

    def _mark_full(self) -> None:
        with self._lock:
            self._full = True
            self.dirty.set()

    def _take_scope(self) -> ScanScope:
        with self._lock:
            scope = ScanScope(
                dirs=set(self._changed_dirs),
                trees=set(self._changed_trees),
                full=self._full,
            )
            self._changed_dirs.clear()
            self._changed_trees.clear()
            self._full = False
            self.dirty.clear()
        return scope

    # -- event handling --------------------------------------------------

    def _handle_event(self, event) -> None:
        if event.mask & flags.Q_OVERFLOW:
            self.log.warning("inotify queue overflowed; rebuilding watches")
            self._rebuild_watches()
            self._mark_full()
            return

        base = self.wd_to_dir.get(event.wd)

        if event.mask & (flags.DELETE_SELF | flags.MOVE_SELF | flags.IGNORED):
            if base is not None:
                self.remove_wd(event.wd)
                self._mark_tree(self._rel_dir(base))
            return

        if base is None:
            return

        is_dir = bool(event.mask & flags.ISDIR)

        if is_dir and event.name and (event.mask & flags.MOVED_FROM):
            self._forget_moved_dir(base / event.name)

        if (
            is_dir
            and event.name
            and (event.mask & (flags.CREATE | flags.MOVED_TO))
        ):
            self.add_tree(base / event.name)

        if event.mask & CHANGE_FLAGS:
            self._mark_dir(self._rel_dir(base))
            if is_dir and event.name:
                # A directory entry appeared, moved, or was removed: its whole
                # subtree is now suspect.
                self._mark_tree(self._rel_dir(base / event.name))

    def _forget_moved_dir(self, path: Path) -> None:
        """Drop the watch of a directory that moved out of its old location.

        inotify watches an inode, so after a rename the stale path would keep
        answering for the new location; the ``MOVED_TO`` event re-adds it.
        """
        wd = self.dir_to_wd.get(path)
        if wd is None:
            return
        try:
            self.inotify.rm_watch(wd)
        except OSError:
            pass
        self.remove_wd(wd)

    def _rebuild_watches(self) -> None:
        for wd in list(self.wd_to_dir):
            try:
                self.inotify.rm_watch(wd)
            except OSError:
                pass
        self.wd_to_dir.clear()
        self.dir_to_wd.clear()
        self.add_tree(self.cfg.source)

    # -- lifecycle -------------------------------------------------------

    def _reconcile_loop(self) -> None:
        while not self.stop.is_set():
            triggered = self.dirty.wait(timeout=self.cfg.rescan_seconds)
            if self.stop.is_set():
                break
            if not triggered:
                self.log.info("periodic rescan")
                self._mark_full()
            else:
                self._settle()
            if self.stop.is_set():
                break
            scope = self._take_scope()
            scope = self._coerce_full(scope)
            try:
                summary = self.reconciler.reconcile(scope)
            except Cancelled:
                break
            except Exception:  # noqa: BLE001 - keep the daemon alive
                self.log.exception("reconciliation failed")
                continue
            for dir_rel in summary.dirty_dirs:
                self._mark_dir(dir_rel)

    def _coerce_full(self, scope: ScanScope) -> ScanScope:
        """A pass with nothing to do (e.g. a spurious wakeup) still needs a scan."""
        if not scope.full and not scope.dirs and not scope.trees:
            return ScanScope(full=True)
        return scope

    def _settle(self) -> None:
        """Wait until no new changes have arrived for ``settle_seconds``.

        The accumulated change set is deliberately left intact; only the wakeup
        event is cleared.
        """
        while not self.stop.is_set():
            if not self.dirty.wait(timeout=self.cfg.settle_seconds):
                return
            self.dirty.clear()

    def run(self) -> None:
        self.cfg.output.mkdir(parents=True, exist_ok=True)
        self.add_tree(self.cfg.source)
        # Inotify cannot replay events from before we started, so begin with a
        # full scan.
        self._mark_full()

        worker = threading.Thread(target=self._reconcile_loop, name="reconcile", daemon=True)
        worker.start()

        self.log.info("watching %s -> %s", self.cfg.source, self.cfg.output)
        try:
            while not self.stop.is_set():
                events = self.inotify.read(timeout=POLL_MS)
                for event in events:
                    self._handle_event(event)
                self._ensure_root_watch()
        finally:
            self.stop.set()
            self.dirty.set()
            worker.join()
            self.inotify.close()
            self.log.info("watcher stopped")

    def _ensure_root_watch(self) -> None:
        if self.cfg.source.is_dir() and self.cfg.source not in self.dir_to_wd:
            self.add_tree(self.cfg.source)
            self._mark_full()
