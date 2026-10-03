import re
from pathlib import Path

import pytest
from sqlglot import exp

from scanisaur.catalog import Catalog, Column, Partitioning, Table
from scanisaur.catalog.fixtures import load_catalog
from scanisaur.engine import check as check_module
from scanisaur.engine.check import (
    Policy,
    check,
    fingerprint,
    new_check_id,
    shape,
    shape_fingerprint,
    tag_for,
)
from scanisaur.engine.resolve import ResolveError
from scanisaur.engine.result import CheckResult, Finding, Severity, Verdict
from scanisaur.engine.rules import UNANALYZABLE, UNKNOWN_IDENTIFIER, WRITE_STATEMENT

CATALOG = load_catalog(Path(__file__).parents[1] / "golden" / "catalog.yaml")
CHECK_ID = re.compile(r"^chk_[0-9a-hjkmnp-tv-z]{18}$")
FINGERPRINT = re.compile(r"^q_[0-9a-hjkmnp-tv-z]{20}$")


class TestCheckId:
    def test_format(self) -> None:
        assert CHECK_ID.match(new_check_id())

    def test_sorts_by_time(self) -> None:
        earlier = new_check_id(now_ms=1_000, randomness=2**40 - 1)
        later = new_check_id(now_ms=1_001, randomness=0)
        assert earlier < later

    def test_largest_value_fits(self) -> None:
        assert CHECK_ID.match(new_check_id(now_ms=2**48 - 1, randomness=2**40 - 1))

    def test_deterministic_for_the_same_inputs(self) -> None:
        assert new_check_id(now_ms=5, randomness=7) == new_check_id(now_ms=5, randomness=7)


class TestTag:
    SQL = "SELECT user_id FROM events WHERE event_date = '2026-09-01'"

    def test_format(self) -> None:
        assert FINGERPRINT.match(fingerprint(self.SQL))
        assert tag_for(self.SQL) == f"/* scanisaur:{fingerprint(self.SQL)} */"

    @pytest.mark.parametrize(
        "same",
        [
            f"  {SQL}\n",
            f"{SQL} /* scanisaur:q_p9j93wazgxbtjn0zmf45 */",
            f"/* scanisaur:q_p9j93wazgxbtjn0zmf45 */\n{SQL}",
            f"/*scanisaur:chk_01m3wk05rsjdxrp063*/ {SQL} /* scanisaur:q_a */ /* scanisaur:q_b */\n",
        ],
    )
    def test_ignores_surrounding_tags_and_whitespace(self, same: str) -> None:
        assert fingerprint(same) == fingerprint(self.SQL)

    def test_retagging_keeps_the_tag(self) -> None:
        for sql in (self.SQL, "CALL ds.proc(1)", "BEGIN SELECT 1; END", "SELECT 'unterminated"):
            assert tag_for(f"{sql} {tag_for(sql)}") == tag_for(sql)

    @pytest.mark.parametrize(
        ("sql", "other"),
        [
            # Each pair runs differently, or misses BigQuery's cache, so the tags differ.
            ("SELECT  user_id FROM events", "SELECT user_id FROM events"),
            ("SELECT user_id FROM events -- note", "SELECT user_id FROM events"),
            ("#legacySQL\nSELECT a FROM [p:d.t]", "SELECT a FROM [p:d.t]"),
            (r"SELECT 'a\x41'", r"SELECT 'a\\x41'"),
            ("SELECT 'x\x1eVAR\x1fy'", "SELECT 'x' y"),
            ("SELECT '/* scanisaur:q_x */'", "SELECT ''"),
            ("SELECT user_id FROM Events", "SELECT user_id FROM events"),
        ],
    )
    def test_any_other_change_counts(self, sql: str, other: str) -> None:
        assert fingerprint(sql) != fingerprint(other)

    def test_unpaired_surrogate(self) -> None:
        assert FINGERPRINT.match(fingerprint("SELECT '\ud800'"))


