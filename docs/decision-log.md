# Decision log

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

`scanisaur audit`, which reads this log, comes in a follow-up PR.
