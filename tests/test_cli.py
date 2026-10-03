import json
import re
import runpy
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from scanisaur import __version__, cli
from scanisaur.cli import EXIT_BLOCKED, EXIT_ERROR, EXIT_OK, app
from scanisaur.engine.result import Estimate

runner = CliRunner()


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"scanisaur {__version__}"


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"scanisaur {__version__}"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output


def test_python_dash_m(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.argv", ["scanisaur", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("scanisaur", run_name="__main__")
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"scanisaur {__version__}"


CATALOG = str(Path(__file__).parent / "golden" / "catalog.yaml")


def run_check(*args: str, sql: str | None = None) -> Result:
    return runner.invoke(app, ["check", "--catalog", CATALOG, *args], input=sql)


class TestCheckCommand:
    def test_pass_from_stdin(self) -> None:
        result = run_check(sql="SELECT user_id FROM events WHERE event_date = '2026-09-01'")
        assert result.exit_code == EXIT_OK
        lines = result.stdout.splitlines()
        assert lines[0] == "pass: 0 findings · reads proj.analytics.events"
        assert lines[-2] == "estimate: 10.5 MB-356.4 GB billed, <$0.01-$2.03 (low confidence)"
        assert re.fullmatch(r"tag: /\* scanisaur:q_\w{20} \*/", lines[-1])

    def test_nearly_equal_range_is_shown_once(self) -> None:
        estimate = Estimate(bytes_low=10_485_760, bytes_high=10_500_000, confidence="low")
        assert cli._estimate(estimate) == "10.5 MB billed (low confidence)"

    def test_estimate_without_dollars(self) -> None:
        result = run_check("--capacity-pricing", sql="SELECT score FROM web.trends")
        lines = result.stdout.splitlines()
        assert lines[-2] == "estimate: 352.3 MB billed (high confidence)"

    def test_block_from_file(self, tmp_path: Path) -> None:
        path = tmp_path / "query.sql"
        path.write_text("SELECT usr_id\nFROM events", encoding="utf-8")
        result = run_check(str(path))
        assert result.exit_code == EXIT_BLOCKED
        assert result.stdout.splitlines()[:3] == [
            "block: 1 finding · reads proj.analytics.events",
            "  1:8     SCN001  block  Column `usr_id` does not exist in `proj.analytics.events`.",
            "                  fix:   Did you mean `user_id`?",
        ]

    def test_json(self) -> None:
        result = run_check("--json", sql="SELECT nope FROM events")
        assert result.exit_code == EXIT_BLOCKED
        payload = json.loads(result.stdout)
        assert payload["schema_version"] == 1
        assert payload["verdict"] == "block"
        assert payload["findings"][0]["rule"] == "SCN001"

    def test_warning_passes_unless_strict(self) -> None:
        assert run_check(sql="DECLARE x INT64").exit_code == EXIT_OK
        strict = run_check("--strict", sql="DECLARE x INT64")
        assert strict.exit_code == EXIT_BLOCKED
        assert "  -       SCN000  warn   DECLARE statements can't be checked." in strict.stdout

    def test_deeply_nested_sql_warns(self) -> None:
        sql = "SELECT " + "(" * 200 + "1" + ")" * 200
        result = run_check(sql=sql)
        assert result.exit_code == EXIT_OK
        assert "SCN000" in result.stdout

    def test_fail_closed(self) -> None:
        result = run_check("--fail-closed", sql="DECLARE x INT64")
        assert result.exit_code == EXIT_BLOCKED
        assert result.stdout.startswith("block:")

    def test_allow_writes(self) -> None:
        sql = "INSERT INTO users (user_id) SELECT user_id FROM events"
        assert run_check(sql=sql).exit_code == EXIT_BLOCKED
        assert run_check("--allow-writes", sql=sql).exit_code == EXIT_OK

    def test_invalid_catalog(self, tmp_path: Path) -> None:
        path = tmp_path / "catalog.yaml"
        path.write_text("tables: []\nextra: 1\n", encoding="utf-8")
        result = runner.invoke(app, ["check", "--catalog", str(path)], input="SELECT 1")
        assert result.exit_code == EXIT_ERROR
        assert result.stderr.startswith(f"error: {path}")

    def test_missing_sql_file(self, tmp_path: Path) -> None:
        result = run_check(str(tmp_path / "missing.sql"))
        assert result.exit_code == EXIT_ERROR
        assert "No such file" in result.stderr

    def test_sql_not_utf8(self, tmp_path: Path) -> None:
        path = tmp_path / "query.sql"
        path.write_bytes(b"SELECT \xff")
        result = run_check(str(path))
        assert result.exit_code == EXIT_ERROR
        assert result.stderr.startswith(f"error: {path}: 'utf-8' codec")

    def test_catalog_not_utf8(self, tmp_path: Path) -> None:
        path = tmp_path / "catalog.yaml"
        path.write_bytes(b"\xff")
        result = runner.invoke(app, ["check", "--catalog", str(path)], input="SELECT 1")
        assert result.exit_code == EXIT_ERROR
        assert "utf-8" in result.stderr

    def test_missing_catalog_is_a_usage_error(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["check", "--catalog", str(tmp_path / "none.yaml")])
        assert result.exit_code == EXIT_ERROR


#: About 347 GB: over the default warn threshold, under the block threshold.
FULL_SCAN = "SELECT user_id FROM events WHERE event_date IS NOT NULL"


class TestPolicyFile:
    def test_config_option(self, tmp_path: Path) -> None:
        config = tmp_path / "policy.yaml"
        config.write_text("policy: {block_bytes: 300GB}\n", encoding="utf-8")
        result = run_check("--config", str(config), sql=FULL_SCAN)
        assert result.exit_code == EXIT_BLOCKED
        assert (
            "SCN010  block  The query would bill 356.4 GB, at or over the block threshold "
            "of 300 GB." in result.stdout
        )

    def test_found_in_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "scanisaur.yaml").write_text("policy: {rules: {SCN010: block}}\n")
        monkeypatch.chdir(tmp_path)
        assert run_check(sql=FULL_SCAN).exit_code == EXIT_BLOCKED

    def test_default_without_a_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = run_check(sql=FULL_SCAN)
        assert result.exit_code == EXIT_OK
        assert "SCN010  warn   The query would bill 356.4 GB" in result.stdout

    def test_flags_override_the_file(self, tmp_path: Path) -> None:
        config = tmp_path / "scanisaur.yaml"
        config.write_text("policy: {read_only: true, fail_mode: open}\n", encoding="utf-8")
        sql = "INSERT INTO users (user_id) SELECT user_id FROM events"
        assert run_check("--config", str(config), sql=sql).exit_code == EXIT_BLOCKED
        assert run_check("--config", str(config), "--allow-writes", sql=sql).exit_code == EXIT_OK
        closed = run_check("--config", str(config), "--fail-closed", sql="DECLARE x INT64")
        assert closed.exit_code == EXIT_BLOCKED
        priced = run_check("--config", str(config), "--capacity-pricing", sql=FULL_SCAN)
        assert "$" not in priced.stdout.splitlines()[-2]

    def test_file_off_flag_still_applies(self, tmp_path: Path) -> None:
        config = tmp_path / "scanisaur.yaml"
        config.write_text("policy: {read_only: false}\n", encoding="utf-8")
        sql = "INSERT INTO users (user_id) SELECT user_id FROM events"
        assert run_check("--config", str(config), sql=sql).exit_code == EXIT_OK

    def test_invalid_config(self, tmp_path: Path) -> None:
        config = tmp_path / "scanisaur.yaml"
        config.write_text("policy: {warn_bytes: lots}\n", encoding="utf-8")
        result = run_check("--config", str(config), sql="SELECT 1")
        assert result.exit_code == EXIT_ERROR
        assert result.stderr.startswith(f"error: {config}")
        assert "expected a size" in result.stderr

    def test_missing_config_is_a_usage_error(self, tmp_path: Path) -> None:
        result = run_check("--config", str(tmp_path / "none.yaml"), sql="SELECT 1")
        assert result.exit_code == EXIT_ERROR
