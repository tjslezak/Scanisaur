# Spike 0001: BigQuery metadata sources

- **Issue:** [#1](https://github.com/tjslezak/Scanisaur/issues/1)
- **Date:** 2026-10-01
- **Status:** Done

## Question

How should the BigQuery connector read catalog metadata, how long does a refresh take, and what does it cost?

## Answer

Use the REST API: `tables.list` to discover tables, then `tables.get` for each one, 32 at a time. It is free, returns everything the rules need, and refreshed 1,000 tables in 7 seconds with no rate limiting. INFORMATION_SCHEMA is faster in bulk for schemas, but every query bills at least 10 MiB and needs `bigquery.jobs.create`. `PARTITIONS` took 142 seconds for 1,000 tables, so never use it for a bulk refresh.

## What each source returns

| Source | Returns | Cost |
| --- | --- | --- |
| `tables.list` | Name, type, time partitioning, clustering and `requirePartitionFilter`, for up to 1,000 tables per page. No schema, row counts or sizes | Free |
| `tables.get` | Schema, `numRows`, `numBytes`, `numPartitions`, partitioning, clustering, `requirePartitionFilter`, description | Free |
| `INFORMATION_SCHEMA.TABLES`, `COLUMNS`, `TABLE_OPTIONS` | One row per table, column or option for the whole dataset | 10 MiB per view per query |
| `INFORMATION_SCHEMA.PARTITIONS` | Rows and bytes per partition | 10 MiB, but slow (below) |
| `INFORMATION_SCHEMA.TABLE_CONSTRAINTS`, `KEY_COLUMN_USAGE` | Declared primary and foreign keys | 10 MiB each. `thelook_ecommerce` declares none, so SCN007 needs keys from config |

A query that reads two INFORMATION_SCHEMA views bills 10 MiB for each. Queries on `INFORMATION_SCHEMA.JOBS_BY_PROJECT` billed nothing while the project had almost no jobs, and 20 MiB each later the same day.

## Refreshing 1,000 tables

Measured in the sandbox on a dataset of 1,000 empty, day-partitioned tables, as the catalog-only service account.

| Method | Time | Errors |
| --- | --- | --- |
| `tables.list`, one page | 0.4 s | None |
| `tables.get` for each table, 8 at a time | 32.7 s | None |
| `tables.get` for each table, 32 at a time | 7.1 s | None: no 429 responses |
| `INFORMATION_SCHEMA.TABLES` | 0.3 s on the server, 3.1 s through `bq` | None |
| `INFORMATION_SCHEMA.COLUMNS` | 0.5 s on the server, 3.0 s through `bq` | None |
| `INFORMATION_SCHEMA.TABLE_OPTIONS` | 0.5 s on the server, 2.7 s through `bq` | None |
| `INFORMATION_SCHEMA.PARTITIONS` | 141.8 s on the server | None |

32 requests at a time didn't reach the API's rate limit, so the limit for `tables.get` sits above about 140 requests per second. The connector should still back off on 429 responses. The test tables had three columns each; wide schemas make each response larger, so expect real refreshes to take somewhat longer.

## On-demand price

The Cloud Billing catalog lists BigQuery analysis at **$6.25 per TiB** in the US multi-region and in `us-central1`, `us-east1`, `us-east4`, `us-east5`, `us-east7`, `us-west1`, `us-west4` and `us-west8`. Other regions cost more, up to $11.25 per TiB in `southamerica-east1`. The default in `scanisaur.yaml` stays at $6.25. The connector should look up the price for each dataset's location, from the SKU whose description is `Analysis (<region>)` (service `24E6-581D-38E5`).

## Recommendation

1. `tables.list` each dataset to find tables, views and their partitioning.
2. `tables.get` each table, 32 at a time, backing off on 429. This fills in the schema, row counts and sizes for free.
3. Query `TABLE_CONSTRAINTS` and `KEY_COLUMN_USAGE` once per dataset for declared keys, and merge in keys from config.
4. Don't query `PARTITIONS` in bulk. Use `numPartitions` from `tables.get`, and read `PARTITIONS` for one table only when a rule needs per-partition sizes.

This approach should need only `roles/bigquery.metadataViewer`, plus `roles/bigquery.jobUser` for the key query. The spike's catalog account also held `roles/bigquery.resourceViewer`, so confirm with an account that has just those two roles. See [spike 0003](0003-permissions.md).
