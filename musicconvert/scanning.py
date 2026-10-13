"""Recursive source-library scanning."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from pathlib import Path

from . import subproc
from .artwork import ExternalArt, find_external_art
from .formats import read_album_info
from .scope import ScanScope
from .util import is_supported, is_temp_name, kind_of, rel_posix


@dataclass(frozen=True)
class SourceFile:
    relpath: str
    path: Path
    kind: str
    size: int
    mtime: int

    @property
    def dir_rel(self) -> str:
        parent = PurePosixPath(self.relpath).parent
        return "" if str(parent) == "." else str(parent)

    @property
    def output_relpath(self) -> str:
        path = PurePosixPath(self.relpath)
        if self.kind == "flac":
            return str(path.with_suffix(".opus"))
        return self.relpath


@dataclass
class ScanResult:
    complete: bool
    files: list[SourceFile] = field(default_factory=list)
    dirs: set[str] = field(default_factory=set)
    external_art: dict[str, ExternalArt] = field(default_factory=dict)
    collisions: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def sources_by_rel(self) -> dict[str, SourceFile]:
        return {sf.relpath: sf for sf in self.files}

    def files_in_dir(self, dir_rel: str) -> list[SourceFile]:
        return [sf for sf in self.files if sf.dir_rel == dir_rel]

    @property
    def source_dirs(self) -> set[str]:
        return {sf.dir_rel for sf in self.files}


def scan_source(
    source: Path,
    scope: ScanScope | None = None,
    progress=None,
    stop=None,
) -> ScanResult:
    """Walk *source* and collect supported tracks, directories, and artwork.

    With ``scope=None`` the whole tree is scanned. A :class:`ScanScope` limits
    the walk to the directories that may have changed; the result then only
    describes that subset of the tree.

    The directories to visit are enumerated first so *progress* can drive a
    determinate display (``scan_start``/``scan_advance``). If *stop* is set the
    scan aborts, raising :class:`~musicconvert.subproc.Cancelled`.

    A scan is only marked complete when the source root is a readable directory
    and no errors were encountered. Callers must not delete outputs based on an
    incomplete scan.
    """
    result = ScanResult(complete=False)
    if not source.is_dir():
        return result

    def onerror(exc: OSError) -> None:
        result.errors.append(str(exc))

    dir_roots, tree_roots = _scan_roots(scope)
    entries = _enumerate(source, dir_roots, tree_roots, onerror)
    if progress is not None:
        progress.scan_start(len(entries))
    for rel_dir, names in entries:
        if stop is not None and stop.is_set():
            raise subproc.Cancelled()
        _collect_dir(source, rel_dir, names, result)
        if progress is not None:
            progress.scan_advance(rel_dir or ".")

    result.complete = not result.errors
    _find_external_art(source, result, stop=stop)
    _detect_collisions(result)
    return result


def _scan_roots(scope: ScanScope | None) -> tuple[list[str], list[str]]:
    """Return the non-recursive and recursive roots to walk for *scope*."""
    if scope is None or scope.full:
        return [], [""]
    trees = _prune_nested(scope.trees)
    dirs = [
        d
        for d in sorted(scope.dirs)
        if not any(_is_under(d, tree) for tree in trees)
    ]
    return dirs, trees


def _prune_nested(roots: set[str]) -> list[str]:
    """Drop any root that is already covered by another root."""
    ordered = sorted(set(roots), key=lambda s: (s.count("/"), s))
    kept: list[str] = []
    for root in ordered:
        if any(_is_under(root, other) for other in kept):
            continue
        kept.append(root)
    return kept


def _is_under(path: str, root: str) -> bool:
    if root == "":
        return True
    return path == root or path.startswith(root + "/")


def _enumerate(
    source: Path, dir_roots: list[str], tree_roots: list[str], onerror
) -> list[tuple[str, list[str] | None]]:
    """List every directory to visit and the file names it holds.

    Reading the names up front lets the caller show progress with a known total
    and avoids listing each directory twice. A non-recursive root that has
    vanished yields ``None`` so the caller can still retire its outputs.
    """
    entries: list[tuple[str, list[str] | None]] = []
    for rel_dir in dir_roots:
        current = source / rel_dir if rel_dir else source
        try:
            names: list[str] | None = os.listdir(current)
        except (FileNotFoundError, NotADirectoryError):
            names = None
        except OSError as exc:
            onerror(exc)
            names = []
        entries.append((rel_dir, names))
    for rel_dir in tree_roots:
        root = source / rel_dir if rel_dir else source
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(
            root, followlinks=False, onerror=onerror
        ):
            current = Path(dirpath)
            dirnames[:] = [d for d in dirnames if not (current / d).is_symlink()]
            entries.append((rel_posix(current, source), list(filenames)))
    return entries


def _collect_dir(
    source: Path, rel_dir: str, names: list[str] | None, result: ScanResult
) -> None:
    result.dirs.add(rel_dir)
    if not names:
        return
    current = source / rel_dir if rel_dir else source
    for name in names:
        _collect_file(source, current, name, result)


def _collect_file(source: Path, current: Path, name: str, result: ScanResult) -> None:
    path = current / name
    if path.is_symlink() or not path.is_file():
        return
    if is_temp_name(name) or not is_supported(path):
        return
    try:
        stat = path.stat()
    except OSError as exc:
        result.errors.append(f"{path}: {exc}")
        return
    result.files.append(
        SourceFile(
            relpath=rel_posix(path, source),
            path=path,
            kind=kind_of(path),
            size=stat.st_size,
            mtime=stat.st_mtime_ns,
        )
    )


def _find_external_art(source: Path, result: ScanResult, stop=None) -> None:
    by_dir: dict[str, list[SourceFile]] = {}
    for sf in result.files:
        by_dir.setdefault(sf.dir_rel, []).append(sf)

    for dir_rel, files in by_dir.items():
        if stop is not None and stop.is_set():
            raise subproc.Cancelled()
        directory = source / dir_rel if dir_rel else source
        first = min(files, key=lambda sf: sf.relpath)
        album, albumartist = read_album_info(first.path)
        art = find_external_art(directory, album, albumartist)
        if art is None:
            continue
        try:
            stat = art.stat()
        except OSError:
            continue
        result.external_art[dir_rel] = ExternalArt(
            path=art,
            relpath=rel_posix(art, source),
            size=stat.st_size,
            mtime=stat.st_mtime_ns,
        )


def _detect_collisions(result: ScanResult) -> None:
    grouped: dict[str, list[str]] = {}
    for sf in result.files:
        grouped.setdefault(sf.output_relpath, []).append(sf.relpath)
    result.collisions = {out: srcs for out, srcs in grouped.items() if len(srcs) > 1}
