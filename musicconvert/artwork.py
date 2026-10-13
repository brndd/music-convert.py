"""Artwork discovery, resizing, and caching."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import ART_SUFFIXES, ARTWORK_EXTENT, ARTWORK_MAX_DIM

_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
}


@dataclass(frozen=True)
class ExternalArt:
    """An external cover image found next to a set of tracks."""

    path: Path
    relpath: str
    size: int
    mtime: int


def _normalise(text: str) -> str:
    keep = [c.lower() for c in text if c.isalnum()]
    return "".join(keep)


def find_external_art(directory: Path, album: str | None, albumartist: str | None) -> Path | None:
    """Find a cover image in *directory* using the project's naming conventions.

    Preference order:
      1. ``cover.*`` or ``folder.*``
      2. a name containing both the album artist and album
    """
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return None

    best: tuple[int, str, Path] | None = None
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() not in ART_SUFFIXES:
            continue
        stem = entry.stem.lower()
        if stem in {"cover", "folder"}:
            rank = 0
        else:
            norm = _normalise(entry.stem)
            artist = _normalise(albumartist) if albumartist else ""
            album_norm = _normalise(album) if album else ""
            if artist and album_norm and artist in norm and album_norm in norm:
                rank = 1
            else:
                continue
        candidate = (rank, entry.name.lower(), entry)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return best[2] if best else None


def _ext_for_mime(mime: str) -> str:
    return _MIME_EXT.get(mime.lower(), ".jpg")


def _cache_key(data: bytes, policy: str) -> str:
    digest = hashlib.blake2b(digest_size=16)
    digest.update(policy.encode())
    digest.update(b"\0")
    digest.update(data)
    return digest.hexdigest()


def _run_magick(src: Path, dst: Path) -> None:
    cmd = [
        "magick",
        str(src),
        "-define",
        f"jpeg:extent={ARTWORK_EXTENT}",
        "-resize",
        f"{ARTWORK_MAX_DIM}x{ARTWORK_MAX_DIM}>",
        str(dst),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except FileNotFoundError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("ImageMagick (magick) is not installed") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"magick failed on {src}: {detail}") from exc


def resize_bytes(data: bytes, mime: str, cache_dir: Path, policy: str) -> Path:
    """Resize image *data* and return a cached JPEG path.

    The result is deterministic for a given image and policy, so repeated
    calls are cheap and safe to run concurrently.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(data, policy)
    dest = cache_dir / f"{key}.jpg"
    if dest.exists():
        return dest

    src = cache_dir / f".{key}.{secrets.token_hex(4)}{_ext_for_mime(mime)}"
    src.write_bytes(data)
    tmp = cache_dir / f".{key}.{secrets.token_hex(4)}.jpg"
    try:
        _run_magick(src, tmp)
        os.replace(tmp, dest)
    finally:
        try:
            src.unlink()
        except OSError:
            pass
        try:
            tmp.unlink()
        except OSError:
            pass
    return dest


def resize_file(path: Path, cache_dir: Path, policy: str) -> Path:
    return resize_bytes(path.read_bytes(), _mime_for_path(path), cache_dir, policy)


def _mime_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix == ".gif":
        return "image/gif"
    return "image/jpeg"


def image_dimensions(path: Path) -> tuple[int, int]:
    """Return the pixel dimensions of an image via ImageMagick."""
    try:
        out = subprocess.run(
            ["magick", "identify", "-format", "%w %h", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        return (0, 0)
    parts = out.split()
    if len(parts) != 2:
        return (0, 0)
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return (0, 0)


def publish(src: Path, dest: Path) -> None:
    """Atomically place *src* at *dest*, copying across filesystems if needed."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.{secrets.token_hex(4)}.tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)
