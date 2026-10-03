from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import bigquery
from google.cloud.bigquery.table import TableListItem

from scanisaur.catalog import Column, Partition, Partitioning
from scanisaur.catalog.connectors import ConnectorError
from scanisaur.catalog.connectors.bigquery import (
    PARTITIONS_FROM_BYTES,
    BigQueryConnector,
    ColumnRow,
    KeyRow,
    ListingRow,
    OptionRow,
    PartitionRow,
    Rows,
    SizeRow,
    assemble,
)
from scanisaur.config import BigQueryWarehouse

BIG = PARTITIONS_FROM_BYTES

#: Rows as the sandbox returns them, for a small analytics dataset.
ROWS = Rows(
    columns=[
        ColumnRow("analytics", "events", "event_date", "DATE", "", True, None),
        ColumnRow("analytics", "events", "user_id", "STRING", "who", False, 1),
        ColumnRow("analytics", "events", "kind", "STRING", "", False, 2),
        ColumnRow("analytics", "orders", "order_id", "INT64", "", False, None),
        ColumnRow("analytics", "orders", "total", "NUMERIC(10, 2)", "", False, None),
        ColumnRow("analytics", "ingested", "x", "INT64", "", False, None),
        ColumnRow("analytics", "ranged", "bucket", "INT64", "", True, None),
        ColumnRow("ga4", "events_20260929", "n", "INT64", "", False, None),
        ColumnRow("ga4", "events_20260930", "n", "INT64", "", False, None),
        ColumnRow("ga4", "events_20260930", "m", "STRING", "", False, None),
        ColumnRow("ga4", "lonely_20260930", "n", "INT64", "", False, None),
        ColumnRow("analytics", "recent", "v", "STRING", "", False, None),
    ],
    options=[
        OptionRow("analytics", "events", "require_partition_filter", "true"),
        OptionRow("analytics", "orders", "description", '"One row per \\"order\\""'),
    ],
    listing=[
        ListingRow("analytics", "events", "TABLE", "DAY"),
        ListingRow("analytics", "orders", "TABLE", None),
        ListingRow("analytics", "ingested", "TABLE", "HOUR"),
        ListingRow("analytics", "ranged", "TABLE", None),
        ListingRow("analytics", "recent", "VIEW", None),
    ],
    sizes=[
        SizeRow("analytics", "events", 1000, BIG, 1_790_000_000_000),
        SizeRow("analytics", "orders", 10, 800, None),
        SizeRow("ga4", "events_20260929", 5, 40, None),
        SizeRow("ga4", "events_20260930", 6, 60, None),
    ],
    keys=[
        KeyRow("analytics", "orders", "orders.pk$", "order_id", 1),
    ],
    partitions=[
        PartitionRow("analytics", "events", "20260930", BIG // 2),
        PartitionRow("analytics", "events", "20260929", BIG // 2),
    ],
)


def test_assemble() -> None:
    catalog = assemble("proj", ROWS)
    assert catalog.default_project == "proj"
    events = catalog.find("events", "analytics")
    assert events is not None
    assert events.partitioning == Partitioning("event_date", "DAY", required=True)
    assert events.clustering == ("user_id", "kind")
    assert events.column("user_id") == Column("user_id", "STRING", "who")
    assert (events.row_count, events.size_bytes) == (1000, BIG)
    assert events.partitions == (Partition("20260929", BIG // 2), Partition("20260930", BIG // 2))
    assert events.last_modified == datetime.fromtimestamp(1_790_000_000, UTC)
    assert events.keys is None

    orders = catalog.find("orders", "analytics")
    assert orders is not None
    assert orders.description == 'One row per "order"'
    assert orders.keys == (("order_id",),)
    assert orders.partitioning is None

    ingested = catalog.find("ingested", "analytics")
    assert ingested is not None
    assert ingested.partitioning == Partitioning(None, "HOUR")
    ranged = catalog.find("ranged", "analytics")
    assert ranged is not None
    assert ranged.partitioning == Partitioning("bucket", "RANGE")
    view = catalog.find("recent", "analytics")
    assert view is not None
    assert view.kind == "VIEW"


def test_shards_become_a_family() -> None:
    catalog = assemble("proj", ROWS)
    family = catalog.find("events_*", "ga4")
    assert family is not None
    assert [c.name for c in family.columns] == ["n", "m"]  # the newest shard's
    assert family.partitions == (Partition("20260929", 40), Partition("20260930", 60))
    assert (family.row_count, family.size_bytes) == (11, 100)
    assert catalog.find("events_2026*", "ga4") == family
    assert catalog.find("events_20260929", "ga4") is not None
    assert catalog.find("lonely_*", "ga4") is None  # one shard is not a family


class _Row:
    """A query row, read by column name like ``bigquery.Row``."""

    def __init__(self, row: NamedTuple) -> None:
        self._fields = row._asdict()

    def __getitem__(self, key: str) -> object:
        return self._fields[key]


class FakeClient:
    """Answers each kind of metadata query with the matching ROWS."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.partition_tables: list[str] = []
        self.metadata_denied = False
        self.data_readable: set[str] = set()

    def test_iam_permissions(self, table: TableListItem, permissions: Sequence[str]) -> Any:
        granted = list(permissions) if table.dataset_id in self.data_readable else []
        return {"permissions": granted}

    def list_datasets(self, project: str) -> list[Any]:
        return [bigquery.DatasetReference(project, d) for d in ("analytics", "ga4", "scratch")]

    def list_tables(self, dataset: str, max_results: int | None = None) -> list[TableListItem]:
        name = dataset.split(".")[1]
        return [
            TableListItem(  # type: ignore[no-untyped-call]
                {
                    "tableReference": {"projectId": "proj", "datasetId": name, "tableId": r.table},
                    "type": r.kind,
                    **({"timePartitioning": {"type": r.granularity}} if r.granularity else {}),
                }
            )
            for r in ROWS.listing
            if r.dataset == name
        ]

    def query(self, sql: str, job_config: bigquery.QueryJobConfig) -> Any:
        self.queries.append(sql)
        rows: Sequence[NamedTuple]
        if self.metadata_denied:
            raise api_exceptions.Forbidden("Access Denied: INFORMATION_SCHEMA")  # type: ignore[no-untyped-call]
        if sql.strip().endswith("LIMIT 1"):
            rows = []
        elif "PARTITIONS" in sql:
            [parameter] = job_config.query_parameters
            self.partition_tables += parameter.values
            rows = ROWS.partitions
        elif "COLUMN_FIELD_PATHS" in sql:
            rows = ROWS.columns
        elif "TABLE_OPTIONS" in sql:
            rows = ROWS.options
        elif "__TABLES__" in sql:
            rows = ROWS.sizes
        elif "KEY_COLUMN_USAGE" in sql:
            rows = ROWS.keys
        else:
            raise AssertionError(sql)
        return _Job([_Row(row) for row in rows])

    def get_table(self, name: str) -> bigquery.Table:
        if not name.endswith(".orders"):
            raise api_exceptions.NotFound("no such table")  # type: ignore[no-untyped-call]
        table = bigquery.Table(
            name,
            schema=[
                bigquery.SchemaField("order_id", "INTEGER", description="id"),
                bigquery.SchemaField("tags", "STRING", mode="REPEATED"),
                bigquery.SchemaField(
                    "shipping", "RECORD", fields=[bigquery.SchemaField("city", "STRING")]
                ),
                bigquery.SchemaField("created", "TIMESTAMP"),
            ],
        )
        table.time_partitioning = bigquery.TimePartitioning(type_="DAY", field="created")
        table.require_partition_filter = True
        table.clustering_fields = ["order_id"]
        return table


class _Job:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def result(self) -> list[_Row]:
        return self._rows


def _connector(client: FakeClient, **kwargs: object) -> BigQueryConnector:
    warehouse = BigQueryWarehouse.model_validate(
        {"type": "bigquery", "project": "proj", "location": "EU", **kwargs}
    )
    return BigQueryConnector(warehouse, client)  # type: ignore[arg-type]


def test_fetch_catalog() -> None:
    client = FakeClient()
    catalog = _connector(client, exclude_datasets=["ga4"]).fetch_catalog()
    assert {t.dataset for t in catalog.tables} == {"analytics"}
    assert catalog.find("events", "analytics") is not None
    assert client.partition_tables == ["events"]  # only the large partitioned table
    region = "`proj`.`region-eu`.INFORMATION_SCHEMA."
    assert any(region + "COLUMNS" in q for q in client.queries)
    sizes = next(q for q in client.queries if "__TABLES__" in q)
    assert "`proj`.`analytics`.__TABLES__" in sizes
    assert "`scratch`" in sizes  # included, though it has no tables
    assert "`ga4`" not in sizes


def test_no_datasets() -> None:
    client = FakeClient()
    catalog = _connector(client, include_datasets=["missing"]).fetch_catalog()
    assert catalog.tables == ()
    assert client.queries == []


def test_fetch_table() -> None:
    connector = _connector(FakeClient())
    table = connector.fetch_table("proj", "analytics", "orders")
    assert table is not None
    assert [(c.name, c.type) for c in table.columns] == [
        ("order_id", "INT64"),
        ("tags", "ARRAY<STRING>"),
        ("shipping", "STRUCT<city STRING>"),
        ("created", "TIMESTAMP"),
    ]
    assert table.partitioning == Partitioning("created", "DAY", required=True)
    assert table.clustering == ("order_id",)
    assert connector.fetch_table("proj", "analytics", "nope") is None
    assert connector.fetch_table("other", "analytics", "orders") is None


def test_api_errors_are_connector_errors() -> None:
    client = FakeClient()

    def forbidden(project: str) -> list[Any]:
        raise api_exceptions.Forbidden("Access Denied")  # type: ignore[no-untyped-call]

    client.list_datasets = forbidden  # type: ignore[method-assign]
    with pytest.raises(ConnectorError, match="Access Denied"):
        _connector(client).fetch_catalog()


def test_name() -> None:
    assert _connector(FakeClient()).name == "bigquery:proj:EU"


def test_check_access_catalog_only() -> None:
    probes = _connector(FakeClient()).check_access()
    assert [(p.name, p.status) for p in probes] == [
        ("credentials", "ok"),
        ("metadata", "ok"),
        ("data access", "ok"),
    ]
    assert probes[0].detail == "3 datasets in proj"


def test_check_access_warns_on_data_access() -> None:
    client = FakeClient()
    client.data_readable = {"analytics"}
    probe = _connector(client).check_access()[-1]
    assert (probe.name, probe.status) == ("data access", "warn")
    assert "read or change table data in analytics" in probe.detail
    assert "several minutes" in probe.detail


def test_check_access_without_project_views() -> None:
    client = FakeClient()
    client.metadata_denied = True
    probe = _connector(client).check_access()[1]
    assert (probe.name, probe.status) == ("metadata", "fail")
    assert "roles/bigquery.metadataViewer" in probe.detail


def test_check_access_without_credentials() -> None:
    client = FakeClient()

    def denied(project: str) -> list[Any]:
        raise api_exceptions.Forbidden("Access Denied")  # type: ignore[no-untyped-call]

    client.list_datasets = denied  # type: ignore[method-assign]
    [probe] = _connector(client).check_access()
    assert (probe.name, probe.status) == ("credentials", "fail")
