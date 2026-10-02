"""Edge cases for SCN003 and SCN004; tests/golden/scn003 and scn004 cover the main behavior."""

from pathlib import Path

import pytest

from scanisaur.catalog import Catalog, Column, Partitioning, Table
from scanisaur.catalog.fixtures import load_catalog
from scanisaur.engine import check as check_module
from scanisaur.engine import pruning
from scanisaur.engine.check import Policy, check
from scanisaur.engine.facts import (
    Predicate,
    QueryFacts,
    TableFacts,
    TooComplexError,
    facts_from_sql,
)
from scanisaur.engine.result import Severity, Verdict
from scanisaur.engine.rules import PARTITION_FILTER, PRUNING_DEFEATED, UNANALYZABLE

CATALOG = load_catalog(Path(__file__).parents[1] / "golden" / "catalog.yaml")


def findings(sql: str, catalog: Catalog = CATALOG) -> list[tuple[str, str, str | None]]:
    return [(f.rule, f.message, f.fix) for f in check(sql, catalog).findings]


def only_finding(sql: str, catalog: Catalog = CATALOG) -> tuple[str, str, str]:
    (finding,) = check(sql, catalog).findings
    assert finding.fix is not None
    return finding.rule, finding.message, finding.fix


class TestWhatLimitsPartitions:
    """Each case was measured with a dry run (docs/rules/scn003.md)."""

    @pytest.mark.parametrize(
        "where",
        [
            # OR of ranges: BigQuery accepted and pruned this on a required-filter table.
            "order_date BETWEEN '2025-09-01' AND '2025-09-07' "
            "OR order_date BETWEEN '2026-09-01' AND '2026-09-07'",
            "(order_date >= '2026-06-01' AND order_date < '2026-06-03') "
            "OR order_date = '2026-07-01'",
            "order_date IN ('2026-09-29') OR order_date = '2026-09-30'",
            "(order_date = '2026-09-30' AND amount > 0) OR order_date = '2026-09-29'",
            # IS NULL reads only the NULL partition (0 bytes measured).
            "order_date IS NULL",
            "order_date >= '2026-09-01' OR order_date IS NULL",
            "'2026-09-01' <= order_date",
            "order_date IN UNNEST(['2026-09-01', '2026-09-02'])",
        ],
    )
    def test_limiting_filters(self, where: str) -> None:
        assert findings(f"SELECT order_id FROM Orders WHERE {where}") == []

    @pytest.mark.parametrize(
        "where",
        [
            "order_date = '2026-09-30' OR amount > 0",  # one branch reads everything
            "IF(amount > 0, order_date, NULL) >= '2026-09-01'",  # depends on amount too
            "order_date IS NOT NULL",
            "order_date BETWEEN '2026-09-01' AND DATE(amount)",
            "order_date IN UNNEST(GENERATE_DATE_ARRAY('2026-09-01', '2026-09-07'))",
        ],
    )
    def test_filters_that_do_not_limit(self, where: str) -> None:
        ((rule, message, _fix),) = findings(f"SELECT order_id FROM Orders WHERE {where}")
        assert rule == PARTITION_FILTER
        assert "can't limit which partitions are read" in message

    def test_filter_reading_another_column(self) -> None:
        rule, message, _fix = only_finding(
            "SELECT user_id FROM events WHERE DATE_DIFF(event_date, DATE(event_ts), DAY) = 0"
        )
        assert rule == PARTITION_FILTER
        assert "can't limit which partitions are read" in message

    def test_having_on_a_grouping_column(self) -> None:
        # A HAVING filter on a GROUP BY column pruned like WHERE (one partition measured).
        sql = (
            "SELECT order_date, COUNT(*) FROM Orders "
            "GROUP BY order_date HAVING order_date = '2026-09-01'"
        )
        assert findings(sql) == []

    def test_having_on_an_aggregate_does_not_count(self) -> None:
        sql = "SELECT order_date FROM Orders GROUP BY order_date HAVING COUNT(*) > 1"
        assert [rule for rule, _m, _f in findings(sql)] == [PARTITION_FILTER]

    def test_in_subquery(self) -> None:
        sql = "SELECT order_id FROM Orders WHERE order_date IN (SELECT signup_date FROM users)"
        ((_rule, message, _fix),) = findings(sql)
        assert "comparing `order_date` with a subquery" in message

    def test_qualify_runs_too_late(self) -> None:
        sql = (
            "SELECT user_id FROM events WHERE TRUE "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY event_ts) = 1 "
            "AND event_date = '2026-09-01'"
        )
        assert [rule for rule, _m, _f in findings(sql)] == [PARTITION_FILTER]

    def test_ingestion_time_streaming_buffer(self) -> None:
        assert findings("SELECT payload FROM raw.ingest WHERE _PARTITIONTIME IS NULL") == []

    def test_count_over_a_star_cte_reads_nothing(self) -> None:
        assert findings("WITH x AS (SELECT * FROM events) SELECT COUNT(*) FROM x") == []
        assert findings("SELECT COUNT(*) FROM (SELECT * FROM events)") == []


