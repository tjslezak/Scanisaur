import re
from pathlib import Path

import pytest

from scanisaur.catalog import Catalog, Column, Table
from scanisaur.catalog.fixtures import load_catalog
from scanisaur.engine import check as check_module
from scanisaur.engine.check import Policy, check, new_check_id, tag_for
from scanisaur.engine.resolve import ResolveError
from scanisaur.engine.result import Severity, Verdict
from scanisaur.engine.rules import UNANALYZABLE, UNKNOWN_IDENTIFIER, WRITE_STATEMENT

CATALOG = load_catalog(Path(__file__).parents[1] / "golden" / "catalog.yaml")
CHECK_ID = re.compile(r"^chk_[0-9a-hjkmnp-tv-z]{18}$")


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

    def test_tag(self) -> None:
        assert tag_for("chk_abc") == "/* scanisaur:chk_abc */"


class TestCheck:
    def test_pass(self) -> None:
        result = check("SELECT user_id FROM events", CATALOG, check_id="chk_test")
        assert result.verdict is Verdict.PASS
        assert result.findings == ()
        assert result.tables == ("proj.analytics.events",)
        assert result.check_id == "chk_test"
        assert result.tag == "/* scanisaur:chk_test */"
        assert result.schema_version == 1

    def test_generates_a_check_id(self) -> None:
        result = check("SELECT 1", CATALOG)
        assert CHECK_ID.match(result.check_id)
        assert result.tag == tag_for(result.check_id)

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

    def test_writes_checked_when_allowed(self) -> None:
        result = check(
            "INSERT INTO users (user_id) SELECT user_id FROM events",
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
        assert finding.fix == "Send one complete SQL query."


class TestDefaults:
    TABLE = Table("proj", "analytics", "events", (Column("user_id", "STRING"),))

    def test_partial_name_without_defaults(self) -> None:
        result = check("SELECT user_id FROM events", Catalog(tables=(self.TABLE,)))
        (finding,) = result.findings
        assert finding.fix == "Qualify the table as `project.dataset.table`."

    def test_two_part_name_with_default_project(self) -> None:
        catalog = Catalog(tables=(self.TABLE,), default_project="proj")
        assert check("SELECT user_id FROM analytics.events", catalog).verdict is Verdict.PASS
        (finding,) = check("SELECT user_id FROM analytics.evnts", catalog).findings
        assert finding.fix == "Did you mean `analytics.events`?"

    def test_fully_qualified_without_defaults(self) -> None:
        catalog = Catalog(tables=(self.TABLE,))
        result = check("SELECT user_id FROM `proj.analytics.events`", catalog)
        assert result.verdict is Verdict.PASS
