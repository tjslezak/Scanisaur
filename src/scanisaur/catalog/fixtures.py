"""Catalogs loaded from YAML, so the engine and tests never need a warehouse.

Example::

    default_project: proj
    default_dataset: analytics
    tables:
      - name: proj.analytics.events
        rows: 1200000000
        bytes: 2100000000000
        partitioning: {column: event_date, granularity: DAY}
        clustering: [user_id]
        columns:
          event_date: DATE
          user_id: STRING
        partitions:              # optional: partition ID (or shard suffix) -> bytes
          "20260930": 7000000000
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlglot import exp
from sqlglot.errors import SqlglotError

from scanisaur.catalog.model import (
    Catalog,
    Column,
    Granularity,
    Partition,
    Partitioning,
    Table,
    TableKind,
)
from scanisaur.errors import ScanisaurError

#: What BigQuery's partition IDs look like, by granularity.
_PARTITION_IDS: dict[Granularity, re.Pattern[str]] = {
    "YEAR": re.compile(r"\d{4}"),
    "MONTH": re.compile(r"\d{6}"),
    "DAY": re.compile(r"\d{8}"),
    "HOUR": re.compile(r"\d{10}"),
    "RANGE": re.compile(r"-?\d+"),
}
_SPECIAL_PARTITIONS = ("__NULL__", "__UNPARTITIONED__")


class FixtureError(ScanisaurError, ValueError):
    """A catalog fixture that can't be read or doesn't match the expected shape."""


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _PartitioningSpec(_Spec):
    column: str | None = None
    granularity: Granularity = "DAY"
    required: bool = False


class _TableSpec(_Spec):
    name: str
    kind: TableKind = "TABLE"
    rows: int | None = Field(default=None, ge=0)
    bytes: int | None = Field(default=None, ge=0)
    partitioning: _PartitioningSpec | None = None
    clustering: tuple[str, ...] = ()
    description: str = ""
    columns: dict[str, str] = Field(min_length=1)
    partitions: dict[str, int] = {}

    @field_validator("columns")
    @classmethod
    def _known_types(cls, columns: dict[str, str]) -> dict[str, str]:
        for name, type_ in columns.items():
            try:
                exp.DataType.build(type_, dialect="bigquery")
            except SqlglotError as error:
                raise ValueError(
                    f"column {name!r} has a type that isn't valid: {type_!r}"
                ) from error
        return columns

    @field_validator("partitions", mode="before")
    @classmethod
    def _quoted_ids(cls, partitions: object) -> object:
        """YAML reads an unquoted ID as a number, octal or date: 0712 becomes 458."""
        if isinstance(partitions, dict):
            unquoted = [pid for pid in partitions if not isinstance(pid, str)]
            if unquoted:
                raise ValueError(f"quote partition IDs, such as '20260930': {unquoted}")
        return partitions

    @field_validator("name")
    @classmethod
    def _qualified(cls, name: str) -> str:
        if len(name.split(".")) != 3 or not all(name.split(".")):
            raise ValueError(f"table name {name!r} must be project.dataset.table")
        return name

    @model_validator(mode="after")
    def _known_columns(self) -> Self:
        names = {name.lower() for name in self.columns}
        referenced = list(self.clustering)
        if self.partitioning is not None and self.partitioning.column is not None:
            referenced.append(self.partitioning.column)
        unknown = [name for name in referenced if name.lower() not in names]
        if unknown:
            raise ValueError(f"partitioning or clustering names unknown columns: {unknown}")
        if self.partitions and self.partitioning is None and not self.name.endswith("*"):
            raise ValueError("only partitioned tables and wildcard families have partitions")
        if self.partitioning is not None:
            pattern = _PARTITION_IDS[self.partitioning.granularity]
            malformed = [
                pid
                for pid in self.partitions
                if pid not in _SPECIAL_PARTITIONS and not pattern.fullmatch(pid)
            ]
            if malformed:
                raise ValueError(
                    f"partition IDs don't match {self.partitioning.granularity} partitions: "
                    f"{malformed}"
                )
        negative = [pid for pid, size in self.partitions.items() if size < 0]
        if negative:
            raise ValueError(f"partitions have negative sizes: {negative}")
        return self

    def to_table(self) -> Table:
        project, dataset, name = self.name.split(".")
        partitioning = None
        if self.partitioning is not None:
            spec = self.partitioning
            partitioning = Partitioning(spec.column, spec.granularity, spec.required)
        return Table(
            project=project,
            dataset=dataset,
            name=name,
            columns=tuple(Column(n, t) for n, t in self.columns.items()),
            kind=self.kind,
            row_count=self.rows,
            size_bytes=self.bytes,
            partitioning=partitioning,
            clustering=self.clustering,
            description=self.description,
            partitions=tuple(Partition(pid, size) for pid, size in self.partitions.items()),
        )


class _CatalogSpec(_Spec):
    default_project: str | None = None
    default_dataset: str | None = None
    tables: tuple[_TableSpec, ...]

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [table.name for table in self.tables]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"tables are listed more than once: {duplicates}")
        return self


def load_catalog(path: str | os.PathLike[str]) -> Catalog:
    """Load and validate a catalog fixture, raising FixtureError with the reason."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise FixtureError(f"{path}: {error}") from error
    try:
        spec = _CatalogSpec.model_validate(data)
    except ValidationError as error:
        raise FixtureError(f"{path}: {error}") from error
    return Catalog(
        tables=tuple(table.to_table() for table in spec.tables),
        default_project=spec.default_project,
        default_dataset=spec.default_dataset,
    )