class TestCheck:
    def test_pass(self) -> None:
        sql = "SELECT user_id FROM events WHERE event_date = '2026-09-01'"
        result = check(sql, CATALOG, check_id="chk_test")
        assert result.verdict is Verdict.PASS
        assert result.findings == ()
        assert result.tables == ("proj.analytics.events",)
        assert result.check_id == "chk_test"
        assert result.tag == tag_for(sql)
        assert result.schema_version == 1

    def test_query_in_parentheses(self) -> None:
        result = check("(SELECT user_id FROM events WHERE event_date = '2026-09-01')", CATALOG)
        assert result.verdict is Verdict.PASS
        assert result.tables == ("proj.analytics.events",)

    def test_generates_a_check_id(self) -> None:
        result = check("SELECT 1", CATALOG)
        assert CHECK_ID.match(result.check_id)

    def test_same_sql_same_tag(self) -> None:
        # Each check has its own ID, but the tag repeats so BigQuery's cache still works.
        first, second = (check("SELECT user_id FROM events", CATALOG) for _ in range(2))
        assert first.check_id != second.check_id
        assert first.tag == second.tag

    def test_block(self) -> None:
        result = check("SELECT nope FROM events", CATALOG)
        assert result.verdict is Verdict.BLOCK
        assert [f.rule for f in result.findings] == [UNKNOWN_IDENTIFIER]

    def test_unanalyzable_warns_by_default(self) -> None:
        result = check("SELECT FROM WHERE", CATALOG)
        assert result.verdict is Verdict.WARN
        (finding,) = result.findings
        assert (finding.rule, finding.severity) == (UNANALYZABLE, Severity.WARN)
        assert finding.fix == "Fix the syntax error."

    def test_unanalyzable_blocks_when_failing_closed(self) -> None:
        result = check("CALL proc()", CATALOG, policy=Policy(fail_mode="closed"))
        assert result.verdict is Verdict.BLOCK
        assert result.findings[0].severity is Severity.BLOCK

    def test_writes_blocked_when_read_only(self) -> None:
        result = check("DELETE FROM users WHERE TRUE", CATALOG)
        assert [f.rule for f in result.findings] == [WRITE_STATEMENT]
        assert result.tables == ()

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1 FROM events; DROP TABLE events",
            "DECLARE x INT64; DELETE FROM events WHERE TRUE",
            "BEGIN; UPDATE users SET country = 'US' WHERE TRUE; COMMIT",
        ],
    )
    def test_writes_blocked_among_several_statements(self, sql: str) -> None:
        result = check(sql, CATALOG)
        assert result.verdict is Verdict.BLOCK
        assert [f.rule for f in result.findings] == [WRITE_STATEMENT, UNANALYZABLE]

    def test_several_statements_warn_when_writes_are_allowed(self) -> None:
        result = check("SELECT 1; DROP TABLE events", CATALOG, policy=Policy(read_only=False))
        assert [f.rule for f in result.findings] == [UNANALYZABLE]

    def test_unchecked_writes_are_unanalyzable_when_allowed(self) -> None:
        sql = "MERGE users u USING events e ON u.user_id = e.user_id WHEN MATCHED THEN DELETE"
        result = check(sql, CATALOG, policy=Policy(read_only=False))
        (finding,) = result.findings
        assert finding.rule == UNANALYZABLE
        assert finding.message == "Names in MERGE statements aren't checked yet."

    def test_writes_checked_when_allowed(self) -> None:
        result = check(
            "INSERT INTO users (user_id) "
            "SELECT user_id FROM events WHERE event_date > '2026-09-01'",
            CATALOG,
            policy=Policy(read_only=False),
        )
        assert result.verdict is Verdict.PASS
        assert result.tables == ("proj.analytics.events",)

    def test_resolve_error_is_unanalyzable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(*_args: object) -> None:
            raise ResolveError("boom")

        monkeypatch.setattr(check_module, "resolve", fail)
        result = check("SELECT 1", CATALOG)
        (finding,) = result.findings
        assert finding.rule == UNANALYZABLE
        assert finding.message == "Names could not be resolved: boom."
        assert finding.fix == "Check the table and column names by hand before running it."


class TestTooDeep:
    """SQL nested deeper than Python's stack allows is unanalyzable, not a crash."""

    WHERE = " FROM events WHERE event_date = '2026-09-01'"

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT " + "(" * 200 + "user_id" + ")" * 200 + WHERE,
            "SELECT " + "CASE WHEN user_id > 1 THEN " * 200 + "1" + " END" * 200 + WHERE,
            "SELECT * FROM " + "(SELECT * FROM " * 500 + "events" + ")" * 500 + WHERE,
        ],
        ids=["parentheses", "case", "subqueries"],
    )
    def test_warns_by_default(self, sql: str) -> None:
        result = check(sql, CATALOG)
        assert result.verdict is Verdict.WARN
        (finding,) = result.findings
        assert (finding.rule, finding.severity) == (UNANALYZABLE, Severity.WARN)
        assert finding.message == "The SQL is nested too deeply to analyze."
        assert result.tables == ()
        assert result.estimate is None
        assert result.tag == tag_for(sql)

    def test_blocks_when_failing_closed(self) -> None:
        sql = "SELECT " + "(" * 200 + "user_id" + ")" * 200 + self.WHERE
        result = check(sql, CATALOG, policy=Policy(fail_mode="closed"))
        assert result.verdict is Verdict.BLOCK
        assert result.findings[0].severity is Severity.BLOCK

    def test_too_deep_after_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(*_args: object) -> None:
            raise RecursionError

        monkeypatch.setattr(check_module, "estimate", fail)
        result = check("SELECT user_id" + self.WHERE, CATALOG)
        assert [f.rule for f in result.findings] == [UNANALYZABLE]
        assert result.estimate is None

    def test_shallower_nesting_is_checked(self) -> None:
        sql = "SELECT " + "(" * 20 + "usr_id" + ")" * 20 + self.WHERE
        assert [f.rule for f in check(sql, CATALOG).findings] == [UNKNOWN_IDENTIFIER]


