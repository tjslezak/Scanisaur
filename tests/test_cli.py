import json
import re
import runpy
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from scanisaur import __version__, cli
from scanisaur.catalog.connectors import ConnectorError, QueryRun
from scanisaur.catalog.fixtures import load_catalog
from scanisaur.catalog.source import Snapshot
from scanisaur.cli import EXIT_BLOCKED, EXIT_ERROR, EXIT_OK, app
from scanisaur.config import BigQueryWarehouse, Config, DuckDBWarehouse, load_config
from scanisaur.engine.check import DEFAULT_POLICY
from scanisaur.engine.result import Estimate
from scanisaur.tools import describe_estimate

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
        assert describe_estimate(estimate) == "10.5 MB billed (low confidence)"

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


def test_serve_rejects_a_bad_catalog(tmp_path: Path) -> None:
    path = tmp_path / "catalog.yaml"
    path.write_text("tables: nope\n", encoding="utf-8")
    result = runner.invoke(app, ["serve", "--catalog", str(path)])
    assert result.exit_code == EXIT_ERROR
    assert "error:" in result.stderr


class TestWarehouse:
    """``refresh`` and ``check`` without ``--catalog``, against a DuckDB warehouse."""

    @pytest.fixture
    def config(self, tmp_path: Path) -> Path:
        import duckdb

        with duckdb.connect(str(tmp_path / "shop.duckdb")) as db:
            db.execute(
                "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, user_id VARCHAR);"
                "CREATE TABLE users (user_id VARCHAR PRIMARY KEY, country VARCHAR)"
            )
        config = tmp_path / "scanisaur.yaml"
        config.write_text(
            "warehouse: {type: duckdb, path: shop.duckdb}\n"
            f"cache: {{path: {tmp_path / 'cache.sqlite'}}}\n",
            encoding="utf-8",
        )
        return config

    def test_refresh(self, config: Path) -> None:
        result = runner.invoke(app, ["refresh", "--config", str(config)])
        assert result.exit_code == EXIT_OK, result.output
        assert re.fullmatch(r"refreshed 2 tables in \d+\.\d s\n", result.stdout)

    def test_check_without_catalog(self, config: Path) -> None:
        sql = "SELECT usr_id FROM orders"
        result = runner.invoke(app, ["check", "--config", str(config), "-"], input=sql)
        assert result.exit_code == EXIT_BLOCKED, result.output
        assert "SCN001" in result.stdout
        assert "shop.main.orders" in result.stdout
        ok = runner.invoke(
            app, ["check", "--config", str(config), "-"], input="SELECT country FROM users LIMIT 5"
        )
        assert ok.exit_code == EXIT_OK, ok.output

    def test_check_without_catalog_or_warehouse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["check", "-"], input="SELECT 1")
        assert result.exit_code == EXIT_ERROR
        assert "give --catalog" in result.stderr

    def test_refresh_without_warehouse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["refresh"])
        assert result.exit_code == EXIT_ERROR
        assert "names no warehouse" in result.stderr

    def test_refresh_of_a_missing_database(self, config: Path) -> None:
        (config.parent / "shop.duckdb").unlink()
        result = runner.invoke(app, ["refresh", "--config", str(config)])
        assert result.exit_code == EXIT_ERROR
        assert "no such DuckDB file" in result.stderr

    def test_table_created_after_refresh(self, config: Path) -> None:
        import duckdb

        assert runner.invoke(app, ["refresh", "--config", str(config)]).exit_code == EXIT_OK
        with duckdb.connect(str(config.parent / "shop.duckdb")) as db:
            db.execute("CREATE TABLE refunds (order_id INTEGER, amount DOUBLE)")
        result = runner.invoke(
            app, ["check", "--config", str(config), "-"], input="SELECT amount FROM refunds LIMIT 5"
        )
        assert result.exit_code == EXIT_OK, result.output
        assert "SCN001" not in result.stdout

    def test_doctor(self, config: Path) -> None:
        result = runner.invoke(app, ["doctor", "--config", str(config)])
        assert result.exit_code == EXIT_OK, result.output
        lines = result.stdout.splitlines()
        assert lines[0].startswith("ok    metadata: 2 tables in ")
        assert lines[1] == "ok    cache: empty: the first check or `scanisaur refresh` fills it"
        runner.invoke(app, ["refresh", "--config", str(config)])
        result = runner.invoke(app, ["doctor", "--config", str(config)])
        assert "ok    cache: 2 tables, refreshed 0 min ago" in result.stdout

    def test_doctor_fails(self, config: Path) -> None:
        (config.parent / "shop.duckdb").unlink()
        result = runner.invoke(app, ["doctor", "--config", str(config)])
        assert result.exit_code == EXIT_BLOCKED
        assert result.stdout.startswith("fail  metadata: ")

    def test_doctor_without_warehouse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == EXIT_ERROR
        assert "names no warehouse" in result.stderr


