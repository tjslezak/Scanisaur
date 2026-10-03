"""The static cost estimate (issue #15) and how it reads partition filters."""

from datetime import UTC, datetime

import pytest
import sqlglot

from scanisaur.catalog import Catalog, Column, Partition, Partitioning, Table
from scanisaur.engine import estimate as estimate_module
from scanisaur.engine.check import Policy, check
from scanisaur.engine.estimate import MIN_BILLED_BYTES
from scanisaur.engine.result import Estimate

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
#: For tables whose newest partition is 30 September.
SEPT_30 = datetime(2026, 9, 30, 12, tzinfo=UTC)
#: Binary units, so every expected size is a whole MiB, as BigQuery bills.
GB = 2**30
#: The three 8-byte columns are 1/16 of the bytes each, `term` the other 13/16.
TRENDS = Table(
    "p",
    "d",
    "trends",
    (
        Column("refresh_date", "DATE"),
        Column("term", "STRING"),
        Column("score", "INT64"),
        Column("rank", "INT64"),
    ),
    row_count=30 * GB // 128,
    size_bytes=30 * GB,
    partitioning=Partitioning("refresh_date", "DAY"),
    partitions=(
        Partition("20260929", 10 * GB),
        Partition("20260930", 10 * GB),
        Partition("20261001", 10 * GB),
    ),
)
#: One partition's `score` (or `refresh_date`, or `rank`) column.
COLUMN = 10 * GB // 16


def estimate_of(
    sql: str, *tables: Table, policy: Policy | None = None, now: datetime = NOW
) -> Estimate:
    catalog = Catalog(tables or (TRENDS,), "p", "d")
    result = check(sql, catalog, now=now, policy=policy or Policy())
    assert result.estimate is not None, result
    return result.estimate


def billed(sql: str, *tables: Table, now: datetime = NOW) -> tuple[int, int]:
    estimate = estimate_of(sql, *tables, now=now)
    return estimate.bytes_low, estimate.bytes_high


ONE_DAY = "refresh_date = '2026-09-30'"


class TestBilledOncePerQuery:
    """Measured on Trends: each column of each partition is billed once per query."""

    def test_cte_read_twice_costs_one_read(self) -> None:
        once = billed(f"SELECT SUM(score) FROM trends WHERE {ONE_DAY}")
        twice = billed(
            f"WITH t AS (SELECT refresh_date, score FROM trends WHERE {ONE_DAY}) "
            "SELECT a.score, b.score FROM t AS a JOIN t AS b ON a.refresh_date = b.refresh_date"
        )
        assert twice == once == (2 * COLUMN, 2 * COLUMN)  # score and refresh_date

    def test_self_join_on_the_same_partition_costs_one_read(self) -> None:
        sql = (
            "SELECT a.score - b.score FROM trends AS a JOIN trends AS b ON a.rank = b.rank "
            f"WHERE a.{ONE_DAY} AND b.{ONE_DAY}"
        )
        assert billed(sql) == (3 * COLUMN, 3 * COLUMN)  # score, rank and refresh_date

    def test_reads_of_different_partitions_add_up(self) -> None:
        sql = (
            "SELECT a.score - b.score FROM trends AS a JOIN trends AS b ON a.rank = b.rank "
            "WHERE a.refresh_date = '2026-09-29' AND b.refresh_date = '2026-09-30'"
        )
        assert billed(sql) == (6 * COLUMN, 6 * COLUMN)

    def test_columns_read_in_the_same_partition_are_combined(self) -> None:
        sql = (
            f"SELECT score FROM trends WHERE {ONE_DAY} "
            f"UNION ALL SELECT rank FROM trends WHERE {ONE_DAY}"
        )
        assert billed(sql) == (3 * COLUMN, 3 * COLUMN)

    def test_union_branches_over_overlapping_partitions(self) -> None:
        sql = (
            "SELECT score FROM trends WHERE refresh_date >= '2026-09-30' "
            "UNION ALL SELECT score FROM trends WHERE refresh_date <= '2026-09-30'"
        )
        assert billed(sql) == (6 * COLUMN, 6 * COLUMN)  # 3 partitions, 2 columns


#: `n` holds 8 GiB. The four leaf fields of `device` and `params` split the other 16 GiB.
NESTED = Table(
    "p",
    "d",
    "nested",
    (
        Column("n", "INT64"),
        Column("device", "STRUCT<category STRING, os STRING>"),
        Column("params", "ARRAY<STRUCT<key STRING, value STRING>>"),
    ),
    row_count=GB,
    size_bytes=24 * GB,
)
LEAF = 4 * GB


