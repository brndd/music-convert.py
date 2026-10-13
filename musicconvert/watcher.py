"""inotify-based daemon mode.

The watcher's only job is to notice that something *may* have changed and to
keep its watches up to date. Correctness comes from the reconciler, which is
re-run after every debounced batch of events and periodically regardless.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

from inotify_simple import INotify, flags

from .config import Config
from .reconcile import Reconciler
from .state import State

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
    def __init__(self, cfg: Config, state: State, logger: logging.Logger):
        self.cfg = cfg
        self.state = state
        self.log = logger
        self.reconciler = Reconciler(cfg, state, logger)
        self.inotify = INotify()
        self.wd_to_dir: dict[int, Path] = {}
        self.dir_to_wd: dict[Path, int] = {}
        self.dirty = threading.Event()
        self.stop = threading.Event()

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

    # -- event handling --------------------------------------------------

    def _handle_event(self, event) -> None:
        if event.mask & flags.Q_OVERFLOW:
            self.log.warning("inotify queue overflowed; rebuilding watches")
            self._rebuild_watches()
            self.dirty.set()
            return

        if event.mask & (flags.DELETE_SELF | flags.MOVE_SELF | flags.IGNORED):
            self.remove_wd(event.wd)
            self.dirty.set()
            return

        base = self.wd_to_dir.get(event.wd)
        if base is None:
            return

        if event.name and (event.mask & flags.ISDIR) and (
            event.mask & (flags.CREATE | flags.MOVED_TO)
        ):
            self.add_tree(base / event.name)

        if event.mask & CHANGE_FLAGS:
            self.dirty.set()

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
            else:
                self._settle()
            if self.stop.is_set():
                break
            self.dirty.clear()
            try:
                summary = self.reconciler.reconcile()
            except Exception:  # noqa: BLE001 - keep the daemon alive
                self.log.exception("reconciliation failed")
                continue
            if summary.dirty_again:
                self.dirty.set()

    def _settle(self) -> None:
        """Wait until no new changes have arrived for ``settle_seconds``."""
        while not self.stop.is_set():
            if not self.dirty.wait(timeout=self.cfg.settle_seconds):
                return
            self.dirty.clear()

    def run(self) -> None:
        self.cfg.output.mkdir(parents=True, exist_ok=True)
        self.add_tree(self.cfg.source)
        self.dirty.set()

        worker = threading.Thread(target=self._reconcile_loop, name="reconcile", daemon=True)
        worker.start()

        previous = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, self._on_signal)

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
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            self.log.info("watcher stopped")

    def _ensure_root_watch(self) -> None:
        if self.cfg.source.is_dir() and self.cfg.source not in self.dir_to_wd:
            self.add_tree(self.cfg.source)

    def _on_signal(self, signum, _frame) -> None:
        self.log.info("received signal %s; shutting down", signum)
        self.stop.set()
