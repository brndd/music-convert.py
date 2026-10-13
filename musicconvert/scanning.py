"""Recursive source-library scanning."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from pathlib import Path

from .artwork import ExternalArt, find_external_art
from .formats import read_album_info
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


def scan_source(source: Path) -> ScanResult:
    """Walk *source* and collect supported tracks, directories, and artwork.

    A scan is only marked complete when the source root is a readable directory
    and no errors were encountered. Callers must not delete outputs based on an
    incomplete scan.
    """
    result = ScanResult(complete=False)
    if not source.is_dir():
        return result

    def onerror(exc: OSError) -> None:
        result.errors.append(str(exc))

    for dirpath, dirnames, filenames in os.walk(
        source, followlinks=False, onerror=onerror
    ):
        current = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (current / d).is_symlink()]
        result.dirs.add(rel_posix(current, source))

        for name in filenames:
            path = current / name
            if path.is_symlink() or not path.is_file():
                continue
            if is_temp_name(name) or not is_supported(path):
                continue
            try:
                stat = path.stat()
            except OSError as exc:
                result.errors.append(f"{path}: {exc}")
                continue
            result.files.append(
                SourceFile(
                    relpath=rel_posix(path, source),
                    path=path,
                    kind=kind_of(path),
                    size=stat.st_size,
                    mtime=stat.st_mtime_ns,
                )
            )

    result.complete = not result.errors
    _find_external_art(source, result)
    _detect_collisions(result)
    return result


def _find_external_art(source: Path, result: ScanResult) -> None:
    by_dir: dict[str, list[SourceFile]] = {}
    for sf in result.files:
        by_dir.setdefault(sf.dir_rel, []).append(sf)

    for dir_rel, files in by_dir.items():
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