class TestStructFields:
    """Measured on GA4 (#22): BigQuery bills the struct fields a query reads, one by one."""

    @pytest.mark.parametrize(
        ("sql", "leaves"),
        [
            ("SELECT device FROM nested", 2),  # a struct of two fields counts two
            ("SELECT device.category FROM nested", 1),
            ("SELECT d.device.category FROM nested AS d WHERE d.device.os = 'x'", 2),
            ("SELECT device FROM nested WHERE device.os = 'x'", 2),
            ("SELECT p.key FROM nested, UNNEST(params) AS p", 1),
            ("SELECT p.key FROM nested, UNNEST(params) AS p WITH OFFSET AS i WHERE i = 0", 1),
            ("SELECT (SELECT value FROM UNNEST(params) WHERE key = 'a') FROM nested", 2),
            ("SELECT ARRAY_LENGTH(params) FROM nested", 2),  # a function of the whole array
            ("SELECT TO_JSON_STRING(p) FROM nested, UNNEST(params) AS p", 2),
            ("WITH b AS (SELECT * FROM nested) SELECT device.category FROM b", 1),
            ("WITH b AS (SELECT DISTINCT device FROM nested) SELECT device.os FROM b", 2),
            (
                "SELECT device.category FROM nested UNION ALL SELECT device.os FROM nested",
                2,  # each field once, however many references read it
            ),
        ],
    )
    def test_fields_read(self, sql: str, leaves: int) -> None:
        estimate = estimate_of(sql, NESTED)
        assert (estimate.bytes_low, estimate.bytes_high) == (leaves * LEAF, leaves * LEAF)
        assert estimate.confidence == "medium"

    def test_nested_struct_fields(self) -> None:
        deep = Table(
            "p",
            "d",
            "deep",
            (Column("s", "STRUCT<a STRING, b STRUCT<c STRING, d STRING, e STRING>>"),),
            row_count=GB,
            size_bytes=8 * GB,
        )
        assert billed("SELECT s.b.c FROM deep", deep) == (2 * GB, 2 * GB)
        assert billed("SELECT s.b FROM deep", deep) == (6 * GB, 6 * GB)
        assert billed("SELECT s.a, s.b.e FROM deep", deep) == (4 * GB, 4 * GB)

    def test_fixed_width_fields_of_a_mixed_struct(self) -> None:
        # `id` and `s.n` hold 8 GiB each; `s.name` is the rest of the table.
        mixed = Table(
            "p",
            "d",
            "mixed",
            (Column("id", "INT64"), Column("s", "STRUCT<n INT64, name STRING>")),
            row_count=GB,
            size_bytes=116 * GB,
        )
        assert estimate_of("SELECT s.n FROM mixed", mixed).bytes_high == 8 * GB
        assert estimate_of("SELECT s.n FROM mixed", mixed).confidence == "high"
        assert estimate_of("SELECT s.name FROM mixed", mixed).bytes_high == 100 * GB
        assert estimate_of("SELECT s FROM mixed", mixed).bytes_high == 108 * GB

    def test_select_star_reads_every_field(self) -> None:
        assert billed("SELECT * FROM nested", NESTED) == (24 * GB, 24 * GB)

    def test_unknown_field_reads_the_whole_column(self) -> None:
        json = Table(
            "p",
            "d",
            "j",
            (Column("doc", "JSON"), Column("s", "STRING")),
            row_count=GB,
            size_bytes=8 * GB,
        )
        # A path into a JSON value reads the column: it is one leaf.
        assert billed("SELECT doc.a.b FROM j", json) == (4 * GB, 4 * GB)


