import json
import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scanisaur.catalog.model import Catalog, Column, Table
from scanisaur.catalog.source import FixtureSource, SearchHit, Snapshot
from scanisaur.engine.check import Policy
from scanisaur.engine.result import Severity
from scanisaur.tools import (
    MAX_COLUMNS,
    MAX_DESCRIBE_TABLES,
    MAX_FINDINGS,
    check_sql,
    schema_describe,
    schema_search,
    summary,
)

GOLDEN = Path(__file__).parent / "golden"
SOURCE = FixtureSource(GOLDEN / "catalog.yaml")
SNAPSHOT = SOURCE.current()
NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
#: The M3 exit criterion: check_sql responses stay under this many tokens at p95.
TOKEN_BUDGET = 500


class _ListSource:
    """A source that returns fixed hits, for checking how they are grouped."""

    def __init__(self, catalog: Catalog, hits: list[SearchHit]) -> None:
        self._snapshot = Snapshot(catalog, "test")
        self._hits = hits

    def current(self) -> Snapshot:
        return self._snapshot

    def search(self, query: str, limit: int) -> list[SearchHit]:
        return self._hits[:limit]


class TestSchemaSearch:
    def test_groups_columns_under_their_table(self) -> None:
        response = schema_search("user", SOURCE)
        users = next(t for t in response.tables if t.table == "proj.analytics.users")
        assert users.columns == ("user_id",)
        assert users.rows == 5_000_000
        assert users.size_bytes == 900_000_000

    def test_partitioning_and_clustering(self) -> None:
        (events, *_) = schema_search("events", SOURCE, limit=1).tables
        assert events.table == "proj.analytics.events"
        assert events.partitioning == "event_date DAY"
        assert events.clustering == ("user_id",)

    def test_required_filter_and_wildcard(self) -> None:
        orders = schema_search("orders", SOURCE, limit=1).tables[0]
        assert orders.partitioning == "order_date DAY, filter required"
        shards = schema_describe(["proj.ga4.events_*"], SNAPSHOT.catalog).tables[0]
        assert shards.partitioning == "_TABLE_SUFFIX, wildcard shards"

    def test_limit_counts_tables_not_columns(self) -> None:
        hits = [
            SearchHit("proj.analytics.users", "user_id", 2.0),
            SearchHit("proj.analytics.users", "country", 2.0),
            SearchHit("proj.analytics.events", "user_id", 1.0),
        ]
        response = schema_search("x", _ListSource(SNAPSHOT.catalog, hits), limit=1)
        assert [(t.table, t.columns) for t in response.tables] == [
            ("proj.analytics.users", ("user_id", "country"))
        ]

    def test_hits_for_unknown_tables_are_skipped(self) -> None:
        hits = [SearchHit("proj.gone.table", None, 9.0), SearchHit("proj.analytics.users", None, 1)]
        response = schema_search("x", _ListSource(SNAPSHOT.catalog, hits))
        assert [t.table for t in response.tables] == ["proj.analytics.users"]

    @pytest.mark.parametrize(("limit", "expected"), [(0, 1), (-5, 1), (1000, 16)])
    def test_limit_is_clamped(self, limit: int, expected: int) -> None:
        hits = [SearchHit(t.qualified_name, None, 1.0) for t in SNAPSHOT.catalog.tables]
        response = schema_search("x", _ListSource(SNAPSHOT.catalog, hits), limit=limit)
        assert len(response.tables) == expected


