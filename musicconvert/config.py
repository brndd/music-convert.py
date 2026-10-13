"""Runtime configuration and fixed conversion policy values."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from . import __version__

# Input formats we understand. Extension matching is case-insensitive.
SUPPORTED_EXTS = frozenset({".flac", ".mp3", ".opus", ".ogg"})
FLAC_EXT = ".flac"

# Conversion policy. Bumping POLICY_VERSION forces a rebuild of every output.
POLICY_VERSION = 1
OPUS_BITRATE = "96"
OPUS_DOWNMIX = True

# Artwork policy.
ARTWORK_MAX_DIM = 500
ARTWORK_EXTENT = "100KB"
ARTWORK_JPEG_QUALITY = None

DEFAULT_SETTLE_SECONDS = 3.0
DEFAULT_RESCAN_SECONDS = 900.0

ART_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})


def policy_id() -> str:
    """A stable identifier for the current conversion + artwork policy."""
    parts = [
        str(POLICY_VERSION),
        OPUS_BITRATE,
        "downmix" if OPUS_DOWNMIX else "stereo",
        str(ARTWORK_MAX_DIM),
        ARTWORK_EXTENT,
    ]
    return hashlib.blake2b("|".join(parts).encode(), digest_size=8).hexdigest()


@dataclass(frozen=True)
class Config:
    source: Path
    output: Path
    jobs: int
    quiet: bool = False
    dry_run: bool = False
    watch: bool = False
    settle_seconds: float = DEFAULT_SETTLE_SECONDS
    rescan_seconds: float = DEFAULT_RESCAN_SECONDS
    progress: str = "bar"
    state_dir: Path | None = None
    cache_dir: Path | None = None

    @property
    def policy(self) -> str:
        return policy_id()

    @property
    def resolved_state_dir(self) -> Path:
        if self.state_dir is not None:
            return self.state_dir
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        tag = hashlib.blake2b(str(self.output).encode(), digest_size=6).hexdigest()
        return Path(base) / "music-convert" / tag

    @property
    def resolved_cache_dir(self) -> Path:
        if self.cache_dir is not None:
            return self.cache_dir
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        return Path(base) / "music-convert" / "artwork"

    @property
    def state_path(self) -> Path:
        return self.resolved_state_dir / "state.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.resolved_state_dir / "lock"

    @property
    def version(self) -> str:
        return __version__