class TestColumns:
    def test_variable_width_columns_split_the_rest(self) -> None:
        estimate = estimate_of(f"SELECT term FROM trends WHERE {ONE_DAY}")
        term = 13 * 10 * GB // 16
        assert (estimate.bytes_low, estimate.bytes_high) == (term + COLUMN, term + COLUMN)
        assert estimate.confidence == "medium"

    def test_fixed_width_columns_are_exact(self) -> None:
        assert estimate_of(f"SELECT score FROM trends WHERE {ONE_DAY}").confidence == "high"

    def test_struct_of_fixed_fields_is_fixed_width(self) -> None:
        table = Table(
            "p",
            "d",
            "t",
            (Column("pos", "STRUCT<x FLOAT64, y FLOAT64>"), Column("note", "STRING")),
            row_count=GB,
            size_bytes=20 * GB,
        )
        estimate = estimate_of("SELECT pos FROM t", table)
        assert (estimate.bytes_high, estimate.confidence) == (16 * GB, "high")

    def test_unknown_row_count_splits_evenly(self) -> None:
        table = Table("p", "d", "t", (Column("a", "INT64"), Column("b", "STRING")), size_bytes=GB)
        estimate = estimate_of("SELECT a FROM t", table)
        assert (estimate.bytes_high, estimate.confidence) == (GB // 2, "low")

    def test_fixed_columns_larger_than_the_table(self) -> None:
        # NULLs take no space, so 8 bytes a row overstates `a`, and `b`'s size is unknown.
        table = Table(
            "p",
            "d",
            "t",
            (Column("a", "INT64"), Column("b", "STRING")),
            row_count=GB,
            size_bytes=4 * GB,
        )
        for column in ("a", "b"):
            estimate = estimate_of(f"SELECT {column} FROM t", table)
            assert (estimate.bytes_high, estimate.confidence) == (2 * GB, "low")

    def test_only_fixed_columns(self) -> None:
        table = Table(
            "p",
            "d",
            "t",
            (Column("a", "INT64"), Column("b", "FLOAT64")),
            row_count=GB,
            size_bytes=20 * GB,  # more than the columns hold: their widths win
        )
        assert estimate_of("SELECT a FROM t", table).bytes_high == 8 * GB
        nulls = Table("p", "d", "n", table.columns, row_count=GB, size_bytes=8 * GB)
        estimate = estimate_of("SELECT a FROM n", nulls)
        assert (estimate.bytes_high, estimate.confidence) == (4 * GB, "medium")

    def test_integer_aliases_are_fixed_width(self) -> None:
        table = Table(
            "p",
            "d",
            "t",
            (Column("a", "INTEGER"), Column("b", "STRING")),
            row_count=GB,
            size_bytes=20 * GB,
        )
        estimate = estimate_of("SELECT a FROM t", table)
        assert (estimate.bytes_high, estimate.confidence) == (8 * GB, "high")

    def test_minimum_per_table(self) -> None:
        small = Table("p", "d", "s", (Column("a", "INT64"),), row_count=10, size_bytes=80)
        assert billed("SELECT a FROM s", small) == (MIN_BILLED_BYTES, MIN_BILLED_BYTES)

    def test_count_star_reads_nothing(self) -> None:
        assert billed("SELECT COUNT(*) FROM trends") == (0, 0)

    def test_joined_table_that_reads_no_columns_bills_the_minimum(self) -> None:
        # Measured in #26: a cross join's COUNT(*) that read only `orders.status` billed
        # 10 MiB for `orders`, and 10 MiB for `users`, of which it read nothing.
        users = Table("p", "d", "users", (Column("id", "INT64"),), row_count=10, size_bytes=80)
        sql = f"SELECT COUNT(*) FROM users AS u, trends AS t WHERE t.{ONE_DAY}"
        assert billed(sql, TRENDS, users) == (
            COLUMN + MIN_BILLED_BYTES,
            COLUMN + MIN_BILLED_BYTES,
        )

    def test_tables_that_read_nothing_stay_free_together(self) -> None:
        users = Table("p", "d", "users", (Column("id", "INT64"),), row_count=10, size_bytes=80)
        assert billed("SELECT COUNT(*) FROM users, trends", TRENDS, users) == (0, 0)

    def test_joined_table_pruned_to_nothing_stays_free(self) -> None:
        users = Table("p", "d", "users", (Column("id", "INT64"),), row_count=10, size_bytes=80)
        sql = (
            "SELECT COUNT(*) FROM users AS u, trends AS t "
            "WHERE u.id > 0 AND t.refresh_date = '2000-01-01'"
        )
        assert billed(sql, TRENDS, users) == (MIN_BILLED_BYTES, MIN_BILLED_BYTES)

    def test_unknown_size_gives_no_estimate(self) -> None:
        table = Table("p", "d", "t", (Column("a", "INT64"),))
        assert check("SELECT a FROM t", Catalog((table,), "p", "d")).estimate is None

    def test_limit_zero_reads_nothing(self) -> None:
        # Measured: `SELECT * FROM top_terms LIMIT 0` processes 0 bytes.
        assert billed("SELECT * FROM trends LIMIT 0") == (0, 0)

    def test_no_tables(self) -> None:
        assert billed("SELECT 1") == (0, 0)


class TestPartitionFilters:
    @pytest.mark.parametrize(
        ("where", "partitions"),
        [
            ("refresh_date = '2026-09-30'", 1),
            ("refresh_date = DATE '2026-09-30'", 1),
            ("refresh_date >= '2026-09-30'", 2),
            ("refresh_date > '2026-09-30'", 1),
            ("refresh_date < '2026-09-30'", 1),
            ("refresh_date BETWEEN '2026-09-29' AND '2026-09-30'", 2),
            ("refresh_date IN ('2026-09-29', '2026-10-01')", 2),
            ("refresh_date = '2026-09-29' OR refresh_date = '2026-10-01'", 2),
            ("refresh_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)", 2),
            ("refresh_date >= CURRENT_DATE() - INTERVAL 1 DAY", 2),
            ("refresh_date = CURRENT_DATE()", 1),
            ("refresh_date >= DATE_TRUNC(CURRENT_DATE(), MONTH)", 1),
            ("refresh_date = DATE(2026, 9, 29)", 1),
            ("DATE_TRUNC(refresh_date, MONTH) = '2026-09-01'", 2),
            ("DATE_ADD(refresh_date, INTERVAL 1 DAY) = '2026-10-01'", 1),
            ("DATE_SUB(refresh_date, INTERVAL 1 MONTH) >= '2026-08-30'", 2),
            ("refresh_date = '2025-01-01'", 0),
            ("(refresh_date = '2026-09-29' AND rank = 1) OR refresh_date = '2026-09-30'", 2),
        ],
    )
    def test_evaluated_filters(self, where: str, partitions: int) -> None:
        estimate = estimate_of(f"SELECT score FROM trends WHERE {where}")
        expected = 0 if partitions == 0 else max(2 * COLUMN * partitions, MIN_BILLED_BYTES)
        if "rank" in where:
            expected = 3 * COLUMN * partitions
        assert (estimate.bytes_low, estimate.bytes_high) == (expected, expected)
        assert estimate.confidence == "high"

    @pytest.mark.parametrize(
        "where",
        [
            "CAST(refresh_date AS STRING) = '2026-09-30'",  # measured not to prune
            "EXTRACT(MONTH FROM refresh_date) = 9",
            "refresh_date != '2026-09-30'",
        ],
    )
    def test_filters_that_do_not_prune_read_everything(self, where: str) -> None:
        assert billed(f"SELECT score FROM trends WHERE {where}") == (6 * COLUMN, 6 * COLUMN)

    def test_equality_with_a_parameter_reads_one_partition(self) -> None:
        estimate = estimate_of("SELECT score FROM trends WHERE refresh_date = @day")
        assert (estimate.bytes_low, estimate.bytes_high) == (MIN_BILLED_BYTES, 2 * COLUMN)
        assert estimate.confidence == "medium"

    def test_range_with_a_parameter_is_unknown(self) -> None:
        estimate = estimate_of("SELECT score FROM trends WHERE refresh_date >= @since")
        assert (estimate.bytes_low, estimate.bytes_high) == (MIN_BILLED_BYTES, 6 * COLUMN)
        assert estimate.confidence == "low"

    def test_without_a_partition_list(self) -> None:
        table = Table(
            "p",
            "d",
            "t",
            (Column("day", "DATE"), Column("n", "INT64")),
            row_count=GB,
            size_bytes=16 * GB,
            partitioning=Partitioning("day", "DAY"),
        )
        estimate = estimate_of("SELECT n FROM t WHERE day = '2026-09-30'", table)
        assert (estimate.bytes_low, estimate.bytes_high) == (MIN_BILLED_BYTES, 16 * GB)
        assert estimate.confidence == "low"
        assert estimate_of("SELECT n FROM t", table).confidence == "high"

    def test_null_partition(self) -> None:
        table = Table(
            "p",
            "d",
            "t",
            (Column("day", "DATE"), Column("n", "INT64")),
            row_count=2 * GB,
            size_bytes=32 * GB,
            partitioning=Partitioning("day", "DAY"),
            partitions=(Partition("__NULL__", 16 * GB), Partition("20260930", 16 * GB)),
        )
        assert billed("SELECT n FROM t WHERE day IS NULL", table) == (16 * GB, 16 * GB)
        assert billed("SELECT n FROM t WHERE day = '2026-09-30'", table) == (16 * GB, 16 * GB)


class TestTimestampPartitions:
    EVENTS = Table(
        "p",
        "d",
        "events",
        (Column("ts", "TIMESTAMP"), Column("n", "INT64")),
        row_count=3 * GB,
        size_bytes=48 * GB,
        partitioning=Partitioning("ts", "DAY"),
        partitions=tuple(Partition(f"202609{day}", 16 * GB) for day in ("28", "29", "30")),
    )

    @pytest.mark.parametrize(
        ("where", "partitions"),
        [
            ("DATE(ts) = '2026-09-29'", 1),
            ("ts >= '2026-09-29'", 2),
            ("ts >= TIMESTAMP '2026-09-29 12:00:00+00'", 2),
            ("ts < TIMESTAMP '2026-09-29 00:00:00 UTC'", 1),
            ("ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY)", 3),  # from noon
            ("TIMESTAMP_TRUNC(ts, DAY) = '2026-09-29'", 1),
            ("CAST(ts AS DATE) BETWEEN '2026-09-28' AND '2026-09-29'", 2),
            ("EXTRACT(DATE FROM ts) = '2026-09-30'", 1),
        ],
    )
    def test_daily_partitions(self, where: str, partitions: int) -> None:
        sql = f"SELECT n FROM events WHERE {where}"
        assert billed(sql, self.EVENTS, now=SEPT_30)[1] == partitions * 16 * GB

    def test_equality_on_date_of_a_parameter(self) -> None:
        estimate = estimate_of(
            "SELECT n FROM events WHERE DATE(ts) = @day", self.EVENTS, now=SEPT_30
        )
        assert (estimate.bytes_high, estimate.confidence) == (16 * GB, "medium")

    def test_time_zone_is_not_evaluated(self) -> None:
        sql = "SELECT n FROM events WHERE DATE(ts, 'America/New_York') = '2026-09-29'"
        assert estimate_of(sql, self.EVENTS, now=SEPT_30).confidence == "low"

    def test_hourly_partitions(self) -> None:
        table = Table(
            "p",
            "d",
            "hits",
            (Column("ts", "TIMESTAMP"), Column("n", "INT64")),
            row_count=24 * GB,
            size_bytes=384 * GB,
            partitioning=Partitioning("ts", "HOUR"),
            partitions=tuple(Partition(f"20260930{h:02}", 16 * GB) for h in range(24)),
        )
        assert (
            billed("SELECT n FROM hits WHERE ts >= '2026-09-30 22:00:00'", table, now=SEPT_30)[1]
            == 32 * GB
        )
        assert (
            billed("SELECT n FROM hits WHERE DATE(ts) = '2026-09-30'", table, now=SEPT_30)[1]
            == 384 * GB
        )
        # DATE(ts) = @day covers 24 hourly partitions, so it doesn't count as one.
        assert (
            estimate_of("SELECT n FROM hits WHERE DATE(ts) = @day", table, now=SEPT_30).confidence
            == "low"
        )

    def test_monthly_partitions(self) -> None:
        table = Table(
            "p",
            "d",
            "sales",
            (Column("day", "DATE"), Column("n", "INT64")),
            row_count=2 * GB,
            size_bytes=32 * GB,
            partitioning=Partitioning("day", "MONTH"),
            partitions=(Partition("202608", 16 * GB), Partition("202609", 16 * GB)),
        )
        assert (
            billed("SELECT n FROM sales WHERE day > '2026-08-31'", table, now=SEPT_30)[1] == 16 * GB
        )
        assert (
            billed("SELECT n FROM sales WHERE day >= '2026-08-31'", table, now=SEPT_30)[1]
            == 32 * GB
        )

    def test_ingestion_time_holds_the_partition_start(self) -> None:
        table = Table(
            "p",
            "d",
            "raw",
            (Column("n", "INT64"),),
            row_count=2 * GB,
            size_bytes=16 * GB,
            partitioning=Partitioning(None, "DAY"),
            partitions=(
                Partition("20260929", 8 * GB),
                Partition("20260930", 8 * GB),
                Partition("__UNPARTITIONED__", 1 * GB),
            ),
        )
        # Rows of the 29th all have _PARTITIONTIME = midnight, before noon.
        sql = "SELECT n FROM raw WHERE _PARTITIONTIME >= TIMESTAMP '2026-09-29 12:00:00'"
        assert billed(sql, table, now=SEPT_30)[1] == 8 * GB
        assert (
            billed("SELECT n FROM raw WHERE _PARTITIONDATE = '2026-09-29'", table, now=SEPT_30)[1]
            == 8 * GB
        )
        # The streaming buffer has a NULL _PARTITIONTIME.
        assert billed("SELECT n FROM raw WHERE _PARTITIONTIME IS NULL", table, now=SEPT_30)[1] == GB


class TestShards:
    GA4 = Table(
        "p",
        "d",
        "events_*",
        (Column("event_name", "STRING"), Column("n", "INT64")),
        row_count=4 * GB,
        size_bytes=64 * GB,
        partitions=(
            *(Partition(suffix, 16 * GB) for suffix in ("20260928", "20260929", "20260930")),
            Partition("intraday_20261001", 16 * GB),
        ),
    )

    @pytest.mark.parametrize(
        ("where", "shards"),
        [
            ("_TABLE_SUFFIX = '20260929'", 1),
            ("_TABLE_SUFFIX BETWEEN '20260929' AND '20260930'", 2),
            ("_TABLE_SUFFIX LIKE '2026092%'", 2),
            ("_TABLE_SUFFIX IN ('20260928', '20260930')", 2),
            ("_TABLE_SUFFIX >= FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY))", 2),
        ],
    )
    def test_suffix_filters(self, where: str, shards: int) -> None:
        sql = f"SELECT n FROM `p.d.events_*` WHERE {where}"
        assert billed(sql, self.GA4)[1] == shards * 8 * GB

    def test_narrower_wildcard(self) -> None:
        assert billed("SELECT n FROM `p.d.events_2026093*`", self.GA4)[1] == 8 * GB

    def test_wrapped_suffix_is_not_evaluated(self) -> None:
        sql = (
            "SELECT n FROM `p.d.events_*` WHERE PARSE_DATE('%Y%m%d', _TABLE_SUFFIX) = '2026-09-29'"
        )
        assert estimate_of(sql, self.GA4).confidence == "low"


class TestClusteringAndPrice:
    def test_cluster_filter_gives_an_upper_bound(self) -> None:
        table = Table(
            "p",
            "d",
            "c",
            (Column("k", "INT64"), Column("n", "INT64")),
            row_count=2 * GB,
            size_bytes=32 * GB,
            clustering=("k",),
        )
        estimate = estimate_of("SELECT n FROM c WHERE k = 7", table)
        assert (estimate.bytes_low, estimate.bytes_high) == (MIN_BILLED_BYTES, 32 * GB)
        assert estimate.confidence == "medium"

    def test_dollars_at_the_on_demand_price(self) -> None:
        estimate = estimate_of("SELECT score FROM trends")
        assert estimate.usd_high == round(3 * COLUMN / 2**40 * 6.25, 4)

    def test_capacity_pricing_has_no_dollars(self) -> None:
        estimate = estimate_of("SELECT score FROM trends", policy=Policy(price_per_tib=None))
        assert (estimate.usd_low, estimate.usd_high) == (None, None)

    def test_confidence_is_the_lowest_of_the_tables(self) -> None:
        other = Table(
            "p",
            "d",
            "t",
            (Column("day", "DATE"), Column("n", "INT64")),
            row_count=10,
            size_bytes=160,
            partitioning=Partitioning("day", "DAY"),
        )
        sql = (
            f"SELECT score FROM trends WHERE {ONE_DAY} "
            "UNION ALL SELECT n FROM t WHERE day = '2026-09-30'"
        )
        assert estimate_of(sql, TRENDS, other).confidence == "low"


class TestConstants:
    """The constant evaluator, on the shapes partition filters compare with."""

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            ("TIMESTAMP '2026-09-30 10:00:00+05:30'", datetime(2026, 9, 30, 4, 30)),
            ("'2026-09-30T10:00:00Z'", datetime(2026, 9, 30, 10)),
            ("TIMESTAMP('2026-09-30')", datetime(2026, 9, 30)),
            ("DATE(TIMESTAMP '2026-09-30 10:00:00')", datetime(2026, 9, 30)),
            ("DATE_TRUNC(DATE '2026-08-15', QUARTER)", datetime(2026, 7, 1)),
            ("DATE_TRUNC(DATE '2026-08-15', YEAR)", datetime(2026, 1, 1)),
            ("TIMESTAMP_TRUNC(TIMESTAMP '2026-09-30 10:30:00', HOUR)", datetime(2026, 9, 30, 10)),
            ("DATE_TRUNC(DATE '2026-10-01', WEEK)", datetime(2026, 9, 27)),
            ("DATE_TRUNC(DATE '2026-10-01', ISOWEEK)", datetime(2026, 9, 28)),
            ("DATE_TRUNC(DATE '2026-10-01', WEEK(WEDNESDAY))", datetime(2026, 9, 30)),
            ("DATE_ADD(DATE '2026-01-31', INTERVAL 1 MONTH)", datetime(2026, 2, 28)),
            ("DATE_SUB(DATE '2026-10-01', INTERVAL -1 DAY)", datetime(2026, 10, 2)),
            (
                "TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 90 MINUTE)",
                datetime(2026, 10, 1, 13, 30),
            ),
            ("CURRENT_DATETIME() - INTERVAL 1 YEAR", datetime(2025, 10, 1, 12)),
            ("DATE(2026, 2, 30)", None),
            ("DATE_TRUNC(DATE '2026-10-01', DAYOFWEEK)", None),
            ("'not a date'", None),
            ("@day", None),
            ("CURRENT_DATE('America/New_York')", None),
            ("DATE('2026-09-30 23:00:00', 'Asia/Tokyo')", None),
            ("CAST('soon' AS DATE)", None),
            ("DATE_ADD(@day, INTERVAL 1 DAY)", None),
            ("CURRENT_DATE() - INTERVAL @n DAY", None),
        ],
    )
    def test_time_values(self, sql: str, expected: datetime | None) -> None:
        tree = sqlglot.parse_one(sql, dialect="bigquery")
        assert estimate_module._time_value(tree, NOW.replace(tzinfo=None)) == expected

    def test_total_does_not_depend_on_order(self) -> None:
        # Added left to right, 0.1 + 0.2 + 0.7 is 1.0000000000000002, which would round
        # 60 MiB up to 61. fsum gives 1.0 in any order.
        leaves: dict[tuple[str, ...], float] = {("a",): 0.1, ("b",): 0.2, ("c",): 0.7}
        scanned = estimate_module._scanned({"u": set(leaves)}, {"u": 60 * 2**20}, leaves)
        assert estimate_module.billed_bytes(scanned) == 60 * 2**20

    @pytest.mark.parametrize(
        ("type_", "fields"),
        [
            ("NOT A TYPE", (((), None),)),
            ("INT64", (((), 8),)),
            ("STRING", (((), None),)),
            (
                "STRUCT<a INT64, b STRUCT<c BOOL, d STRING>>",
                ((("a",), 8), (("b", "c"), 1), (("b", "d"), None)),
            ),
            # An array holds any number of elements, so every field has a variable width.
            ("ARRAY<STRUCT<k STRING, v INT64>>", ((("k",), None), (("v",), None))),
            ("ARRAY<INT64>", (((), None),)),
            # A field without a name can't be read alone: the struct is one variable field.
            ("STRUCT<a INT64, STRING>", (((), None),)),
        ],
    )
    def test_fields_of_a_type(
        self, type_: str, fields: tuple[tuple[tuple[str, ...], int | None], ...]
    ) -> None:
        assert estimate_module._fields(type_) == fields

    @pytest.mark.parametrize(
        ("where", "high"),
        [
            # Unknown branches keep the partition; known ones still decide.
            ("refresh_date = '2026-09-30' OR refresh_date = @day", 6 * COLUMN),
            ("refresh_date BETWEEN @start AND '2026-09-29'", 2 * COLUMN),
            ("refresh_date IN UNNEST(@days)", 6 * COLUMN),
            ("'2026-09-30' = refresh_date", 2 * COLUMN),
            ("TIMESTAMP(refresh_date) >= TIMESTAMP '2026-09-30 00:00:00'", 4 * COLUMN),
            ("DATE_TRUNC(refresh_date, ISOWEEK) = '2026-09-28'", 6 * COLUMN),
        ],
    )
    def test_partially_known_filters(self, where: str, high: int) -> None:
        assert billed(f"SELECT score FROM trends WHERE {where}")[1] == high


