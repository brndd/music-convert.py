"""Command-line entry point."""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import shutil
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .config import (
    DEFAULT_RESCAN_SECONDS,
    DEFAULT_SETTLE_SECONDS,
    SUPPORTED_EXTS,
    Config,
)
from .progress import LiveAwareStream, ProgressReporter
from .reconcile import Reconciler
from .state import State
from .subproc import Cancelled, terminate_all

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music-convert",
        description=(
            "Convert a FLAC library to Opus, copying and re-tagging MP3, Opus, "
            "and Ogg Vorbis files."
        ),
    )
    parser.add_argument("source", type=Path, help="input music directory")
    parser.add_argument("output", type=Path, help="output directory")
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help="number of concurrent jobs (default: CPU count)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only log warnings and errors; disables progress")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="run continuously and keep the output in sync using inotify",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show planned changes without modifying anything",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=DEFAULT_SETTLE_SECONDS,
        help="how long to wait for changes to settle (watch mode)",
    )
    parser.add_argument(
        "--rescan-interval",
        type=float,
        default=DEFAULT_RESCAN_SECONDS,
        help="periodic full rescan interval in seconds (watch mode)",
    )
    parser.add_argument(
        "--progress",
        choices=("bar", "lines", "none"),
        default="bar",
        help="progress display: bar (default), lines (one line per item), or none",
    )
    parser.add_argument("--state-dir", type=Path, default=None, help="directory for persistent state")
    parser.add_argument("--cache-dir", type=Path, default=None, help="directory for artwork cache")
    parser.add_argument("-v", "--version", action="version", version=f"music-convert {__version__}")
    return parser


def _validate_paths(source: Path, output: Path) -> tuple[Path, Path]:
    source = source.resolve()
    output = output.resolve()
    if not source.is_dir():
        raise SystemExit(f"input directory not found: {source}")
    if source == output:
        raise SystemExit("input and output directories must differ")
    if _is_within(output, source) or _is_within(source, output):
        raise SystemExit("input and output directories must not overlap")
    return source, output


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _check_dependencies(watch: bool) -> None:
    missing = [name for name in ("opusenc", "magick") if shutil.which(name) is None]
    if watch:
        try:
            import inotify_simple  # noqa: F401
        except ImportError:
            missing.append("inotify_simple")
    if missing:
        raise SystemExit("missing dependencies: " + ", ".join(missing))


def _count_inputs(source: Path) -> int:
    count = 0
    for _dirpath, _dirnames, filenames in os.walk(source, followlinks=False):
        for name in filenames:
            if Path(name).suffix.lower() in SUPPORTED_EXTS:
                count += 1
    return count


def _confirm(source: Path, output: Path) -> None:
    total = _count_inputs(source)
    print(f"This will convert up to {total} audio files from {source} into {output}.")
    reply = input("Are you sure? (y/N) ")
    if reply.strip().lower() not in {"y", "yes"}:
        raise SystemExit("Aborting.")


class _ProcessLock:
    """A non-blocking exclusive lock so two runs cannot fight over one output."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "w")
        try:
            fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(
                f"another music-convert run is already using this output ({path})"
            )

    def release(self) -> None:
        try:
            fcntl.flock(self._file, fcntl.LOCK_UN)
        finally:
            self._file.close()


def _install_signal_handlers(stop: threading.Event, logger: logging.Logger) -> dict:
    """Turn SIGINT/SIGTERM into a clean, prompt Cancelled.

    The handler kills any in-flight child processes and raises, so a blocking
    wait is interrupted immediately instead of after the current build.
    """
    previous: dict = {}

    def handler(signum, _frame):
        if stop.is_set():
            os._exit(130)
        logger.info("received signal %s; cancelling", signum)
        stop.set()
        terminate_all()
        raise Cancelled(signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, handler)
    return previous


def _restore_signal_handlers(previous: dict) -> None:
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format=LOG_FORMAT,
        stream=LiveAwareStream(),
    )
    logger = logging.getLogger("music-convert")

    stop = threading.Event()
    previous = _install_signal_handlers(stop, logger)
    try:
        return _run(args, parser, logger, stop)
    except Cancelled as exc:
        logger.warning("cancelled")
        return 128 + exc.signum if exc.signum else 130
    finally:
        _restore_signal_handlers(previous)


def _run(args, parser, logger: logging.Logger, stop: threading.Event) -> int:
    source, output = _validate_paths(args.source, args.output)

    jobs = args.jobs if args.jobs else (os.cpu_count() or 1)
    if jobs < 1:
        parser.error("--jobs must be a positive integer")

    cfg = Config(
        source=source,
        output=output,
        jobs=jobs,
        quiet=args.quiet,
        dry_run=args.dry_run,
        watch=args.watch,
        settle_seconds=args.settle_seconds,
        rescan_seconds=args.rescan_interval,
        progress="none" if args.quiet else args.progress,
        state_dir=args.state_dir,
        cache_dir=args.cache_dir,
    )

    _check_dependencies(watch=args.watch)

    if not args.watch and not args.yes and not args.dry_run:
        _confirm(source, output)

    cfg.resolved_state_dir.mkdir(parents=True, exist_ok=True)
    lock = _ProcessLock(cfg.lock_path)
    state = State(cfg.state_path)
    reporter = ProgressReporter(cfg.progress)
    try:
        if args.watch:
            from .watcher import Watcher

            Watcher(cfg, state, logger, reporter, stop).run()
            return 0

        summary = Reconciler(cfg, state, logger, reporter, stop).reconcile()
        ok = not summary.failed and not summary.collisions and not summary.incomplete
        return 0 if ok else 1
    finally:
        state.close()
        lock.release()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
