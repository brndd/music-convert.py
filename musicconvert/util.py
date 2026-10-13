"""Small filesystem and path helpers."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from .config import SUPPORTED_EXTS


def rel_posix(path: Path, root: Path) -> str:
    """Return *path* relative to *root* as a POSIX-style string."""
    return Path(os.path.relpath(path, root)).as_posix()


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXTS


def kind_of(path: Path) -> str:
    """Return a short format kind for a supported file."""
    suffix = path.suffix.lower()
    if suffix == ".flac":
        return "flac"
    if suffix == ".mp3":
        return "mp3"
    if suffix == ".opus":
        return "opus"
    if suffix == ".ogg":
        return "ogg"
    raise ValueError(f"unsupported extension: {path}")


def temp_path(dest: Path, tag: str = "tmp") -> Path:
    """Return a unique sibling path for *dest* that keeps its suffix.

    Keeping the suffix matters for tools such as ``opusenc`` that infer the
    output format from the extension.
    """
    token = secrets.token_hex(4)
    return dest.parent / f".{dest.stem}.{tag}.{token}{dest.suffix}"


def atomic_replace(tmp: Path, dest: Path) -> None:
    os.replace(tmp, dest)


def remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def is_temp_name(name: str) -> bool:
    """Whether *name* looks like one of our temporary files."""
    return name.startswith(".") and ".tmp." in name


def prune_empty_dirs(root: Path) -> None:
    """Remove empty directories under *root* bottom-up, leaving *root* itself."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path == root:
            continue
        try:
            if not any(path.iterdir()):
                path.rmdir()
        except OSError:
            pass