class TestInit:
    @pytest.fixture(autouse=True)
    def _in_tmp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

    def test_bigquery_with_options(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "init",
                "--warehouse",
                "bigquery",
                "--project",
                "acme",
                "--location",
                "EU",
                "--dataset",
                "analytics",
                "--dataset",
                "yes",
            ],
        )
        assert result.exit_code == EXIT_OK, result.output
        config = load_config(tmp_path / "scanisaur.yaml")
        assert config.warehouse == BigQueryWarehouse(
            type="bigquery", project="acme", location="EU", include_datasets=("analytics", "yes")
        )
        assert config.policy == DEFAULT_POLICY
        assert "SA=scanisaur-catalog@acme.iam.gserviceaccount.com" in result.stdout
        assert "roles/$role" in result.stdout
        assert "scanisaur doctor" in result.stdout

    def test_bigquery_prompts(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["init"], input="\nacme\n\na, b\n")
        assert result.exit_code == EXIT_OK, result.output
        warehouse = load_config(tmp_path / "scanisaur.yaml").warehouse
        assert warehouse == BigQueryWarehouse(
            type="bigquery", project="acme", location="US", include_datasets=("a", "b")
        )

    def test_duckdb(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["init", "--warehouse", "duckdb"], input="shop.duckdb\n")
        assert result.exit_code == EXIT_OK, result.output
        warehouse = load_config(tmp_path / "scanisaur.yaml").warehouse
        assert warehouse == DuckDBWarehouse(type="duckdb", path=tmp_path / "shop.duckdb")
        assert "gcloud" not in result.stdout

    def test_refuses_to_overwrite(self, tmp_path: Path) -> None:
        (tmp_path / "scanisaur.yaml").write_text("policy: {}\n", encoding="utf-8")
        result = runner.invoke(app, ["init", "--warehouse", "duckdb", "--path", "x.duckdb"])
        assert result.exit_code == EXIT_ERROR
        assert "--force" in result.stderr
        assert (tmp_path / "scanisaur.yaml").read_text(encoding="utf-8") == "policy: {}\n"
        forced = runner.invoke(
            app, ["init", "--warehouse", "duckdb", "--path", "x.duckdb", "--force"]
        )
        assert forced.exit_code == EXIT_OK

    def test_unknown_warehouse(self) -> None:
        result = runner.invoke(app, ["init", "--warehouse", "snowflake"])
        assert result.exit_code == 2  # a usage error, from the option's choices
        assert "'snowflake' is not one of" in result.stderr

    def test_unknown_warehouse_answer(self) -> None:
        result = runner.invoke(app, ["init"], input="snowflake\n")
        assert result.exit_code == 2
        assert "use bigquery or duckdb" in result.stderr


class TestDecisionLog:
    def test_check_is_logged(self, tmp_path: Path) -> None:
        config = tmp_path / "scanisaur.yaml"
        config.write_text(f"log: {{path: {tmp_path / 'log'}, raw_sql: true}}\n", encoding="utf-8")
        sql = "SELECT user_id FROM events WHERE event_date = '2026-09-01'"
        assert run_check("--config", str(config), sql=sql).exit_code == EXIT_OK
        [file] = (tmp_path / "log").iterdir()
        entry = json.loads(file.read_text(encoding="utf-8"))
        assert (entry["source"], entry["verdict"], entry["sql"]) == ("cli", "pass", sql)

    def test_log_off(self, tmp_path: Path) -> None:
        config = tmp_path / "scanisaur.yaml"
        config.write_text(f"log: {{enabled: false, path: {tmp_path / 'log'}}}\n", encoding="utf-8")
        assert run_check("--config", str(config), sql="SELECT 1").exit_code == EXIT_OK
        assert not (tmp_path / "log").exists()

    def test_unwritable_log_warns(self, tmp_path: Path) -> None:
        (tmp_path / "file").write_text("", encoding="utf-8")
        config = tmp_path / "scanisaur.yaml"
        config.write_text(f"log: {{path: {tmp_path / 'file'}}}\n", encoding="utf-8")
        result = run_check("--config", str(config), sql="SELECT 1")
        assert result.exit_code == EXIT_OK
        assert "warning: decision log not written" in result.stderr