class TestSchemaDescribe:
    def test_names_full_partial_and_quoted(self) -> None:
        names = ["users", "analytics.Orders", "`proj.web.trends`"]
        response = schema_describe(names, SNAPSHOT.catalog)
        assert [t.table for t in response.tables] == [
            "proj.analytics.users",
            "proj.analytics.Orders",
            "proj.web.trends",
        ]
        assert response.unknown == ()

    @pytest.mark.parametrize("name", ["nope", "a.b.c.d", "proj..users", ""])
    def test_unknown(self, name: str) -> None:
        response = schema_describe([name], SNAPSHOT.catalog)
        assert response.tables == ()
        assert response.unknown == (name,)

    def test_fields(self) -> None:
        (users,) = schema_describe(["users"], SNAPSHOT.catalog).tables
        assert users.kind == "TABLE"
        assert users.keys == (("user_id",),)
        assert [(c.name, c.type) for c in users.columns][:2] == [
            ("user_id", "STRING"),
            ("country", "STRING"),
        ]
        assert users.omitted_columns == 0

    def test_at_most_five_tables(self) -> None:
        response = schema_describe(["users"] * 7, SNAPSHOT.catalog)
        assert len(response.tables) == MAX_DESCRIBE_TABLES

    def test_wide_table_shows_key_columns_first(self) -> None:
        columns = tuple(Column(f"c{i:03}", "STRING") for i in range(300))
        wide = Table(
            "p",
            "d",
            "wide",
            columns=columns,
            clustering=("c299",),
            keys=(("c250",),),
        )
        (table,) = schema_describe(["p.d.wide"], Catalog((wide,))).tables
        names = [c.name for c in table.columns]
        assert len(names) == MAX_COLUMNS
        assert names[:3] == ["c250", "c299", "c000"]
        assert table.omitted_columns == 100


class TestCheckSql:
    def test_carries_the_snapshot(self) -> None:
        result = check_sql("SELECT user_id FROM users", SNAPSHOT)
        assert result.snapshot_id == SNAPSHOT.snapshot_id
        assert result.omitted_findings == 0

    def test_keeps_the_most_severe_findings_in_sql_order(self) -> None:
        unknown = ", ".join(f"nope{i}" for i in range(8))
        sql = f"SELECT *, {unknown} FROM users"
        policy = Policy(rules={"SCN001": "warn"})
        full = check_sql(sql, Snapshot(SNAPSHOT.catalog, "s"), policy, now=NOW)
        assert len(full.findings) == MAX_FINDINGS
        assert full.omitted_findings == 3
        assert all(f.severity is Severity.WARN for f in full.findings)
        columns = [f.column for f in full.findings]
        assert columns == sorted(columns, key=lambda c: c or 0)

    def test_block_findings_win_over_warnings(self) -> None:
        sql = "SELECT " + ", ".join(f"nope{i}" for i in range(6)) + " FROM Orders"
        result = check_sql(sql, SNAPSHOT, now=NOW)
        assert result.omitted_findings > 0
        assert {f.severity for f in result.findings} == {Severity.BLOCK}


class TestSummary:
    def test_pass(self) -> None:
        result = check_sql("SELECT day FROM calendar", SNAPSHOT, now=NOW)
        line = summary(result)
        assert line.startswith("pass: 0 findings, 10.5 MB billed")
        assert line.endswith(f"Run it with {result.tag} at the start or end.")

    def test_block_counts_omitted_findings(self) -> None:
        sql = "SELECT " + ", ".join(f"nope{i}" for i in range(7)) + " FROM users"
        line = summary(check_sql(sql, SNAPSHOT, now=NOW))
        assert line == "block: 7 findings. Apply the fixes and check again before running it."


def test_check_sql_responses_fit_the_token_budget() -> None:
    """Characters / 4 approximates tokens: the structured result plus its text line."""
    sizes = []
    for case in sorted(GOLDEN.glob("*/*.sql")):
        sql = case.read_text(encoding="utf-8")
        first_line = sql.partition("\n")[0]
        policy = (
            Policy(**json.loads(first_line.removeprefix("-- policy:")))
            if first_line.startswith("-- policy:")
            else Policy()
        )
        result = check_sql(sql, SNAPSHOT, policy, now=NOW)
        sizes.append((len(result.model_dump_json()) + len(summary(result))) / 4)
    sizes.sort()
    p95 = sizes[math.ceil(0.95 * len(sizes)) - 1]
    assert p95 < TOKEN_BUDGET, f"p95 response is {p95:.0f} tokens"
