import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scanisaur import hook
from scanisaur.catalog.source import FixtureSource
from scanisaur.config import CONFIG_FILE
from scanisaur.engine.check import Policy
from scanisaur.hook import (
    adk_callback,
    bq_query_sql,
    claude_output,
    cursor_output,
    extract_sql,
    request_check,
    socket_path,
)
from scanisaur.listener import hook_listener

CATALOG = Path(__file__).parent / "golden" / "catalog.yaml"
SOURCE = FixtureSource(CATALOG)
BAD_SQL = "SELECT usr_id FROM users"
GOOD_SQL = "SELECT day FROM calendar"


@pytest.fixture
def sock() -> Iterator[Path]:
    """A socket path short enough for macOS, which pytest's tmp_path isn't."""
    directory = tempfile.mkdtemp(prefix="sc", dir="/tmp")
    yield Path(directory) / "s.sock"
    shutil.rmtree(directory, ignore_errors=True)


def _call(tool: str, **arguments: Any) -> dict[str, Any]:
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": arguments}


def _run_hook(
    call: object, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str
) -> str:
    text = call if isinstance(call, str) else json.dumps(call)
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    assert hook.main(["--catalog", str(CATALOG), *args]) == 0
    return capsys.readouterr().out


def test_config_file_name_matches_the_config_module() -> None:
    assert hook.CONFIG_FILE == CONFIG_FILE


class TestExtractSql:
    @pytest.mark.parametrize("key", ["query", "sql"])
    def test_mcp_tool(self, key: str) -> None:
        call = _call("mcp__bigquery__execute_sql", **{key: GOOD_SQL})
        assert extract_sql(call) == GOOD_SQL

    def test_other_tools_are_ignored(self) -> None:
        assert extract_sql(_call("mcp__bigquery__get_table_info", query=GOOD_SQL)) is None

    def test_tool_patterns(self) -> None:
        call = _call("mcp__duck__run", sql=GOOD_SQL)
        assert extract_sql(call) is None
        assert extract_sql(call, ("mcp__duck__*",)) == GOOD_SQL

    def test_bq_query_in_bash(self) -> None:
        command = f"bq query --use_legacy_sql=false '{GOOD_SQL}'"
        assert extract_sql(_call("Bash", command=command)) == GOOD_SQL

    @pytest.mark.parametrize(
        "call",
        [
            _call("Bash", command="ls -la"),
            _call("mcp__bigquery__execute_sql", query="  "),
            {"tool_name": "mcp__bigquery__execute_sql", "tool_input": "SELECT 1"},
            {"tool_input": {"query": GOOD_SQL}},
        ],
    )
    def test_no_sql(self, call: dict[str, Any]) -> None:
        assert extract_sql(call) is None


class TestBqQuerySql:
    @pytest.mark.parametrize(
        ("command", "sql"),
        [
            ("bq query 'SELECT 1'", "SELECT 1"),
            ('/usr/bin/bq --project_id=p query --nouse_legacy_sql "SELECT 1"', "SELECT 1"),
            ("cd x && bq query --format json 'SELECT 1'", "SELECT 1"),
            ("bq query 'SELECT 1' | head -5", "SELECT 1"),
            ("bq query 'SELECT 1'|head", "SELECT 1"),
            ("bq query 'SELECT 1' > out.txt; echo done", "SELECT 1"),
            ("bq query 'SELECT a FROM t WHERE x > 1'", "SELECT a FROM t WHERE x > 1"),
        ],
    )
    def test_reads_the_sql(self, command: str, sql: str) -> None:
        assert bq_query_sql(command) == sql

    @pytest.mark.parametrize(
        "command",
        [
            "bq ls",
            "bq query",
            "bq query --flag",
            "echo 'unclosed",
            "query bq",
            "bq ls && echo query x",
            "bq query < q.sql",
        ],
    )
    def test_nothing_to_read(self, command: str) -> None:
        assert bq_query_sql(command) is None


