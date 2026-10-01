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
"""

from __future__ import annotations

import os
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

from scanisaur.catalog.model import Catalog, Column, Granularity, Partitioning, Table, TableKind


class FixtureError(ValueError):
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
    except (OSError, yaml.YAMLError) as error:
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