class _History:
    name = "bigquery:p:US"

    def __init__(self, runs: list[QueryRun]) -> None:
        self.runs = runs

    def fetch_query_history(self, since: datetime) -> Iterator[QueryRun]:
        return iter(self.runs)


class _Source:
    def __init__(self, config: Config, runs: list[QueryRun]) -> None:
        self.connector = _History(runs)

    def current(self) -> Snapshot:
        return Snapshot(load_catalog(Path(CATALOG)), "s1")


class TestAudit:
    @pytest.fixture
    def config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        started = datetime.now(UTC) - timedelta(hours=1)
        runs = [
            QueryRun("j1", started, "a@x", "SELECT * FROM events WHERE event_date = 'x'", 2**30),
            QueryRun(
                "j2", started, None, "SELECT user_id FROM events WHERE event_date = '2026-09-01'", 0
            ),
        ]
        monkeypatch.setattr(cli, "CachedSource", lambda config: _Source(config, runs))
        config = tmp_path / "scanisaur.yaml"
        config.write_text(f"log: {{path: {tmp_path / 'log'}}}\n", encoding="utf-8")
        return config

    def test_report(self, config: Path) -> None:
        result = runner.invoke(app, ["audit", "--config", str(config), "--days", "7"])
        assert result.exit_code == EXIT_OK, result.output
        lines = result.stdout.splitlines()
        assert lines[0].startswith("2 queries since ")
        assert lines[1] == "flagged: 1 queries, 1.1 GB billed"
        assert "SELECT * FROM events WHERE event_date = ?" in result.stdout
        assert "top rules, by bytes billed:" in result.stdout

    def test_json(self, config: Path) -> None:
        result = runner.invoke(app, ["audit", "--config", str(config), "--json"])
        assert json.loads(result.stdout)["flagged_runs"] == 1

    def test_without_warehouse(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["audit"])
        assert result.exit_code == EXIT_ERROR
        assert "names no warehouse" in result.stderr


def test_audit_shows_partial_billing_and_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = datetime.now(UTC)
    runs = [
        QueryRun("unknown", started, None, "SELECT * FROM events", None),
        QueryRun("failed", started, None, "SELECT 1", 2**30, "stopped"),
    ]
    monkeypatch.setattr(cli, "CachedSource", lambda config: _Source(config, runs))
    config = tmp_path / "scanisaur.yaml"
    config.write_text(f"log: {{path: {tmp_path / 'log'}}}\n", encoding="utf-8")
    result = runner.invoke(app, ["audit", "--config", str(config)])
    assert result.exit_code == EXIT_OK, result.output
    assert "1 queries since" in result.stdout
    assert "known + unknown (1 queries)" in result.stdout
    assert "billing totals are incomplete" in result.stdout
    assert "failed attempts: 1, 1.1 GB billed" in result.stdout
    result = runner.invoke(app, ["audit", "--config", str(config), "--json"])
    data = json.loads(result.stdout)
    assert (data["runs"], data["failed_runs"], data["unknown_billing_runs"]) == (1, 1, 1)


def test_audit_reports_errors_during_history_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def history(self: _History, since: datetime) -> Iterator[QueryRun]:
        yield QueryRun("j1", since, None, "SELECT 1", 0)
        raise ConnectorError("history page failed")

    monkeypatch.setattr(_History, "fetch_query_history", history)
    monkeypatch.setattr(cli, "CachedSource", lambda config: _Source(config, []))
    config = tmp_path / "scanisaur.yaml"
    config.write_text(f"log: {{path: {tmp_path / 'log'}}}\n", encoding="utf-8")
    result = runner.invoke(app, ["audit", "--config", str(config), "--json"])
    assert result.exit_code == EXIT_ERROR
    assert "history page failed" in result.stderr
    assert result.stdout == ""
