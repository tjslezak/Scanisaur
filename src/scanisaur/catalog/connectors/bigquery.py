"""BigQuery: catalog metadata only, never table data.

A full fetch follows spike 0001 (docs/spikes/0001-bigquery-metadata.md):

1. ``region-<location>.INFORMATION_SCHEMA.COLUMNS`` with ``COLUMN_FIELD_PATHS`` for
   descriptions, and ``TABLE_OPTIONS``: every column, partition and cluster column,
   ``require_partition_filter`` and table descriptions, in two queries.
2. ``tables.list`` per dataset: table kinds and time-partitioning granularity. Free.
3. One ``UNION ALL`` over each dataset's ``__TABLES__``: rows, bytes and last-modified
   times. Billed 0 bytes.
4. One ``UNION ALL`` over each dataset's ``TABLE_CONSTRAINTS`` and ``KEY_COLUMN_USAGE``:
   declared primary keys.
5. ``INFORMATION_SCHEMA.PARTITIONS`` only for partitioned tables of at least
   ``PARTITIONS_FROM_BYTES``: a bulk query takes minutes per 1,000 tables.

Steps 1 to 4 return plain rows, and :func:`assemble` builds the catalog from them, so
everything but the I/O is tested without a warehouse.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, NamedTuple, TypeVar

import google.auth.exceptions
from google.api_core import exceptions as api_exceptions
from google.cloud import bigquery

from scanisaur.catalog.connectors.base import ConnectorError, Probe, QueryRun, included
from scanisaur.catalog.model import (
    Catalog,
    Column,
    Granularity,
    Partition,
    Partitioning,
    Table,
    TableKind,
)
from scanisaur.config import BigQueryWarehouse

#: Partitioned tables at least this large get per-partition sizes.
PARTITIONS_FROM_BYTES = 10 * 2**30

#: Permissions that let an account read or change table data. Catalog-only access has
#: neither (docs/spikes/0003-permissions.md).
DATA_PERMISSIONS = ("bigquery.tables.getData", "bigquery.tables.updateData")
#: A date-sharded table such as ``events_20260930``.
_SHARD = re.compile(r"(?P<family>.+_)(?P<suffix>\d{8})")
_KINDS: dict[str, TableKind] = {
    "TABLE": "TABLE",
    "VIEW": "VIEW",
    "MATERIALIZED_VIEW": "MATERIALIZED_VIEW",
    "EXTERNAL": "EXTERNAL",
    "SNAPSHOT": "TABLE",
    "CLONE": "TABLE",
}
_GRANULARITIES: dict[str, Granularity] = {
    "HOUR": "HOUR",
    "DAY": "DAY",
    "MONTH": "MONTH",
    "YEAR": "YEAR",
}


class ColumnRow(NamedTuple):
    dataset: str
    table: str
    column: str
    type: str
    description: str
    partitioning: bool
    #: 1 for the first clustering column; None when not a clustering column.
    cluster_position: int | None


class OptionRow(NamedTuple):
    dataset: str
    table: str
    name: str
    #: GoogleSQL literal, such as ``true`` or ``"Orders, one row each"``.
    value: str


class ListingRow(NamedTuple):
    dataset: str
    table: str
    kind: str
    #: ``DAY`` and so on for time partitioning; None otherwise.
    granularity: str | None


class SizeRow(NamedTuple):
    dataset: str
    table: str
    row_count: int | None
    size_bytes: int | None
    #: Milliseconds since the epoch.
    last_modified: int | None


class KeyRow(NamedTuple):
    dataset: str
    table: str
    constraint: str
    column: str
    position: int


class PartitionRow(NamedTuple):
    dataset: str
    table: str
    partition_id: str
    bytes: int


@dataclass(frozen=True, slots=True)
class Rows:
    """Everything a full fetch reads."""

    columns: Sequence[ColumnRow]
    options: Sequence[OptionRow] = ()
    listing: Sequence[ListingRow] = ()
    sizes: Sequence[SizeRow] = ()
    keys: Sequence[KeyRow] = ()
    partitions: Sequence[PartitionRow] = ()


def assemble(project: str, rows: Rows) -> Catalog:
    """Build the catalog of ``project`` from fetched rows. Pure."""
    columns: defaultdict[tuple[str, str], list[ColumnRow]] = defaultdict(list)
    for row in rows.columns:
        columns[row.dataset, row.table].append(row)
    options = {(r.dataset, r.table, r.name): _literal(r.value) for r in rows.options}
    listing = {(r.dataset, r.table): r for r in rows.listing}
    sizes = {(r.dataset, r.table): r for r in rows.sizes}
    keys: defaultdict[tuple[str, str], dict[str, list[KeyRow]]] = defaultdict(dict)
    for key_row in rows.keys:
        keys[key_row.dataset, key_row.table].setdefault(key_row.constraint, []).append(key_row)
    partitions: defaultdict[tuple[str, str], list[Partition]] = defaultdict(list)
    for part in rows.partitions:
        partitions[part.dataset, part.table].append(Partition(part.partition_id, part.bytes))

    tables = []
    for (dataset, name), table_columns in columns.items():
        key = (dataset, name)
        listed, size = listing.get(key), sizes.get(key)
        constraints = keys.get(key)
        tables.append(
            Table(
                project=project,
                dataset=dataset,
                name=name,
                columns=tuple(Column(c.column, c.type, c.description) for c in table_columns),
                kind=_KINDS.get(listed.kind, "TABLE") if listed else "TABLE",
                row_count=size.row_count if size else None,
                size_bytes=size.size_bytes if size else None,
                partitioning=_partitioning(
                    table_columns,
                    listed.granularity if listed else None,
                    options.get((dataset, name, "require_partition_filter")) == "true",
                ),
                clustering=tuple(
                    c.column
                    for c in sorted(
                        (c for c in table_columns if c.cluster_position is not None),
                        key=lambda c: c.cluster_position or 0,
                    )
                ),
                description=options.get((dataset, name, "description"), ""),
                partitions=tuple(sorted(partitions[key], key=lambda p: p.id)),
                keys=_keys(constraints) if constraints is not None else None,
                last_modified=_millis(size.last_modified) if size else None,
            )
        )
    return Catalog(tuple(tables) + _families(tables), default_project=project)


def _partitioning(
    columns: Iterable[ColumnRow], granularity: str | None, required: bool
) -> Partitioning | None:
    column = next((c.column for c in columns if c.partitioning), None)
    unit = _GRANULARITIES.get(granularity or "")
    if unit is not None:
        return Partitioning(column, unit, required)  # column None: ingestion time
    if column is not None:
        return Partitioning(column, "RANGE", required)  # integer-range partitioning
    return None


def _keys(constraints: dict[str, list[KeyRow]]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(r.column for r in sorted(rows, key=lambda r: r.position))
        for _, rows in sorted(constraints.items())
    )


def _families(tables: Sequence[Table]) -> tuple[Table, ...]:
    """Wildcard families such as ``events_*`` for date-sharded tables.

    Each family takes its columns from its newest shard, and lists its shards as
    partitions, so a missing ``_TABLE_SUFFIX`` filter can be caught. The shards stay in
    the catalog too, for queries that name one.
    """
    shards: defaultdict[tuple[str, str], list[tuple[str, Table]]] = defaultdict(list)
    for table in tables:
        match = _SHARD.fullmatch(table.name)
        if match and table.partitioning is None and table.kind == "TABLE":
            shards[table.dataset, match["family"]].append((match["suffix"], table))
    families = []
    names = {(t.dataset, t.name) for t in tables}
    for (dataset, prefix), members in shards.items():
        if len(members) < 2 or (dataset, f"{prefix}*") in names:
            continue
        members.sort()
        newest = members[-1][1]
        families.append(
            replace(
                newest,
                name=f"{prefix}*",
                row_count=_total(t.row_count for _, t in members),
                size_bytes=_total(t.size_bytes for _, t in members),
                partitions=tuple(Partition(s, t.size_bytes or 0) for s, t in members),
                keys=None,
            )
        )
    return tuple(families)


def _total(values: Iterable[int | None]) -> int | None:
    values = list(values)
    return None if any(v is None for v in values) else sum(v or 0 for v in values)


def _literal(value: str) -> str:
    """A GoogleSQL option value as text: ``"abc"`` as ``abc``, ``true`` as is."""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return str(json.loads(value))
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def _millis(value: int | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value / 1000, UTC)


class BigQueryConnector:
    def __init__(self, warehouse: BigQueryWarehouse, client: bigquery.Client | None = None) -> None:
        self._warehouse = warehouse
        self._client = client

    @property
    def name(self) -> str:
        return self._warehouse.name

    @property
    def client(self) -> bigquery.Client:
        if self._client is None:
            warehouse = self._warehouse
            with _errors():
                self._client = bigquery.Client(
                    project=warehouse.billing_project or warehouse.project,
                    location=warehouse.location,
                )
        return self._client

    def fetch_catalog(self) -> Catalog:
        with _errors():
            by_location: defaultdict[str, list[str]] = defaultdict(list)
            for dataset, location in self._datasets().items():
                by_location[location].append(dataset)
            # A job runs in one location, so each location's datasets are read apart.
            parts = [self._rows(location, ds) for location, ds in sorted(by_location.items())]
        return assemble(
            self._warehouse.project,
            Rows(
                columns=[r for part in parts for r in part.columns],
                options=[r for part in parts for r in part.options],
                listing=[r for part in parts for r in part.listing],
                sizes=[r for part in parts for r in part.sizes],
                keys=[r for part in parts for r in part.keys],
                partitions=[r for part in parts for r in part.partitions],
            ),
        )

    def _rows(self, location: str, datasets: Sequence[str]) -> Rows:
        """Everything a full fetch reads for ``datasets``, all in ``location``."""
        wanted = set(datasets)
        rows = Rows(
            columns=[r for r in self._columns(location) if r.dataset in wanted],
            options=[r for r in self._options(location) if r.dataset in wanted],
            listing=list(self._listing(datasets)),
            sizes=list(self._sizes(location, datasets)),
            keys=list(self._declared_keys(location, datasets)),
        )
        return replace(rows, partitions=list(self._partitions(location, rows)))

    def fetch_table(self, project: str, dataset: str, name: str) -> Table | None:
        if project != self._warehouse.project or not self._included(dataset):
            return None
        with _errors():
            try:
                table = self.client.get_table(f"{project}.{dataset}.{name}")
            except api_exceptions.NotFound:
                return None
        return _from_api(table)

    def check_access(self) -> list[Probe]:
        """Credentials, the project-wide views a refresh needs, and data access.

        Data access is tested with ``testIamPermissions`` on one table per dataset, which
        is free and reads no data. The one query bills BigQuery's 10 MiB minimum.
        """
        try:
            with _errors():
                client = self.client
                datasets = self._datasets()
        except ConnectorError as error:
            return [Probe("credentials", "fail", str(error))]
        probes = [
            Probe("credentials", "ok", f"{len(datasets)} datasets in {self._warehouse.project}")
        ]
        try:
            with _errors():
                location = self._warehouse.location
                sql = f"SELECT 1 FROM {self._region('COLUMNS', location)} LIMIT 1"
                list(self._query(sql, location=location))
            probes.append(
                Probe("metadata", "ok", "can read the project-wide INFORMATION_SCHEMA views")
            )
        except ConnectorError as error:
            probes.append(
                Probe(
                    "metadata",
                    "fail",
                    f"{error}. Grant roles/bigquery.metadataViewer and roles/bigquery.jobUser "
                    "on the project; a basic role such as Owner isn't enough",
                )
            )
        readable = []
        for dataset in datasets:
            try:
                with _errors():
                    tables = list(
                        client.list_tables(f"{self._warehouse.project}.{dataset}", max_results=1)
                    )
                    if not tables:
                        continue
                    granted = client.test_iam_permissions(tables[0], DATA_PERMISSIONS)
            except ConnectorError as error:
                probes.append(Probe(f"data access: {dataset}", "fail", str(error)))
                continue
            if granted.get("permissions"):
                readable.append(dataset)
        if readable:
            probes.append(
                Probe(
                    "data access",
                    "warn",
                    f"this account can read or change table data in {', '.join(readable)}. "
                    "Catalog-only access needs only metadataViewer and jobUser. "
                    "IAM changes can take several minutes to apply",
                )
            )
        else:
            probes.append(Probe("data access", "ok", "can't read table data"))
        return probes

    def fetch_query_history(self, since: datetime) -> Iterator[QueryRun]:
        """SELECT jobs from ``JOBS_BY_PROJECT``, which needs ``bigquery.jobs.listAll``
        (roles/bigquery.resourceViewer). The history holds every query's full text,
        literal values included (docs/spikes/0002-job-history-comments.md)."""
        location = self._warehouse.location
        sql = f"""
            SELECT job_id, creation_time AS started, user_email AS user, query AS sql,
                   total_bytes_billed AS bytes_billed, error_result.reason AS error_reason
            FROM {self._region("JOBS_BY_PROJECT", location)}
            WHERE creation_time >= @since AND job_type = 'QUERY'
              AND statement_type = 'SELECT' AND state = 'DONE'
              AND NOT EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'tool'
                              AND value = 'scanisaur')
            ORDER BY creation_time
        """
        parameters = [bigquery.ScalarQueryParameter("since", "TIMESTAMP", since)]
        try:
            with _errors():
                rows = list(self._query(sql, parameters, location=location))
        except ConnectorError as error:
            raise ConnectorError(
                f"{error}. Reading query history needs bigquery.jobs.listAll on the project, "
                "from roles/bigquery.resourceViewer"
            ) from error
        return (
            QueryRun(
                row["job_id"],
                row["started"],
                row["user"],
                row["sql"],
                row["bytes_billed"],
                row["error_reason"],
            )
            for row in rows
        )

    def _included(self, dataset: str) -> bool:
        w = self._warehouse
        return included(dataset, w.include_datasets, w.exclude_datasets)

    def _datasets(self) -> dict[str, str]:
        """Each included dataset's location, by name."""
        listed = self.client.list_datasets(self._warehouse.project)
        # datasets.list returns each location; DatasetListItem has no property for it.
        return {
            d.dataset_id: d._properties.get("location") or self._warehouse.location
            for d in sorted(listed, key=lambda d: d.dataset_id)
            if self._included(d.dataset_id)
        }

    def _region(self, view: str, location: str) -> str:
        project = self._warehouse.project
        return f"`{project}`.`region-{location.lower()}`.INFORMATION_SCHEMA.{view}"

    def _columns(self, location: str) -> Iterator[ColumnRow]:
        sql = f"""
            SELECT c.table_schema AS dataset, c.table_name AS table, c.column_name AS column,
                   c.data_type AS type, IFNULL(p.description, '') AS description,
                   c.is_partitioning_column = 'YES' AS partitioning,
                   c.clustering_ordinal_position AS cluster_position
            FROM {self._region("COLUMNS", location)} AS c
            LEFT JOIN {self._region("COLUMN_FIELD_PATHS", location)} AS p
              ON p.table_schema = c.table_schema AND p.table_name = c.table_name
             AND p.field_path = c.column_name
            WHERE c.is_hidden = 'NO'
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
        """
        return (_row(ColumnRow, row) for row in self._query(sql, location=location))

    def _options(self, location: str) -> Iterator[OptionRow]:
        sql = f"""
            SELECT table_schema AS dataset, table_name AS table, option_name AS name,
                   option_value AS value
            FROM {self._region("TABLE_OPTIONS", location)}
            WHERE option_name IN ('require_partition_filter', 'description')
        """
        return (_row(OptionRow, row) for row in self._query(sql, location=location))

    def _listing(self, datasets: Sequence[str]) -> Iterator[ListingRow]:
        for dataset in datasets:
            for item in self.client.list_tables(f"{self._warehouse.project}.{dataset}"):
                partitioning = item.time_partitioning
                yield ListingRow(
                    dataset,
                    item.table_id,
                    item.table_type or "TABLE",
                    partitioning.type_ if partitioning is not None else None,
                )

    def _sizes(self, location: str, datasets: Sequence[str]) -> Iterator[SizeRow]:
        project = self._warehouse.project
        sql = " UNION ALL ".join(
            f"SELECT dataset_id AS dataset, table_id AS table, row_count, size_bytes,"
            f" last_modified_time AS last_modified"
            f" FROM `{project}`.`{d}`.__TABLES__"
            for d in datasets
        )
        return (_row(SizeRow, row) for row in self._query(sql, location=location))

    def _declared_keys(self, location: str, datasets: Sequence[str]) -> Iterator[KeyRow]:
        project = self._warehouse.project
        sql = " UNION ALL ".join(
            f"""SELECT k.table_schema AS dataset, k.table_name AS table,
                       k.constraint_name AS constraint, k.column_name AS column,
                       k.ordinal_position AS position
                FROM `{project}`.`{d}`.INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS k
                JOIN `{project}`.`{d}`.INFORMATION_SCHEMA.TABLE_CONSTRAINTS AS t
                  USING (constraint_name)
                WHERE t.constraint_type = 'PRIMARY KEY'"""
            for d in datasets
        )
        return (_row(KeyRow, row) for row in self._query(sql, location=location))

    def _partitions(self, location: str, rows: Rows) -> Iterator[PartitionRow]:
        partitioned = {(r.dataset, r.table) for r in rows.columns if r.partitioning} | {
            (r.dataset, r.table) for r in rows.listing if r.granularity is not None
        }
        large: defaultdict[str, list[str]] = defaultdict(list)
        for size in rows.sizes:
            key = (size.dataset, size.table)
            if key in partitioned and (size.size_bytes or 0) >= PARTITIONS_FROM_BYTES:
                large[size.dataset].append(size.table)
        project = self._warehouse.project
        for dataset, tables in sorted(large.items()):
            sql = f"""
                SELECT table_schema AS dataset, table_name AS table, partition_id,
                       total_logical_bytes AS bytes
                FROM `{project}`.`{dataset}`.INFORMATION_SCHEMA.PARTITIONS
                WHERE table_name IN UNNEST(@tables) AND total_logical_bytes IS NOT NULL
            """
            parameters = [bigquery.ArrayQueryParameter("tables", "STRING", sorted(tables))]
            yield from (
                _row(PartitionRow, row) for row in self._query(sql, parameters, location=location)
            )

    def _query(
        self,
        sql: str,
        parameters: Sequence[bigquery.ArrayQueryParameter | bigquery.ScalarQueryParameter] = (),
        *,
        location: str,
    ) -> Iterator[Any]:
        config = bigquery.QueryJobConfig(
            query_parameters=list(parameters), labels={"tool": "scanisaur"}
        )
        return iter(self.client.query(sql, job_config=config, location=location).result())


