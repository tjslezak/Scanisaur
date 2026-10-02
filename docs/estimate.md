# Cost estimate

Every check that gets past name resolution returns an estimate: the bytes BigQuery would bill for the query under on-demand pricing, as a range with a confidence, plus dollars at the policy's price ($6.25 per TiB by default). It uses table metadata only, never a dry run.

```json
"estimate": {
  "bytes_low": 374341632,
  "bytes_high": 374341632,
  "confidence": "medium",
  "usd_low": 0.0021,
  "usd_high": 0.0021
}
```

The estimate is `null` when:

- the query can't be analyzed (SCN000), names something that doesn't exist (SCN001), or is blocked as a write (SCN002);
- BigQuery would reject it, because a table requires a partition filter it doesn't have (SCN003 or SCN004 blocks), so it would bill nothing;
- it reads a view or an external table, which bill differently, or a table whose size the catalog doesn't have;
- it reads through a table-valued function such as `ML.PREDICT(MODEL m, TABLE t)`, or an `INFORMATION_SCHEMA` view.

With capacity (Editions) pricing, the dollar fields are `null` and only bytes are given.

## Each column of each partition is billed once

BigQuery bills each column of each partition once per query, however many times the query reads it. Measured on 2026-10-01 on Google Trends' `top_terms` ([#15](https://github.com/tjslezak/Scanisaur/issues/15)):

| Query | Billed |
| --- | --- |
| A CTE read twice: two aggregates over one partition, joined | 66.1 MB |
| The same two aggregates in one pass | 66.1 MB |
| A self-join with the same partition on both sides | 59.8 MB, the same as one read of those columns |
| A self-join of two different partitions | 120.6 MB |

So for each table, the estimate goes partition by partition. For each one, it takes the union of the columns that every reference to the table reads there. It never counts a CTE once per reference. A repeated read costs slot time, not bytes.

## How the bytes are worked out

- **Columns and fields:** the unit is the leaf field. A scalar column is one; a `STRUCT`, or an `ARRAY` of structs, has one for each field, nested fields included. Fixed-width fields use BigQuery's sizes: 8 bytes for `INT64` (and its aliases such as `INTEGER`), `FLOAT64`, `DATE`, `DATETIME`, `TIME` and `TIMESTAMP`; 16 for `NUMERIC` and `INTERVAL`; 32 for `BIGNUMERIC`; and 1 for `BOOL`. Each one is the row count times its width. The fields of variable-width columns (`STRING`, `BYTES`, `JSON`, `GEOGRAPHY`, `ARRAY`, and structs holding them) split the rest of the table's bytes equally, so a struct of ten fields counts ten times a `STRING`. When the fixed-width fields would fill the table on their own (NULLs take no space), or there is no row count, all fields split the table equally. Pseudo-columns such as `_PARTITIONTIME` and `_TABLE_SUFFIX` are free.
- **Struct fields:** a query that reads `device.category` is billed for that field alone, as BigQuery bills it. So is a field reached through `UNNEST`, as in `SELECT p.key FROM t, UNNEST(params) AS p` or `(SELECT value FROM UNNEST(params) WHERE key = 'page')`, and a field read through a CTE or subquery. The whole column counts when the query uses it whole: selecting, comparing or grouping it, `DISTINCT`, or a function of it such as `ARRAY_LENGTH(params)` or `TO_JSON_STRING(p)`.
- **Partitions:** the filters that [SCN003](rules/scn003.md) counts as pruning are evaluated against the catalog's list of partitions. Filters that don't prune, such as `CAST(day AS STRING) = '…'` or `day != '…'`, read every partition, as BigQuery does. `__NULL__` holds NULLs; on a column-partitioned table `__UNPARTITIONED__` holds dates before 1960 or after 2159, and on an ingestion-time table the streaming buffer. Each column's share of the table is assumed to be the same in every partition.
- **Shards:** every condition on `_TABLE_SUFFIX` is evaluated against the shard names, `!=`, `NOT IN` and `NOT LIKE` included, because BigQuery checks constant filters against them. In a narrower wildcard such as `events_2026*`, `_TABLE_SUFFIX` is what follows `events_2026`.
- **Newer partitions:** partitions written after the catalog was read aren't listed. When a filter can pick dates after the newest listed partition, such as `day = CURRENT_DATE()`, each of those days counts in the high end at the size of the newest partition, and the confidence is at most medium.
- **Clustering and sampling:** a filter on a cluster column may skip blocks that metadata can't see, and `TABLESAMPLE` reads only the blocks it picks. The high end stays at the partitions' size and the low end drops to the minimum.
- **Rounding and minimum:** each table's bytes are rounded up to a whole MiB, with at least 10 MiB, as BigQuery bills them. A table the query reads no columns of, as in `SELECT COUNT(*)`, adds nothing, and so does a query with an outer `LIMIT 0`.

## What partition filters are evaluated

