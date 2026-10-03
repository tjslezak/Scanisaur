"""Edge cases for SCN003, SCN004 and SCN011; the golden cases cover the main behavior."""

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
from scanisaur.engine.rules import (
    CLUSTER_PREFIX,
    PARTITION_FILTER,
    PRUNING_DEFEATED,
    UNANALYZABLE,
)

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


class TestShardExclusions:
    def test_not_like_on_the_suffix_reads_nearly_every_shard(self) -> None:
        sql = (
            "SELECT COUNT(*) FROM ga4.`events_*` "
            "WHERE _TABLE_SUFFIX NOT LIKE '2019%' AND event_name = 'x'"
        )
        assert [rule for rule, _m, _f in findings(sql)] == [PARTITION_FILTER]


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

    def test_not_like_does_not_use_the_clustering(self) -> None:
        sql = (
            "SELECT COUNT(*) FROM web.downloads WHERE DATE(timestamp) = '2026-09-28' "
            "AND LOWER(project) = 'requests' AND project NOT LIKE 'req%'"
        )
        assert [rule for rule, _m, _f in findings(sql)] == [PRUNING_DEFEATED]

    def test_leading_wildcard_does_not_use_the_clustering(self) -> None:
        sql = (
            "SELECT COUNT(*) FROM web.downloads WHERE DATE(timestamp) = '2026-09-28' "
            "AND LOWER(project) = 'requests' AND project LIKE '%quests'"
        )
        assert [rule for rule, _m, _f in findings(sql)] == [PRUNING_DEFEATED]