class TestCatalogEdges:
    def test_unparsable_catalog_type_is_unanalyzable(self) -> None:
        table = Table("p", "d", "t", (Column("a", "INT64"), Column("b", "NOT A TYPE")))
        result = check("SELECT * FROM t", Catalog((table,), "p", "d"))
        (finding,) = result.findings
        assert finding.rule == UNANALYZABLE
        assert finding.message.startswith("Names could not be resolved:")

    def test_partitiondate_needs_daily_ingestion_partitions(self) -> None:
        hourly = Table(
            "p", "d", "t", (Column("a", "INT64"),), partitioning=Partitioning(None, "HOUR")
        )
        catalog = Catalog((hourly,), "p", "d")
        sql = "SELECT a FROM t WHERE _PARTITIONTIME >= TIMESTAMP '2026-09-01'"
        assert check(sql, catalog).findings == ()
        (finding,) = check("SELECT a FROM t WHERE _PARTITIONDATE IS NULL", catalog).findings
        assert finding.message.startswith("`_PARTITIONDATE` only exists on tables partitioned")


class TestDefaults:
    TABLE = Table("proj", "analytics", "events", (Column("user_id", "STRING"),))

    def test_partial_name_without_defaults(self) -> None:
        result = check("SELECT user_id FROM events", Catalog(tables=(self.TABLE,)))
        (finding,) = result.findings
        assert finding.fix == "Write the table as `project.dataset.table`."

    def test_two_part_name_with_default_project(self) -> None:
        catalog = Catalog(tables=(self.TABLE,), default_project="proj")
        assert check("SELECT user_id FROM analytics.events", catalog).verdict is Verdict.PASS
        (finding,) = check("SELECT user_id FROM analytics.evnts", catalog).findings
        assert finding.fix == "Did you mean `analytics.events`?"

    def test_fully_qualified_without_defaults(self) -> None:
        catalog = Catalog(tables=(self.TABLE,))
        result = check("SELECT user_id FROM `proj.analytics.events`", catalog)
        assert result.verdict is Verdict.PASS


def _unknown_names(result: CheckResult) -> list[Finding]:
    """SCN001 findings only; cost rules may also warn about these queries."""
    return [finding for finding in result.findings if finding.rule == UNKNOWN_IDENTIFIER]


