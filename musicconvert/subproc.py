"""Cancellable subprocess execution.

Conversions shell out to ``opusenc`` and ``magick``. A Ctrl-C must stop them
promptly, but signals are only delivered to the main thread while the work runs
in worker threads. To bridge that, every child is tracked in a registry the
signal handler can terminate, and :func:`run` raises :class:`Cancelled` as soon
as a stop flag is set.
"""

from __future__ import annotations

import subprocess
import threading
from typing import Sequence

POLL_SECONDS = 0.1


class Cancelled(Exception):
    """Raised when a stop request interrupts work in progress."""

    def __init__(self, signum: int | None = None) -> None:
        super().__init__(signum)
        self.signum = signum


_lock = threading.RLock()
_children: "set[subprocess.Popen[bytes]]" = set()


def terminate_all() -> None:
    """Terminate every child process currently running.

    Safe to call from a signal handler: it only sends signals, never waits.
    """
    with _lock:
        children = list(_children)
    for proc in children:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass


def active() -> int:
    """Return the number of tracked child processes."""
    with _lock:
        return len(_children)


def run(
    cmd: Sequence[str],
    *,
    stop: threading.Event | None = None,
    poll: float = POLL_SECONDS,
) -> subprocess.CompletedProcess:
    """Run *cmd*, capturing output, aborting promptly when *stop* is set.

    Behaves like the subset of :func:`subprocess.run` we rely on: output is
    captured and a non-zero exit raises :class:`subprocess.CalledProcessError`.
    A stop request terminates the child and raises :class:`Cancelled`.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with _lock:
        _children.add(proc)
    try:
        while True:
            if stop is not None and stop.is_set():
                _stop(proc)
                raise Cancelled()
            try:
                out, err = proc.communicate(timeout=poll)
                break
            except subprocess.TimeoutExpired:
                continue
        if stop is not None and stop.is_set():
            raise Cancelled()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, output=out, stderr=err
            )
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    finally:
        with _lock:
            _children.discard(proc)


def _stop(proc: "subprocess.Popen[bytes]") -> None:
    """Terminate *proc*, escalating to a kill, and reap it."""
    for signaller in (proc.terminate, proc.kill):
        if proc.poll() is not None:
            break
        try:
            signaller()
        except OSError:
            break
        try:
            proc.communicate(timeout=1)
            return
        except subprocess.TimeoutExpired:
            continue
    try:
        proc.wait(timeout=1)
    except (subprocess.TimeoutExpired, OSError):
        pass


__all__ = ["Cancelled", "active", "run", "terminate_all"]
