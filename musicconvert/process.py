"""Track processing: FLAC encoding and copied-format artwork updates."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import formats
from .config import OPUS_BITRATE, OPUS_DOWNMIX
from .formats import Picture


class BuildError(RuntimeError):
    pass


def encode_flac(src: Path, dest_tmp: Path, picture_path: Path | None) -> None:
    """Encode a FLAC file to Opus at *dest_tmp*."""
    cmd = ["opusenc", "--quiet", "--discard-pictures", "--bitrate", OPUS_BITRATE]
    if OPUS_DOWNMIX:
        cmd.append("--downmix-stereo")
    if picture_path is not None:
        cmd += ["--picture", str(picture_path)]
    cmd += [str(src), str(dest_tmp)]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise BuildError("opusenc is not installed") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace").strip()
        raise BuildError(f"opusenc failed on {src}: {detail}") from exc


def copy_with_artwork(
    src: Path,
    dest_tmp: Path,
    picture: Picture | None,
    dims: tuple[int, int],
    kind: str,
) -> None:
    """Copy *src* to *dest_tmp*, replacing embedded artwork when provided."""
    shutil.copyfile(src, dest_tmp)
    if picture is not None:
        formats.apply_artwork(dest_tmp, picture, dims, kind)


def verify(path: Path) -> None:
    formats.verify_audio(path)
