# Reference hooks

A hook makes the check independent of the agent: the harness sends each SQL tool call to Scanisaur before running it. A block denies the call and gives the agent the fixes; a warning reaches the agent with the findings; a pass says nothing. If Scanisaur can't be reached, the call runs.

Each hook needs the `scanisaur` MCP server from [docs/clients](../clients/README.md). The examples match tools named like `execute_sql`, as in Google's BigQuery MCP server, and `bq query` shell commands.

## Claude Code

Claude Code calls the `scanisaur_hook` tool on the `scanisaur` server it already runs, so a check adds about a millisecond and starts no process. Add to `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "mcp__.*__execute_sql.*",
        "hooks": [
          {
            "type": "mcp_tool",
            "server": "scanisaur",
            "tool": "scanisaur_hook",
            "input": { "sql": "${tool_input.query}" }
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "mcp_tool",
            "server": "scanisaur",
            "tool": "scanisaur_hook",
            "input": { "command": "${tool_input.command}" }
          }
        ]
      }
    ]
  }
}
```

If the SQL tool names its argument `sql` rather than `query`, use `"${tool_input.sql}"`. If the server isn't connected, Claude Code reports a non-blocking hook error and runs the call.

## Cursor

Cursor runs hooks as commands, so `scanisaur hook` sends the SQL to the running `scanisaur serve` over a local socket. Give it the same `--catalog` and `--config` as the server. Add to `.cursor/hooks.json`:

```json
{
  "version": 1,
  "hooks": {
    "beforeMCPExecution": [
      { "command": "uvx scanisaur hook --format cursor --catalog /abs/path/catalog.yaml" }
    ],
    "beforeShellExecution": [
      { "command": "uvx scanisaur hook --format cursor --catalog /abs/path/catalog.yaml" }
    ]
  }
}
```

With no server running, the hook checks the SQL itself, which takes a few hundred milliseconds. `--tool` changes which MCP tools count as SQL tools (default `*execute_sql*`).

The Cursor input and output fields follow Cursor's hooks documentation as of October 2026 and haven't been tested against Cursor itself yet.

## Google ADK

ADK runs Python callbacks in the agent's process. `adk_callback` builds a `before_tool_callback` that asks the running `serve` and falls back to checking in-process:

```python
from pathlib import Path

from google.adk.agents import Agent

from scanisaur.hook import adk_callback

agent = Agent(
    name="analyst",
    model="gemini-2.5-flash",
    tools=[...],  # your BigQuery tools
    before_tool_callback=adk_callback(Path("/abs/path/catalog.yaml")),
)
```

On block, the callback returns `{"error": ...}` with the findings and fixes, so the tool doesn't run and the agent reads why. ADK has no way to attach findings to a call that runs, so warnings aren't shown here.
