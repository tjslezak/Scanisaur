"""DuckDB: a local database file, for the no-credentials demo and end-to-end tests.

Database, schema and table map to project, dataset and table. Types are translated to
BigQuery's, which the rules expect. DuckDB has no byte sizes, so its catalogs get rule
findings without cost estimates.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

import duckdb
from sqlglot import exp
from sqlglot.errors import SqlglotError

from scanisaur.catalog.connectors.base import ConnectorError, Probe, included
from scanisaur.catalog.model import Catalog, Column, Table, TableKind
from scanisaur.config import DuckDBWarehouse

_RELATIONS = """
SELECT database_name, schema_name, table_name, 'TABLE', comment, estimated_size
FROM duckdb_tables() WHERE NOT internal AND NOT temporary
UNION ALL
SELECT database_name, schema_name, view_name, 'VIEW', comment, NULL
FROM duckdb_views() WHERE NOT internal AND NOT temporary
"""
_COLUMNS = """
SELECT database_name, schema_name, table_name, column_name, data_type, comment
FROM duckdb_columns() WHERE NOT internal ORDER BY column_index
"""
_KEYS = """
SELECT database_name, schema_name, table_name, constraint_column_names
FROM duckdb_constraints() WHERE constraint_type IN ('PRIMARY KEY', 'UNIQUE')
"""

_Name = tuple[str, str, str]


class DuckDBConnector:
    def __init__(self, warehouse: DuckDBWarehouse) -> None:
        self._warehouse = warehouse

    @property
    def name(self) -> str:
        return self._warehouse.name

    def fetch_catalog(self) -> Catalog:
        with self._connect() as db:
            row = db.execute("SELECT current_database(), current_schema()").fetchone()
            if row is None:
                raise ConnectorError(f"{self._warehouse.path}: no current database")
            project, dataset = row
            tables = self._tables(db)
        return Catalog(tables, default_project=project, default_dataset=dataset)

    def fetch_table(self, project: str, dataset: str, name: str) -> Table | None:
        with self._connect() as db:
            tables = self._tables(db)
        return next(
            (t for t in tables if (t.project, t.dataset, t.name) == (project, dataset, name)), None
        )

    def check_access(self) -> list[Probe]:
        try:
            catalog = self.fetch_catalog()
        except ConnectorError as error:
            return [Probe("metadata", "fail", str(error))]
        return [Probe("metadata", "ok", f"{len(catalog.tables)} tables in {self._warehouse.path}")]

    def _tables(self, db: duckdb.DuckDBPyConnection) -> tuple[Table, ...]:
        columns: defaultdict[_Name, list[Column]] = defaultdict(list)
        for *name, column, type_, comment in db.execute(_COLUMNS).fetchall():
            columns[tuple(name)].append(Column(column, _bigquery_type(type_), comment or ""))
        keys: defaultdict[_Name, list[tuple[str, ...]]] = defaultdict(list)
        for *name, key in db.execute(_KEYS).fetchall():
            keys[tuple(name)].append(tuple(key))
        warehouse = self._warehouse
        tables = []
        for project, dataset, name, kind, comment, rows in db.execute(_RELATIONS).fetchall():
            key = (project, dataset, name)
            if not included(dataset, warehouse.include_datasets, warehouse.exclude_datasets):
                continue
            table_kind: TableKind = "VIEW" if kind == "VIEW" else "TABLE"
            tables.append(
                Table(
                    project,
                    dataset,
                    name,
                    tuple(columns[key]),
                    kind=table_kind,
                    row_count=rows,
                    description=comment or "",
                    keys=tuple(keys[key]) if keys.get(key) else None,  # None: not known
                )
            )
        return tuple(tables)

    @contextmanager
    def _connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        path = self._warehouse.path
        if not path.is_file():
            raise ConnectorError(f"{path}: no such DuckDB file")
        try:
            with duckdb.connect(str(path), read_only=True) as db:
                yield db
        except duckdb.Error as error:
            raise ConnectorError(f"{path}: {error}") from error


def _bigquery_type(duckdb_type: str) -> str:
    """``VARCHAR`` as ``STRING``, ``INTEGER`` as ``INT64``, and so on."""
    try:
        return exp.DataType.build(duckdb_type, dialect="duckdb").sql("bigquery")
    except SqlglotError:
        return duckdb_type
