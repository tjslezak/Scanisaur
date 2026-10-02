"""Edge cases for SCN003 and SCN004; tests/golden/scn003 and scn004 cover the main behavior."""

from pathlib import Path

import pytest
from sqlglot import exp

from scanisaur.catalog import Catalog, Column, Partitioning, Table
from scanisaur.catalog.fixtures import load_catalog
from scanisaur.engine import check as check_module
from scanisaur.engine import pruning
from scanisaur.engine.check import Policy, check
from scanisaur.engine.facts import Predicate, QueryFacts, TableFacts, TooComplexError
from scanisaur.engine.result import Severity, Verdict
from scanisaur.engine.rules import PARTITION_FILTER, PRUNING_DEFEATED, UNANALYZABLE

CATALOG = load_catalog(Path(__file__).parents[1] / "golden" / "catalog.yaml")


def only_finding(sql: str, catalog: Catalog = CATALOG) -> tuple[str, str, str]:
    (finding,) = check(sql, catalog).findings
    assert finding.fix is not None
    return finding.rule, finding.message, finding.fix


class TestPartitionFilter:
    def test_ingestion_time_names_the_pseudo_column_used(self) -> None:
        rule, message, fix = only_finding(
            "SELECT payload FROM raw.ingest WHERE _PARTITIONDATE IS NOT NULL"
        )
        assert rule == PARTITION_FILTER
        assert "`_PARTITIONDATE IS NOT NULL` can't limit which partitions" in message
        assert fix.endswith("`_PARTITIONDATE >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)`.")

    def test_suffix_compared_with_a_column(self) -> None:
        _rule, message, _fix = only_finding(
            "SELECT event_name FROM `proj.ga4.events_*` WHERE _TABLE_SUFFIX = event_name"
        )
        assert "`_TABLE_SUFFIX = event_name` can't limit which shards are read" in message

    def test_reading_only_shard_names_reads_nothing(self) -> None:
        assert (
            check("SELECT DISTINCT _TABLE_SUFFIX FROM `proj.ga4.events_*`", CATALOG).findings == ()
        )

    def test_unknown_size_and_datetime_column(self) -> None:
        table = Table(
            "p",
            "d",
            "t",
            (Column("at", "DATETIME"), Column("v", "INT64")),
            partitioning=Partitioning("at", "DAY"),
        )
        rule, message, fix = only_finding("SELECT v FROM t", Catalog((table,), "p", "d"))
        assert rule == PARTITION_FILTER
        assert message.endswith("so it reads every partition.")
        assert fix == (
            "Add a filter on `at`, for example "
            "`at >= DATETIME_SUB(CURRENT_DATETIME(), INTERVAL 7 DAY)`."
        )


class TestCheckIntegration:
    def test_too_complex_is_unanalyzable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(*_args: object) -> None:
            raise TooComplexError("the query reads its CTEs in too many ways to analyze")

        monkeypatch.setattr(check_module, "extract", fail)
        result = check("SELECT user_id FROM users", CATALOG)
        (finding,) = result.findings
        assert (finding.rule, finding.severity) == (UNANALYZABLE, Severity.WARN)
        assert finding.message.startswith("Partition filters weren't checked:")

    def test_statement_that_reads_nothing(self) -> None:
        result = check("CREATE TABLE t2 (a INT64)", CATALOG, policy=Policy(read_only=False))
        assert result.verdict is Verdict.PASS


class TestHelpers:
    TABLE = Table(
        "p",
        "d",
        "t",
        (Column("day", "DATE"), Column("name", "STRING")),
        partitioning=Partitioning("day", "DAY"),
        clustering=("name",),
    )

    def predicate(self, sql: str, column: str = "day", wrapper: str | None = "CAST") -> Predicate:
        return Predicate(column, "=", ("'x'",), True, wrapper, "where", sql)

    def test_unparsable_condition_is_left_alone(self) -> None:
        broken = self.predicate("NOT ((( SQL")
        assert pruning._defeating(broken, self.TABLE) is None
        assert pruning._display(broken.sql) == broken.sql
        suffix = Predicate("_table_suffix", "other", (), False, None, "where", "NOT ((( SQL")
        assert not pruning._limits_shards(suffix)

    def test_unparsable_cluster_condition_is_skipped(self) -> None:
        facts = TableFacts(
            table=self.TABLE,
            alias="t",
            columns=frozenset({"name", "day"}),
            star=False,
            star_except=frozenset(),
            predicates=(
                self.predicate("day = '2026-09-01'", wrapper=None),
                self.predicate("NOT ((( SQL", column="name", wrapper="LOWER"),
            ),
            scans=1,
        )
        assert pruning.pruning_findings(QueryFacts((facts,), (), None, False)) == []

    def test_condition_without_a_comparison(self) -> None:
        assert pruning._wrapped(self.predicate("STARTS_WITH(LOWER(name), 'a')", "name")) is None
        assert pruning._functions(exp.column("day")) == []

    def test_pseudo_column_types(self) -> None:
        assert pruning._column_type(self.TABLE, "_partitiondate") == "DATE"
        assert pruning._column_type(self.TABLE, "_partitiontime") == "TIMESTAMP"

    def test_display_keeps_needed_quotes(self) -> None:
        assert pruning._display("`t`.`my col` = 1") == "`my col` = 1"

    @pytest.mark.parametrize(
        ("size", "shown"),
        [(512, " (512 B in all)"), (1_378_507_468_714_688, " (1.4 PB in all)")],
    )
    def test_size(self, size: int, shown: str) -> None:
        table = Table("p", "d", "t", (), size_bytes=size)
        assert pruning._size(table) == shown

    def test_rules_are_distinct(self) -> None:
        assert PARTITION_FILTER != PRUNING_DEFEATED
