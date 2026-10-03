# Connect an MCP client

Each client starts `scanisaur serve` itself, over stdio. Until the warehouse connector lands, `serve` answers from a YAML catalog: give its absolute path, since clients start servers in a directory of their own choosing.

Then paste the [agent instruction](#agent-instruction) into the agent's instructions, such as `CLAUDE.md`, Cursor rules or a system prompt.

## Claude Code

From the project directory:

```bash
claude mcp add scanisaur --scope project -- uvx scanisaur serve --catalog /abs/path/catalog.yaml
```

That writes `.mcp.json`, which you can also write by hand:

```json
{
  "mcpServers": {
    "scanisaur": {
      "command": "uvx",
      "args": ["scanisaur", "serve", "--catalog", "/abs/path/catalog.yaml"]
    }
  }
}
```

## Cursor

`.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` for every project:

```json
{
  "mcpServers": {
    "scanisaur": {
      "command": "uvx",
      "args": ["scanisaur", "serve", "--catalog", "/abs/path/catalog.yaml"]
    }
  }
}
```

## Claude Desktop

Settings > Developer > Edit Config opens `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "scanisaur": {
      "command": "uvx",
      "args": ["scanisaur", "serve", "--catalog", "/abs/path/catalog.yaml"]
    }
  }
}
```

Add `"--config", "/abs/path/scanisaur.yaml"` to `args` to use a policy file; otherwise `serve` reads `scanisaur.yaml` from the directory the client starts it in, if there is one.

## Agent instruction

```text
Before running any SQL, call scanisaur_check_sql with the exact query. On block, don't
run it: apply the fixes and check the new SQL. On warn, run it and tell me what the
findings say. Add the returned tag, a SQL comment, at the start or end of the query
you run. Use scanisaur_schema_search and scanisaur_schema_describe to find tables and
their partition columns before writing a query.
```

The server sends the same guidance when a client connects, but not every client shows it to the model. To have every SQL call checked whether or not the agent remembers, add a [hook](../hooks/README.md).
