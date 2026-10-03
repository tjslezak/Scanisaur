"""SCN006: joins that pair every row of one side with every row of the other (#23)."""

from datetime import UTC, datetime

import pytest

from scanisaur.catalog import Catalog, Column, Partition, Partitioning, Table
from scanisaur.engine import cross_join
from scanisaur.engine.check import Policy, check
from scanisaur.engine.result import Finding, Severity

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
USERS = Table(
    "p",
    "d",
    "users",
    (Column("id", "INT64"), Column("age", "INT64")),
    row_count=100_000,
    size_bytes=1_600_000,
)
ORDERS = Table(
    "p",
    "d",
    "orders",
    (Column("order_id", "INT64"), Column("user_id", "INT64"), Column("status", "STRING")),
    row_count=125_000,
    size_bytes=4_000_000,
)
ITEMS = Table(
    "p",
    "d",
    "order_items",
    (Column("order_id", "INT64"), Column("price", "FLOAT64")),
    row_count=180_000,
    size_bytes=2_880_000,
)
DAYS = Table("p", "d", "days", (Column("day", "DATE"),), row_count=7, size_bytes=56)
UNSIZED = Table("p", "d", "unsized", (Column("x", "INT64"),))
EMPTY = Table("p", "d", "empty", (Column("x", "INT64"),), row_count=0, size_bytes=0)
#: 30 days of 1 million rows each.
LOG = Table(
    "p",
    "d",
    "log",
    (Column("ts", "TIMESTAMP"), Column("msg", "STRING")),
    row_count=30_000_000,
    size_bytes=30 * 2**30,
    partitioning=Partitioning("ts", "DAY"),
    partitions=tuple(Partition(f"202609{day:02}", 2**30) for day in range(1, 31)),
)
EVENTS = Table(
    "p",
    "d",
    "events",
    (Column("id", "INT64"), Column("items", "ARRAY<STRUCT<sku STRING>>")),
    row_count=1_000_000,
    size_bytes=64_000_000,
)
CATALOG = Catalog((USERS, ORDERS, ITEMS, DAYS, UNSIZED, EMPTY, LOG, EVENTS), "p", "d")


def scn006(sql: str, policy: Policy | None = None) -> list[Finding]:
    result = check(sql, CATALOG, now=NOW, policy=policy or Policy())
    return [f for f in result.findings if f.rule == "SCN006"]


class TestSeverity:
    def test_known_sizes_over_the_block_threshold_block(self) -> None:
        # 100,000 x 125,000 = 12.5 billion pairs, as measured on thelook (#23).
        (finding,) = scn006("SELECT COUNT(*) FROM users AS u CROSS JOIN orders AS o")
        assert finding.severity is Severity.BLOCK
        assert "about 12.5 billion pairs" in finding.message

    def test_thresholds_come_from_the_policy(self) -> None:
        sql = "SELECT COUNT(*) FROM users AS u CROSS JOIN orders AS o"
        relaxed = Policy(cross_join_block_pairs=10**11)
        assert [f.severity for f in scn006(sql, relaxed)] == [Severity.WARN]
        assert (
            scn006(sql, Policy(cross_join_warn_pairs=10**11, cross_join_block_pairs=10**12)) == []
        )

    def test_a_filter_makes_the_size_an_upper_bound(self) -> None:
        sql = "SELECT COUNT(*) FROM users AS u, orders AS o WHERE o.status = 'Complete'"
        (finding,) = scn006(sql)
        assert finding.severity is Severity.WARN
        assert "up to about 12.5 billion pairs" in finding.message

    def test_a_small_upper_bound_is_not_reported(self) -> None:
        # Filters only shrink a side: 100,000 x 7 days can't reach the warn threshold.
        assert scn006("SELECT * FROM users AS u, days AS d WHERE d.day > '2026-01-01'") == []

    def test_unknown_row_count_warns(self) -> None:
        (finding,) = scn006("SELECT * FROM users AS u CROSS JOIN unsized AS z")
        assert finding.severity is Severity.WARN
        assert (
            "how many isn't known, as `z` (unsized) may hold any number of rows" in finding.message
        )

    def test_inequality_only_compares_every_pair(self) -> None:
        (finding,) = scn006("SELECT COUNT(*) FROM users AS u JOIN orders AS o ON o.user_id < u.id")
        assert finding.severity is Severity.BLOCK  # the comparison filters pairs, not rows
        assert "joined only by `o.user_id < u.id`, which isn't an equality" in finding.message
        assert finding.fix is not None
        assert "`o.user_id = u.id`" in finding.fix