class TestShards:
    @pytest.mark.parametrize("where", ["_TABLE_SUFFIX IS NOT NULL", "_TABLE_SUFFIX != ''"])
    def test_filters_matching_every_shard(self, where: str) -> None:
        rule, message, _fix = only_finding(
            f"SELECT event_name FROM `proj.ga4.events_*` WHERE {where}"
        )
        assert rule == PARTITION_FILTER
        assert "can't limit which shards are read" in message

    def test_suffix_compared_with_a_column(self) -> None:
        _rule, message, _fix = only_finding(
            "SELECT event_name FROM `proj.ga4.events_*` WHERE _TABLE_SUFFIX = event_name"
        )
        assert "`_TABLE_SUFFIX = event_name` can't limit which shards are read" in message

    def test_reading_only_shard_names_reads_nothing(self) -> None:
        assert findings("SELECT DISTINCT _TABLE_SUFFIX FROM `proj.ga4.events_*`") == []


class TestDefeatedPruning:
    def test_wrapped_column_compared_with_like(self) -> None:
        rule, message, fix = only_finding(
            "SELECT user_id FROM events WHERE CAST(event_date AS STRING) LIKE '2026%'"
        )
        assert rule == PRUNING_DEFEATED
        assert "wraps the partition column `event_date` in CAST(... AS STRING)" in message
        assert fix.startswith("Compare `event_date` itself with a constant")

    def test_cast_fix_only_for_whole_dates(self) -> None:
        _rule, _message, fix = only_finding(
            "SELECT term FROM web.trends WHERE CAST(refresh_date AS STRING) >= '2026-09'"
        )
        assert fix.startswith("Compare `refresh_date` itself with a constant")

    def test_cast_on_a_range_partition_is_not_flagged(self) -> None:
        # Unmeasured on INT64 partitions, so assumed to prune: no finding either way.
        assert findings("SELECT score FROM web.scores WHERE CAST(bucket AS STRING) = '5'") == []

    def test_week_with_a_start_day(self) -> None:
        rule, message, _fix = only_finding(
            "SELECT term FROM web.trends WHERE EXTRACT(WEEK(MONDAY) FROM refresh_date) = 3"
        )
        assert rule == PRUNING_DEFEATED
        assert "EXTRACT(WEEK(MONDAY) FROM ...)" in message

    def test_ingestion_time_names_the_reported_pseudo_column(self) -> None:
        rule, message, fix = only_finding(
            "SELECT payload FROM raw.ingest "
            "WHERE _PARTITIONTIME IS NOT NULL AND CAST(_PARTITIONDATE AS STRING) = '2026-09-01'"
        )
        assert rule == PRUNING_DEFEATED
        assert "wraps the partition column `_PARTITIONDATE`" in message
        assert fix == "Compare `_PARTITIONDATE` itself: `_PARTITIONDATE = '2026-09-01'`."

    def test_cluster_fix_keeps_pruning_functions(self) -> None:
        _rule, _message, fix = only_finding(
            "SELECT COUNT(*) FROM web.downloads WHERE DATE(timestamp) = '2026-09-28' "
            "AND SUBSTR(LOWER(project), 1, 3) = 'req'"
        )
        assert "`SUBSTR(project, 1, 3) = 'req'`" in fix

    def test_cluster_column_under_like(self) -> None:
        rule, _message, _fix = only_finding(
            "SELECT COUNT(*) FROM web.downloads WHERE DATE(timestamp) = '2026-09-28' "
            "AND LOWER(project) LIKE 'req%'"
        )
        assert rule == PRUNING_DEFEATED

    def test_plain_like_uses_the_clustering(self) -> None:
        sql = (
            "SELECT COUNT(*) FROM web.downloads WHERE DATE(timestamp) = '2026-09-28' "
            "AND LOWER(project) = 'requests' AND project LIKE 'req%'"
        )
        assert findings(sql) == []

    def test_wrapped_cluster_filter_that_never_prunes(self) -> None:
        sql = (
            "SELECT COUNT(*) FROM web.downloads WHERE DATE(timestamp) = '2026-09-28' "
            "AND LOWER(project) != 'requests'"
        )
        assert findings(sql) == []  # != can't use clustering anyway, so LOWER costs nothing

    def test_leading_wildcard_does_not_use_the_clustering(self) -> None:
        sql = (
            "SELECT COUNT(*) FROM web.downloads WHERE DATE(timestamp) = '2026-09-28' "
            "AND LOWER(project) = 'requests' AND project LIKE '%quests'"
        )
        assert [rule for rule, _m, _f in findings(sql)] == [PRUNING_DEFEATED]


