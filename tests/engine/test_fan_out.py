"""SCN007: joins on columns that aren't a unique key. The golden cases cover the main
behavior; these cover how keys pass through CTEs and subqueries, and the edges."""

from datetime import UTC, datetime

import pytest

from scanisaur.catalog import Catalog, Column, Table
from scanisaur.engine.check import check
from scanisaur.engine.result import Finding

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
USERS = Table(
    "p",
    "d",
    "users",
    (Column("ID", "INT64"), Column("country", "STRING")),
    row_count=100_000,
    size_bytes=1_600_000,
    keys=(("id",),),
)
ORDERS = Table(
    "p",
    "d",
    "orders",
    (Column("order_id", "INT64"), Column("user_id", "INT64"), Column("amount", "NUMERIC")),
    row_count=125_000,
    size_bytes=4_000_000,
    keys=(("order_id",),),
)
ITEMS = Table(
    "p",
    "d",
    "order_items",
    (
        Column("id", "INT64"),
        Column("order_id", "INT64"),
        Column("user_id", "INT64"),
        Column("price", "NUMERIC"),
    ),
    row_count=180_000,
    size_bytes=5_760_000,
    keys=(("id",),),
)
#: Keys not configured.
EVENTS = Table(
    "p",
    "d",
    "events",
    (Column("user_id", "INT64"), Column("kind", "STRING")),
    row_count=2_000_000,
    size_bytes=40_000_000,
)
#: Known to have no unique key, such as a log.
LOG = Table(
    "p",
    "d",
    "log",
    (Column("user_id", "INT64"), Column("msg", "STRING")),
    row_count=2_000_000,
    size_bytes=40_000_000,
    keys=(),
)
CATALOG = Catalog((USERS, ORDERS, ITEMS, EVENTS, LOG), "p", "d")


def fan_out(sql: str) -> list[Finding]:
    result = check(sql, CATALOG, now=NOW)
    return [f for f in result.findings if f.rule == "SCN007"]


@pytest.mark.parametrize(
    "sql",
    [
        # Keys pass through a CTE that keeps the key column, renamed or under *.
        "WITH o AS (SELECT order_id AS oid, amount FROM orders) "
        "SELECT SUM(i.price) FROM order_items i JOIN o ON o.oid = i.order_id",
        "WITH o AS (SELECT * FROM orders WHERE amount > 0) "
        "SELECT SUM(i.price) FROM order_items i JOIN o ON o.order_id = i.order_id",
        "WITH o AS (SELECT * FROM orders), p AS (SELECT order_id FROM o) "
        "SELECT SUM(i.price) FROM order_items i JOIN p ON p.order_id = i.order_id",
        # GROUP BY by an output alias, and by an expression it outputs.
        "SELECT SUM(o.amount) FROM orders o JOIN (SELECT order_id AS k, COUNT(*) AS n "
        "FROM order_items GROUP BY k) i ON i.k = o.order_id",
        "SELECT SUM(o.amount) FROM orders o JOIN (SELECT order_id + 0 AS k "
        "FROM order_items GROUP BY order_id + 0) i ON i.k = o.order_id",
        # A single row matches any row once.
        "SELECT SUM(o.amount) FROM orders o JOIN (SELECT MAX(order_id) AS m FROM order_items) i "
        "ON i.m = o.order_id",
        # Keys are compared without regard to case.
        "SELECT SUM(o.amount) FROM orders o JOIN users u ON u.id = o.user_id",
        # A source whose keys aren't known is never reported, nor is anything past it.
        "SELECT SUM(u.id) FROM users u JOIN events e ON e.user_id = u.id",
        "SELECT SUM(o.amount) FROM orders o JOIN events e ON e.user_id = o.user_id "
        "JOIN log l ON l.user_id = e.user_id",
        # What isn't a key match: an OR, a function around a column, a comparison.
        "SELECT SUM(o.amount) FROM orders o JOIN order_items i "
        "ON i.order_id = o.order_id OR i.id = o.order_id",
        "SELECT SUM(o.amount) FROM orders o JOIN order_items i ON i.order_id = ABS(o.order_id)",
        "SELECT SUM(o.amount) FROM orders o JOIN order_items i ON i.order_id < o.order_id",
        # MIN, MAX and DISTINCT aggregates don't count repeated rows.
        "SELECT MAX(o.amount), SUM(DISTINCT o.amount) FROM orders o "
        "JOIN order_items i ON i.order_id = o.order_id",
        # An aggregate over columns of both sides.
        "SELECT SUM(o.amount * i.price) FROM orders o "
        "JOIN order_items i ON i.order_id = o.order_id",
        # A correlated subquery matches the outer row; it doesn't join it.
        "SELECT o.order_id, (SELECT SUM(i.price) FROM order_items i "
        "WHERE i.order_id = o.order_id) FROM orders o",
    ],
)
def test_silent(sql: str) -> None:
    assert fan_out(sql) == []


