# 0003: Support BigQuery first

- **Status:** Accepted
- **Date:** 2026-10-01

## Context

Supporting two warehouses at once would delay the first release and double the estimator tuning work. The first warehouse should make waste easy to measure and estimates easy to verify.

## Decision

v0.1 supports BigQuery, plus DuckDB for tests and a zero-credentials demo. Snowflake follows in v0.2.

## Consequences

- With on-demand pricing, BigQuery bills by bytes scanned, so wasted scans map directly to cost.
- BigQuery metadata includes per-partition row counts and sizes, so partition-pruning estimates can be accurate.
- BigQuery dry runs are free and report exact bytes, which gives the estimator benchmark ground truth.
- Customers on capacity-based (Editions) pricing get byte estimates without a dollar figure in v0.1.
- Rules and the estimator must stay warehouse-neutral at their interfaces, so the Snowflake connector can be added in v0.2 without reworking the engine.
