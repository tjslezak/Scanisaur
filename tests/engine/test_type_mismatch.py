"""SCN008: comparisons between types BigQuery refuses, or compares in a way that changes
the answer. Each behavior was measured with a dry run or a literal-only query
(docs/rules/scn008.md)."""

from datetime import UTC, datetime

import pytest
from sqlglot.errors import SqlglotError

from scanisaur.catalog import Catalog, Column, Table
from scanisaur.engine import type_mismatch
from scanisaur.engine.check import check
from scanisaur.engine.result import Finding, Severity
from scanisaur.engine.rules import TYPE_MISMATCH

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
ORDERS = Table(
    "p",
    "d",
    "orders",
    (
        Column("order_id", "INT64"),
        Column("user_id", "STRING"),
        Column("created_at", "TIMESTAMP"),
        Column("shipped_at", "DATETIME"),
        Column("ship_day", "DATE"),
        Column("weight", "FLOAT64"),
        Column("total", "NUMERIC(12, 2)"),
        Column("tags", "ARRAY<STRING>"),
    ),
)
CATALOG = Catalog((ORDERS,), default_project="p", default_dataset="d")


def mismatches(where: str) -> list[Finding]:
    result = check(f"SELECT order_id FROM orders AS o WHERE {where} LIMIT 10", CATALOG, now=NOW)
    return [f for f in result.findings if f.rule == TYPE_MISMATCH]


@pytest.mark.parametrize(
    "where",
    [
        "o.user_id = o.order_id",  # STRING with INT64
        "o.created_at = o.ship_day",  # TIMESTAMP with DATE
        "o.created_at < o.shipped_at",  # TIMESTAMP with DATETIME
        "o.order_id BETWEEN '1' AND '9'",  # a string literal doesn't become a number
        "o.total = 'ten'",
    ],
)
def test_refused(where: str) -> None:
    (finding,) = mismatches(where)
    assert finding.severity is Severity.WARN
    assert finding.message.startswith("BigQuery refuses")


@pytest.mark.parametrize(
    "where",
    [
        "o.order_id < o.weight",  # numbers convert; only equality needs exact values
        "o.total < 10.5",
        "o.shipped_at >= o.ship_day",  # a DATE becomes midnight, from midnight on is a day
        "o.created_at < '2026-10-01'",
        "o.created_at <= '2026-09-30T12:00:00'",  # a time of day, not a date
        "o.ship_day = '2026-09-30'",
        "o.weight = 1",  # a constant: no IDs to confuse
        "o.user_id IN UNNEST(o.tags)",
        "o.user_id IN (SELECT user_id FROM orders)",
        "o.order_id = NULL",
    ],
)
def test_silent(where: str) -> None:
    assert mismatches(where) == []


def test_midnight_from_a_date_is_whole_days() -> None:
    assert mismatches("o.shipped_at <= DATETIME(o.ship_day)") == []
    assert mismatches("o.shipped_at <= DATETIME_TRUNC(o.shipped_at, MONTH)") == []
    (finding,) = mismatches("DATETIME_TRUNC(o.shipped_at, HOUR) <= o.ship_day")
    assert "leaves out the rest of that day" in finding.message


def test_impossible_date() -> None:
    # BigQuery refuses to convert it ("Could not cast literal"), which isn't this rule's.
    assert mismatches("o.created_at <= '2026-02-30'") == []


def test_numeric_with_a_scale_is_numeric() -> None:
    (finding,) = mismatches("o.total = o.weight")
    assert "compares NUMERIC with FLOAT64" in finding.message
    assert finding.fix == "Compare exact values: `CAST(o.weight AS NUMERIC)`."


def test_one_finding_per_comparison() -> None:
    (finding,) = mismatches("o.user_id IN (1, 2) AND o.order_id = 3")
    assert "`o.user_id IN (1, 2)`" in finding.message


def test_correlated_subquery() -> None:
    sql = (
        "SELECT order_id FROM orders AS o WHERE EXISTS "
        "(SELECT 1 FROM orders AS p WHERE p.user_id = o.order_id) LIMIT 1"
    )
    (finding,) = check(sql, CATALOG, now=NOW).findings
    assert finding.rule == TYPE_MISMATCH
    assert "`p.user_id = o.order_id`" in finding.message


def test_types_that_cant_be_worked_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise SqlglotError("no types")

    monkeypatch.setattr(type_mismatch, "annotate_types", fail)
    assert mismatches("o.user_id = o.order_id") == []


def test_any_compares_with_the_subquery_column() -> None:
    assert mismatches("o.user_id = ANY (SELECT user_id FROM orders)") == []


def test_padded_date_literal() -> None:
    # BigQuery refuses it ("Could not cast literal"), so it isn't read as midnight.
    assert mismatches("o.created_at <= ' 2026-09-30 '") == []


def test_quoted_text_against_a_number() -> None:
    (finding,) = mismatches("o.order_id = 'x'")
    assert finding.fix is not None
    assert "without quotes" not in finding.fix
    (finding,) = mismatches("o.order_id = '42'")
    assert finding.fix == "Write the number without quotes, as `42`."


def test_not_turns_the_midnight_message_around() -> None:
    assert mismatches("NOT (o.created_at <= '2026-09-30')") == []
    (finding,) = mismatches("NOT (o.user_id = 42)")
    assert finding.message.startswith("BigQuery refuses")
