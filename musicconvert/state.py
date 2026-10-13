"""Persistent state for managed outputs and generated covers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .scope import ScanScope

SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _like_escape(value: str) -> str:
    """Escape ``%``, ``_`` and ``\\`` for a SQL ``LIKE`` with ``ESCAPE '\\'``."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class OutputRow:
    relpath: str
    source: str
    kind: str
    policy: str
    src_size: int | None
    src_mtime: int | None
    art_source: str | None
    art_size: int | None
    art_mtime: int | None
    out_size: int | None
    out_mtime: int | None
    built_at: str | None


@dataclass
class CoverRow:
    dir_relpath: str
    art_source: str
    art_kind: str
    art_size: int | None
    art_mtime: int | None
    policy: str
    built_at: str | None


class State:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS outputs (
                    relpath     TEXT PRIMARY KEY,
                    source      TEXT NOT NULL,
                    kind        TEXT NOT NULL,
                    policy      TEXT NOT NULL,
                    src_size    INTEGER,
                    src_mtime   INTEGER,
                    art_source  TEXT,
                    art_size    INTEGER,
                    art_mtime   INTEGER,
                    out_size    INTEGER,
                    out_mtime   INTEGER,
                    built_at    TEXT
                );
                CREATE TABLE IF NOT EXISTS covers (
                    dir_relpath TEXT PRIMARY KEY,
                    art_source  TEXT NOT NULL,
                    art_kind    TEXT NOT NULL,
                    art_size    INTEGER,
                    art_mtime   INTEGER,
                    policy      TEXT NOT NULL,
                    built_at    TEXT
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema', ?)",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        self._conn.close()

    # -- outputs ---------------------------------------------------------

    def get_output(self, relpath: str) -> OutputRow | None:
        row = self._conn.execute(
            "SELECT * FROM outputs WHERE relpath = ?", (relpath,)
        ).fetchone()
        return _output_from_row(row) if row else None

    def all_outputs(self) -> list[OutputRow]:
        rows = self._conn.execute("SELECT * FROM outputs").fetchall()
        return [_output_from_row(row) for row in rows]

    def outputs_in_scope(self, scope: ScanScope) -> list[OutputRow]:
        rows = self._rows_in_scope("outputs", "relpath", scope)
        return [
            _output_from_row(row)
            for row in rows
            if scope.path_in_scope(row["relpath"])
        ]

    def upsert_output(self, row: OutputRow) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO outputs (relpath, source, kind, policy, src_size, src_mtime,
                                     art_source, art_size, art_mtime, out_size, out_mtime, built_at)
                VALUES (:relpath, :source, :kind, :policy, :src_size, :src_mtime,
                        :art_source, :art_size, :art_mtime, :out_size, :out_mtime, :built_at)
                ON CONFLICT(relpath) DO UPDATE SET
                    source=:source, kind=:kind, policy=:policy,
                    src_size=:src_size, src_mtime=:src_mtime,
                    art_source=:art_source, art_size=:art_size, art_mtime=:art_mtime,
                    out_size=:out_size, out_mtime=:out_mtime, built_at=:built_at
                """,
                _output_to_dict(row),
            )

    def delete_output(self, relpath: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM outputs WHERE relpath = ?", (relpath,))

    # -- covers ----------------------------------------------------------

    def get_cover(self, dir_relpath: str) -> CoverRow | None:
        row = self._conn.execute(
            "SELECT * FROM covers WHERE dir_relpath = ?", (dir_relpath,)
        ).fetchone()
        return _cover_from_row(row) if row else None

    def all_covers(self) -> list[CoverRow]:
        rows = self._conn.execute("SELECT * FROM covers").fetchall()
        return [_cover_from_row(row) for row in rows]

    def covers_in_scope(self, scope: ScanScope) -> list[CoverRow]:
        rows = self._rows_in_scope("covers", "dir_relpath", scope)
        return [
            _cover_from_row(row)
            for row in rows
            if scope.dir_in_scope(row["dir_relpath"])
        ]

    def upsert_cover(self, row: CoverRow) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO covers (dir_relpath, art_source, art_kind, art_size, art_mtime,
                                    policy, built_at)
                VALUES (:dir_relpath, :art_source, :art_kind, :art_size, :art_mtime,
                        :policy, :built_at)
                ON CONFLICT(dir_relpath) DO UPDATE SET
                    art_source=:art_source, art_kind=:art_kind,
                    art_size=:art_size, art_mtime=:art_mtime,
                    policy=:policy, built_at=:built_at
                """,
                _cover_to_dict(row),
            )

    def delete_cover(self, dir_relpath: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM covers WHERE dir_relpath = ?", (dir_relpath,))

    # -- scope -----------------------------------------------------------

    def _rows_in_scope(
        self, table: str, column: str, scope: ScanScope
    ) -> list[sqlite3.Row]:
        """Fetch the *table* rows that could fall within *scope*.

        The ``LIKE`` filter only narrows the candidate set; callers must still
        check membership with the scope because it is case-insensitive and
        coarse.
        """
        prefixes = scope.candidate_prefixes()
        if prefixes is not None and not prefixes:
            return []
        if prefixes is None:
            return self._conn.execute(f"SELECT * FROM {table}").fetchall()
        clauses = []
        params: list[str] = []
        for prefix in prefixes:
            clauses.append(f"({column} = ? OR {column} LIKE ? ESCAPE '\\')")
            params.append(prefix)
            params.append(_like_escape(prefix) + "/%")
        where = " OR ".join(clauses)
        return self._conn.execute(
            f"SELECT * FROM {table} WHERE {where}", params
        ).fetchall()


def _output_from_row(row: sqlite3.Row) -> OutputRow:
    return OutputRow(
        relpath=row["relpath"],
        source=row["source"],
        kind=row["kind"],
        policy=row["policy"],
        src_size=row["src_size"],
        src_mtime=row["src_mtime"],
        art_source=row["art_source"],
        art_size=row["art_size"],
        art_mtime=row["art_mtime"],
        out_size=row["out_size"],
        out_mtime=row["out_mtime"],
        built_at=row["built_at"],
    )


def _cover_from_row(row: sqlite3.Row) -> CoverRow:
    return CoverRow(
        dir_relpath=row["dir_relpath"],
        art_source=row["art_source"],
        art_kind=row["art_kind"],
        art_size=row["art_size"],
        art_mtime=row["art_mtime"],
        policy=row["policy"],
        built_at=row["built_at"],
    )


def _output_to_dict(row: OutputRow) -> dict:
    return {
        "relpath": row.relpath,
        "source": row.source,
        "kind": row.kind,
        "policy": row.policy,
        "src_size": row.src_size,
        "src_mtime": row.src_mtime,
        "art_source": row.art_source,
        "art_size": row.art_size,
        "art_mtime": row.art_mtime,
        "out_size": row.out_size,
        "out_mtime": row.out_mtime,
        "built_at": row.built_at,
    }


def _cover_to_dict(row: CoverRow) -> dict:
    return {
        "dir_relpath": row.dir_relpath,
        "art_source": row.art_source,
        "art_kind": row.art_kind,
        "art_size": row.art_size,
        "art_mtime": row.art_mtime,
        "policy": row.policy,
        "built_at": row.built_at,
    }


__all__ = ["State", "OutputRow", "CoverRow", "SCHEMA_VERSION", "new_output", "new_cover"]


def new_output(**kwargs) -> OutputRow:
    defaults = {
        "relpath": "",
        "source": "",
        "kind": "",
        "policy": "",
        "src_size": None,
        "src_mtime": None,
        "art_source": None,
        "art_size": None,
        "art_mtime": None,
        "out_size": None,
        "out_mtime": None,
        "built_at": None,
    }
    defaults.update(kwargs)
    return OutputRow(**defaults)


def new_cover(**kwargs) -> CoverRow:
    defaults = {
        "dir_relpath": "",
        "art_source": "",
        "art_kind": "",
        "art_size": None,
        "art_mtime": None,
        "policy": "",
        "built_at": None,
    }
    defaults.update(kwargs)
    return CoverRow(**defaults)
