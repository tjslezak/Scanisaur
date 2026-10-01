from pathlib import Path

import pytest

from scanisaur.catalog import Catalog, Column, Partitioning, Table
from scanisaur.catalog.fixtures import FixtureError, load_catalog
from scanisaur.catalog.model import PARTITIONDATE, PARTITIONTIME, TABLE_SUFFIX


def make_table(
    name: str, dataset: str = "analytics", partitioning: Partitioning | None = None
) -> Table:
    columns = (Column("event_date", "DATE"), Column("User_Id", "STRING"))
    return Table("proj", dataset, name, columns, partitioning=partitioning)


CATALOG = Catalog(
    tables=(
        make_table("events"),
        make_table("Orders"),
        make_table("events_*", dataset="ga4"),
        make_table("ingest", partitioning=Partitioning(None, "DAY")),
    ),
    default_project="proj",
    default_dataset="analytics",
)


class TestTable:
    def test_qualified_name(self) -> None:
        assert make_table("events").qualified_name == "proj.analytics.events"

    def test_column_lookup_is_case_insensitive(self) -> None:
        table = make_table("events")
        assert table.column("user_id") == Column("User_Id", "STRING")
        assert table.column("missing") is None

    def test_pseudo_columns(self) -> None:
        assert make_table("events").pseudo_columns == frozenset()
        assert make_table("events_*").pseudo_columns == {TABLE_SUFFIX}
        partitioned = make_table("p", partitioning=Partitioning("event_date", "DAY"))
        assert partitioned.pseudo_columns == frozenset()
        ingestion = make_table("i", partitioning=Partitioning(None, "DAY"))
        assert ingestion.pseudo_columns == {PARTITIONTIME, PARTITIONDATE}

    @pytest.mark.parametrize("granularity", ["HOUR", "MONTH", "YEAR"])
    def test_partitiondate_only_on_daily_ingestion_partitions(self, granularity: str) -> None:
        table = make_table("i", partitioning=Partitioning(None, granularity))  # type: ignore[arg-type]
        assert table.pseudo_columns == {PARTITIONTIME}


class TestCatalogFind:
    @pytest.mark.parametrize(
        ("name", "dataset", "project", "expected"),
        [
            ("events", None, None, "proj.analytics.events"),
            ("events", "analytics", None, "proj.analytics.events"),
            ("events", "analytics", "proj", "proj.analytics.events"),
            ("events_*", "ga4", None, "proj.ga4.events_*"),
            ("events_2026*", "ga4", None, "proj.ga4.events_*"),
            ("Orders", None, None, "proj.analytics.Orders"),
        ],
    )
    def test_finds(
        self, name: str, dataset: str | None, project: str | None, expected: str
    ) -> None:
        table = CATALOG.find(name, dataset, project)
        assert table is not None
        assert table.qualified_name == expected

    @pytest.mark.parametrize(
        ("name", "dataset", "project"),
        [
            ("orders", None, None),  # table names are case-sensitive
            ("events", "ga4", None),
            ("events", "analytics", "other"),
            ("other_*", "ga4", None),
            ("events_*", None, None),  # the wildcard family is in another dataset
        ],
    )
    def test_misses(self, name: str, dataset: str | None, project: str | None) -> None:
        assert CATALOG.find(name, dataset, project) is None

    def test_overlapping_wildcard_families_take_the_longest(self) -> None:
        catalog = Catalog(
            tables=(make_table("ev_*"), make_table("ev_intraday_*"), make_table("ev_in*")),
            default_project="proj",
            default_dataset="analytics",
        )
        for name, expected in [
            ("ev_intraday_2026*", "ev_intraday_*"),
            ("ev_in_2026*", "ev_in*"),
            ("ev_2026*", "ev_*"),
        ]:
            table = catalog.find(name)
            assert table is not None
            assert table.name == expected

    def test_needs_defaults_for_partial_names(self) -> None:
        catalog = Catalog(tables=(make_table("events"),))
        assert catalog.find("events") is None
        assert catalog.find("events", "analytics", "proj") is not None


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "catalog.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestLoadCatalog:
    def test_loads_every_field(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            """
default_project: proj
default_dataset: analytics
tables:
  - name: proj.analytics.events
    kind: VIEW
    rows: 10
    bytes: 2048
    partitioning: {column: event_date, granularity: MONTH, required: true}
    clustering: [user_id]
    description: Raw events
    columns:
      event_date: DATE
      user_id: STRING
""",
        )
        catalog = load_catalog(str(path))
        assert catalog.default_project == "proj"
        assert catalog.default_dataset == "analytics"
        (table,) = catalog.tables
        assert table == Table(
            project="proj",
            dataset="analytics",
            name="events",
            columns=(Column("event_date", "DATE"), Column("user_id", "STRING")),
            kind="VIEW",
            row_count=10,
            size_bytes=2048,
            partitioning=Partitioning("event_date", "MONTH", required=True),
            clustering=("user_id",),
            description="Raw events",
        )

    def test_ingestion_time_partitioning(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "tables:\n  - name: p.d.t\n    partitioning: {}\n    columns: {payload: STRING}\n",
        )
        (table,) = load_catalog(path).tables
        assert table.partitioning == Partitioning(None, "DAY")
        assert PARTITIONTIME in table.pseudo_columns

    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            ("tables: [", "while parsing"),
            ("[]", "Input should be"),
            ("tables:\n  - name: events\n    columns: {a: INT64}\n", "project.dataset.table"),
            ("tables:\n  - name: p.d.t\n    columns: {}\n", "at least 1"),
            ("tables:\n  - name: p.d.t\n    rows: -1\n    columns: {a: INT64}\n", "greater than"),
            ("tables:\n  - name: p.d.t\n    colums: {a: INT64}\n", "colums"),
            (
                "tables:\n  - name: p.d.t\n    clustering: [b]\n    columns: {a: INT64}\n",
                "unknown columns: ['b']",
            ),
            (
                "tables:\n  - {name: p.d.t, partitioning: {column: b}, columns: {a: INT64}}\n",
                "unknown columns: ['b']",
            ),
            (
                "tables:\n  - {name: p.d.t, columns: {a: NOT A TYPE}}\n",
                "column 'a' has a type that isn't valid: 'NOT A TYPE'",
            ),
            (
                "tables:\n"
                "  - {name: p.d.t, columns: {a: INT64}}\n"
                "  - {name: p.d.t, columns: {b: INT64}}\n",
                "more than once: ['p.d.t']",
            ),
        ],
    )
    def test_rejects_invalid_fixtures(self, tmp_path: Path, text: str, reason: str) -> None:
        path = write(tmp_path, text)
        with pytest.raises(FixtureError, match=str(path)) as error:
            load_catalog(path)
        assert reason in str(error.value)

    def test_nested_types_are_valid(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "tables:\n  - name: p.d.t\n    columns:\n"
            "      a: ARRAY<STRUCT<key STRING, value STRUCT<n INT64, s STRING>>>\n"
            "      b: NUMERIC(10, 2)\n",
        )
        assert len(load_catalog(path).tables[0].columns) == 2

    def test_not_utf8(self, tmp_path: Path) -> None:
        path = tmp_path / "catalog.yaml"
        path.write_bytes(b"\xff")
        with pytest.raises(FixtureError, match="utf-8"):
            load_catalog(path)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FixtureError, match="No such file"):
            load_catalog(tmp_path / "missing.yaml")
