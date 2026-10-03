from datetime import UTC, datetime, timedelta

from scanisaur.audit.log import decision
from scanisaur.audit.report import audit
from scanisaur.catalog import Catalog, Column, Table
from scanisaur.catalog.connectors import QueryRun
from scanisaur.engine.check import check
from scanisaur.engine.result import Verdict

GIB = 2**30
CATALOG = Catalog(
    (
        Table("p", "d", "users", (Column("email", "STRING"), Column("id", "INT64")), row_count=10),
        Table("p", "d", "events", (Column("id", "INT64"), Column("kind", "STRING"))),
    ),
    default_project="p",
    default_dataset="d",
)
NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
SELECT_STAR = "SELECT * FROM events WHERE kind = '{}'"
CLEAN = "SELECT id FROM users WHERE email = 'a@example.com' LIMIT 5"


def _run(sql: str, minutes: int, billed: int) -> QueryRun:
    return QueryRun(f"job{minutes}", NOW + timedelta(minutes=minutes), "agent@x", sql, billed)


def test_report() -> None:
    runs = [
        _run(SELECT_STAR.format("click"), 0, 3 * GIB),
        _run(SELECT_STAR.format("view"), 10, 2 * GIB),
        _run(CLEAN, 20, GIB),
    ]
    # The first run was checked just before it ran; the others weren't.
    sql = runs[0].sql
    logged = [decision(check(sql, CATALOG), sql, source="cli", now=NOW - timedelta(minutes=1))]
    report = audit(runs, CATALOG, logged, since=NOW - timedelta(days=1))
    assert (report.runs, report.bytes_billed) == (3, 6 * GIB)
    assert (report.flagged_runs, report.flagged_bytes) == (2, 5 * GIB)
    assert (report.unchecked_runs, report.ran_after_block) == (2, 0)
    [shape] = report.flagged
    assert shape.sql == "SELECT * FROM events WHERE kind = ?"
    assert (shape.runs, shape.bytes_billed, shape.unchecked_runs) == (2, 5 * GIB, 1)
    assert shape.verdict is Verdict.WARN
    assert shape.rules == ("SCN005",)  # SELECT *
    assert [t.name for t in report.tables] == ["p.d.events", "p.d.users"]
    assert report.rules[0].bytes_billed == 5 * GIB


def test_check_too_early_or_blocked() -> None:
    sql = "DELETE FROM users WHERE TRUE"
    blocked = check(sql, CATALOG)
    assert blocked.verdict is Verdict.BLOCK
    logged = [
        decision(blocked, sql, source="hook", now=NOW - timedelta(hours=2)),
        decision(blocked, sql, source="hook", now=NOW + timedelta(minutes=29)),
    ]
    runs = [_run(sql, 0, 0), _run(sql, 30, 0)]
    report = audit(runs, CATALOG, logged, since=NOW, top=1)
    assert (report.unchecked_runs, report.ran_after_block) == (1, 1)
    assert report.flagged[0].verdict is Verdict.BLOCK


def test_empty_history() -> None:
    report = audit([], CATALOG, [], since=NOW)
    assert (report.runs, report.flagged, report.tables) == (0, (), ())
