"""Metadata and embedded-artwork handling for FLAC, MP3, Opus, and Ogg Vorbis."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

import mutagen
from mutagen.flac import FLAC
from mutagen.flac import Picture as FlacPicture
from mutagen.id3 import APIC, ID3, PictureType

from .util import kind_of

FRONT_COVER = 3


@dataclass(frozen=True)
class Picture:
    data: bytes
    mime: str
    is_front: bool


def read_album_info(path: Path) -> tuple[str | None, str | None]:
    """Return ``(album, albumartist)`` for *path*, or ``(None, None)``."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return None, None
    if audio is None or audio.tags is None:
        return None, None

    def first(key: str) -> str | None:
        values = audio.tags.get(key)
        if values:
            return str(values[0])
        return None

    album = first("album")
    albumartist = first("albumartist") or first("artist")
    return album, albumartist


def _load_id3(path: Path) -> ID3:
    try:
        return ID3(path, translate=False)
    except Exception:
        return ID3()


def _flac_pictures(path: Path) -> list[Picture]:
    audio = FLAC(path)
    return [
        Picture(data=p.data, mime=p.mime or "image/jpeg", is_front=p.type == FRONT_COVER)
        for p in audio.pictures
    ]


def _mp3_pictures(path: Path) -> list[Picture]:
    tags = _load_id3(path)
    return [
        Picture(data=frame.data, mime=frame.mime or "image/jpeg", is_front=frame.type == FRONT_COVER)
        for frame in tags.getall("APIC")
    ]


def _vorbis_pictures(path: Path) -> list[Picture]:
    audio = mutagen.File(path)
    if audio is None or audio.tags is None:
        return []
    tags = audio.tags
    pictures: list[Picture] = []

    for encoded in tags.get("metadata_block_picture", []):
        try:
            raw = base64.b64decode(encoded)
            pic = FlacPicture(raw)
        except Exception:
            continue
        pictures.append(
            Picture(data=pic.data, mime=pic.mime or "image/jpeg", is_front=pic.type == FRONT_COVER)
        )

    mimes = list(tags.get("coverartmime", []))
    for index, encoded in enumerate(tags.get("coverart", [])):
        try:
            raw = base64.b64decode(encoded)
        except Exception:
            continue
        mime = mimes[index] if index < len(mimes) else "image/jpeg"
        pictures.append(Picture(data=raw, mime=mime, is_front=False))

    return pictures


def extract_pictures(path: Path) -> list[Picture]:
    kind = kind_of(path)
    if kind == "flac":
        return _flac_pictures(path)
    if kind == "mp3":
        return _mp3_pictures(path)
    return _vorbis_pictures(path)


def choose_embedded(pictures: list[Picture]) -> Picture | None:
    """Prefer a front cover, otherwise the first usable picture."""
    for pic in pictures:
        if pic.is_front:
            return pic
    return pictures[0] if pictures else None


def apply_artwork(dst: Path, picture: Picture, dims: tuple[int, int], kind: str) -> None:
    """Replace embedded artwork in the copied file at *dst*."""
    if kind == "mp3":
        _apply_mp3(dst, picture)
    else:
        _apply_vorbis(dst, picture, dims)


def _apply_mp3(dst: Path, picture: Picture) -> None:
    tags = _load_id3(dst)
    version = tags.version
    tags.delall("APIC")
    tags.add(
        APIC(
            encoding=3,
            mime=picture.mime,
            type=PictureType.COVER_FRONT,
            desc="Cover",
            data=picture.data,
        )
    )
    tags.save(dst, v2_version=3 if version == 3 else 4)


def _apply_vorbis(dst: Path, picture: Picture, dims: tuple[int, int]) -> None:
    audio = mutagen.File(dst)
    if audio is None:
        raise RuntimeError(f"cannot parse audio file: {dst}")
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    for key in list(tags.keys()):
        if key.lower() in {"coverart", "coverartmime"}:
            del tags[key]

    width, height = dims
    flac_picture = FlacPicture()
    flac_picture.data = picture.data
    flac_picture.type = PictureType.COVER_FRONT
    flac_picture.mime = picture.mime
    flac_picture.desc = "Cover"
    flac_picture.width = width
    flac_picture.height = height
    flac_picture.depth = 24
    tags["metadata_block_picture"] = [base64.b64encode(flac_picture.write()).decode("ascii")]
    audio.save()


def verify_audio(path: Path) -> None:
    """Raise if *path* cannot be parsed as audio by Mutagen."""
    audio = mutagen.File(path)
    if audio is None:
        raise RuntimeError(f"output is not readable audio: {path}")
