"""The MCP server: the three tools over stdio. The only module that imports ``mcp``.

The tools themselves live in :mod:`scanisaur.tools`; this module names them, describes them
to the agent and turns their results into MCP responses.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from scanisaur import __version__, tools
from scanisaur.catalog.source import CatalogSource
from scanisaur.engine.check import Policy
from scanisaur.engine.result import CheckResult
from scanisaur.hook import claude_output, sql_from
from scanisaur.listener import hook_listener

#: Sent when a client connects. The agent evaluation (#10) tunes these words.
INSTRUCTIONS = """\
Scanisaur checks BigQuery SQL against warehouse metadata before it runs. It never runs \
queries and never reads table data.

Before running any SQL, call scanisaur_check_sql with the exact query.
- pass: run it.
- warn: run it, and tell the user what the findings say.
- block: don't run it. Apply the fixes, then check the new SQL.
Add the returned tag, a SQL comment, at the start or end of the query you run.

To find tables, call scanisaur_schema_search; to see their columns and partitioning, call \
scanisaur_schema_describe."""

_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)


def build_server(
    source: CatalogSource, policy: Policy, hook_socket: Path | None = None
) -> MCPServer:
    """An MCP server answering from ``source`` under ``policy``, and answering
    ``scanisaur hook`` on ``hook_socket`` while it runs."""

    @contextlib.asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        if hook_socket is None:
            yield
            return
        async with hook_listener(hook_socket, source, policy):
            yield

    server = MCPServer(
        name="scanisaur", version=__version__, instructions=INSTRUCTIONS, lifespan=lifespan
    )

    @server.tool(name="scanisaur_schema_search", annotations=_READ_ONLY)
    def schema_search(
        query: Annotated[str, Field(description="Words to look for in table and column names.")],
        limit: Annotated[
            int, Field(ge=1, le=tools.MAX_SEARCH_LIMIT, description="Tables to return.")
        ] = tools.DEFAULT_SEARCH_LIMIT,
    ) -> Annotated[CallToolResult, tools.SearchResponse]:
        """Find tables by keyword. Returns the best matching tables, with their size,
        partitioning, clustering and the columns that matched."""
        response = tools.schema_search(query, source, limit)
        count = len(response.tables)
        return _result(response, f"{count} matching table{'' if count == 1 else 's'}.")

    @server.tool(name="scanisaur_schema_describe", annotations=_READ_ONLY)
    def schema_describe(
        tables: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=tools.MAX_DESCRIBE_TABLES,
                description="Table names: table, dataset.table or project.dataset.table.",
            ),
        ],
    ) -> Annotated[CallToolResult, tools.DescribeResponse]:
        """Describe tables: columns and types, partitioning, clustering, unique keys, rows
        and size. Check the partition column here before filtering a large table."""
        response = tools.schema_describe(tables, source.current().catalog)
        line = f"Described {len(response.tables)} of {len(tables)}."
        if response.unknown:
            line += f" Unknown: {', '.join(response.unknown)}."
        return _result(response, line)

    @server.tool(name="scanisaur_check_sql", annotations=_READ_ONLY)
    def check_sql(
        sql: Annotated[str, Field(description="One BigQuery SQL statement, as it will run.")],
    ) -> Annotated[CallToolResult, CheckResult]:
        """Check SQL before running it: a verdict (pass, warn or block), the bytes it would
        bill, findings with fixes, and a tag to add to the query."""
        result = tools.check_sql(sql, source.current(), policy)
        return _result(result, tools.summary(result))

    @server.tool(name="scanisaur_hook", annotations=_READ_ONLY, structured_output=False)
    def hook(
        sql: Annotated[str, Field(description="SQL from the tool call.")] = "",
        command: Annotated[str, Field(description="A shell command that may run SQL.")] = "",
    ) -> CallToolResult:
        """For Claude Code's PreToolUse hook only. Agents: call scanisaur_check_sql."""
        found = sql_from(sql, command)
        if found is None:
            return CallToolResult(content=[TextContent(type="text", text="")])
        result = tools.check_sql(found, source.current(), policy).model_dump(mode="json")
        output = claude_output(result)
        text = "" if output is None else json.dumps(output)
        return CallToolResult(content=[TextContent(type="text", text=text)])

    return server


def _result(model: BaseModel, line: str) -> CallToolResult:
    """Structured content, plus text for clients that pass only text to the model: a
    one-line summary over compact JSON. The SDK's default text is indented JSON, which
    costs about a third more tokens."""
    return CallToolResult(
        content=[TextContent(type="text", text=f"{line}\n{model.model_dump_json()}")],
        structured_content=model.model_dump(mode="json"),
    )