def test_cte_that_drops_the_key() -> None:
    (finding,) = fan_out(
        "WITH i AS (SELECT order_id, price FROM order_items) "
        "SELECT SUM(o.amount) FROM orders o JOIN i ON i.order_id = o.order_id"
    )
    assert finding.message == (
        "`SUM(o.amount)` counts each row of `o` (orders) once for every row of `i` it matches, "
        "as `order_id` isn't a unique key of `i`. The result is wrong, and BigQuery gives no "
        "error."
    )


def test_subquery_without_alias() -> None:
    (finding,) = fan_out(
        "SELECT SUM(o.amount) FROM orders o JOIN (SELECT order_id AS oid FROM order_items) "
        "ON oid = o.order_id"
    )
    assert "every row of a subquery it matches" in finding.message


def test_union_of_keys_is_unknown() -> None:
    sql = (
        "WITH i AS (SELECT order_id FROM order_items UNION ALL SELECT order_id FROM orders) "
        "SELECT SUM(o.amount) FROM orders o JOIN i ON i.order_id = o.order_id"
    )
    assert fan_out(sql) == []


def test_rollup_and_missing_group_column_are_unknown() -> None:
    for group in ("ROLLUP (order_id)", "CUBE (order_id)", "order_id, user_id", "ALL"):
        sql = (
            f"SELECT SUM(o.amount) FROM orders o JOIN (SELECT order_id FROM order_items "
            f"GROUP BY {group}) i ON i.order_id = o.order_id"
        )
        assert fan_out(sql) == []


def test_known_without_keys_is_never_unique() -> None:
    # users is unique on id, so this is one to many: only an aggregate makes it wrong.
    assert fan_out("SELECT u.country, l.msg FROM users u JOIN log l ON l.user_id = u.id") == []
    (finding,) = fan_out("SELECT COUNT(u.id) FROM users u JOIN log l ON l.user_id = u.id")
    assert finding.fix is not None
    assert "`COUNT(DISTINCT u.id)`" in finding.fix


def test_self_join_is_many_to_many() -> None:
    (finding,) = fan_out(
        "SELECT a.order_id, b.order_id FROM orders a JOIN orders b ON b.user_id = a.user_id"
    )
    assert finding.message == (
        "`a` (orders) and `b` (orders) are matched on `b.user_id = a.user_id`, a unique key of "
        "neither, so each row of one pairs with every matching row of the other: the result can "
        "hold more rows than both, and counts or sums over it are multiplied."
    )
    assert finding.fix == (
        "Match on a unique key of one side, for example add `b.order_id = a.order_id`, or "
        "reduce one side to one row per matched value with GROUP BY or DISTINCT first."
    )
    assert (finding.line, finding.column) == (1, 50)


def test_many_to_many_without_a_shared_key_column() -> None:
    (finding,) = fan_out(
        "SELECT o.order_id, l.msg FROM orders o JOIN log l ON l.user_id = o.user_id"
    )
    assert finding.fix == (
        "Match on a unique key of one side as well, or reduce one side to one row per matched "
        "value with GROUP BY or DISTINCT first."
    )


def test_countif() -> None:
    (finding,) = fan_out(
        "SELECT COUNTIF(o.amount > 100) FROM orders o JOIN order_items i ON i.order_id = o.order_id"
    )
    assert finding.message.startswith("`COUNTIF(o.amount > 100)` counts each row of `o`")


def test_one_finding_per_select() -> None:
    findings = fan_out(
        "SELECT SUM(o.amount), COUNT(o.order_id) FROM orders o "
        "JOIN order_items i ON i.user_id = o.user_id"
    )
    assert [f.message.split(" counts")[0] for f in findings] == ["`SUM(o.amount)`"]


def test_each_union_branch_is_reported() -> None:
    branch = "SELECT SUM(o.amount) FROM orders o JOIN order_items i ON i.order_id = o.order_id"
    assert len(fan_out(f"{branch}\nUNION ALL\n{branch}")) == 2


def test_recursive_cte() -> None:
    sql = (
        "WITH RECURSIVE r AS (SELECT order_id FROM orders UNION ALL "
        "SELECT r.order_id FROM r JOIN orders o ON o.order_id = r.order_id) "
        "SELECT SUM(o.amount) FROM orders o JOIN r ON r.order_id = o.order_id"
    )
    assert fan_out(sql) == []


def test_tables_without_aliases_and_two_matched_columns() -> None:
    (finding,) = fan_out(
        "SELECT SUM(orders.amount) FROM orders JOIN order_items "
        "ON order_items.order_id = orders.order_id AND order_items.user_id = orders.user_id"
    )
    assert finding.message.startswith(
        "`SUM(orders.amount)` counts each row of `orders` once for every row of `order_items` "
        "it matches, as (`order_id`, `user_id`) isn't a unique key of `order_items`."
    )
