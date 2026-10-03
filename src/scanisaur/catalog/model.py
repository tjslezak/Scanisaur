"""Warehouse-neutral catalog model.

A catalog is a snapshot of warehouse metadata: tables, columns, partitioning, sizes
and unique keys. It never contains row data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Granularity = Literal["HOUR", "DAY", "MONTH", "YEAR", "RANGE"]
TableKind = Literal["TABLE", "VIEW", "MATERIALIZED_VIEW", "EXTERNAL"]

#: BigQuery pseudo-columns. They never appear in ``SELECT *``.
TABLE_SUFFIX = "_TABLE_SUFFIX"
PARTITIONTIME = "_PARTITIONTIME"
PARTITIONDATE = "_PARTITIONDATE"


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class Partitioning:
    #: The partition column; None for ingestion-time partitioning (``_PARTITIONTIME``).
    column: str | None
    granularity: Granularity
    #: BigQuery rejects queries that don't filter on the partition column.
    required: bool = False


@dataclass(frozen=True, slots=True)
class Partition:
    """One partition, or one shard of a wildcard family.

    ``id`` is BigQuery's partition ID: ``20260930`` for a day, ``2026093014`` for an hour,
    ``202609`` for a month, ``2026`` for a year, the start of an integer range, or
    ``__NULL__`` and ``__UNPARTITIONED__``. A shard's ID is its table suffix.
    """

    id: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class Table:
    project: str
    dataset: str
    #: Table name; a wildcard family such as ``events_*`` stands for sharded tables.
    name: str
    columns: tuple[Column, ...]
    kind: TableKind = "TABLE"
    row_count: int | None = None
    size_bytes: int | None = None
    partitioning: Partitioning | None = None
    clustering: tuple[str, ...] = ()
    description: str = ""
    #: Partitions, or a wildcard family's shards, when the catalog has them. Without them,
    #: cost estimates can't tell how much a partition filter keeps.
    partitions: tuple[Partition, ...] = ()
    #: Sets of columns whose values are unique in the table, such as ``(("order_id",),)``.
    #: BigQuery doesn't enforce its primary keys and most tables declare none, so these
    #: usually come from configuration. None when not known; empty when no set is unique.
    keys: tuple[tuple[str, ...], ...] | None = None
    #: When the table's data or schema last changed, if the warehouse reports it.
    last_modified: datetime | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.project}.{self.dataset}.{self.name}"

    @property
    def is_wildcard(self) -> bool:
        return self.name.endswith("*")

    @property
    def pseudo_columns(self) -> frozenset[str]:
        """BigQuery pseudo-columns this table can be filtered on."""
        names: set[str] = set()
        if self.is_wildcard:
            names.add(TABLE_SUFFIX)
        partitioning = self.partitioning
        if partitioning is not None and partitioning.column is None:
            names.add(PARTITIONTIME)
            if partitioning.granularity == "DAY":
                names.add(PARTITIONDATE)  # BigQuery has it on daily partitions only
        return frozenset(names)

    def column(self, name: str) -> Column | None:
        """Look up a column. BigQuery column names are case-insensitive."""
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)


@dataclass(frozen=True, slots=True)
class Catalog:
    tables: tuple[Table, ...]
    #: Used for table references that leave out the project or dataset.
    default_project: str | None = None
    default_dataset: str | None = None
    _index: dict[str, Table] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_index", {t.qualified_name: t for t in self.tables})

    def find(
        self, name: str, dataset: str | None = None, project: str | None = None
    ) -> Table | None:
        """Find a table by its name parts, filling in the default project and dataset.

        Table names are case-sensitive, as in BigQuery. A narrower wildcard such as
        ``events_2026*`` resolves to its family ``events_*``, the longest one that matches
        when families overlap (``events_intraday_*`` before ``events_*``).
        """
        project = project or self.default_project
        dataset = dataset or self.default_dataset
        if project is None or dataset is None:
            return None
        table = self._index.get(f"{project}.{dataset}.{name}")
        if table is not None or not name.endswith("*"):
            return table
        prefix = name[:-1]
        families = [
            t
            for t in self.tables
            if t.is_wildcard
            and (t.project, t.dataset) == (project, dataset)
            and prefix.startswith(t.name[:-1])
        ]
        return max(families, key=lambda t: len(t.name), default=None)
