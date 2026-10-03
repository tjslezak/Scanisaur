"""Where checks get their catalog: a snapshot that can be refreshed and searched."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from scanisaur.catalog.model import Catalog


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A catalog as fetched at one time. ``snapshot_id`` identifies it in results."""

    catalog: Catalog
    snapshot_id: str
    #: When the metadata was read from the warehouse; None for a fixture.
    fetched_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    #: ``project.dataset.table``.
    table: str
    #: The matching column, or None when the table itself matched.
    column: str | None
    #: Higher is better.
    score: float


class CatalogSource(Protocol):
    def current(self) -> Snapshot:
        """The snapshot to check against. Cheap: called before every check."""
        ...

    def search(self, query: str, limit: int) -> list[SearchHit]:
        """Tables and columns whose names or descriptions match ``query``, best first."""
        ...