| Filter | Example |
| --- | --- |
| The partition column against a constant: `=`, `<`, `<=`, `>`, `>=`, `BETWEEN`, `IN`, an `OR` of these, and `IS NULL` | `day BETWEEN '2026-09-01' AND '2026-09-07'` |
| Under `DATE()`, `CAST(… AS DATE)`, `EXTRACT(DATE FROM …)`, `TIMESTAMP()`, the `_TRUNC` functions, `DATE_ADD` and the like | `DATE(ts) = '2026-09-30'` |
| Constants built from `CURRENT_DATE()`, `CURRENT_TIMESTAMP()`, date arithmetic, `DATE(y, m, d)` and typed literals | `day >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)` |
| `_PARTITIONTIME` and `_PARTITIONDATE`, which hold the partition's start | `_PARTITIONDATE = '2026-09-30'` |
| `_TABLE_SUFFIX` against a string, `LIKE`, or `FORMAT_DATE` of a date constant, and a narrower wildcard name | `_TABLE_SUFFIX >= FORMAT_DATE('%Y%m%d', CURRENT_DATE())` |

Conditions known not to prune keep every partition: `!=`, `IS NOT NULL`, the functions [SCN004](rules/scn004.md) lists, and comparisons with another column or a subquery. Any other condition on the partition column, such as a time zone (`DATE(ts, 'America/New_York')`), `EXTRACT(YEAR FROM …)`, `NOT (…)`, `IN UNNEST(@days)`, `PARSE_DATE` of `_TABLE_SUFFIX`, or an integer-range partition, may prune in ways this doesn't evaluate. The high end then keeps every partition the other filters allow, and the low end drops to the minimum. One exception: `=` or `IN` with values that aren't known here, such as `day = @day`, keeps at most that many partitions, so the high end is the largest of them.

## Confidence

| Confidence | When |
| --- | --- |
| **high** | Every column read has a fixed width, and every partition filter was evaluated. |
| **medium** | A variable-width column is read, `=` picks a partition that isn't known here, a filter may pick partitions newer than the catalog, or a cluster filter may skip blocks. |
| **low** | A partition filter's effect isn't known: it wasn't evaluated, or the catalog has no partition list. Also with `TABLESAMPLE`, and when column sizes can't be told apart: no row count, or fixed-width columns that would fill the table. |

A query's confidence is the lowest of its tables'.

## Measured assumptions

Measured on 2026-10-02 with dry runs, and four real queries that billed at most 20 MiB:

| Assumption | Result |
| --- | --- |
| A query that prunes a table to nothing is billed nothing, not the minimum | **Holds.** Trends filtered to a date with no partition processed and billed 0 bytes. |
| Each table read is billed at least 10 MiB | **Holds.** A query that processed 169 bytes billed 10,485,760. A join of two tables that processed 3.9 MB billed 20 MiB. Seven shards of a wildcard table that processed 3.2 MB billed 10 MiB: a wildcard family counts as one table. |
| `_TABLE_SUFFIX != '…'` and `NOT LIKE` skip the shards they rule out | **Holds.** GA4's shards: 55.95 MB in all, 55.61 MB with `!= '20210131'`, 40.39 MB with `NOT LIKE '202101%'`. |
| An outer `LIMIT 0` reads nothing | **Holds.** It processed 0 bytes, and the estimate now gives 0. |
| BigQuery bills the whole struct column | **Doesn't hold:** it bills the fields a query reads, and the estimate now does too ([#22](https://github.com/tjslezak/Scanisaur/issues/22)). On GA4's 92 shards, `device.category` processed 36.9 MB against 310 MB for all of `device`, and `i.item_name` through `UNNEST(items) AS i` 113 MB against 992 MB. `ARRAY_LENGTH(items)` read all 992 MB. |

## How close the split is

Metadata gives a table's size, not its columns'. Splitting the variable-width bytes by leaf field is the best guess it allows, and a single column can still be far off. Measured with dry runs on 2026-10-02 ([#22](https://github.com/tjslezak/Scanisaur/issues/22)), the estimate against the real size:

| Table | Column | Estimate ÷ real |
| --- | --- | --- |
| GA4 `events_*` | `event_name`, `event_date`, `user_pseudo_id` (STRING) | 0.68, 0.88, 0.43 |
| GA4 `events_*` | `device` (15 fields), `items` (array of 26) | 1.8, 1.0 |
| GA4 `events_*` | `event_params` (array of 5, about 15 per row) | 0.13 |
| deps.dev `PackageVersions` | `Name`, `System` (STRING) | 0.79, 5.9 |
| deps.dev `PackageVersions` | `Hashes` (array of 2), `Attestations` (array of 5, mostly empty) | 0.19, 16 |

Arrays vary the most, because metadata doesn't say how many elements a row holds. Across the nine array columns of these two tables, the middle ratio is 0.95: counting each field once is neither high nor low on the whole. The [dry-run benchmark](benchmark.md) shows how this works out for whole queries.
