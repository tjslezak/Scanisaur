"""Where a long-running process gets its catalog: the seam between the server and a cache.

``scanisaur serve`` holds one :class:`CatalogSource` and asks it for the current snapshot on
every request. :class:`FixtureSource` reads a YAML catalog; the warehouse cache implements
the same protocol, so the server doesn't know which one it has.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from scanisaur.catalog.fixtures import FixtureError, load_catalog
from scanisaur.catalog.model import Catalog, Table

_WORD = re.compile(r"[a-z0-9]+")
#: Points for a query word found in a table name, a column name, or a description.
_TABLE_NAME, _COLUMN_NAME, _DESCRIPTION = 3.0, 2.0, 1.0


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A catalog as of one moment. Never changed once made, so readers need no lock."""

    catalog: Catalog
    #: Identifies the metadata a check ran against, so the check can be reproduced.
    snapshot_id: str
    #: When the metadata was read from the warehouse; None for a fixture.
    fetched_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    #: ``project.dataset.table``.
    table: str
    #: The matching column, or None when the table itself matched.
    column: str | None
    #: Higher is a better match. Only the order matters, not the scale.
    score: float


class CatalogSource(Protocol):
    def current(self) -> Snapshot:
        """The latest snapshot. Called on every check, so it must be cheap."""
        ...

    def search(self, query: str, limit: int) -> list[SearchHit]:
        """Tables and columns matching ``query``, best first, at most ``limit`` of them."""
        ...


class FixtureSource:
    """A YAML catalog fixture, loaded once. See :mod:`scanisaur.catalog.fixtures`."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as error:
            raise FixtureError(f"{path}: {error}") from error
        digest = hashlib.sha256(data).hexdigest()
        self._snapshot = Snapshot(load_catalog(path), snapshot_id=f"fixture_{digest[:16]}")

    def current(self) -> Snapshot:
        return self._snapshot

    def search(self, query: str, limit: int) -> list[SearchHit]:
        return search_catalog(self._snapshot.catalog, query, limit)


def search_catalog(catalog: Catalog, query: str, limit: int) -> list[SearchHit]:
    """Score every table and column by the query words found in its name and description.

    A plain scan, fine for the few hundred tables a fixture holds.
    """
    words = set(_WORD.findall(query.lower()))
    if not words or limit <= 0:
        return []
    hits = [hit for table in catalog.tables for hit in _table_hits(table, words)]
    hits.sort(key=lambda hit: (-hit.score, hit.table, hit.column or ""))
    return hits[:limit]


def _table_hits(table: Table, words: set[str]) -> list[SearchHit]:
    name = table.qualified_name
    hits = []
    score = _score(words, table.name, _TABLE_NAME) + _score(words, table.description, _DESCRIPTION)
    if score:
        hits.append(SearchHit(name, None, score))
    for column in table.columns:
        score = _score(words, column.name, _COLUMN_NAME) + _score(
            words, column.description, _DESCRIPTION
        )
        if score:
            hits.append(SearchHit(name, column.name, score))
    return hits


def _score(words: set[str], text: str, points: float) -> float:
    """``points`` for each query word that appears in ``text``, even inside a longer word."""
    lowered = text.lower()
    return points * sum(word in lowered for word in words)