class TestReviewCases:
    """Cases from the code review of the first version."""

    def test_narrower_wildcard_suffix_is_what_follows_the_name(self) -> None:
        ga4 = TestShards.GA4
        one = "SELECT n FROM `p.d.events_2026*` WHERE _TABLE_SUFFIX = '0929'"
        assert billed(one, ga4) == (8 * GB, 8 * GB)
        two = "SELECT n FROM `p.d.events_2026*` WHERE _TABLE_SUFFIX >= '0929'"
        assert billed(two, ga4)[1] == 16 * GB

    @pytest.mark.parametrize(
        ("where", "shards"),
        [
            ("_TABLE_SUFFIX NOT LIKE 'intraday%'", 3),
            ("_TABLE_SUFFIX != '20260928'", 3),
            ("_TABLE_SUFFIX NOT IN ('20260928', '20260929')", 2),
            ("_TABLE_SUFFIX LIKE 'intraday\\\\_%'", 1),
            ("NOT (_TABLE_SUFFIX NOT LIKE '2026092%')", 2),
            ("_TABLE_SUFFIX IS NOT NULL", 4),
            ("_TABLE_SUFFIX IN UNNEST(['20260930'])", 1),
        ],
    )
    def test_suffix_exclusions_skip_shards(self, where: str, shards: int) -> None:
        sql = f"SELECT n FROM `p.d.events_*` WHERE {where}"
        assert billed(sql, TestShards.GA4) == (shards * 8 * GB, shards * 8 * GB)

    @pytest.mark.parametrize(
        "where",
        [
            "DATE_TRUNC(refresh_date, WEEK(FOO)) = '2026-09-27'",
            "refresh_date >= DATE_ADD(CURRENT_DATE(), INTERVAL 8000 YEAR)",
            "refresh_date >= DATE_ADD(CURRENT_DATE(), INTERVAL 9999999999 DAY)",
            "refresh_date >= DATE_SUB(DATE '0001-01-02', INTERVAL 3 DAY)",
        ],
    )
    def test_values_out_of_range_do_not_raise(self, where: str) -> None:
        estimate = estimate_of(f"SELECT score FROM trends WHERE {where}")
        assert estimate.bytes_high <= 6 * COLUMN

    UNPARTITIONED = Table(
        "p",
        "d",
        "t",
        (Column("day", "DATE"), Column("n", "INT64")),
        row_count=3 * GB,
        size_bytes=48 * GB,
        partitioning=Partitioning("day", "DAY"),
        partitions=(Partition("20260930", 16 * GB), Partition("__UNPARTITIONED__", 32 * GB)),
    )

    @pytest.mark.parametrize(
        ("where", "high"),
        [
            # Dates before 1960 or after 2159 are in __UNPARTITIONED__.
            ("day < '1950-01-01'", 32 * GB),
            ("day >= '2026-01-01'", 48 * GB),
            ("day = '2026-09-30'", 16 * GB),
            ("day = '2200-01-01'", 32 * GB),
            ("day BETWEEN '2026-01-01' AND '2026-12-31'", 16 * GB),
        ],
    )
    def test_unpartitioned_holds_dates_out_of_range(self, where: str, high: int) -> None:
        sql = f"SELECT n, day FROM t WHERE {where}"
        assert billed(sql, self.UNPARTITIONED, now=SEPT_30)[1] == high

    def test_equality_with_a_parameter_skips_the_null_partition(self) -> None:
        table = Table(
            "p",
            "d",
            "t",
            (Column("day", "DATE"), Column("n", "INT64")),
            row_count=2 * GB,
            size_bytes=32 * GB,
            partitioning=Partitioning("day", "DAY"),
            partitions=(Partition("__NULL__", 30 * GB), Partition("20260930", 2 * GB)),
        )
        assert billed("SELECT n, day FROM t WHERE day = @day", table)[1] == 2 * GB

    @pytest.mark.parametrize("kind", ["VIEW", "EXTERNAL"])
    def test_views_and_external_tables_are_not_estimated(self, kind: str) -> None:
        table = Table("p", "d", "v", (Column("a", "INT64"),), kind=kind, size_bytes=0)  # type: ignore[arg-type]
        assert check("SELECT a FROM v", Catalog((table,), "p", "d")).estimate is None
        assert check("SELECT a FROM v LIMIT 0", Catalog((table,), "p", "d")).estimate is None

    def test_rejected_query_has_no_estimate(self) -> None:
        required = Table(
            "p",
            "d",
            "r",
            (Column("day", "DATE"), Column("n", "INT64")),
            row_count=10,
            size_bytes=160,
            partitioning=Partitioning("day", "DAY", required=True),
        )
        result = check("SELECT n FROM r", Catalog((required,), "p", "d"))
        assert (result.verdict.value, result.estimate) == ("block", None)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM ML.PREDICT(MODEL p.d.m, TABLE p.d.trends)",
            "SELECT table_name FROM p.d.INFORMATION_SCHEMA.TABLES",
        ],
    )
    def test_reads_the_facts_do_not_follow(self, sql: str) -> None:
        assert check(sql, Catalog((TRENDS,), "p", "d")).estimate is None


