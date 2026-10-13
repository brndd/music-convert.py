"""End-to-end integration tests for the music-convert rewrite.

These build small real audio fixtures with ffmpeg, embed artwork with Mutagen,
run the converter, and verify the results. Run with::

    python3 tests/test_integration.py
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, PictureType
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

REPO = Path(__file__).resolve().parent.parent
CONVERTER = REPO / "music-convert"

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok: {message}")
    else:
        print(f"  FAIL: {message}")
        failures.append(message)


def run(*args: str, check_return: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CONVERTER), *args],
        capture_output=True,
        text=True,
        check=check_return,
    )


def make_cover(path: Path, size: int, colour: str) -> bytes:
    subprocess.run(
        ["magick", "-size", f"{size}x{size}", f"xc:{colour}", str(path)],
        check=True,
        capture_output=True,
    )
    return path.read_bytes()


def make_audio(path: Path, seconds: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    codec = {
        ".flac": ["-c:a", "flac"],
        ".mp3": ["-c:a", "libmp3lame", "-b:a", "128k"],
        ".ogg": ["-c:a", "libvorbis", "-q:a", "4"],
        ".opus": ["-c:a", "libopus", "-b:a", "96k"],
    }[path.suffix]
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            *codec,
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def set_tags(path: Path, **tags: str) -> None:
    audio = mutagen.File(path, easy=True)
    assert audio is not None
    for key, value in tags.items():
        audio[key] = value
    audio.save()


def embed(path: Path, image: bytes) -> None:
    if path.suffix == ".flac":
        audio = FLAC(path)
        pic = Picture()
        pic.data = image
        pic.type = PictureType.COVER_FRONT
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        audio.add_picture(pic)
        audio.save()
    elif path.suffix == ".mp3":
        tags = ID3(path)
        tags.add(
            APIC(
                encoding=3,
                mime="image/jpeg",
                type=PictureType.COVER_FRONT,
                desc="Cover",
                data=image,
            )
        )
        tags.save(path)
    else:
        cls = OggOpus if path.suffix == ".opus" else OggVorbis
        audio = cls(path)
        pic = Picture()
        pic.data = image
        pic.type = PictureType.COVER_FRONT
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        audio["metadata_block_picture"] = [
            base64.b64encode(pic.write()).decode("ascii")
        ]
        audio.save()


def first_embedded(path: Path) -> bytes | None:
    if path.suffix == ".flac":
        pics = FLAC(path).pictures
        return pics[0].data if pics else None
    if path.suffix == ".mp3":
        apics = ID3(path).getall("APIC")
        return apics[0].data if apics else None
    cls = OggOpus if path.suffix == ".opus" else OggVorbis
    values = cls(path).get("metadata_block_picture", [])
    if not values:
        return None
    return Picture(base64.b64decode(values[0])).data


def image_size(data: bytes) -> tuple[int, int]:
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
        handle.write(data)
        name = handle.name
    try:
        out = subprocess.run(
            ["magick", "identify", "-format", "%w %h", name],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        width, height = out.split()
        return int(width), int(height)
    finally:
        os.unlink(name)


def audio_md5(path: Path) -> str:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-f", "md5", "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def build_library(root: Path) -> tuple[Path, bytes, bytes]:
    source = root / "library"
    cover1 = make_cover(root / "cover1.jpg", 1000, "red")
    cover2 = make_cover(root / "cover2.jpg", 800, "blue")

    album_a = source / "Artist" / "Album A"
    flac = album_a / "01 - one.flac"
    mp3 = album_a / "02 - two.mp3"
    ogg = album_a / "03 - three.ogg"
    opus = album_a / "04 - four.opus"
    for path in (flac, mp3, ogg, opus):
        make_audio(path)
        set_tags(path, album="Album A", albumartist="Artist", title=path.stem)
        embed(path, cover1)

    album_b = source / "Artist" / "Album B"
    flac_b = album_b / "01 - five.flac"
    make_audio(flac_b)
    set_tags(flac_b, album="Album B", albumartist="Artist", title="five")
    shutil.copyfile(root / "cover2.jpg", album_b / "cover.jpg")

    album_c = source / "Artist" / "Album C"
    flac_c = album_c / "01 - six.flac"
    mp3_c = album_c / "02 - seven.mp3"
    for path in (flac_c, mp3_c):
        make_audio(path)
        set_tags(path, album="Album C", albumartist="Artist", title=path.stem)

    return source, cover1, cover2


def test_conversion(root: Path) -> None:
    print("test_conversion")
    source, cover1, cover2 = build_library(root)
    output = root / "converted"
    state = root / "state"
    cache = root / "cache"

    result = run("-y", "--state-dir", str(state), "--cache-dir", str(cache), str(source), str(output))
    check(result.returncode == 0, f"converter exits 0 ({result.stderr.strip()})")

    album_a = output / "Artist" / "Album A"
    check((album_a / "01 - one.opus").is_file(), "FLAC converted to Opus")
    check((album_a / "02 - two.mp3").is_file(), "MP3 copied")
    check((album_a / "03 - three.ogg").is_file(), "Ogg copied")
    check((album_a / "04 - four.opus").is_file(), "Opus copied")

    opus_art = first_embedded(album_a / "01 - one.opus")
    check(opus_art is not None, "converted Opus has embedded art")
    if opus_art:
        width, height = image_size(opus_art)
        check(max(width, height) <= 500, f"converted art resized ({width}x{height})")
        check(len(opus_art) < 100_000, f"converted art under 100KB ({len(opus_art)})")

    for name in ("02 - two.mp3", "03 - three.ogg", "04 - four.opus"):
        data = first_embedded(album_a / name)
        check(data is not None, f"{name} has artwork")
        if data:
            check(max(image_size(data)) <= 500, f"{name} artwork resized")

    check((album_a / "cover.jpg").is_file(), "Album A cover.jpg generated")
    check((output / "Artist" / "Album B" / "cover.jpg").is_file(), "Album B cover.jpg generated")
    check(not (output / "Artist" / "Album C" / "cover.jpg").exists(), "Album C has no cover")

    # Copied audio must be byte-identical at the stream level.
    for rel, out_rel in [
        ("Artist/Album A/02 - two.mp3", "Artist/Album A/02 - two.mp3"),
        ("Artist/Album A/03 - three.ogg", "Artist/Album A/03 - three.ogg"),
        ("Artist/Album A/04 - four.opus", "Artist/Album A/04 - four.opus"),
    ]:
        check(
            audio_md5(source / rel) == audio_md5(output / out_rel),
            f"audio preserved for {out_rel}",
        )

    # External cover in Album B should be used for the copied FLAC's artwork.
    art_b = first_embedded(output / "Artist" / "Album B" / "01 - five.opus")
    check(art_b is not None, "Album B Opus embeds external cover")

    # Second run should be a no-op.
    result = run("-y", "--state-dir", str(state), "--cache-dir", str(cache), str(source), str(output))
    check("built=0" in result.stderr and "deleted=0" in result.stderr, "second run is a no-op")

    return source, output, state, cache


def test_tag_change(root: Path, source: Path, output: Path, state: Path, cache: Path) -> None:
    print("test_tag_change")
    mp3 = source / "Artist" / "Album A" / "02 - two.mp3"
    time.sleep(0.01)
    set_tags(mp3, album="Album A (Remastered)")
    result = run("-y", "--state-dir", str(state), "--cache-dir", str(cache), str(source), str(output))
    check("built=1" in result.stderr, "changed tags trigger a rebuild")

    converted = mutagen.File(output / "Artist" / "Album A" / "02 - two.mp3", easy=True)
    check(
        converted is not None and converted.get("album") == ["Album A (Remastered)"],
        "rebuilt copy reflects new tags",
    )


def test_deletion(root: Path, source: Path, output: Path, state: Path, cache: Path) -> None:
    print("test_deletion")
    ogg = source / "Artist" / "Album A" / "03 - three.ogg"
    ogg.unlink()
    result = run("-y", "--state-dir", str(state), "--cache-dir", str(cache), str(source), str(output))
    check("deleted=1" in result.stderr, "removed source triggers output deletion")
    check(not (output / "Artist" / "Album A" / "03 - three.ogg").exists(), "deleted output removed")


def test_dry_run(root: Path, source: Path, output: Path, state: Path, cache: Path) -> None:
    print("test_dry_run")
    mp3 = source / "Artist" / "Album C" / "02 - seven.mp3"
    time.sleep(0.01)
    set_tags(mp3, album="Album C (Deluxe)")
    result = run("--dry-run", "-q", "--state-dir", str(state), "--cache-dir", str(cache), str(source), str(output))
    check("build" in result.stdout, "dry-run prints planned builds")
    converted = mutagen.File(output / "Artist" / "Album C" / "02 - seven.mp3", easy=True)
    check(
        converted is not None and converted.get("album") == ["Album C"],
        "dry-run does not modify outputs",
    )


def test_external_art_change(root: Path, source: Path, output: Path, state: Path, cache: Path) -> None:
    print("test_external_art_change")
    album_b = source / "Artist" / "Album B"
    time.sleep(0.01)
    make_cover(album_b / "cover.jpg", 900, "green")
    result = run("-y", "--state-dir", str(state), "--cache-dir", str(cache), str(source), str(output))
    check("built=1" in result.stderr, "external art change rebuilds the track")
    check("covers_built=1" in result.stderr, "external art change regenerates cover.jpg")


def test_unrelated_preserved(source: Path, output: Path, state: Path, cache: Path) -> None:
    print("test_unrelated_preserved")
    keep = output / "Artist" / "notes.txt"
    keep.write_text("keep me")
    result = run("-y", "-q", "--state-dir", str(state), "--cache-dir", str(cache), str(source), str(output))
    check(result.returncode == 0, "run with unrelated output file succeeds")
    check(keep.read_text() == "keep me", "unrelated output file preserved")


def test_collision(root: Path) -> None:
    print("test_collision")
    source = root / "collision-library"
    output = root / "collision-output"
    state = root / "collision-state"
    cache = root / "collision-cache"
    album = source / "Solo" / "Record"
    make_audio(album / "song.flac")
    make_audio(album / "song.opus")
    result = run(
        "-y", "--state-dir", str(state), "--cache-dir", str(cache),
        str(source), str(output), check_return=False,
    )
    check("collision" in result.stderr, "collision is reported")
    check(result.returncode != 0, "collision produces a non-zero exit code")
    check(not (output / "Solo" / "Record" / "song.opus").exists(), "colliding output is skipped")


def test_watch(root: Path) -> None:
    print("test_watch")
    source = root / "watch-library"
    output = root / "watch-output"
    state = root / "watch-state"
    cache = root / "watch-cache"

    album = source / "Band" / "Record"
    flac = album / "01 - tune.flac"
    make_audio(flac)
    set_tags(flac, album="Record", albumartist="Band", title="tune")

    process = subprocess.Popen(
        [
            sys.executable,
            str(CONVERTER),
            "--watch",
            "-q",
            "--settle-seconds",
            "0.5",
            "--rescan-interval",
            "3600",
            "--state-dir",
            str(state),
            "--cache-dir",
            str(cache),
            str(source),
            str(output),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        target = output / "Band" / "Record" / "01 - tune.opus"
        if not wait_for(lambda: target.is_file(), timeout=15):
            check(False, "watch mode performs initial conversion")
            return
        check(True, "watch mode performs initial conversion")

        mp3 = album / "02 - second.mp3"
        make_audio(mp3)
        set_tags(mp3, album="Record", albumartist="Band", title="second")
        target2 = output / "Band" / "Record" / "02 - second.mp3"
        if not wait_for(lambda: target2.is_file(), timeout=15):
            check(False, "watch mode copies a newly added file")
            return
        check(True, "watch mode copies a newly added file")

        mp3.unlink()
        if not wait_for(lambda: not target2.exists(), timeout=15):
            check(False, "watch mode removes deleted outputs")
            return
        check(True, "watch mode removes deleted outputs")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def test_scope_units(root: Path) -> None:
    print("test_scope_units")
    sys.path.insert(0, str(REPO))
    from musicconvert.scanning import scan_source
    from musicconvert.scope import ScanScope
    from musicconvert.state import State, new_cover, new_output

    lib = root / "scope-library"
    for rel in ("A/one.flac", "A/two.mp3", "B/three.opus", "root.flac"):
        path = lib / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"stub")
    (lib / "A" / "cover.jpg").write_bytes(b"img")

    full = scan_source(lib)
    check(
        sorted(sf.relpath for sf in full.files)
        == ["A/one.flac", "A/two.mp3", "B/three.opus", "root.flac"],
        "full scan sees every file",
    )

    partial = scan_source(lib, ScanScope(dirs={"A"}))
    check(
        sorted(sf.relpath for sf in partial.files) == ["A/one.flac", "A/two.mp3"],
        "directory scope scans only that directory",
    )
    check("A" in partial.external_art, "directory scope finds external art")

    top = scan_source(lib, ScanScope(dirs={""}))
    check(
        [sf.relpath for sf in top.files] == ["root.flac"],
        "root directory scope is non-recursive",
    )

    subtree = scan_source(lib, ScanScope(trees={"A"}))
    check(
        sorted(sf.relpath for sf in subtree.files) == ["A/one.flac", "A/two.mp3"],
        "tree scope scans the subtree",
    )

    state = State(root / "scope-state" / "state.sqlite3")
    for out in ("A/one.opus", "A/two.mp3", "B/three.opus"):
        state.upsert_output(new_output(relpath=out, source=out, kind="opus", policy="p"))
    state.upsert_cover(new_cover(dir_relpath="A", art_source="A/cover.jpg", art_kind="external", policy="p"))
    state.upsert_cover(new_cover(dir_relpath="B", art_source="B/x.jpg", art_kind="external", policy="p"))

    dir_scope = ScanScope(dirs={"A"})
    check(
        sorted(r.relpath for r in state.outputs_in_scope(dir_scope))
        == ["A/one.opus", "A/two.mp3"],
        "directory scope state query is exact",
    )
    check(
        [c.dir_relpath for c in state.covers_in_scope(dir_scope)] == ["A"],
        "directory scope cover query is exact",
    )
    tree_scope = ScanScope(trees={"A"})
    check(
        sorted(r.relpath for r in state.outputs_in_scope(tree_scope))
        == ["A/one.opus", "A/two.mp3"],
        "tree scope state query matches the subtree",
    )
    check(len(state.outputs_in_scope(ScanScope(full=True))) == 3, "full scope loads all outputs")
    check(len(state.outputs_in_scope(ScanScope())) == 0, "empty scope loads nothing")
    state.close()


def test_watch_rename(root: Path) -> None:
    print("test_watch_rename")
    source = root / "rename-library"
    output = root / "rename-output"
    state = root / "rename-state"
    cache = root / "rename-cache"

    album = source / "Band" / "Record"
    for name in ("01 - a.flac", "02 - b.mp3"):
        make_audio(album / name)
        set_tags(album / name, album="Record", albumartist="Band", title=name)
    make_cover(root / "rename-cover.jpg", 600, "purple")
    shutil.copyfile(root / "rename-cover.jpg", album / "cover.jpg")

    process = subprocess.Popen(
        [
            sys.executable,
            str(CONVERTER),
            "--watch",
            "-q",
            "--settle-seconds",
            "0.5",
            "--rescan-interval",
            "3600",
            "--state-dir",
            str(state),
            "--cache-dir",
            str(cache),
            str(source),
            str(output),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        converted = output / "Band" / "Record" / "01 - a.opus"
        if not wait_for(lambda: converted.is_file(), timeout=15):
            check(False, "rename setup: initial conversion")
            return
        check((output / "Band" / "Record" / "cover.jpg").is_file(), "rename setup: cover generated")

        new_album = source / "Band" / "Record Two"
        album.rename(new_album)
        moved = output / "Band" / "Record Two" / "01 - a.opus"
        if not wait_for(lambda: moved.is_file(), timeout=15):
            check(False, "watch mode follows a renamed directory")
            return
        check(True, "watch mode follows a renamed directory")
        check(not (output / "Band" / "Record" / "01 - a.opus").exists(), "old output removed after rename")
        check((output / "Band" / "Record Two" / "cover.jpg").is_file(), "cover follows the renamed directory")

        shutil.rmtree(new_album)
        if not wait_for(lambda: not moved.exists(), timeout=15):
            check(False, "watch mode removes outputs after directory deletion")
            return
        check(True, "watch mode removes outputs after directory deletion")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def test_progress(root: Path) -> None:
    print("test_progress")
    source = root / "progress-library"
    album = source / "Q" / "R"
    for name in ("01.flac", "02.flac", "03.mp3"):
        make_audio(album / name)

    def convert(tag: str, *extra: str) -> subprocess.CompletedProcess:
        return run(
            *extra,
            "-y",
            "--state-dir",
            str(root / f"progress-state-{tag}"),
            "--cache-dir",
            str(root / f"progress-cache-{tag}"),
            str(source),
            str(root / f"progress-output-{tag}"),
        )

    result = convert("lines", "--progress", "lines")
    check(result.returncode == 0, "lines progress run succeeds")
    check(
        "build " in result.stderr and "/3]" in result.stderr,
        "lines mode prints one line per processed item",
    )

    result = convert("bar")
    check(
        "build " in result.stderr and "/3]" in result.stderr,
        "default bar mode falls back to per-item lines when not a terminal",
    )

    result = convert("none", "--progress", "none")
    check("build " not in result.stderr, "none mode shows no per-item lines")
    check("built=3" in result.stderr, "summary is still logged without a progress display")

    result = convert("quiet", "-q")
    check(result.stderr.strip() == "", "quiet mode suppresses progress and info logs")


def test_scan_progress(root: Path) -> None:
    print("test_scan_progress")
    sys.path.insert(0, str(REPO))
    from musicconvert.scanning import scan_source

    lib = root / "scan-progress"
    for i in range(3):
        album = lib / f"album{i}"
        album.mkdir(parents=True)
        (album / "track.flac").write_bytes(b"stub")

    class Fake:
        def __init__(self) -> None:
            self.starts: list[int] = []
            self.advances = 0

        def scan_start(self, total: int) -> None:
            self.starts.append(total)

        def scan_advance(self, label: str = "") -> None:
            self.advances += 1

    expected = sum(1 for _ in os.walk(lib))
    progress = Fake()
    scan_source(lib, progress=progress)
    check(progress.starts == [expected], "scan reports a known directory total up front")
    check(progress.advances == expected, "scan advances once per directory")


def test_cancellation(root: Path) -> None:
    print("test_cancellation")
    sys.path.insert(0, str(REPO))
    import threading
    from musicconvert import subproc

    stop = threading.Event()
    timer = threading.Timer(0.3, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        subproc.run(
            [sys.executable, "-c", "import time; time.sleep(30)"], stop=stop
        )
        check(False, "stop request cancels a running subprocess")
    except subproc.Cancelled:
        check(True, "stop request cancels a running subprocess")
    finally:
        timer.cancel()
    check(time.monotonic() - started < 5, "cancellation returns promptly")
    check(subproc.active() == 0, "no child processes are left registered")


def wait_for(predicate, timeout: float, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="music-convert-test-") as tmp:
        root = Path(tmp)
        source, output, state, cache = test_conversion(root)
        test_tag_change(root, source, output, state, cache)
        test_external_art_change(root, source, output, state, cache)
        test_deletion(root, source, output, state, cache)
        test_dry_run(root, source, output, state, cache)
        test_unrelated_preserved(source, output, state, cache)
        test_scope_units(root)
        test_scan_progress(root)
        test_cancellation(root)
        test_progress(root)
        test_collision(root)
        test_watch(root)
        test_watch_rename(root)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
