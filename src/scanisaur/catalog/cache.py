"""The local SQLite cache of warehouse metadata.

Each table is one row holding the :class:`Table` as JSON. A save writes a whole new
snapshot in one transaction and then drops older ones, so a reader sees either the old
catalog or the new one, never a mix. The file is disposable: one written by another
version of this module is emptied and refetched.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from scanisaur.catalog.model import Catalog, Table
from scanisaur.catalog.source import SearchHit, Snapshot
from scanisaur.errors import ScanisaurError

#: Bump when the tables below or the JSON shape of a Table changes.
SCHEMA_VERSION = 1

_TABLE_JSON = TypeAdapter(Table)
logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY,
    warehouse TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    default_project TEXT,
    default_dataset TEXT
);
CREATE TABLE tables (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots ON DELETE CASCADE,
    project TEXT NOT NULL,
    dataset TEXT NOT NULL,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, project, dataset, name)
);
"""
#: Names and descriptions of tables and columns, for search. Optional: some SQLite
#: builds lack FTS5, and search then falls back to LIKE.
_SEARCH_SCHEMA = """
CREATE VIRTUAL TABLE search USING fts5(
    snapshot_id UNINDEXED, tbl UNINDEXED, col UNINDEXED, words
)
"""


class CacheError(ScanisaurError):
    """The metadata cache can't be read or written."""