class TestUnnest:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT nope FROM events e, UNNEST(SPLIT(e.event_name)) AS s",  # a function result
            "SELECT nope FROM events e, UNNEST(e.device.os)",  # a nested path
            "SELECT nope FROM events e, UNNEST(e.event_name)",  # not an array
            "SELECT nope FROM events e, UNNEST(e.params, e.params)",
            "SELECT nope FROM UNNEST([STRUCT(1 AS x)])",
            "WITH c AS (SELECT params FROM events) SELECT nope FROM c, UNNEST(c.params)",
        ],
    )
    def test_unknown_elements_stand_down(self, sql: str) -> None:
        assert _unknown_names(check(sql, CATALOG)) == []

    def test_two_unaliased_unnests(self) -> None:
        # sqlglot gives both the same empty name and failed with "Alias already used".
        sql = "SELECT e.user_id, nope FROM events e, UNNEST(e.params), UNNEST([1, 2])"
        result = check(sql, CATALOG)
        assert [f.message for f in _unknown_names(result)] == [
            "Column `nope` does not exist in `proj.analytics.events`, the UNNEST or the UNNEST."
        ]

    @staticmethod
    def _catalog(type_: str) -> Catalog:
        table = Table("proj", "analytics", "t", (Column("xs", type_),))
        return Catalog(tables=(table,), default_project="proj", default_dataset="analytics")

    def test_unnamed_struct_fields_stand_down(self) -> None:
        catalog = self._catalog("ARRAY<STRUCT<INT64>>")
        assert _unknown_names(check("SELECT nope FROM t, UNNEST(xs)", catalog)) == []

    def test_catalog_array_of_scalars_exposes_only_its_alias(self) -> None:
        catalog = self._catalog("ARRAY<STRING>")
        (finding,) = _unknown_names(check("SELECT x, nope FROM t, UNNEST(xs) AS x", catalog))
        assert finding.message == "Column `nope` does not exist in `proj.analytics.t` or `x`."

    def test_offset_without_alias_is_named_offset(self) -> None:
        sql = "SELECT key, offset FROM events, UNNEST(params) WITH OFFSET"
        assert _unknown_names(check(sql, CATALOG)) == []

    def test_correlated_unnest_of_outer_column(self) -> None:
        sql = "SELECT (SELECT COUNT(*) FROM UNNEST(e.params) p WHERE p.kye = 'x') FROM events e"
        (finding,) = _unknown_names(check(sql, CATALOG))
        assert finding.fix == "Did you mean `p.key`?"

    def test_field_in_two_unnests_is_ambiguous(self) -> None:
        sql = "SELECT key FROM events e, UNNEST(e.params) a, UNNEST(e.params) b"
        (finding,) = _unknown_names(check(sql, CATALOG))
        assert finding.message == "Column `key` is ambiguous: it exists in `a`, `b`."
        assert finding.fix == "Qualify it, for example `a.key`."

    def test_wrong_alias_points_at_the_unnest_field(self) -> None:
        sql = "SELECT e.key FROM events e, UNNEST(e.params) AS p"
        (finding,) = _unknown_names(check(sql, CATALOG))
        assert finding.fix == "Did you mean `p.key`?"

    def test_unknown_alias_lists_only_written_aliases(self) -> None:
        sql = "SELECT q.key FROM events e, UNNEST(e.params) AS p"
        (finding,) = _unknown_names(check(sql, CATALOG))
        assert finding.fix == "Use one of: `e`, `p`."


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "/* scanisaur:q_x */ SELECT a FROM t WHERE b = 'x@y.com' AND c IN (1, 2) -- hi",
            "SELECT a FROM t WHERE b = ? AND c IN (?)",
        ),
        ("SELECT a FROM t WHERE c IN (1, d)", "SELECT a FROM t WHERE c IN (?, d)"),
        ("SELECT 1; SELECT 'a'", "SELECT ?; SELECT ?"),
        (
            "SELECT a FROM t WHERE e = r'a@x.com' AND b = b'xy' AND h = 0xFF AND c IN (r'q')",
            "SELECT a FROM t WHERE e = ? AND b = ? AND h = ? AND c IN (?)",
        ),
        ("SELECT TRUE, FALSE, NULL, -1, +2, -1.5e3", "SELECT ?, ?, ?, ?, ?, ?"),
        ("SELECT a FROM t WHERE c IN (-1, 2, -3)", "SELECT a FROM t WHERE c IN (?)"),
        ("SELECT a FROM t WHERE c IN (TRUE, FALSE, NULL)", "SELECT a FROM t WHERE c IN (?)"),
        ("SELECT a FROM t WHERE c IN (-1, -d)", "SELECT a FROM t WHERE c IN (?, -d)"),
        ("SELECT -d FROM t", "SELECT -d FROM t"),
        # SQL that doesn't parse is shaped by pattern.
        (
            "SELEC a FROM t WHERE b = 'it\\'s -- x' and c=1.5e3 # note",
            "SELEC a FROM t WHERE b = ? and c=?",
        ),
    ],
)
def test_shape(sql: str, expected: str) -> None:
    assert shape(sql) == expected


def test_shape_fingerprint() -> None:
    same = (
        shape_fingerprint("SELECT a FROM t WHERE b = 1"),
        shape_fingerprint("SELECT a FROM t\nWHERE b = 2"),
    )
    assert same[0] == same[1]
    assert re.fullmatch(r"s_[0-9a-z]{20}", same[0])
    assert shape_fingerprint("SELECT b FROM t WHERE b = 1") != same[0]


def test_shape_never_keeps_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    # A literal kind the type list misses falls back to the pattern shape.
    monkeypatch.setattr("scanisaur.engine.check._LITERALS", (exp.Literal,))
    assert shape("SELECT a FROM t WHERE e = r'a@x.com'") == "SELECT a FROM t WHERE e = r?"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("SELECT a FROM t WHERE active = TRUE", "SELECT a FROM t WHERE active = FALSE"),
        ("SELECT a FROM t WHERE id = -1", "SELECT a FROM t WHERE id = 2"),
        ("SELECT a FROM t WHERE id IN (-1, -2)", "SELECT a FROM t WHERE id IN (1, 2, 3)"),
    ],
)
def test_shape_fingerprint_normalizes_constants(left: str, right: str) -> None:
    assert shape_fingerprint(left) == shape_fingerprint(right)