class TestCheckIntegration:
    def raise_too_complex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(*_args: object) -> None:
            raise TooComplexError("the query reads its CTEs in too many ways to analyze")

        monkeypatch.setattr(check_module, "extract", fail)

    def test_too_complex_is_unanalyzable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.raise_too_complex(monkeypatch)
        (finding,) = check("SELECT user_id FROM events", CATALOG).findings
        assert (finding.rule, finding.severity) == (UNANALYZABLE, Severity.WARN)
        assert finding.message.startswith("Partition filters weren't checked:")

    def test_too_complex_without_partitioned_tables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.raise_too_complex(monkeypatch)
        result = check("SELECT user_id FROM users", CATALOG, policy=Policy(fail_mode="closed"))
        assert result.verdict is Verdict.PASS

    def test_statement_that_reads_nothing(self) -> None:
        result = check("CREATE TABLE t2 (a INT64)", CATALOG, policy=Policy(read_only=False))
        assert result.verdict is Verdict.PASS

    def test_identical_union_branches_merge(self) -> None:
        branch = "SELECT user_id FROM events WHERE event_date = '2026-09-01'"
        (facts,) = facts_from_sql(f"{branch} UNION ALL {branch}", CATALOG).tables
        assert facts.scans == 2
        assert facts.position == (1, 21)  # the first branch's table


class TestMessages:
    TABLE = Table(
        "p",
        "d",
        "t",
        (Column("at", "DATETIME"), Column("v", "INT64"), Column("my col", "STRING")),
        partitioning=Partitioning("at", "DAY"),
    )

    def test_unknown_size_and_datetime_column(self) -> None:
        rule, message, fix = only_finding("SELECT v FROM t", Catalog((self.TABLE,), "p", "d"))
        assert rule == PARTITION_FILTER
        assert message.endswith("so it reads every partition.")
        assert fix == (
            "Add a filter on `at`, for example "
            "`at >= DATETIME_SUB(CURRENT_DATETIME(), INTERVAL 7 DAY)`."
        )

    def test_not_null_is_shown_only_for_the_whole_condition(self) -> None:
        _rule, message, _fix = only_finding(
            "SELECT user_id FROM events WHERE event_date IS NOT NULL OR user_id IS NULL"
        )
        assert "`NOT event_date IS NULL OR user_id IS NULL`" in message

    def test_render_keeps_needed_quotes(self) -> None:
        tree = pruning._parse("`t`.`my col` = 1")
        assert tree is not None
        assert pruning._render(tree) == "`my col` = 1"

    def test_unparsable_condition_is_skipped(self) -> None:
        table = Table(
            "p", "d", "t", (Column("day", "DATE"),), partitioning=Partitioning("day", "DAY")
        )
        broken = Predicate("day", "other", (), False, None, "where", "NOT ((( SQL")
        facts = TableFacts(table, "t", frozenset({"day"}), False, frozenset(), (broken,), 1)
        (finding,) = pruning.pruning_findings(QueryFacts((facts,), (), None, False))
        assert "doesn't filter on it" in finding.message

    def test_pseudo_column_types(self) -> None:
        assert pruning._column_type(self.TABLE, "_partitiondate") == "DATE"
        assert pruning._column_type(self.TABLE, "_partitiontime") == "TIMESTAMP"

    @pytest.mark.parametrize(
        ("size", "shown"),
        [(512, " (512 B in all)"), (1_378_507_468_714_688, " (1.4 PB in all)")],
    )
    def test_size(self, size: int, shown: str) -> None:
        assert pruning._size(Table("p", "d", "t", (), size_bytes=size)) == shown