class MetadataCache:
    """One SQLite file holding the latest snapshot of one warehouse's metadata."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def load(self) -> Snapshot | None:
        """The latest snapshot, or None when nothing has been saved."""
        with self._connect() as db:
            row = db.execute(
                "SELECT id, fetched_at, default_project, default_dataset"
                " FROM snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            snapshot_id, fetched_at, project, dataset = row
            bodies = db.execute(
                "SELECT body FROM tables WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchall()
        try:
            tables = tuple(_TABLE_JSON.validate_json(body) for (body,) in bodies)
        except ValidationError as error:
            logger.warning("%s: discarding a cache this version can't read: %s", self.path, error)
            with self._connect() as db:
                _rebuild(db)
            return None
        return Snapshot(
            catalog=Catalog(tables, default_project=project, default_dataset=dataset),
            snapshot_id=str(snapshot_id),
            fetched_at=datetime.fromisoformat(fetched_at),
        )

    def save(self, catalog: Catalog, *, warehouse: str) -> Snapshot:
        """Store ``catalog`` as the new snapshot and drop older ones."""
        fetched_at = datetime.now(UTC)
        with self._connect() as db, db:
            cursor = db.execute(
                "INSERT INTO snapshots (warehouse, fetched_at, default_project, default_dataset)"
                " VALUES (?, ?, ?, ?)",
                (
                    warehouse,
                    fetched_at.isoformat(),
                    catalog.default_project,
                    catalog.default_dataset,
                ),
            )
            snapshot_id = cursor.lastrowid
            if snapshot_id is None:
                raise CacheError(f"{self.path}: the snapshot wasn't saved")
            for table in catalog.tables:
                self._insert(db, snapshot_id, table)
            db.execute("DELETE FROM snapshots WHERE id < ?", (snapshot_id,))
            if self._has_search(db):
                db.execute("DELETE FROM search WHERE snapshot_id < ?", (snapshot_id,))
        return Snapshot(catalog, str(snapshot_id), fetched_at)

    def put_table(self, snapshot: Snapshot, table: Table) -> Snapshot:
        """Add or replace one table in ``snapshot``, as fetched after it was saved."""
        with self._connect() as db, db:
            snapshot_id = int(snapshot.snapshot_id)
            db.execute(
                "DELETE FROM tables WHERE snapshot_id = ? AND project = ? AND dataset = ?"
                " AND name = ?",
                (snapshot_id, table.project, table.dataset, table.name),
            )
            if self._has_search(db):
                db.execute(
                    "DELETE FROM search WHERE snapshot_id = ? AND tbl = ?",
                    (snapshot_id, table.qualified_name),
                )
            self._insert(db, snapshot_id, table)
        catalog = snapshot.catalog
        others = tuple(t for t in catalog.tables if t.qualified_name != table.qualified_name)
        return Snapshot(
            Catalog((*others, table), catalog.default_project, catalog.default_dataset),
            snapshot.snapshot_id,
            snapshot.fetched_at,
        )

    def search(self, query: str, limit: int) -> list[SearchHit]:
        """Tables and columns of the latest snapshot whose words match ``query``."""
        terms = [term for term in query.replace('"', " ").split() if term]
        if not terms or limit <= 0:
            return []
        with self._connect() as db:
            if self._has_search(db):
                rows = db.execute(
                    "SELECT tbl, col, -bm25(search) FROM search"
                    " WHERE search MATCH ? AND snapshot_id = (SELECT max(id) FROM snapshots)"
                    " ORDER BY bm25(search) LIMIT ?",
                    (" OR ".join(f'"{term}"*' for term in terms), limit),
                ).fetchall()
            else:
                rows = self._search_without_fts(db, terms, limit)
        return [SearchHit(table, column or None, score) for table, column, score in rows]

    def _insert(self, db: sqlite3.Connection, snapshot_id: int, table: Table) -> None:
        db.execute(
            "INSERT INTO tables (snapshot_id, project, dataset, name, body) VALUES (?, ?, ?, ?, ?)",
            (
                snapshot_id,
                table.project,
                table.dataset,
                table.name,
                _TABLE_JSON.dump_json(table).decode(),
            ),
        )
        if self._has_search(db):
            name = table.qualified_name
            db.executemany(
                "INSERT INTO search (snapshot_id, tbl, col, words) VALUES (?, ?, ?, ?)",
                [(snapshot_id, name, "", _words(name, table.description))]
                + [
                    (snapshot_id, name, column.name, _words(column.name, column.description))
                    for column in table.columns
                ],
            )

    @staticmethod
    def _search_without_fts(
        db: sqlite3.Connection, terms: list[str], limit: int
    ) -> list[tuple[str, str, float]]:
        hits: list[tuple[str, str, float]] = []
        snapshot = db.execute("SELECT max(id) FROM snapshots").fetchone()[0]
        for (body,) in db.execute("SELECT body FROM tables WHERE snapshot_id = ?", (snapshot,)):
            table = _TABLE_JSON.validate_json(body)
            candidates = [("", _words(table.qualified_name, table.description))] + [
                (c.name, _words(c.name, c.description)) for c in table.columns
            ]
            for column, words in candidates:
                score = sum(term.lower() in words.lower() for term in terms)
                if score:
                    hits.append((table.qualified_name, column, float(score)))
        hits.sort(key=lambda hit: -hit[2])
        return hits[:limit]

    @staticmethod
    def _has_search(db: sqlite3.Connection) -> bool:
        return bool(db.execute("SELECT 1 FROM sqlite_schema WHERE name = 'search'").fetchone())

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("PRAGMA journal_mode = WAL")
                db.execute("PRAGMA foreign_keys = ON")
                if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    _rebuild(db)
                yield db
        except sqlite3.Error as error:
            raise CacheError(f"{self.path}: {error}") from error


def _rebuild(db: sqlite3.Connection) -> None:
    """Empty the file and create this version's tables."""
    with db:
        for (name,) in db.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " AND name NOT LIKE 'search_%'"
        ).fetchall():
            db.execute(f'DROP TABLE IF EXISTS "{name}"')
        db.executescript(_SCHEMA)
        with suppress(sqlite3.OperationalError):  # no FTS5 in this SQLite build
            db.execute(_SEARCH_SCHEMA)
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _words(name: str, description: str) -> str:
    """Searchable words: snake_case and dotted names split into their parts."""
    return " ".join([name, name.replace(".", " ").replace("_", " "), description])
