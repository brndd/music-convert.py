"""Rich-powered progress reporting for long conversion runs.

Three modes are supported:

``bar``
    In-place progress bars on stderr, built with :mod:`rich.progress`. A
    ``scanning`` bar covers the source-tree walk, then a ``working`` bar covers
    conversion. When stderr is not a terminal there is nothing to redraw, so a
    busy run falls back to ``lines``.
``lines``
    One line per processed item, suitable for logs and non-interactive runs.
``none``
    No progress output at all (used by ``-q``).

The bars are drawn on the same stream as the logging output. Log records are
emitted through :func:`logging` as usual; while a bar is displayed, Rich
installs a proxy on ``sys.stderr`` so those records are printed above the bar
instead of colliding with it.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Column
from rich.text import Text

if TYPE_CHECKING:
    from typing import TextIO

BAR_WIDTH = 30


class LiveAwareStream:
    """A stream that always writes to the current ``sys.stderr``.

    Rich's live display replaces ``sys.stderr`` with a proxy while a progress
    bar is on screen. Delegating at write time means a :class:`logging.Handler`
    built once at startup still goes through that proxy, so log records appear
    above the bar rather than on top of it.
    """

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()

    def isatty(self) -> bool:
        return sys.stderr.isatty()

    def writable(self) -> bool:
        return True


class ProgressReporter:
    def __init__(self, mode: str = "bar", stream: "TextIO | None" = None):
        self.mode = mode
        self.console = Console(file=stream if stream is not None else sys.stderr)
        self._progress: Progress | None = None
        self._scan_task = None
        self._scan_total = 0
        self._scan_done = 0
        self._work_task = None
        self._total = 0
        self._done = 0

    # -- scanning phase --------------------------------------------------

    def scan_start(self, total: int) -> None:
        self._scan_total = max(total, 0)
        self._scan_done = 0
        if self._scan_total == 0 or not self._use_bar:
            return
        self._scan_task = self._ensure().add_task(
            "scanning", total=self._scan_total, item=""
        )

    def scan_advance(self, label: str = "") -> None:
        if self._scan_total == 0:
            return
        self._scan_done += 1
        if self._scan_task is not None:
            self._progress.update(self._scan_task, advance=1, item=label)

    # -- working phase ---------------------------------------------------

    def start(self, total: int) -> None:
        self._total = max(total, 0)
        self._done = 0
        if self._total == 0 or not self._use_bar:
            return
        progress = self._ensure()
        if self._scan_task is not None:
            progress.remove_task(self._scan_task)
            self._scan_task = None
        self._work_task = progress.add_task("working", total=self._total, item="")

    def advance(self, label: str = "") -> None:
        if self._total == 0:
            return
        self._done += 1
        if self._work_task is not None:
            self._progress.update(self._work_task, advance=1, item=label)
        elif self.mode in {"bar", "lines"}:
            self._emit_line(label)

    def finish(self) -> None:
        if self._progress is not None:
            self._progress.stop()
        self._progress = None
        self._scan_task = None
        self._work_task = None

    # -- helpers ---------------------------------------------------------

    @property
    def _use_bar(self) -> bool:
        return self.mode == "bar" and self.console.is_terminal

    def _ensure(self) -> Progress:
        if self._progress is None:
            self._progress = Progress(
                TextColumn("[bold cyan]{task.description}"),
                BarColumn(bar_width=BAR_WIDTH),
                MofNCompleteColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                TextColumn(
                    "{task.fields[item]}",
                    style="dim",
                    table_column=Column(ratio=1, no_wrap=True, overflow="ellipsis"),
                ),
                console=self.console,
                transient=False,
                expand=True,
            )
            self._progress.start()
        return self._progress

    def _emit_line(self, label: str) -> None:
        line = Text()
        line.append(f"[{self._done:>4}/{self._total}]", style="dim")
        if label:
            line.append(f" {label}")
        self.console.print(line)


__all__ = ["ProgressReporter", "LiveAwareStream"]