_R = TypeVar("_R", ColumnRow, OptionRow, SizeRow, KeyRow, PartitionRow)


def _row(kind: type[_R], row: Any) -> _R:
    """A query row as ``kind``, by column name: the SQL aliases each column to a field."""
    return kind._make(row[field] for field in kind._fields)


def _from_api(table: bigquery.Table) -> Table:
    """One table from ``tables.get``. Partition sizes are left out."""
    fields = table.schema or []
    time = table.time_partitioning
    range_ = table.range_partitioning
    partitioning = None
    unit = _GRANULARITIES.get(time.type_ or "") if time is not None else None
    if time is not None and unit is not None:
        partitioning = Partitioning(time.field, unit, bool(table.require_partition_filter))
    elif range_ is not None:
        partitioning = Partitioning(range_.field, "RANGE", bool(table.require_partition_filter))
    return Table(
        project=table.project,
        dataset=table.dataset_id,
        name=table.table_id,
        columns=tuple(Column(f.name, _field_type(f), f.description or "") for f in fields),
        kind=_KINDS.get(table.table_type or "TABLE", "TABLE"),
        row_count=table.num_rows,
        size_bytes=table.num_bytes,
        partitioning=partitioning,
        clustering=tuple(table.clustering_fields or ()),
        description=table.description or "",
        last_modified=table.modified,
    )


def _field_type(field: bigquery.SchemaField) -> str:
    """The GoogleSQL type of a schema field, as INFORMATION_SCHEMA.COLUMNS spells it."""
    names = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "RECORD": "STRUCT"}
    base = names.get(field.field_type, field.field_type)
    if base == "STRUCT":
        base = "STRUCT<" + ", ".join(f"{f.name} {_field_type(f)}" for f in field.fields) + ">"
    return f"ARRAY<{base}>" if field.mode == "REPEATED" else base


@contextmanager
def _errors() -> Iterator[None]:
    """Driver and credential errors as ConnectorError."""
    try:
        yield
    except google.auth.exceptions.DefaultCredentialsError as error:
        raise ConnectorError(
            "no Google credentials: run `gcloud auth application-default login`, "
            "or set GOOGLE_APPLICATION_CREDENTIALS"
        ) from error
    except (api_exceptions.GoogleAPIError, google.auth.exceptions.GoogleAuthError) as error:
        raise ConnectorError(f"BigQuery: {error}") from error
