"""The reconciliation engine shared by one-shot runs and the daemon.

Inotify only signals *that* something may have changed. This module decides
what actually needs doing by scanning the source tree, comparing fingerprints
against persistent state, and rebuilding the affected outputs.

A daemon pass is limited by a :class:`~musicconvert.scope.ScanScope`: only the
directories named by the change set are re-read, and only outputs and covers
inside them are candidates for building or deletion. One-shot runs use a full
scope.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from . import artwork, process
from .artwork import ExternalArt
from .config import Config
from .formats import Picture, choose_embedded, extract_pictures
from .progress import ProgressReporter
from .scanning import ScanResult, SourceFile, scan_source
from .scope import ScanScope
from .state import CoverRow, OutputRow, State, new_cover, new_output
from .subproc import Cancelled
from .util import (
    atomic_replace,
    kind_of,
    prune_empty_dirs,
    remove_quietly,
    temp_path,
)

COVER_NAME = "cover.jpg"


@dataclass
class BuildItem:
    source: SourceFile
    output_relpath: str
    external: ExternalArt | None


@dataclass
class CoverBuild:
    dir_rel: str
    art_kind: str
    art_source: str
    art_size: int
    art_mtime: int


@dataclass
class Plan:
    scan: ScanResult
    builds: list[BuildItem] = field(default_factory=list)
    deletes: list[OutputRow] = field(default_factory=list)
    cover_builds: list[CoverBuild] = field(default_factory=list)
    cover_deletes: list[str] = field(default_factory=list)
    collisions: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Summary:
    scanned: int = 0
    built: int = 0
    deleted: int = 0
    covers_built: int = 0
    covers_deleted: int = 0
    failed: int = 0
    collisions: int = 0
    incomplete: bool = False
    dirty_again: bool = False
    dirty_dirs: set[str] = field(default_factory=set)

    def log(self, logger: logging.Logger) -> None:
        if self.incomplete:
            logger.error("source scan was incomplete; skipping all deletions")
        logger.info(
            "scanned=%d built=%d deleted=%d covers_built=%d covers_deleted=%d "
            "failed=%d collisions=%d",
            self.scanned,
            self.built,
            self.deleted,
            self.covers_built,
            self.covers_deleted,
            self.failed,
            self.collisions,
        )


class Reconciler:
    def __init__(
        self,
        cfg: Config,
        state: State,
        logger: logging.Logger,
        reporter: ProgressReporter | None = None,
        stop: threading.Event | None = None,
    ):
        self.cfg = cfg
        self.state = state
        self.log = logger
        self.progress = reporter if reporter is not None else ProgressReporter(cfg.progress)
        self.stop = stop if stop is not None else threading.Event()

    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise Cancelled()

    # -- planning --------------------------------------------------------

    def plan(self, scan: ScanResult, scope: ScanScope) -> Plan:
        plan = Plan(scan=scan)
        plan.collisions = dict(scan.collisions)

        out_sources: dict[str, list[SourceFile]] = {}
        for sf in scan.files:
            out_sources.setdefault(sf.output_relpath, []).append(sf)

        valid = {
            out: sources[0]
            for out, sources in out_sources.items()
            if len(sources) == 1
        }

        # Only rows the current scope can touch are loaded; everything else is
        # left exactly as it is.
        existing = {row.relpath: row for row in self.state.outputs_in_scope(scope)}

        for out, sf in sorted(valid.items()):
            self._check_stop()
            external = scan.external_art.get(sf.dir_rel)
            row = existing.get(out)
            if row is not None and self._output_matches(row, sf, external):
                continue
            plan.builds.append(BuildItem(sf, out, external))

        current = set(valid)
        for row in existing.values():
            self._check_stop()
            if row.relpath in current:
                continue
            # Do not delete an output whose target path is now ambiguous; a
            # collision is reported and left untouched for the user to resolve.
            if row.relpath in scan.collisions:
                continue
            plan.deletes.append(row)

        self._plan_covers(scan, plan, scope)
        return plan

    def _output_matches(
        self, row: OutputRow, sf: SourceFile, external: ExternalArt | None
    ) -> bool:
        if row.policy != self.cfg.policy or row.kind != sf.kind:
            return False
        if row.src_size != sf.size or row.src_mtime != sf.mtime:
            return False
        art_source = external.relpath if external else None
        art_size = external.size if external else None
        art_mtime = external.mtime if external else None
        return (row.art_source, row.art_size, row.art_mtime) == (
            art_source,
            art_size,
            art_mtime,
        )

    def _plan_covers(self, scan: ScanResult, plan: Plan, scope: ScanScope) -> None:
        existing = {c.dir_relpath: c for c in self.state.covers_in_scope(scope)}
        source_dirs = scan.source_dirs

        for d in sorted(source_dirs):
            self._check_stop()
            prev = existing.get(d)
            desired = self._desired_cover(d, scan, prev)
            if desired is None:
                if prev is not None:
                    plan.cover_deletes.append(d)
                continue
            kind, art_source, art_size, art_mtime = desired
            unchanged = (
                prev is not None
                and prev.art_kind == kind
                and prev.art_source == art_source
                and prev.art_size == art_size
                and prev.art_mtime == art_mtime
                and prev.policy == self.cfg.policy
            )
            if not unchanged:
                plan.cover_builds.append(
                    CoverBuild(d, kind, art_source, art_size, art_mtime)
                )

        for d in existing:
            if d not in source_dirs:
                plan.cover_deletes.append(d)

    def _desired_cover(
        self, dir_rel: str, scan: ScanResult, prev: CoverRow | None
    ) -> tuple[str, str, int, int] | None:
        external = scan.external_art.get(dir_rel)
        if external is not None:
            return ("external", external.relpath, external.size, external.mtime)

        sources = scan.sources_by_rel
        if prev is not None and prev.art_kind == "embedded":
            sf = sources.get(prev.art_source)
            if sf is not None and sf.size == prev.art_size and sf.mtime == prev.art_mtime:
                return ("embedded", sf.relpath, sf.size, sf.mtime)

        for sf in sorted(scan.files_in_dir(dir_rel), key=lambda s: s.relpath):
            self._check_stop()
            try:
                picture = choose_embedded(extract_pictures(sf.path))
            except Exception:
                picture = None
            if picture is not None:
                return ("embedded", sf.relpath, sf.size, sf.mtime)
        return None

    # -- execution -------------------------------------------------------

    def reconcile(self, scope: ScanScope | None = None) -> Summary:
        scope = scope if scope is not None else ScanScope(full=True)
        if not scope.full:
            self.log.info(
                "incremental reconcile over %d changed location(s)",
                len(scope.dirs) + len(scope.trees),
            )
        try:
            return self._reconcile(scope)
        finally:
            self.progress.finish()

    def _reconcile(self, scope: ScanScope) -> Summary:
        scan = scan_source(
            self.cfg.source, scope, progress=self.progress, stop=self.stop
        )
        summary = Summary(scanned=len(scan.files), incomplete=not scan.complete)

        if scan.collisions:
            for out, sources in sorted(scan.collisions.items()):
                self.log.error(
                    "output collision for %s: %s", out, ", ".join(sorted(sources))
                )
            summary.collisions = len(scan.collisions)

        if not scan.complete:
            for error in scan.errors[:10]:
                self.log.error("scan error: %s", error)
            self.progress.finish()
            summary.log(self.log)
            return summary

        plan = self.plan(scan, scope)
        if self.cfg.dry_run:
            self.progress.finish()
            self._describe(plan)
            summary.log(self.log)
            return summary

        self.cfg.output.mkdir(parents=True, exist_ok=True)
        self._execute(plan, summary)
        if summary.built or summary.deleted or summary.covers_deleted:
            prune_empty_dirs(self.cfg.output)
        summary.log(self.log)
        return summary

    def _describe(self, plan: Plan) -> None:
        for item in plan.builds:
            print(f"build   {item.output_relpath}  <- {item.source.relpath}")
        for row in plan.deletes:
            print(f"delete  {row.relpath}")
        for cb in plan.cover_builds:
            print(f"cover   {cb.dir_rel or '.'}/{COVER_NAME}  <- {cb.art_source}")
        for d in plan.cover_deletes:
            print(f"cover-  {d or '.'}/{COVER_NAME}")

    def _execute(self, plan: Plan, summary: Summary) -> None:
        computed_images: dict[str, Path | None] = {}
        total = (
            len(plan.cover_builds)
            + len(plan.cover_deletes)
            + len(plan.builds)
            + len(plan.deletes)
        )
        self.progress.start(total)
        try:
            self._execute_covers(plan, summary, computed_images)
            self._execute_builds(plan, summary, computed_images)
            self._execute_deletes(plan, summary)
        finally:
            self.progress.finish()

    # -- covers ----------------------------------------------------------

    def _execute_covers(
        self,
        plan: Plan,
        summary: Summary,
        computed_images: dict[str, Path | None],
    ) -> None:
        if plan.cover_builds:
            self._check_stop()
            pool = ThreadPoolExecutor(max_workers=self.cfg.jobs)
            futures = {
                pool.submit(self._prepare_cover, cb): cb for cb in plan.cover_builds
            }
            try:
                for future in as_completed(futures):
                    if self.stop.is_set():
                        raise Cancelled()
                    cb = futures[future]
                    image, error = future.result()
                    if error is not None:
                        self.log.error("cover for %s failed: %s", cb.dir_rel or ".", error)
                        summary.failed += 1
                        self.progress.advance(f"cover {cb.dir_rel or '.'}/{COVER_NAME} (failed)")
                        continue
                    computed_images[cb.dir_rel] = image
                    self._publish_cover(cb, image)
                    summary.covers_built += 1
                    self.progress.advance(f"cover {cb.dir_rel or '.'}/{COVER_NAME}")
            finally:
                pool.shutdown(wait=True, cancel_futures=True)

        for d in plan.cover_deletes:
            self._check_stop()
            remove_quietly(self.cfg.output / d / COVER_NAME if d else self.cfg.output / COVER_NAME)
            self.state.delete_cover(d)
            computed_images[d] = None
            summary.covers_deleted += 1
            self.progress.advance(f"cover- {d or '.'}/{COVER_NAME}")

    def _prepare_cover(self, cb: CoverBuild) -> tuple[Path | None, str | None]:
        if self.stop.is_set():
            raise Cancelled()
        try:
            image = self._cover_image_for(cb.dir_rel, cb.art_kind, cb.art_source)
            return image, None
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - report and continue
            return None, str(exc)

    def _cover_image_for(
        self, dir_rel: str, art_kind: str, art_source: str
    ) -> Path | None:
        if art_kind == "external":
            art = self.cfg.source / art_source
            return artwork.resize_file(
                art, self.cfg.resolved_cache_dir, self.cfg.policy, stop=self.stop
            )
        sf = self._source_by_rel(art_source)
        if sf is None:
            return None
        picture = choose_embedded(extract_pictures(sf.path))
        if picture is None:
            return None
        return artwork.resize_bytes(
            picture.data,
            picture.mime,
            self.cfg.resolved_cache_dir,
            self.cfg.policy,
            stop=self.stop,
        )

    def _publish_cover(self, cb: CoverBuild, image: Path | None) -> None:
        dest = self.cfg.output / cb.dir_rel / COVER_NAME if cb.dir_rel else self.cfg.output / COVER_NAME
        if image is not None:
            artwork.publish(image, dest)
        self.state.upsert_cover(
            new_cover(
                dir_relpath=cb.dir_rel,
                art_source=cb.art_source,
                art_kind=cb.art_kind,
                art_size=cb.art_size,
                art_mtime=cb.art_mtime,
                policy=self.cfg.policy,
            )
        )

    # -- builds ----------------------------------------------------------

    def _execute_builds(
        self,
        plan: Plan,
        summary: Summary,
        computed_images: dict[str, Path | None],
    ) -> None:
        if not plan.builds:
            return

        build_dirs = {item.source.dir_rel for item in plan.builds}
        for d in build_dirs:
            self._check_stop()
            if d in computed_images:
                continue
            computed_images[d] = self._fallback_cover_image(plan.scan, d)

        self._check_stop()
        pool = ThreadPoolExecutor(max_workers=self.cfg.jobs)
        futures = {
            pool.submit(self._run_build, item, computed_images.get(item.source.dir_rel)): item
            for item in plan.builds
        }
        try:
            for future in as_completed(futures):
                if self.stop.is_set():
                    raise Cancelled()
                item = futures[future]
                tmp, error = future.result()
                if error is not None:
                    self.log.error("build %s failed: %s", item.output_relpath, error)
                    summary.failed += 1
                    self.progress.advance(f"build {item.output_relpath} (failed)")
                    continue
                if self._source_changed(item) or self._art_changed(item):
                    self.log.info("source changed during build; rescheduling %s", item.output_relpath)
                    if tmp is not None:
                        remove_quietly(tmp)
                    summary.dirty_again = True
                    summary.dirty_dirs.add(item.source.dir_rel)
                    self.progress.advance(f"build {item.output_relpath} (rescheduled)")
                    continue
                dest = self.cfg.output / item.output_relpath
                atomic_replace(tmp, dest)
                self._record_output(item, dest)
                summary.built += 1
                self.progress.advance(f"build {item.output_relpath}")
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def _fallback_cover_image(self, scan: ScanResult, dir_rel: str) -> Path | None:
        external = scan.external_art.get(dir_rel)
        if external is not None:
            try:
                return artwork.resize_file(
                    external.path,
                    self.cfg.resolved_cache_dir,
                    self.cfg.policy,
                    stop=self.stop,
                )
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.error("cover resize for %s failed: %s", dir_rel or ".", exc)
                return None
        prev = self.state.get_cover(dir_rel)
        if prev is None:
            return None
        try:
            return self._cover_image_for(dir_rel, prev.art_kind, prev.art_source)
        except Cancelled:
            raise
        except Exception:  # noqa: BLE001
            return None

    def _run_build(
        self, item: BuildItem, cover_image: Path | None
    ) -> tuple[Path | None, str | None]:
        if self.stop.is_set():
            raise Cancelled()
        dest = self.cfg.output / item.output_relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = temp_path(dest, "build")
        try:
            image_path, dims = self._select_artwork(item, cover_image)
            if item.source.kind == "flac":
                process.encode_flac(item.source.path, tmp, image_path, stop=self.stop)
            else:
                picture = None
                if image_path is not None:
                    picture = Picture(
                        data=image_path.read_bytes(), mime="image/jpeg", is_front=True
                    )
                process.copy_with_artwork(
                    item.source.path, tmp, picture, dims, item.source.kind
                )
            process.verify(tmp)
            return tmp, None
        except Cancelled:
            remove_quietly(tmp)
            raise
        except Exception as exc:  # noqa: BLE001 - report and continue
            remove_quietly(tmp)
            return None, str(exc)

    def _select_artwork(
        self, item: BuildItem, cover_image: Path | None
    ) -> tuple[Path | None, tuple[int, int]]:
        pictures = extract_pictures(item.source.path)
        chosen = choose_embedded(pictures)
        if chosen is not None:
            path = artwork.resize_bytes(
                chosen.data,
                chosen.mime,
                self.cfg.resolved_cache_dir,
                self.cfg.policy,
                stop=self.stop,
            )
            return path, artwork.image_dimensions(path, stop=self.stop)
        if cover_image is not None:
            return cover_image, artwork.image_dimensions(cover_image, stop=self.stop)
        return None, (0, 0)

    def _record_output(self, item: BuildItem, dest: Path) -> None:
        stat = dest.stat()
        external = item.external
        self.state.upsert_output(
            new_output(
                relpath=item.output_relpath,
                source=item.source.relpath,
                kind=item.source.kind,
                policy=self.cfg.policy,
                src_size=item.source.size,
                src_mtime=item.source.mtime,
                art_source=external.relpath if external else None,
                art_size=external.size if external else None,
                art_mtime=external.mtime if external else None,
                out_size=stat.st_size,
                out_mtime=stat.st_mtime_ns,
            )
        )

    def _source_changed(self, item: BuildItem) -> bool:
        try:
            stat = item.source.path.stat()
        except OSError:
            return True
        return stat.st_size != item.source.size or stat.st_mtime_ns != item.source.mtime

    def _art_changed(self, item: BuildItem) -> bool:
        if item.external is None:
            return False
        try:
            stat = item.external.path.stat()
        except OSError:
            return True
        return (
            stat.st_size != item.external.size
            or stat.st_mtime_ns != item.external.mtime
        )

    # -- deletes ---------------------------------------------------------

    def _execute_deletes(self, plan: Plan, summary: Summary) -> None:
        for row in plan.deletes:
            self._check_stop()
            dest = self.cfg.output / row.relpath
            remove_quietly(dest)
            self.state.delete_output(row.relpath)
            summary.deleted += 1
            self.progress.advance(f"delete {row.relpath}")

    # -- helpers ---------------------------------------------------------

    def _source_by_rel(self, relpath: str) -> SourceFile | None:
        path = self.cfg.source / relpath
        try:
            stat = path.stat()
        except OSError:
            return None
        return SourceFile(relpath, path, kind_of(path), stat.st_size, stat.st_mtime_ns)
