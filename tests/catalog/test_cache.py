import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scanisaur.catalog import Catalog, Column, Partition, Partitioning, Table
from scanisaur.catalog.cache import SCHEMA_VERSION, CacheError, MetadataCache
from scanisaur.catalog.fixtures import load_catalog
from scanisaur.errors import ScanisaurError

GOLDEN = Path(__file__).parents[1] / "golden" / "catalog.yaml"


def _table(name: str, *columns: str, description: str = "") -> Table:
    return Table(
        "p", "d", name, tuple(Column(c, "STRING") for c in columns), description=description
    )


def test_empty_cache_has_no_snapshot(tmp_path: Path) -> None:
    assert MetadataCache(tmp_path / "c.sqlite").load() is None


def test_round_trip(tmp_path: Path) -> None:
    catalog = load_catalog(GOLDEN)
    cache = MetadataCache(tmp_path / "nested" / "c.sqlite")
    saved = cache.save(catalog, warehouse="bigquery:proj")
    loaded = cache.load()
    assert loaded is not None
    assert loaded.snapshot_id == saved.snapshot_id
    assert loaded.fetched_at == saved.fetched_at
    assert sorted(loaded.catalog.tables, key=lambda t: t.qualified_name) == sorted(
        catalog.tables, key=lambda t: t.qualified_name
    )
    assert (loaded.catalog.default_project, loaded.catalog.default_dataset) == (
        catalog.default_project,
        catalog.default_dataset,
    )


def test_every_table_field_survives(tmp_path: Path) -> None:
    table = Table(
        "p",
        "d",
        "events_*",
        (Column("event_date", "DATE", "day"), Column("n", "INT64")),
        kind="TABLE",
        row_count=10,
        size_bytes=80,
        partitioning=Partitioning("event_date", "DAY", required=True),
        clustering=("n",),
        description="events",
        keys=(("n",),),
        partitions=(Partition("20260930", 40),),
        last_modified=datetime(2026, 10, 1, 12, tzinfo=UTC),
    )
    cache = MetadataCache(tmp_path / "c.sqlite")
    cache.save(Catalog((table,)), warehouse="w")
    loaded = cache.load()
    assert loaded is not None
    assert loaded.catalog.tables == (table,)


def test_save_replaces_the_old_snapshot(tmp_path: Path) -> None:
    cache = MetadataCache(tmp_path / "c.sqlite")
    first = cache.save(Catalog((_table("a", "x"),)), warehouse="w")
    second = cache.save(Catalog((_table("b", "y"),)), warehouse="w")
    loaded = cache.load()
    assert loaded is not None
    assert int(second.snapshot_id) > int(first.snapshot_id)
    assert [t.name for t in loaded.catalog.tables] == ["b"]
    with sqlite3.connect(cache.path) as db:
        assert db.execute("SELECT count(*) FROM tables").fetchone() == (1,)


def test_reader_sees_old_or_new_never_a_mix(tmp_path: Path) -> None:
    cache = MetadataCache(tmp_path / "c.sqlite")
    cache.save(Catalog(tuple(_table(f"old{i}", "x") for i in range(3))), warehouse="w")
    with sqlite3.connect(cache.path) as writer:
        writer.execute("BEGIN")
        writer.execute(
            "INSERT INTO snapshots (warehouse, fetched_at) VALUES ('w', '2026-10-03T00:00:00')"
        )
        loaded = cache.load()  # while the write is uncommitted
        writer.rollback()
    assert loaded is not None
    assert {t.name for t in loaded.catalog.tables} == {"old0", "old1", "old2"}


def test_put_table(tmp_path: Path) -> None:
    cache = MetadataCache(tmp_path / "c.sqlite")
    snapshot = cache.save(Catalog((_table("a", "x"),)), warehouse="w")
    updated = cache.put_table(snapshot, _table("a", "x", "y"))
    updated = cache.put_table(updated, _table("new", "z"))
    loaded = cache.load()
    assert loaded is not None
    for catalog in (updated.catalog, loaded.catalog):
        assert catalog.find("a", "d", "p") == _table("a", "x", "y")
        assert catalog.find("new", "d", "p") == _table("new", "z")
    assert loaded.snapshot_id == snapshot.snapshot_id


def test_other_version_is_rebuilt(tmp_path: Path) -> None:
    cache = MetadataCache(tmp_path / "c.sqlite")
    cache.save(Catalog((_table("a", "x"),)), warehouse="w")
    with sqlite3.connect(cache.path) as db:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    assert cache.load() is None


def test_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "c.sqlite"
    path.write_bytes(b"not a database" * 100)
    with pytest.raises(CacheError, match=r"c\.sqlite") as error:
        MetadataCache(path).load()
    assert isinstance(error.value, ScanisaurError)


def test_search(tmp_path: Path) -> None:
    cache = MetadataCache(tmp_path / "c.sqlite")
    cache.save(
        Catalog(
            (
                _table("orders", "order_id", "customer_id", description="One row per order"),
                _table("users", "user_id", "country"),
            )
        ),
        warehouse="w",
    )
    hits = cache.search("customer", 10)
    assert [(h.table, h.column) for h in hits] == [("p.d.orders", "customer_id")]
    tables = {(h.table, h.column) for h in cache.search("order", 10)}
    assert ("p.d.orders", None) in tables
    assert ("p.d.orders", "order_id") in tables
    assert cache.search("country users", 1)[0].table == "p.d.users"
    assert cache.search("", 5) == []
    assert cache.search('"', 5) == []


def test_load_10k_tables(tmp_path: Path) -> None:
    tables = tuple(_table(f"t{i}", *(f"c{j}" for j in range(20))) for i in range(10_000))
    cache = MetadataCache(tmp_path / "c.sqlite")
    cache.save(Catalog(tables), warehouse="w")
    start = time.perf_counter()
    loaded = cache.load()
    elapsed = time.perf_counter() - start
    assert loaded is not None
    assert len(loaded.catalog.tables) == 10_000
    print(f"loaded 10,000 tables in {elapsed * 1000:.0f} ms")
