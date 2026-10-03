# Decision log and audit

## Decision log

Every `scanisaur check` adds one JSON line to the decision log (OSS-18). The log is a directory of monthly files such as `2026-10.jsonl`, in the user state directory by default (`~/.local/state/scanisaur/log` on Linux). Turn it off, or move it, in `scanisaur.yaml`:

```yaml
log:
  enabled: true
  path: ~/scanisaur-log   # a directory
  raw_sql: false          # true also logs each query's SQL, literal values included
```

A line looks like this:

```json
{"v":1,"time":"2026-10-03T15:40:12.345Z","check_id":"chk_...","query":"q_...","shape":"s_...","source":"cli","warehouse":"bigquery:acme-analytics:US","verdict":"warn","findings":[{"rule":"SCN005","severity":"warn","line":1,"column":8}],"tables":["acme-analytics.web.events"],"estimate":{"bytes_low":1073741824,"bytes_high":1073741824,"confidence":"high","usd_low":0.01,"usd_high":0.01}}
```

| Field | Meaning |
| --- | --- |
| `query` | Fingerprint of the query's exact text, the one in its tag. It matches the query in BigQuery's job history. |
| `shape` | Fingerprint of the query with every literal value replaced by `?`. Queries that differ only in constants share it. |
| `source` | `cli` for `scanisaur check`. MCP tool calls and hooks will be logged too. |
| `findings` | Rule, severity and position only. Finding messages can quote filter values, so they aren't logged. |

The log never holds literal values unless `raw_sql` is on. Processes that check at the same time append whole lines, without interleaving. Lines that don't parse are skipped when the log is read.

## `scanisaur audit`

```
scanisaur audit --days 30 [--top 10] [--json]
```

`audit` reads the warehouse's SELECT jobs from `region-<location>.INFORMATION_SCHEMA.JOBS_BY_PROJECT`, checks each successful execution at its start time against the current catalog with the current policy, and reports:

- How many queries ran and what they billed, and how many of those the checks flag (warn or block).
- Flagged queries grouped by shape, most billed first, with their rules.
- Unchecked runs: runs with no check of the same query for the audited warehouse in the decision log in the hour before them. A tag only identifies a query, so the log, not the tag, shows that a check happened (spike 0002).
- Runs after a block: the latest check before the run blocked the query.
- Failed attempts (including canceled queries), their known billed bytes, and attempts after a block, separately from successful runs.
- The top tables and rules, by bytes billed.

Checks logged without a warehouse identity do not establish that a warehouse run was checked. BigQuery hides billed bytes for some jobs, including queries using row-level security: these remain unknown, never zero. JSON byte totals sum only known values, with `unknown_billing_runs` counters for the report and each table, rule, and shape (`flagged_unknown_billing_runs` and `failed_unknown_billing_runs` for those totals). Text output labels partial totals and lists entries with unknown billing first. `runs`, `flagged_runs`, `unchecked_runs`, and `ran_after_block` count only successful executions; failures have separate counters.

Scanisaur's own metadata queries (labelled `tool: scanisaur`) are left out. DuckDB keeps no query history, so `audit` needs BigQuery.

**Access.** Reading `JOBS_BY_PROJECT` needs `bigquery.jobs.listAll` on the project, for example from `roles/bigquery.resourceViewer`. This grant is optional: `check` doesn't need it. **It exposes the full text of every query run in the project, literal values such as email addresses included**, though no table data. `audit` keeps that text in memory only. Its report shows query shapes, never literal values, and it writes nothing to the decision log. Reading the history bills BigQuery's 10 MiB minimum.
