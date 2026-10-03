"""What every warehouse connector provides."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol

from scanisaur.catalog.model import Catalog, Table
from scanisaur.errors import ScanisaurError


class ConnectorError(ScanisaurError):
    """A warehouse that can't be reached or read, or a driver that isn't installed."""


@dataclass(frozen=True, slots=True)
class Probe:
    """One line of ``scanisaur doctor``."""

    name: str
    status: Literal["ok", "warn", "fail"]
    detail: str


@dataclass(frozen=True, slots=True)
class QueryRun:
    """One query the warehouse ran, from its query history."""

    job_id: str
    started: datetime
    #: The account that ran it, when history says.
    user: str | None
    sql: str
    bytes_billed: int


class Connector(Protocol):
    #: Identifies the warehouse, such as ``bigquery:acme-analytics:US``.
    @property
    def name(self) -> str: ...

    def fetch_catalog(self) -> Catalog:
        """Every table's metadata, raising ConnectorError on failure."""
        ...

    def fetch_table(self, project: str, dataset: str, name: str) -> Table | None:
        """One table's metadata, or None when it doesn't exist."""
        ...

    def check_access(self) -> list[Probe]:
        """Whether the account can read metadata, and whether it can also read data."""
        ...

    def fetch_query_history(self, since: datetime) -> Iterator[QueryRun]:
        """SELECT queries run since ``since``, oldest first, raising ConnectorError when
        the warehouse keeps no history or the account can't read it."""
        ...


def included(dataset: str, include: tuple[str, ...], exclude: tuple[str, ...]) -> bool:
    """Whether config includes ``dataset``: every dataset when ``include`` is empty."""
    return (not include or dataset in include) and dataset not in exclude


def with_keys(table: Table, keys: tuple[tuple[str, ...], ...] | None) -> Table:
    """``table`` with configured unique keys added to the ones it declares."""
    if not keys:
        return table
    merged = tuple(dict.fromkeys((*(table.keys or ()), *keys)))
    return replace(table, keys=merged)
