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

### Try the checker

Until the warehouse connector lands, `scanisaur check` reads table metadata from a YAML catalog such as the one the tests use:

```console
$ echo "SELECT usr_id FROM events WHERE event_date = '2026-09-01'" \
    | uv run scanisaur check --catalog tests/golden/catalog.yaml
block: 1 finding · reads proj.analytics.events
  1:8     SCN001  block  Column `usr_id` does not exist in `proj.analytics.events`.
                  fix:   Did you mean `user_id`?
tag: /* scanisaur:chk_01m3wk05rsjdxrp063 */
```

It exits with 0 when the query may run, 1 when it's blocked (or warned, with `--strict`), and 2 on a usage or input error. Add `--json` for the full result.

## License

[Apache License 2.0](LICENSE)
