"""SCN009: a query that returns every row of a large table."""

from datetime import UTC, datetime

import pytest

from scanisaur.catalog import Catalog, Column, Partition, Partitioning, Table
from scanisaur.engine.check import Policy, check
from scanisaur.engine.rules import UNBOUNDED_RESULT

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
USERS = Table(
    "p",
    "d",
    "users",
    (Column("id", "INT64"), Column("country", "STRING")),
    row_count=100_000,
    size_bytes=1_600_000,
)
#: 30 days of 1 million rows each.
EVENTS = Table(
    "p",
    "d",
    "events",
    (Column("day", "DATE"), Column("user_id", "INT64"), Column("name", "STRING")),
    row_count=30_000_000,
    size_bytes=30_000,
    partitioning=Partitioning("day", "DAY"),
    partitions=tuple(Partition(f"202609{d:02}", 1_000) for d in range(1, 31)),
)
UNSIZED = Table("p", "d", "unsized", (Column("x", "INT64"),))
CATALOG = Catalog((USERS, EVENTS, UNSIZED), default_project="p", default_dataset="d")


def unbounded(sql: str, policy: Policy | None = None) -> list[str]:
    result = check(sql, CATALOG, policy=policy or Policy(), now=NOW)
    return [f.message for f in result.findings if f.rule == UNBOUNDED_RESULT]


def test_partition_filter_keeps_the_count_known() -> None:
    (message,) = unbounded("SELECT name FROM events WHERE day = '2026-09-30'")
    assert "about 1 million rows" in message


@pytest.mark.parametrize(
    "sql",
    [
        # A filter on more than the partition column keeps some rows of each partition.
        "SELECT name FROM events WHERE DATE_DIFF(day, DATE '2026-09-01', DAY) = user_id",
        "SELECT name FROM events WHERE day = '2026-09-30' AND user_id = 7",
        # A filter that involves another source, or a subquery.
        "SELECT u.id FROM users AS u LEFT JOIN events AS e ON e.user_id = u.id "
        "WHERE e.day = '2026-09-30'",
        "SELECT id FROM users WHERE id IN (SELECT user_id FROM events WHERE day = '2026-09-30')",
        "SELECT id FROM users WHERE id > (SELECT 5)",
        # Nothing about the rows is known.
        "SELECT x FROM unsized",
        "SELECT 1 AS one",
        "SELECT x FROM UNNEST([1, 2, 3]) AS x",
        "SELECT name FROM events, UNNEST([1, 2]) AS n WHERE day = '2026-09-30'",
        # A set operation that drops rows, or a LIMIT on the whole union.
        "SELECT id FROM users INTERSECT DISTINCT SELECT id FROM users",
        "(SELECT id FROM users UNION ALL SELECT id FROM users) LIMIT 5",
        "SELECT id FROM users UNION ALL SELECT x FROM unsized",
    ],
)
def test_silent(sql: str) -> None:
    assert unbounded(sql) == []


def test_union_all_names_each_table() -> None:
    (message,) = unbounded("SELECT id FROM users UNION ALL SELECT user_id FROM events")
    assert "of `p.d.users` and `p.d.events`, about 30.1 million rows" in message


def test_threshold_is_a_policy_setting() -> None:
    sql = "SELECT id FROM users"
    assert unbounded(sql, Policy(unbounded_result_rows=100_001)) == []
    assert len(unbounded(sql, Policy(unbounded_result_rows=100_000))) == 1


def test_writes_return_no_rows() -> None:
    sql = "INSERT INTO users SELECT id, country FROM users"
    assert unbounded(sql, Policy(read_only=False)) == []