class TestClaudeOutput:
    def test_pass_says_nothing(self) -> None:
        assert claude_output({"verdict": "pass", "findings": []}) is None

    def test_warn_adds_context_without_allowing(self) -> None:
        finding = {"rule": "SCN005", "severity": "warn", "message": "SELECT *.", "fix": None}
        output = claude_output({"verdict": "warn", "findings": [finding]})
        assert output == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "Scanisaur: warn, 1 finding(s).\n- SCN005 (warn): SELECT *.",
            }
        }

    def test_block_denies_with_fixes(self) -> None:
        finding = {"rule": "SCN001", "severity": "block", "message": "No.", "fix": "Use x."}
        output = claude_output({"verdict": "block", "findings": [finding]})
        assert output is not None
        decision = output["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "- SCN001 (block): No. Fix: Use x." in decision["permissionDecisionReason"]


class TestSocketPath:
    def test_same_arguments_same_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/7")
        path = socket_path(CATALOG, None)
        assert path.parent == Path("/run/user/7/scanisaur")
        assert path == socket_path(CATALOG.parent / "." / CATALOG.name, None)
        assert path != socket_path(CATALOG, Path("scanisaur.yaml"))
        assert len(str(path)) < 104

    def test_temp_dir_without_runtime_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        path = socket_path(CATALOG, None)
        assert path.parent.name == f"scanisaur-{os.getuid()}"


class TestListener:
    def _round_trip(self, sock: Path, sql: str) -> dict[str, Any] | None:
        async def run() -> dict[str, Any] | None:
            async with hook_listener(sock, SOURCE, Policy()) as server:
                assert server is not None
                assert sock.stat().st_mode & 0o777 == 0o600
                return await asyncio.to_thread(request_check, sock, sql)

        return asyncio.run(run())

    def test_round_trip(self, sock: Path) -> None:
        result = self._round_trip(sock, BAD_SQL)
        assert result is not None
        assert result["verdict"] == "block"
        assert result["snapshot_id"] == SOURCE.current().snapshot_id
        assert not sock.exists()  # removed on shutdown

    def test_replaces_a_stale_socket_file(self, sock: Path) -> None:
        sock.write_text("left behind")
        result = self._round_trip(sock, GOOD_SQL)
        assert result is not None
        assert result["verdict"] == "pass"

    def test_second_listener_stands_aside(self, sock: Path) -> None:
        async def run() -> None:
            async with hook_listener(sock, SOURCE, Policy()) as first:
                async with hook_listener(sock, SOURCE, Policy()) as second:
                    assert first is not None
                    assert second is None
                assert sock.exists()  # the second one leaves the first one's socket alone

        asyncio.run(run())

    @pytest.mark.parametrize(
        ("request_line", "error"),
        [
            (b"nope\n", "the request isn't JSON"),
            (b'{"v": 9, "sql": "SELECT 1"}\n', "expected protocol version 1"),
            (b'{"v": 1, "sql": 1}\n', "sql must be a string"),
        ],
    )
    def test_bad_requests(self, sock: Path, request_line: bytes, error: str) -> None:
        async def run() -> dict[str, Any]:
            async with hook_listener(sock, SOURCE, Policy()):
                reader, writer = await asyncio.open_unix_connection(str(sock))
                writer.write(request_line)
                answer = json.loads(await reader.readline())
                writer.close()
                return dict(answer)

        assert asyncio.run(run()) == {"error": error}

    def test_shared_directory_is_not_trusted(self, sock: Path) -> None:
        sock.parent.chmod(0o755)  # as if another user had made it, or left it open

        async def run() -> None:
            async with hook_listener(sock, SOURCE, Policy()) as server:
                assert server is None
                assert not sock.exists()

        asyncio.run(run())

    def test_client_skips_a_shared_directory(self, sock: Path) -> None:
        async def run() -> dict[str, Any] | None:
            async with hook_listener(sock, SOURCE, Policy()):
                sock.parent.chmod(0o755)
                return await asyncio.to_thread(request_check, sock, GOOD_SQL)

        assert asyncio.run(run()) is None

    def test_no_server(self, sock: Path) -> None:
        assert request_check(sock, GOOD_SQL) is None


class TestMain:
    def test_blocks_through_the_server(
        self, sock: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(hook, "socket_path", lambda catalog, config: sock)
        monkeypatch.setattr(hook, "check_here", lambda *args: pytest.fail("not via the server"))

        async def run() -> str:
            async with hook_listener(sock, SOURCE, Policy()):
                call = _call("mcp__bigquery__execute_sql", query=BAD_SQL)
                return await asyncio.to_thread(_run_hook, call, monkeypatch, capsys)

        output = json.loads(asyncio.run(run()))
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_checks_here_without_a_server(
        self, sock: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(hook, "socket_path", lambda catalog, config: sock)
        call = _call("mcp__bigquery__execute_sql", query=BAD_SQL)
        output = json.loads(_run_hook(call, monkeypatch, capsys))
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_pass_prints_nothing(
        self, sock: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(hook, "socket_path", lambda catalog, config: sock)
        call = _call("mcp__bigquery__execute_sql", query=GOOD_SQL)
        assert _run_hook(call, monkeypatch, capsys) == ""

    def test_fails_open_without_a_catalog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_call("x_execute_sql", sql="S"))))
        assert hook.main(["--catalog", str(tmp_path / "missing.yaml")]) == 0
        captured = capsys.readouterr()
        context = json.loads(captured.out)["hookSpecificOutput"]["additionalContext"]
        assert context.startswith("Scanisaur couldn't check this SQL")
        assert "missing.yaml" in captured.err

    @pytest.mark.parametrize("stdin", ["not json", "[1, 2]", json.dumps(_call("Read", path="x"))])
    def test_nothing_to_check(
        self, stdin: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _run_hook(stdin, monkeypatch, capsys) == ""


def test_hook_against_a_running_serve() -> None:
    """Start serve as an MCP client would, then run the hook command against it."""
    serve = subprocess.Popen(
        [sys.executable, "-m", "scanisaur", "serve", "--catalog", str(CATALOG)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        path = socket_path(CATALOG, hook.policy_file(None))
        deadline = time.monotonic() + 30
        while request_check(path, GOOD_SQL) is None:
            assert time.monotonic() < deadline, "serve never answered on its socket"
            time.sleep(0.05)
        call = json.dumps(_call("mcp__bigquery__execute_sql", query=BAD_SQL))
        hooked = subprocess.run(
            [sys.executable, "-m", "scanisaur", "hook", "--catalog", str(CATALOG)],
            input=call,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        output = json.loads(hooked.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    finally:
        serve.terminate()
        serve.wait(timeout=10)


class TestCursor:
    def test_mcp_tool_input_as_a_json_string(self) -> None:
        call = {"tool_name": "execute_sql", "tool_input": json.dumps({"query": GOOD_SQL})}
        assert extract_sql(call) == GOOD_SQL

    def test_shell_command(self) -> None:
        assert extract_sql({"command": f"bq query '{GOOD_SQL}'", "cwd": "/x"}) == GOOD_SQL

    def test_bad_json_string(self) -> None:
        assert extract_sql({"tool_name": "execute_sql", "tool_input": "{nope"}) is None

    def test_outputs(self) -> None:
        finding = {"rule": "SCN001", "severity": "block", "message": "No.", "fix": None}
        assert cursor_output({"verdict": "pass", "findings": []}) is None
        warn = cursor_output({"verdict": "warn", "findings": [finding]})
        assert warn is not None
        assert warn["permission"] == "allow"
        block = cursor_output({"verdict": "block", "findings": [finding]})
        assert block is not None
        assert block["permission"] == "deny"
        assert "SCN001" in block["agent_message"]

    def test_main(
        self, sock: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(hook, "socket_path", lambda catalog, config: sock)
        call = {"command": f"bq query '{BAD_SQL}'"}
        output = json.loads(_run_hook(call, monkeypatch, capsys, "--format", "cursor"))
        assert output["permission"] == "deny"

    def test_fails_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"command": "bq query 'S'"})))
        missing = str(tmp_path / "missing.yaml")
        assert hook.main(["--format", "cursor", "--catalog", missing]) == 0
        assert json.loads(capsys.readouterr().out)["permission"] == "allow"


class TestAdk:
    class Tool:
        name = "execute_sql"

    def test_block_returns_the_fixes(self, sock: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook, "socket_path", lambda catalog, config: sock)
        callback = adk_callback(CATALOG)
        answer = callback(self.Tool(), {"query": BAD_SQL}, None)
        assert answer is not None
        assert "Did you mean `user_id`?" in answer["error"]

    def test_pass_and_other_tools_run(self, sock: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook, "socket_path", lambda catalog, config: sock)
        callback = adk_callback(CATALOG)
        assert callback(self.Tool(), {"query": GOOD_SQL}, None) is None
        assert callback(object(), {"query": BAD_SQL}, None) is None

    def test_fails_open(self, tmp_path: Path) -> None:
        callback = adk_callback(tmp_path / "missing.yaml")
        assert callback(self.Tool(), {"query": BAD_SQL}, None) is None


DOCS = Path(__file__).parents[1] / "docs"


@pytest.mark.parametrize("page", ["clients/README.md", "hooks/README.md"])
def test_json_examples_in_docs_parse(page: str) -> None:
    text = (DOCS / page).read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
    assert blocks
    for block in blocks:
        json.loads(block)


def test_claude_code_hook_example_calls_the_hook_tool() -> None:
    text = (DOCS / "hooks" / "README.md").read_text(encoding="utf-8")
    settings = json.loads(re.findall(r"```json\n(.*?)```", text, re.DOTALL)[0])
    for entry in settings["hooks"]["PreToolUse"]:
        (handler,) = entry["hooks"]
        assert handler["server"] == "scanisaur"
        assert handler["tool"] == "scanisaur_hook"
