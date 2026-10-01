# Scanisaur
The apex predator of agent-generated SQL.

Scanisaur is an [MCP](https://modelcontextprotocol.io) server that AI agents call **before** they run SQL. It reads only warehouse metadata (tables, columns, types, partitioning, clustering, row counts and sizes) and uses it to:

- **validate** the query: are these tables and columns real?
- **optimize** it: catch missing partition filters, `SELECT *` on wide tables, joins with no condition and other costly patterns, each with a concrete fix;
- **quantify** it: estimate bytes scanned and cost before anything runs.

Scanisaur never reads table data and never runs the queries it checks. The agent's own SQL tool does that.

> **Status: pre-alpha.** Nothing is published yet. BigQuery is the first supported warehouse; Snowflake follows.

## Planned MCP tools

| Tool | Purpose |
| --- | --- |
| `scanisaur_schema_search` | Find relevant tables and columns by keyword |
| `scanisaur_schema_describe` | Compact, token-efficient table descriptions with partition and cluster keys |
| `scanisaur_check_sql` | Verdict (`pass`, `warn`, `block`), cost estimate, findings with fixes, and a tracking tag |

## Development

You need [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run scanisaur --version
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full set of checks, and [docs/adr/](docs/adr/) for the design decisions so far.

## License

[Apache License 2.0](LICENSE)