class TestClusterPrefix:
    DAY = "DATE(p.datehour) = '2025-06-01'"
    WIKIS = "wikis AS (SELECT wiki FROM UNNEST(['en', 'de']) AS wiki)"
    TITLE_ONLY = f"SELECT SUM(views) FROM web.pageviews AS p WHERE {DAY} AND p.title = 'x'"

    def rules(self, sql: str) -> list[str]:
        return [rule for rule, _m, _f in findings(sql)]

    def test_join_in_where_limits_the_leading_column(self) -> None:
        sql = (
            f"WITH {self.WIKIS} SELECT SUM(p.views) FROM web.pageviews AS p, wikis AS w "
            f"WHERE p.wiki = w.wiki AND {self.DAY} AND p.title = 'x'"
        )
        assert self.rules(sql) == []

    def test_join_on_the_leading_column_through_a_cte(self) -> None:
        sql = (
            f"WITH {self.WIKIS}, day AS (SELECT wiki, title, views FROM web.pageviews AS p "
            f"WHERE {self.DAY}) SELECT SUM(d.views) FROM day AS d "
            "JOIN wikis AS w ON d.wiki = w.wiki WHERE d.title = 'x'"
        )
        assert self.rules(sql) == []

    def test_outer_join_on_the_leading_column(self) -> None:
        # A WHERE filter on w would make it an inner join, so it stays silent either way.
        sql = (
            f"WITH {self.WIKIS} SELECT SUM(p.views) FROM web.pageviews AS p "
            f"LEFT JOIN wikis AS w ON p.wiki = w.wiki WHERE {self.DAY} AND p.title = 'x' "
            "AND w.wiki = 'en'"
        )
        assert self.rules(sql) == []

    def test_correlated_exists_on_the_leading_column(self) -> None:
        sql = (
            f"WITH {self.WIKIS} SELECT SUM(p.views) FROM web.pageviews AS p "
            f"WHERE {self.DAY} AND p.title = 'x' "
            "AND EXISTS (SELECT 1 FROM wikis AS w WHERE w.wiki = p.wiki)"
        )
        assert self.rules(sql) == []

    @pytest.mark.parametrize(
        "where",
        [
            "p.wiki IN UNNEST(@wikis)",
            "STARTS_WITH(p.wiki, 'en')",
            "p.wiki = (SELECT MAX(region) FROM web.store_sales)",
            "p.wiki != p.title",
        ],
    )
    def test_leading_filters_that_may_pick_values(self, where: str) -> None:
        sql = f"{self.TITLE_ONLY} AND {where}"
        assert self.rules(sql) == []

    @pytest.mark.parametrize(
        "where",
        [
            "p.wiki NOT IN ('commons', 'meta')",
            "p.wiki NOT IN (SELECT region FROM web.store_sales)",
            "p.wiki <> 'commons'",
            "p.wiki NOT LIKE 'commons%'",
        ],
    )
    def test_leading_filters_that_only_exclude(self, where: str) -> None:
        sql = f"{self.TITLE_ONLY} AND {where}"
        assert self.rules(sql) == [CLUSTER_PREFIX]

    def test_leading_column_in_qualify(self) -> None:
        sql = (
            "SELECT wiki, views FROM web.pageviews AS p "
            f"WHERE {self.DAY} AND p.title = 'x' QUALIFY wiki = 'en'"
        )
        assert self.rules(sql) == []

    def test_leading_column_named_inside_an_unrelated_subquery(self) -> None:
        sql = (
            f"SELECT SUM(p.views) FROM web.pageviews AS p WHERE {self.DAY} AND p.title = 'x' "
            "AND p.views > (SELECT AVG(views) FROM web.pageviews "
            "WHERE wiki = 'en' AND DATE(datehour) = '2025-06-01')"
        )
        assert self.rules(sql) == [CLUSTER_PREFIX]

    def test_partition_column_as_a_later_cluster_column(self) -> None:
        table = Table(
            "p",
            "d",
            "ev",
            (Column("customer_id", "STRING"), Column("ts", "TIMESTAMP"), Column("n", "INT64")),
            partitioning=Partitioning("ts", "DAY"),
            clustering=("customer_id", "ts"),
        )
        catalog = Catalog((table,), "p", "d")
        sql = "SELECT SUM(n) FROM ev WHERE ts = TIMESTAMP '2026-09-28 10:00:00'"
        assert findings(sql, catalog) == []

    def test_range_on_a_later_column_is_not_reported(self) -> None:
        sql = self.TITLE_ONLY.replace("p.title = 'x'", "p.title >= 'x'")
        assert self.rules(sql) == []

    def test_struct_field_named_like_a_cluster_column(self) -> None:
        table = Table(
            "p",
            "d",
            "st",
            (
                Column("customer_id", "STRING"),
                Column("status", "STRING"),
                Column("meta", "STRUCT<status STRING, customer_id STRING>"),
            ),
            clustering=("customer_id", "status"),
        )
        catalog = Catalog((table,), "p", "d")
        assert findings("SELECT COUNT(*) FROM st AS t WHERE t.meta.status = 'x'", catalog) == []
        sql = "SELECT COUNT(*) FROM st AS t WHERE t.status = 'x' AND t.meta.customer_id = 'c'"
        assert [rule for rule, _m, _f in findings(sql, catalog)] == [CLUSTER_PREFIX]

    def test_correlated_exists_through_a_cte_with_a_reused_alias(self) -> None:
        sql = (
            "WITH d AS (SELECT p.wiki, p.title, p.views FROM web.pageviews AS p "
            f"WHERE {self.DAY} AND p.title = 'x') SELECT SUM(q.views) FROM d AS q "
            "WHERE EXISTS (SELECT 1 FROM web.store_sales AS p WHERE p.region = q.wiki)"
        )
        assert self.rules(sql) == []

    @pytest.mark.parametrize(
        "cte",
        [
            "ROW_NUMBER() OVER (PARTITION BY p.wiki ORDER BY p.views) AS rn",
            "SUM(p.views) OVER (PARTITION BY p.title) AS total",
        ],
    )
    def test_join_on_a_cte_with_a_window(self, cte: str) -> None:
        sql = (
            f"WITH d AS (SELECT p.wiki, p.title, p.views, {cte} FROM web.pageviews AS p "
            f"WHERE {self.DAY} AND p.title = 'x') "
            "SELECT * FROM d JOIN web.store_sales AS w ON d.wiki = w.region"
        )
        assert self.rules(sql) == []

    @pytest.mark.parametrize(
        "tail",
        [
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY p.title ORDER BY p.views DESC) <= 3",
            "LIMIT 100",
            "ORDER BY p.views DESC LIMIT 10",
        ],
    )
    def test_outer_filter_that_cannot_move_into_the_cte(self, tail: str) -> None:
        sql = (
            "WITH r AS (SELECT p.wiki, p.title, p.views FROM web.pageviews AS p "
            f"WHERE {self.DAY} AND p.title = 'x' {tail}) SELECT * FROM r WHERE wiki = 'en'"
        )
        assert self.rules(sql) == []

    def test_outer_filter_moves_below_a_window_aggregate(self) -> None:
        sql = (
            "WITH d AS (SELECT p.datehour, p.wiki, p.title, p.views, "
            "SUM(p.views) OVER (PARTITION BY p.wiki) AS total FROM web.pageviews AS p "
            "WHERE p.title = 'x') "
            "SELECT * FROM d WHERE d.wiki = 'en' AND DATE(d.datehour) = '2025-06-01'"
        )
        # The wiki filter moves below the window; the date filter can't.
        assert self.rules(sql) == [PARTITION_FILTER]
        sql = sql.replace("PARTITION BY p.wiki", "PARTITION BY p.wiki, DATE(p.datehour)")
        assert self.rules(sql) == [PARTITION_FILTER]  # only plain columns are matched
        sql = sql.replace(
            "PARTITION BY p.wiki, DATE(p.datehour)", "PARTITION BY p.wiki, p.datehour"
        )
        assert self.rules(sql) == []

    def test_correlated_exists_in_having(self) -> None:
        sql = (
            f"WITH {self.WIKIS} SELECT p.wiki, SUM(p.views) FROM web.pageviews AS p "
            f"WHERE {self.DAY} AND p.title = 'x' GROUP BY p.wiki "
            "HAVING EXISTS (SELECT 1 FROM wikis AS w WHERE w.wiki = p.wiki)"
        )
        assert self.rules(sql) == []

    @pytest.mark.parametrize(
        "title",
        [
            "(p.title = 'x' OR p.title IN ('y', 'z'))",
            "((p.title = 'x' AND p.views > 1) OR p.title = 'y')",
        ],
    )
    def test_later_column_pinned_through_or(self, title: str) -> None:
        sql = f"SELECT SUM(views) FROM web.pageviews AS p WHERE {self.DAY} AND {title}"
        assert self.rules(sql) == [CLUSTER_PREFIX]

    def test_qualify_on_a_cte_column(self) -> None:
        sql = (
            "WITH d AS (SELECT p.wiki, p.title, p.views FROM web.pageviews AS p "
            f"WHERE {self.DAY} AND p.title = 'x') SELECT * FROM d QUALIFY d.wiki = 'en'"
        )
        assert self.rules(sql) == []

    EVENTS = Table(
        "p",
        "d",
        "ev",
        (
            Column("event_date", "DATE"),
            Column("customer_id", "STRING"),
            Column("event_ts", "TIMESTAMP"),
            Column("active", "BOOL"),
            Column("n", "INT64"),
        ),
        partitioning=Partitioning("event_date", "DAY"),
        clustering=("customer_id", "event_ts"),
    )

    @pytest.mark.parametrize(
        "where",
        [
            "DATE(event_ts) = '2026-09-01'",
            "TIMESTAMP_TRUNC(event_ts, HOUR) = TIMESTAMP '2026-09-01 10:00:00'",
            "EXTRACT(HOUR FROM event_ts) = 3",
        ],
    )
    def test_function_of_a_later_column_is_a_range(self, where: str) -> None:
        catalog = Catalog((self.EVENTS,), "p", "d")
        sql = f"SELECT SUM(n) FROM ev WHERE event_date = '2026-09-01' AND {where}"
        assert findings(sql, catalog) == []

    @pytest.mark.parametrize("title", ["SUBSTR(p.title, 1, 1) = 'P'", "LENGTH(p.title) = 5"])
    def test_function_of_a_later_string_column(self, title: str) -> None:
        sql = f"SELECT SUM(views) FROM web.pageviews AS p WHERE {self.DAY} AND {title}"
        assert self.rules(sql) == []

    def test_partition_column_as_the_leading_cluster_column(self) -> None:
        table = Table(
            "p",
            "d",
            "ev",
            (Column("ts", "TIMESTAMP"), Column("customer_id", "STRING"), Column("n", "INT64")),
            partitioning=Partitioning("ts", "DAY"),
            clustering=("ts", "customer_id"),
        )
        catalog = Catalog((table,), "p", "d")
        sql = "SELECT SUM(n) FROM ev WHERE customer_id = 'c'"
        assert [rule for rule, _m, _f in findings(sql, catalog)] == [PARTITION_FILTER]

    def test_bool_leading_column_compared_with_not_equal(self) -> None:
        table = Table(
            "p",
            "d",
            "acc",
            (Column("active", "BOOL"), Column("user_id", "STRING")),
            clustering=("active", "user_id"),
        )
        catalog = Catalog((table,), "p", "d")
        assert findings("SELECT 1 FROM acc WHERE active != TRUE AND user_id = 'u'", catalog) == []
        sql = "SELECT 1 FROM acc WHERE user_id = 'u'"
        assert [rule for rule, _m, _f in findings(sql, catalog)] == [CLUSTER_PREFIX]

    def test_filter_on_an_outer_joined_side_that_keeps_nulls(self) -> None:
        sql = (
            "SELECT SUM(p.views) FROM web.store_sales AS s LEFT JOIN web.pageviews AS p "
            f"ON p.views = s.amount AND {self.DAY} AND p.title = 'x' "
            "WHERE (p.wiki = 'en' OR p.wiki IS NULL)"
        )
        assert self.rules(sql) == []

    @pytest.mark.parametrize(
        "tail",
        [
            "GROUP BY lw HAVING lw = 'en'",
            "GROUP BY ROLLUP(lw) HAVING lw = 'en'",
            "GROUP BY lw HAVING lw = 'en' OR SUM(p.views) > 10",
        ],
    )
    def test_having_on_the_leading_column(self, tail: str) -> None:
        sql = (
            "SELECT p.wiki AS lw, SUM(p.views) FROM web.pageviews AS p "
            f"WHERE {self.DAY} AND p.title = 'x' {tail}"
        )
        assert self.rules(sql) == []

    def test_intersect_on_the_leading_column(self) -> None:
        sql = (
            f"WITH {self.WIKIS} SELECT wiki, title FROM web.pageviews AS p "
            f"WHERE {self.DAY} AND title = 'x' INTERSECT DISTINCT SELECT wiki, 'x' FROM wikis"
        )
        assert self.rules(sql) == []

    def test_correlated_subquery_in_a_cte_column(self) -> None:
        sql = (
            f"WITH {self.WIKIS}, d AS (SELECT p.title, p.views, "
            "(SELECT COUNT(*) FROM wikis AS w WHERE w.wiki = p.wiki) AS known "
            f"FROM web.pageviews AS p WHERE {self.DAY} AND p.title = 'x') "
            "SELECT SUM(views) FROM d WHERE known > 0"
        )
        assert self.rules(sql) == []

    def test_double_negated_like(self) -> None:
        assert self.rules(f"{self.TITLE_ONLY} AND NOT (p.wiki NOT LIKE 'en%')") == []

    def test_filter_on_an_outer_joined_side_with_ifnull(self) -> None:
        sql = (
            "SELECT SUM(p.views) FROM web.store_sales AS s "
            "LEFT JOIN web.pageviews AS p ON p.views = s.amount "
            f"WHERE {self.DAY} AND p.title = 'x' AND p.wiki = IFNULL(@wiki, 'en')"
        )
        assert self.rules(sql) == []

    def test_outer_alias_matching_an_inner_alias(self) -> None:
        sql = (
            "WITH d AS (SELECT p.wiki, p.title, p.views FROM web.pageviews AS p "
            f"WHERE {self.DAY} AND p.title = 'x') SELECT * FROM d AS q "
            "JOIN (SELECT region AS wiki FROM web.store_sales) AS p ON q.title = p.wiki"
        )
        assert self.rules(sql) == [CLUSTER_PREFIX]

    def test_deep_self_joined_ctes_stay_analyzable(self) -> None:
        ctes = [
            "c0 AS (SELECT wiki, title, views FROM web.pageviews WHERE "
            "DATE(datehour) = '2025-06-01')"
        ]
        for i in range(1, 25):
            ctes.append(
                f"c{i} AS (SELECT a.wiki, a.title, a.views FROM c{i - 1} AS a "
                f"JOIN c{i - 1} AS b ON a.wiki = b.wiki)"
            )
        sql = f"WITH {', '.join(ctes)} SELECT COUNT(*) FROM c24 WHERE title = 'x'"
        assert self.rules(sql) == []

    def test_self_join_on_the_leading_column(self) -> None:
        # Each side could limit the other, so neither is reported.
        sql = (
            "SELECT a.views, b.views FROM web.pageviews AS a JOIN web.pageviews AS b "
            "ON a.wiki = b.wiki WHERE DATE(a.datehour) = '2025-06-01' "
            "AND DATE(b.datehour) = '2025-06-01' AND a.title = 'x' AND b.title = 'y'"
        )
        assert self.rules(sql) == []

    def test_leading_column_compared_with_a_scalar_subquery(self) -> None:
        sql = (
            f"SELECT SUM(views) FROM web.pageviews AS p WHERE {self.DAY} AND p.title = 'x' "
            "AND p.wiki = (SELECT MAX(region) FROM web.store_sales)"
        )
        assert self.rules(sql) == []

    def test_reported_once_for_the_branch_without_the_leading_column(self) -> None:
        with_wiki = f"SELECT views FROM web.pageviews AS p WHERE {self.DAY} AND wiki = 'en'"
        without = f"SELECT views FROM web.pageviews AS p WHERE {self.DAY}"
        sql = (
            f"{with_wiki} AND title = 'x' UNION ALL {without} AND title = 'x' "
            f"UNION ALL {without} AND title = 'y'"
        )
        (finding,) = check(sql, CATALOG).findings
        assert finding.rule == CLUSTER_PREFIX
        assert finding.column == sql.index("pageviews", len(with_wiki)) + 1

    def test_first_filtered_later_column_is_named(self) -> None:
        sql = (
            "SELECT licenses FROM web.packages WHERE DATE(snapshot_at) = '2026-09-28' "
            "AND version = '1.0.0' AND name = 'requests'"
        )
        (_rule, message, _fix) = only_finding(sql)
        assert "The query filters `name` but doesn't limit `system`" in message

    def test_filter_that_does_not_limit_the_later_column(self) -> None:
        sql = f"SELECT SUM(views) FROM web.pageviews AS p WHERE {self.DAY} AND title != 'x'"
        assert self.rules(sql) == []


class TestCheckIntegration:
    def raise_too_complex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(*_args: object) -> None:
            raise TooComplexError("the query reads its CTEs in too many ways to analyze")

        monkeypatch.setattr(check_module, "extract", fail)

    def test_too_complex_is_unanalyzable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.raise_too_complex(monkeypatch)
        (finding,) = check("SELECT user_id FROM events", CATALOG).findings
        assert (finding.rule, finding.severity) == (UNANALYZABLE, Severity.WARN)
        assert finding.message.startswith("Partition and cluster filters weren't checked:")

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