class TestSecondReview:
    """Cases from the second code review."""

    def test_tablesample_bills_only_what_it_picks(self) -> None:
        sql = f"SELECT score FROM trends TABLESAMPLE SYSTEM (1 PERCENT) WHERE {ONE_DAY}"
        estimate = estimate_of(sql)
        assert (estimate.bytes_low, estimate.bytes_high) == (MIN_BILLED_BYTES, 2 * COLUMN)
        assert estimate.confidence == "low"

    def test_partitions_newer_than_the_catalog(self) -> None:
        # The catalog's newest partition is 1 October; on 2 October, today's may exist too.
        later = datetime(2026, 10, 2, 9, tzinfo=UTC)
        today = estimate_of(
            "SELECT score FROM trends WHERE refresh_date = CURRENT_DATE()", now=later
        )
        assert (today.bytes_low, today.bytes_high) == (MIN_BILLED_BYTES, 2 * COLUMN)
        assert today.confidence == "medium"
        since = estimate_of(
            "SELECT score FROM trends WHERE refresh_date >= '2026-10-01'", now=later
        )
        assert (since.bytes_low, since.bytes_high) == (2 * COLUMN, 4 * COLUMN)

    @pytest.mark.parametrize(
        "where",
        [
            "refresh_date IN UNNEST(@days)",
            "NOT (refresh_date < '2026-10-01')",
            "refresh_date NOT BETWEEN '2026-09-01' AND '2026-09-30'",
            "EXTRACT(YEAR FROM refresh_date) = 2026",
        ],
    )
    def test_filters_that_may_prune_lower_the_low_end(self, where: str) -> None:
        estimate = estimate_of(f"SELECT score FROM trends WHERE {where}")
        assert (estimate.bytes_low, estimate.confidence) == (MIN_BILLED_BYTES, "low")

    @pytest.mark.parametrize(
        ("where", "confidence"),
        [
            ("_TABLE_SUFFIX != '20260930'", "low"),
            ("_TABLE_SUFFIX NOT LIKE '%0930'", "low"),
            ("_TABLE_SUFFIX IS NOT NULL", "medium"),  # medium: event_name is a STRING
            ("_TABLE_SUFFIX = event_name", "medium"),
        ],
    )
    def test_wildcard_without_shards(self, where: str, confidence: str) -> None:
        family = Table(
            "p",
            "d",
            "e_*",
            (Column("event_name", "STRING"), Column("n", "INT64")),
            row_count=GB,
            size_bytes=16 * GB,
        )
        sql = f"SELECT event_name FROM `p.d.e_*` WHERE {where}"
        assert estimate_of(sql, family).confidence == confidence

    def test_non_constant_suffix_filter_reads_every_shard(self) -> None:
        sql = "SELECT n, event_name FROM `p.d.events_*` WHERE _TABLE_SUFFIX = event_name"
        assert billed(sql, TestShards.GA4) == (64 * GB, 64 * GB)

    def test_date_out_of_range_does_not_raise(self) -> None:
        # An unknown value: at most one partition, as with `= @day`.
        sql = "SELECT score FROM trends WHERE refresh_date = DATE(99999999999999999999, 1, 1)"
        assert estimate_of(sql).confidence == "medium"

    def test_unfiltered_reads_only_the_listed_partitions(self) -> None:
        later = datetime(2026, 10, 9, tzinfo=UTC)
        estimate = estimate_of("SELECT score FROM trends", now=later)
        assert (estimate.bytes_low, estimate.bytes_high) == (3 * COLUMN, 3 * COLUMN)

    def test_billing_rounds_up_to_a_mib(self) -> None:
        table = Table(
            "p", "d", "t", (Column("a", "INT64"),), row_count=2_000_000, size_bytes=16_000_000
        )
        estimate = estimate_of("SELECT a FROM t", table)
        assert estimate.bytes_high == MIN_BILLED_BYTES + 6 * 2**20  # 15.3 MiB, billed as 16

    @pytest.mark.parametrize(
        ("where", "size"),
        [
            ("DATE_ADD(day, INTERVAL 1 DAY) >= '2026-10-01'", 48 * GB),  # dates after 2159
            ("DATE_TRUNC(day, WEEK) = '2026-09-27'", 16 * GB),
        ],
    )
    def test_unpartitioned_with_date_arithmetic_is_exact(self, where: str, size: int) -> None:
        sql = f"SELECT n, day FROM t WHERE {where}"
        estimate = estimate_of(sql, TestReviewCases.UNPARTITIONED, now=SEPT_30)
        assert (estimate.bytes_low, estimate.bytes_high) == (size, size)

    def test_filter_that_does_not_prune_inside_an_or(self) -> None:
        sql = (
            "SELECT n, ts FROM events "
            "WHERE (ts >= '2026-09-30' AND CAST(ts AS STRING) LIKE '2026%') OR ts IS NULL"
        )
        estimate = estimate_of(sql, TestTimestampPartitions.EVENTS, now=SEPT_30)
        assert (estimate.bytes_low, estimate.bytes_high, estimate.confidence) == (
            16 * GB,
            16 * GB,
            "high",
        )

    def test_table_named_like_information_schema(self) -> None:
        table = Table(
            "p", "d", "jobs_information_schema_copy", (Column("v", "INT64"),), size_bytes=1
        )
        result = check("SELECT v FROM jobs_information_schema_copy", Catalog((table,), "p", "d"))
        assert result.estimate is not None
