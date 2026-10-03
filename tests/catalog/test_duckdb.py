from pathlib import Path

import duckdb
import pytest

from scanisaur.catalog import Column
from scanisaur.catalog.connectors import ConnectorError, connect
from scanisaur.config import DuckDBWarehouse

DDL = """
CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    user_id VARCHAR,
    amount DECIMAL(10, 2),
    created TIMESTAMP,
    UNIQUE (user_id, created)
);
COMMENT ON TABLE orders IS 'One row per order';
COMMENT ON COLUMN orders.user_id IS 'The buyer';
CREATE TABLE events (user_id VARCHAR, tags VARCHAR[]);
INSERT INTO events VALUES ('a', ['x']), ('b', ['y']);
CREATE SCHEMA scratch;
CREATE TABLE scratch.tmp (x INTEGER);
CREATE VIEW big_orders AS SELECT * FROM orders WHERE amount > 100;
"""


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "shop.duckdb"
    with duckdb.connect(str(path)) as db:
        db.execute(DDL)
    return path


def _warehouse(path: Path, **kwargs: object) -> DuckDBWarehouse:
    return DuckDBWarehouse.model_validate({"type": "duckdb", "path": path, **kwargs})


def test_fetch_catalog(database: Path) -> None:
    catalog = connect(_warehouse(database)).fetch_catalog()
    assert (catalog.default_project, catalog.default_dataset) == ("shop", "main")
    orders = catalog.find("orders")
    assert orders is not None
    assert orders.columns == (
        Column("order_id", "INT64"),
        Column("user_id", "STRING", "The buyer"),
        Column("amount", "NUMERIC(10, 2)"),
        Column("created", "DATETIME"),
    )
    assert orders.description == "One row per order"
    assert orders.keys == (("order_id",), ("user_id", "created"))
    events = catalog.find("events")
    assert events is not None
    assert events.row_count == 2
    assert events.keys is None  # no constraint: unknown, not "nothing unique"
    assert events.column("tags") == Column("tags", "ARRAY<STRING>")
    view = catalog.find("big_orders")
    assert view is not None
    assert (view.kind, view.keys) == ("VIEW", None)
    assert catalog.find("tmp", "scratch") is not None


def test_datasets_filter(database: Path) -> None:
    connector = connect(_warehouse(database, exclude_datasets=["scratch"]))
    names = {t.qualified_name for t in connector.fetch_catalog().tables}
    assert names == {"shop.main.orders", "shop.main.events", "shop.main.big_orders"}
    only = connect(_warehouse(database, include_datasets=["scratch"]))
    assert [t.name for t in only.fetch_catalog().tables] == ["tmp"]


def test_fetch_table(database: Path) -> None:
    connector = connect(_warehouse(database))
    table = connector.fetch_table("shop", "main", "events")
    assert table is not None
    assert table.name == "events"
    assert connector.fetch_table("shop", "main", "nope") is None


def test_check_access(database: Path, tmp_path: Path) -> None:
    [probe] = connect(_warehouse(database)).check_access()
    assert (probe.status, probe.detail) == ("ok", f"4 tables in {database}")
    [probe] = connect(_warehouse(tmp_path / "missing.duckdb")).check_access()
    assert probe.status == "fail"
    assert "no such DuckDB file" in probe.detail


def test_not_a_database(tmp_path: Path) -> None:
    path = tmp_path / "bad.duckdb"
    path.write_text("nope")
    with pytest.raises(ConnectorError, match=r"bad\.duckdb"):
        connect(_warehouse(path)).fetch_catalog()


def test_name_is_the_resolved_path(database: Path) -> None:
    assert connect(_warehouse(database)).name == f"duckdb:{database.resolve()}"
