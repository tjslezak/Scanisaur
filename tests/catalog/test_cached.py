from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scanisaur.catalog import Catalog, Column, Table
from scanisaur.catalog.cache import MetadataCache
from scanisaur.catalog.cached import CachedSource
from scanisaur.catalog.connectors import ConnectorError, Probe
from scanisaur.config import CacheSettings, Config, ConfigError, DuckDBWarehouse


class FakeConnector:
    name = "fake:warehouse"

    def __init__(self) -> None:
        self.tables = [Table("p", "d", "orders", (Column("order_id", "INT64"),))]
        self.fetches = 0
        self.fail = False
        self.new: dict[str, Table] = {}
        self.lookups: list[tuple[str, str, str]] = []

    def fetch_catalog(self) -> Catalog:
        if self.fail:
            raise ConnectorError("warehouse down")
        self.fetches += 1
        return Catalog(tuple(self.tables), default_project="p", default_dataset="d")

    def fetch_table(self, project: str, dataset: str, name: str) -> Table | None:
        self.lookups.append((project, dataset, name))
        if self.fail:
            raise ConnectorError("warehouse down")
        return self.new.get(name)

    def check_access(self) -> list[Probe]:
        return []


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def connector() -> FakeConnector:
    return FakeConnector()


def _source(
    tmp_path: Path, connector: FakeConnector, clock: Clock, **config: object
) -> CachedSource:
    return CachedSource(
        Config(cache=CacheSettings(ttl=timedelta(hours=6)), **config),  # type: ignore[arg-type]
        connector=connector,
        cache=MetadataCache(tmp_path / "c.sqlite"),
        now=clock,
    )


def test_first_use_fetches_then_reuses(tmp_path: Path, connector: FakeConnector) -> None:
    clock = Clock()
    source = _source(tmp_path, connector, clock)
    first = source.current()
    assert first.catalog.find("orders") is not None
    assert source.current() is first
    # A new process reads the cache instead of the warehouse.
    assert _source(tmp_path, connector, clock).current().snapshot_id == first.snapshot_id
    assert connector.fetches == 1


def test_stale_snapshot_is_refreshed(tmp_path: Path, connector: FakeConnector) -> None:
    clock = Clock()
    source = _source(tmp_path, connector, clock)
    first = source.current()
    clock.now += timedelta(hours=7)
    connector.tables.append(Table("p", "d", "users", (Column("user_id", "INT64"),)))
    second = source.current()
    assert second.snapshot_id != first.snapshot_id
    assert second.catalog.find("users") is not None


def test_failed_refresh_keeps_the_stale_snapshot(
    tmp_path: Path, connector: FakeConnector, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    source = _source(tmp_path, connector, clock)
    first = source.current()
    clock.now += timedelta(days=1)
    connector.fail = True
    assert source.current() is first
    assert "stale catalog" in caplog.text
    assert "warehouse down" in caplog.text


def test_first_fetch_failure_raises(tmp_path: Path, connector: FakeConnector) -> None:
    connector.fail = True
    with pytest.raises(ConnectorError, match="warehouse down"):
        _source(tmp_path, connector, Clock()).current()


def test_configured_keys_are_added(tmp_path: Path, connector: FakeConnector) -> None:
    source = _source(tmp_path, connector, Clock(), keys={"p.d.orders": (("order_id",),)})
    orders = source.current().catalog.find("orders")
    assert orders is not None
    assert orders.keys == (("order_id",),)


def test_search(tmp_path: Path, connector: FakeConnector) -> None:
    hits = _source(tmp_path, connector, Clock()).search("order", 5)
    assert {(h.table, h.column) for h in hits} == {("p.d.orders", None), ("p.d.orders", "order_id")}


def test_needs_a_warehouse() -> None:
    with pytest.raises(ConfigError, match="names no warehouse"):
        CachedSource(Config())


def test_new_table_is_fetched_once(tmp_path: Path, connector: FakeConnector) -> None:
    clock = Clock()
    source = _source(tmp_path, connector, clock, keys={"p.d.users": (("user_id",),)})
    source.current()
    connector.new["users"] = Table("p", "d", "users", (Column("user_id", "INT64"),))
    sql = "WITH u AS (SELECT 1 AS x) SELECT * FROM users JOIN orders USING (user_id), u"
    snapshot = source.snapshot_for(sql)
    users = snapshot.catalog.find("users")
    assert users is not None
    assert users.keys == (("user_id",),)
    assert connector.lookups == [("p", "d", "users")]  # not the CTE, not orders
    # Saved: a new process finds it without asking the warehouse.
    again = _source(tmp_path, connector, clock, keys={"p.d.users": (("user_id",),)}).snapshot_for(
        sql
    )
    assert again.catalog.find("users") is not None
    assert connector.lookups == [("p", "d", "users")]


def test_missing_table_stays_missing(tmp_path: Path, connector: FakeConnector) -> None:
    source = _source(tmp_path, connector, Clock())
    snapshot = source.snapshot_for("SELECT * FROM nope")
    assert snapshot.catalog.find("nope") is None
    assert connector.lookups == [("p", "d", "nope")]


def test_lookup_failure_is_a_warning(
    tmp_path: Path, connector: FakeConnector, caplog: pytest.LogCaptureFixture
) -> None:
    source = _source(tmp_path, connector, Clock())
    source.current()
    connector.fail = True
    assert source.snapshot_for("SELECT * FROM nope").catalog.find("nope") is None
    assert "couldn't look up p.d.nope" in caplog.text


def test_unparsable_sql_is_left_to_check(tmp_path: Path, connector: FakeConnector) -> None:
    source = _source(tmp_path, connector, Clock())
    source.snapshot_for("SELECT FROM WHERE (")
    source.snapshot_for("DROP TABLE x")
    assert connector.lookups == []


def test_fresh_cache_never_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, connector: FakeConnector
) -> None:
    config = Config(
        warehouse=DuckDBWarehouse(type="duckdb", path=tmp_path / "w.duckdb"),
        cache=CacheSettings(path=tmp_path / "c.sqlite"),
    )
    CachedSource(config, connector=connector).refresh()

    def refuse(_: object) -> None:
        raise AssertionError("connected")

    monkeypatch.setattr("scanisaur.catalog.cached.connect", refuse)
    assert CachedSource(config).current().catalog.find("orders", "d") is not None


def test_changed_keys_start_a_fresh_snapshot(tmp_path: Path, connector: FakeConnector) -> None:
    _source(tmp_path, connector, Clock()).current()
    keyed = _source(tmp_path, connector, Clock(), keys={"p.d.orders": (("order_id",),)})
    orders = keyed.current().catalog.find("orders", "d")
    assert orders is not None
    assert orders.keys == (("order_id",),)
    assert connector.fetches == 2
