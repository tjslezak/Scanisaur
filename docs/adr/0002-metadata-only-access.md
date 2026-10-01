# 0002: Metadata-only access, with an optional planner mode

- **Status:** Accepted
- **Date:** 2026-10-01

## Context

Scanisaur checks SQL that AI agents write before they run it. Teams are cautious about giving a third-party tool access to their data. Warehouse planners (BigQuery dry runs, `EXPLAIN`) give more accurate estimates, but usually need the same privileges as running the query.

## Decision

- **By default, Core reads catalog metadata only:** tables, columns, types, partitioning, clustering, keys, row counts and sizes. It never reads table data and never runs the queries it checks; the agent's own SQL tool does that.
- **Planner mode is opt-in.** If an operator grants the extra rights, Core uses dry runs to measure column sizes and to confirm large estimates. Even then it never reads data or runs the agent's query.
- **No gated execution:** Core will not offer a tool that runs queries.

## Consequences

- Least-privilege setup is simple to explain and verify. `scanisaur doctor` will warn if the configured role can read table data.
- Without planner mode, estimates are static: ranges with a confidence level, not exact numbers.
- Planner mode's exact permission requirements must be confirmed for each warehouse (a week-1 spike for BigQuery). Documentation must state plainly that those rights could read data even though Core does not.
- Because checks are advisory, agents can skip them. Each check returns a tag to embed in the SQL, so query history can show which queries were checked.
