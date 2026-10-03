from datetime import UTC, datetime, timedelta

from scanisaur.audit.log import decision
from scanisaur.audit.report import audit
from scanisaur.catalog import Catalog, Column, Partition, Partitioning, Table
from scanisaur.catalog.connectors import QueryRun
from scanisaur.engine.check import Policy, check
from scanisaur.engine.result import Verdict

WAREHOUSE = "bigquery:p:US"
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
    logged = [
        decision(
            check(sql, CATALOG),
            sql,
            source="cli",
            warehouse=WAREHOUSE,
            now=NOW - timedelta(minutes=1),
        )
    ]
    report = audit(runs, CATALOG, logged, warehouse=WAREHOUSE, since=NOW - timedelta(days=1))
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
        decision(blocked, sql, source="hook", warehouse=WAREHOUSE, now=NOW - timedelta(hours=2)),
        decision(blocked, sql, source="hook", warehouse=WAREHOUSE, now=NOW + timedelta(minutes=29)),
    ]
    runs = [_run(sql, 0, 0), _run(sql, 30, 0)]
    report = audit(runs, CATALOG, logged, warehouse=WAREHOUSE, since=NOW, top=1)
    assert (report.unchecked_runs, report.ran_after_block) == (1, 1)
    assert report.flagged[0].verdict is Verdict.BLOCK


def test_empty_history() -> None:
    report = audit([], CATALOG, [], warehouse=WAREHOUSE, since=NOW)
    assert (report.runs, report.flagged, report.tables) == (0, (), ())


def test_only_checks_for_the_audited_warehouse_match() -> None:
    sql = SELECT_STAR.format("click")
    result = check(sql, CATALOG)
    matching = decision(result, sql, source="cli", warehouse=WAREHOUSE, now=NOW)
    foreign = matching.model_copy(
        update={"warehouse": "bigquery:other:US", "verdict": Verdict.BLOCK}
    )
    unscoped = matching.model_copy(update={"warehouse": None})
    run = _run(sql, 0, GIB)
    report = audit([run], CATALOG, [foreign, unscoped], since=NOW, warehouse=WAREHOUSE)
    assert (report.unchecked_runs, report.ran_after_block) == (1, 0)
    report = audit([run], CATALOG, [matching, foreign], since=NOW, warehouse=WAREHOUSE)
    assert (report.unchecked_runs, report.ran_after_block) == (0, 0)


def test_relative_date_is_evaluated_for_each_execution() -> None:
    table = Table(
        "p",
        "d",
        "daily",
        (Column("day", "DATE"), Column("value", "INT64")),
        size_bytes=410 * GIB,
        row_count=410 * GIB // 16,
        partitioning=Partitioning("day", "DAY"),
        partitions=(Partition("20261003", 10 * GIB), Partition("20261004", 400 * GIB)),
    )
    catalog = Catalog((table,), "p", "d")
    sql = "SELECT SUM(value) FROM daily WHERE day = CURRENT_DATE()"
    runs = [_run(sql, 0, 10 * GIB), _run(sql, 24 * 60, 400 * GIB)]
    policy = Policy(warn_bytes=100 * GIB, block_bytes=None)
    report = audit(runs, catalog, [], since=NOW, warehouse=WAREHOUSE, policy=policy)
    reverse = audit(reversed(runs), catalog, [], since=NOW, warehouse=WAREHOUSE, policy=policy)
    assert report == reverse
    assert (report.flagged_runs, report.flagged_bytes) == (1, 400 * GIB)


def test_failed_attempts_do_not_count_as_successful_runs() -> None:
    sql = SELECT_STAR.format("click")
    blocked = decision(check(sql, CATALOG), sql, source="cli", warehouse=WAREHOUSE, now=NOW)
    blocked = blocked.model_copy(update={"verdict": Verdict.BLOCK})
    runs = [
        QueryRun("rejected", NOW, None, sql, 0, "invalidQuery"),
        QueryRun("canceled", NOW, None, sql, GIB, "stopped"),
        QueryRun("hidden-failure", NOW, None, sql, None, "internalError"),
        _run(sql, 0, GIB),
    ]
    report = audit(runs, CATALOG, [blocked], since=NOW, warehouse=WAREHOUSE)
    assert (report.runs, report.flagged_runs, report.ran_after_block) == (1, 1, 1)
    assert (report.failed_runs, report.failed_bytes, report.failed_after_block) == (3, GIB, 3)
    assert report.failed_unknown_billing_runs == 1
    assert report.bytes_billed == GIB
    assert report.tables[0].runs == 1


def test_unknown_billing_is_reported_and_ranked_separately() -> None:
    runs = [
        QueryRun("unknown", NOW, None, SELECT_STAR.format("click"), None),
        _run("SELECT * FROM users", 0, GIB),
        _run(CLEAN, 0, 0),
    ]
    report = audit(runs, CATALOG, [], since=NOW, warehouse=WAREHOUSE)
    assert report.bytes_billed == report.flagged_bytes == GIB
    assert report.unknown_billing_runs == report.flagged_unknown_billing_runs == 1
    assert report.flagged[0].unknown_billing_runs == 1
    assert report.flagged[0].bytes_billed == 0
    assert report.tables[0].name == "p.d.events"
    assert report.tables[0].unknown_billing_runs == report.rules[0].unknown_billing_runs == 1
    assert report.model_dump()["unknown_billing_runs"] == 1
