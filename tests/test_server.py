import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent

from scanisaur.catalog.source import FixtureSource
from scanisaur.engine.check import Policy
from scanisaur.server import INSTRUCTIONS, build_server

CATALOG = Path(__file__).parent / "golden" / "catalog.yaml"
TOOLS = {"scanisaur_schema_search", "scanisaur_schema_describe", "scanisaur_check_sql"}
HOOK_TOOL = "scanisaur_hook"


def _call(name: str, arguments: dict[str, Any]) -> CallToolResult:
    server = build_server(FixtureSource(CATALOG), Policy())

    async def call() -> CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(call())


def _text(result: CallToolResult) -> str:
    (content,) = result.content
    assert isinstance(content, TextContent)
    return content.text


def test_lists_three_read_only_tools_with_schemas() -> None:
    server = build_server(FixtureSource(CATALOG), Policy())

    async def list_tools() -> Any:
        async with Client(server) as client:
            return (await client.list_tools()).tools

    tools = asyncio.run(list_tools())
    assert {tool.name for tool in tools} == {*TOOLS, HOOK_TOOL}
    for tool in tools:
        assert tool.description
        assert (tool.output_schema is None) == (tool.name == HOOK_TOOL)
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint


def test_check_sql() -> None:
    result = _call("scanisaur_check_sql", {"sql": "SELECT usr_id FROM users"})
    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["verdict"] == "block"
    assert result.structured_content["snapshot_id"].startswith("fixture_")
    line, _, payload = _text(result).partition("\n")
    assert line == "block: 1 finding. Apply the fixes and check again before running it."
    assert json.loads(payload) == result.structured_content


def test_schema_search() -> None:
    result = _call("scanisaur_schema_search", {"query": "orders", "limit": 1})
    assert result.structured_content is not None
    (table,) = result.structured_content["tables"]
    assert table["table"] == "proj.analytics.Orders"
    assert _text(result).startswith("1 matching table.\n")


def test_schema_describe_reports_unknown_names() -> None:
    result = _call("scanisaur_schema_describe", {"tables": ["users", "nope"]})
    assert result.structured_content is not None
    assert result.structured_content["unknown"] == ["nope"]
    assert _text(result).startswith("Described 1 of 2. Unknown: nope.\n")


def test_describe_refuses_more_than_five_tables() -> None:
    result = _call("scanisaur_schema_describe", {"tables": ["users"] * 6})
    assert result.is_error


class TestHookTool:
    """The tool Claude Code's mcp_tool hook calls; its text is the hook's answer."""

    def test_block_denies(self) -> None:
        result = _call(HOOK_TOOL, {"sql": "SELECT usr_id FROM users"})
        output = json.loads(_text(result))
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "Did you mean `user_id`?" in output["hookSpecificOutput"]["permissionDecisionReason"]

    def test_bq_command(self) -> None:
        result = _call(HOOK_TOOL, {"command": "bq query 'SELECT usr_id FROM users'"})
        assert json.loads(_text(result))["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_chained_bq_commands(self) -> None:
        command = "bq query 'SELECT day FROM calendar' && bq query 'SELECT usr_id FROM users'"
        result = _call(HOOK_TOOL, {"command": command})
        assert json.loads(_text(result))["hookSpecificOutput"]["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        "arguments",
        [
            {"sql": "SELECT day FROM calendar"},
            {"command": "ls"},
            {"sql": "${tool_input.query}"},
            {},
        ],
    )
    def test_says_nothing(self, arguments: dict[str, str]) -> None:
        result = _call(HOOK_TOOL, arguments)
        assert not result.is_error
        assert _text(result) == ""


def test_serve_speaks_only_json_rpc_on_stdout() -> None:
    """Start ``scanisaur serve`` as an MCP client would, and initialize it over stdio."""
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    process = subprocess.run(
        [sys.executable, "-m", "scanisaur", "serve", "--catalog", str(CATALOG)],
        input=json.dumps(initialize) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    lines = process.stdout.splitlines()
    assert lines, process.stderr
    for line in lines:
        assert json.loads(line)["jsonrpc"] == "2.0"
    response = json.loads(lines[0])
    assert response["result"]["serverInfo"]["name"] == "scanisaur"
    assert response["result"]["instructions"] == INSTRUCTIONS
