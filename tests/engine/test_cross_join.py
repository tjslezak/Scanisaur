"""SCN006: joins that pair every row of one side with every row of the other (#23)."""

from datetime import UTC, datetime

import pytest

from scanisaur.catalog import Catalog, Column, Table
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
CATALOG = Catalog((USERS, ORDERS, ITEMS, DAYS, UNSIZED), "p", "d")


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
    ],
)
def test_count(n: int, words: str) -> None:
    assert cross_join._count(n) == words
