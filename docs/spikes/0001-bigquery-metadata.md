# Spike 0001: BigQuery metadata sources

- **Issue:** [#1](https://github.com/tjslezak/Scanisaur/issues/1)
- **Date:** 2026-10-01
- **Status:** Done

## Question

How should the BigQuery connector read catalog metadata, how long does a refresh take, and what does it cost?

## Answer

Read in bulk, then fetch single tables only when needed:

- **Schemas and options:** query the project-wide INFORMATION_SCHEMA views once per region. `COLUMNS` and `TABLE_OPTIONS` each covered all 1,001 tables in the sandbox in about 1 second, for 10 MiB each.
- **Partition types:** `tables.list` each dataset.
- **Row counts, sizes and change times:** query each dataset's `__TABLES__`. It covered 1,000 tables in 0.5 seconds, and it's free.
- **Changes after that:** refetch only the tables whose last-modified time moved, with `tables.get`.

Calling `tables.get` on every table is free and complete, but it took 7 seconds per 1,000 tables, about 12 minutes for 100,000. That is too slow as the only way to refresh a large warehouse. Never query `PARTITIONS` in bulk: it took 142 seconds for 1,000 tables.

## What each source returns

| Source | Returns | Cost |
| --- | --- | --- |
| `tables.list` | Name, type, time partitioning, clustering and `requirePartitionFilter`, for up to 1,000 tables per page. No schema, row counts or sizes | Free |
| `tables.get` | Schema, `numRows`, `numBytes`, `numPartitions`, partitioning, clustering, `requirePartitionFilter`, description | Free |
| `INFORMATION_SCHEMA.TABLES`, `COLUMNS`, `TABLE_OPTIONS` | One row per table, column or option. `COLUMNS` flags the partition column and gives each cluster column's position; `TABLE_OPTIONS` holds `require_partition_filter`. Query them per dataset, or per region (`region-us.INFORMATION_SCHEMA.COLUMNS`) to cover every dataset in the project | 10 MiB per view per query |
| `INFORMATION_SCHEMA.TABLE_STORAGE` | Rows and bytes per table, project-wide | 10 MiB, but off by default: an admin must turn on the project option `region-us.enable_info_schema_storage` with `ALTER PROJECT`, which needs `bigquery.config.update`, and history takes about a day to fill |
| `<dataset>.__TABLES__` | Row count, size in bytes, creation and last-modified times, and type, for every table in the dataset | Free: billed 0 bytes. It predates INFORMATION_SCHEMA and is left out of Google's current docs, so keep `tables.get` as a fallback |
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
| `region-us.INFORMATION_SCHEMA.COLUMNS`, both datasets (1,001 tables) | 1.0 s on the server | None, as the catalog account |
| `region-us.INFORMATION_SCHEMA.TABLE_OPTIONS`, both datasets | 1.1 s on the server | None, as the catalog account |
| `spike_many.__TABLES__` | 0.5 s on the server | None |

32 requests at a time didn't reach the API's rate limit, so the limit for `tables.get` sits above about 140 requests per second. The connector should still back off on 429 responses. The test tables had three columns each; wide schemas make each response larger, so expect real refreshes to take somewhat longer. `spikes.sh` also started a new `curl` process, with its own TLS handshake, for every request; a connector that reuses connections should do better than these `tables.get` times.

## Permissions for the project-wide views

The project-wide (`region-us`) views were denied to a user holding only the basic `roles/owner` role, with `User does not have the required permissions ('bigquery.tables.list' permission(s) at the dataset level …)`. The catalog account, which holds `roles/bigquery.metadataViewer` on the project, read them. The same owner could read per-dataset views and `__TABLES__`. After `roles/bigquery.metadataViewer` was granted on the project, the same owner read both views. So the basic role was the cause: Scanisaur's service account needs a BigQuery role granted on the project, not a basic role, and `scanisaur doctor` should test a project-wide query.

## On-demand price

The Cloud Billing catalog lists BigQuery analysis at **$6.25 per TiB** in the US multi-region and in `us-central1`, `us-east1`, `us-east4`, `us-east5`, `us-east7`, `us-west1`, `us-west4` and `us-west8`. Other regions cost more, up to $11.25 per TiB in `southamerica-east1`. The default in `scanisaur.yaml` stays at $6.25. The connector should look up the price for each dataset's location, from the SKU whose description is `Analysis (<region>)` (service `24E6-581D-38E5`).

## Recommendation

**Full sync**, once per region:

1. Query the project-wide `COLUMNS` and `TABLE_OPTIONS` views. These give the schema, partition column, cluster columns and required-filter flag for every table.
2. `tables.list` each dataset, for partition types and views.
3. Query each dataset's `__TABLES__`, for row counts, sizes and last-modified times.
4. Query `TABLE_CONSTRAINTS` and `KEY_COLUMN_USAGE` per dataset for declared keys, and merge in keys from config.

**Incremental sync:** query `__TABLES__` again and `tables.get` only the tables whose last-modified time moved.

**On a cache miss**, when a check names a table the catalog doesn't have, `tables.get` that one table.

Don't query `PARTITIONS` in bulk. Use `numPartitions` from `tables.get`, fetched when the estimator first needs it. Read `PARTITIONS` for one table only when a rule needs per-partition sizes.

At 100,000 tables, full syncs should take seconds to minutes rather than the 12 minutes `tables.get` alone would take. That figure is an estimate; a spike on a larger catalog should measure it. For updates within seconds of a change, BigQuery's audit-log events (table created, updated or deleted) can drive the incremental sync.

This approach needs `roles/bigquery.metadataViewer` and `roles/bigquery.jobUser`, both granted on the project. The spike's catalog account also held `roles/bigquery.resourceViewer`, so confirm with an account that has just those two roles. See [spike 0003](0003-permissions.md).