class TestSizes:
    def test_a_correlated_subquery_limits_a_side(self) -> None:
        sql = (
            "SELECT COUNT(*) FROM users AS u JOIN orders AS o ON o.user_id < u.id "
            "WHERE EXISTS (SELECT 1 FROM days AS d WHERE d.day = DATE '2026-01-01' AND u.id > 0)"
        )
        assert [f.severity for f in scn006(sql)] == [Severity.WARN]

    def test_a_sample_reads_a_fraction(self) -> None:
        sql = (
            "SELECT COUNT(*) FROM users AS u TABLESAMPLE SYSTEM (1 PERCENT) CROSS JOIN orders AS o"
        )
        assert [f.severity for f in scn006(sql)] == [Severity.WARN]

    def test_a_filter_finer_than_a_partition_keeps_fewer_rows(self) -> None:
        window = "l.ts BETWEEN TIMESTAMP '2026-09-10 10:00:00' AND TIMESTAMP '2026-09-10 10:00:05'"
        (finding,) = scn006(f"SELECT COUNT(*) FROM log AS l CROSS JOIN users AS u WHERE {window}")
        assert finding.severity is Severity.WARN
        # A whole day is a whole partition: 1 million x 100,000 rows.
        whole_day = "DATE(l.ts) = '2026-09-10'"
        (finding,) = scn006(
            f"SELECT COUNT(*) FROM log AS l CROSS JOIN users AS u WHERE {whole_day}"
        )
        assert finding.severity is Severity.BLOCK
        assert "about 100 billion pairs" in finding.message

    def test_an_empty_table_pairs_with_nothing(self) -> None:
        assert scn006("SELECT * FROM users AS u CROSS JOIN empty AS z") == []

    def test_unnest_changes_a_sides_rows(self) -> None:
        plain = scn006("SELECT COUNT(*) FROM events AS e CROSS JOIN users AS u")
        flattened = scn006(
            "SELECT COUNT(*) FROM events AS e, UNNEST(e.items) AS i CROSS JOIN users AS u"
        )
        assert [f.severity for f in plain] == [Severity.BLOCK]
        assert [f.severity for f in flattened] == [Severity.WARN]
        assert "`e` (events) may hold any number of rows" in flattened[0].message

    def test_a_joined_group_has_no_known_size(self) -> None:
        sql = (
            "SELECT * FROM users AS u JOIN orders AS o ON o.status = CAST(u.age AS STRING), "
            "days AS d"
        )
        (finding,) = scn006(sql)
        assert finding.severity is Severity.WARN
        assert "the join of `u` (users) and `o` (orders) may hold any" in finding.message


class TestLimit:
    def test_a_limit_stops_early(self) -> None:
        # Measured: LIMIT 10 took 0.14 slot-seconds where the whole product took 160.
        (finding,) = scn006("SELECT * FROM users AS u CROSS JOIN orders AS o LIMIT 10")
        assert finding.severity is Severity.WARN
        assert "The LIMIT 10 stops BigQuery early" in finding.message

    def test_sorting_needs_every_pair(self) -> None:
        sql = "SELECT * FROM users AS u CROSS JOIN orders AS o ORDER BY u.age LIMIT 10"
        assert [f.severity for f in scn006(sql)] == [Severity.BLOCK]


def test_each_select_gets_its_own_finding() -> None:
    branch = "SELECT u.id FROM users AS u, orders AS o"
    findings = scn006(f"{branch}\nUNION ALL\n{branch}")
    assert [f.line for f in findings] == [1, 3]


class TestFix:
    @pytest.mark.parametrize(
        ("sql", "key"),
        [
            (
                "SELECT * FROM users AS u, orders AS o",
                "o.user_id = u.id",
            ),  # orders.user_id -> users.id
            (
                "SELECT * FROM orders AS o, order_items AS i",
                "o.order_id = i.order_id",
            ),  # shared *_id
            ("SELECT * FROM order_items AS i, users AS u", None),
        ],
    )
    def test_suggested_key(self, sql: str, key: str | None) -> None:
        (finding,) = scn006(sql, Policy(cross_join_warn_pairs=1))
        assert finding.fix is not None
        if key is None:
            assert "for example" not in finding.fix
        else:
            assert f"for example `{key}`" in finding.fix


@pytest.mark.parametrize(
    ("n", "words"),
    [
        (999_999, "999,999"),
        (12_500_000_000, "12.5 billion"),
        (2 * 10**12, "2 trillion"),
        (3 * 10**15, "3 quadrillion"),
        (999_990_000, "1 billion"),  # rounding reaches 1,000 million
        (999_990_000_000, "1 trillion"),
    ],
)
def test_count(n: int, words: str) -> None:
    assert cross_join._count(n) == words
