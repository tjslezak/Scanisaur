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
tag: /* scanisaur:q_6bg128p0yte0vj6h18d4 */
```

A query that gets past name checks also gets a cost estimate: the bytes BigQuery would bill under on-demand pricing, as a range with a confidence, and the dollars at $6.25 per TiB. [docs/estimate.md](docs/estimate.md) explains how it's worked out.

```console
$ echo "SELECT term, score FROM web.trends WHERE refresh_date BETWEEN '2026-09-25' AND '2026-10-01'" \
    | uv run scanisaur check --catalog tests/golden/catalog.yaml
pass: 0 findings · reads proj.web.trends
estimate: 374.3 MB billed, <$0.01 (medium confidence)
tag: /* scanisaur:q_mhpj9m1bfssq5f4fb3mv */
```

It exits with 0 when the query may run, 1 when it's blocked (or warned, with `--strict`), and 2 on a usage or input error. Add `--json` for the full result, or `--capacity-pricing` for bytes without dollars.

The agent adds the tag at the start or end of the SQL it runs, so the query can be found in BigQuery's job history. The tag comes from the SQL itself: the same query always gets the same tag, so repeated queries can still be served from BigQuery's cache.

## License

[Apache License 2.0](LICENSE)
