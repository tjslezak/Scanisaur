# Dry-run benchmark

Scanisaur's cost estimates and rules against BigQuery dry runs, measured on 2026-10-02 with `benchmark/run.py` (62 queries on public tables, issue [#10](https://github.com/tjslezak/Scanisaur/issues/10)). Dry-run bytes are shown as BigQuery bills them: rounded up to a MiB, with at least 10 MiB for each table read.

## Cost estimate

| Confidence | Queries | High end within 3x of the bill | Range contains the bill |
| --- | --- | --- | --- |
| high | 4 | 4 (100%) | 4 (100%) |
| medium | 51 | 27 (53%) | 30 (59%) |
| low | 4 | 3 (75%) | 4 (100%) |
| **All** | 59 | 34 (58%) | 38 (64%) |

| Estimate | Queries | High end within 3x of the bill | Range contains the bill |
| --- | --- | --- | --- |
| One value | 36 | 31 (86%) | 16 (44%) |
| A range | 23 | 3 (13%) | 22 (96%) |

3 queries that BigQuery rejected have no estimate, as intended.

## Rules

| Queries | Count | As expected |
| --- | --- | --- |
| Traps (a rule should fire) | 22 | 22 |
| Others (no rule should fire) | 40 | 40 |

## Every query

| Query | Billed (dry run) | Estimate | High / billed | Rules expected | Rules found |
| --- | --- | --- | --- | --- | --- |
| `trends-one-day` | 47.2 MB | 44 MB (medium) | 0.93 | none | none |
| `trends-no-filter` | 1.2 GB | 1 GB (medium) | 0.89 | SCN003 | SCN003 |
| `trends-week-instead-of-refresh-date` | 1.5 GB | 1.4 GB (medium) | 0.91 | SCN003 | SCN003 |
| `trends-week-and-refresh-date` | 58.7 MB | 55.6 MB (medium) | 0.95 | none | none |
| `trends-last-week` | 268.4 MB | 309.3 MB to 355.5 MB (medium) | 1.32 | none | none |
| `trends-range-of-days` | 507.5 MB | 438.3 MB (medium) | 0.86 | none | none |
| `trends-two-days-in-list` | 71.3 MB | 66.1 MB (medium) | 0.93 | none | none |
| `trends-cast-to-string` | 1.2 GB | 1 GB (medium) | 0.89 | SCN004 | SCN004 |
| `trends-extract-month` | 1.2 GB | 1 GB (medium) | 0.89 | SCN004 | SCN004 |
| `trends-date-trunc-week` | 241.2 MB | 227.5 MB (medium) | 0.94 | none | none |
| `trends-preview` | 99.6 MB | 99.6 MB (medium) | 1.00 | none | none |
| `trends-preview-every-partition` | 3.1 GB | 3.1 GB (medium) | 1.00 | SCN003, SCN005 | SCN003, SCN005 |
| `trends-star-except` | 2.8 GB | 2.8 GB (medium) | 1.00 | SCN003, SCN005 | SCN003, SCN005 |
| `trends-limit-zero` | 0 B | 0 B (high) | 1.00 | none | none |
| `trends-count-star` | 0 B | 0 B (high) | 1.00 | none | none |
| `trends-group-by` | 39.8 MB | 33.6 MB (medium) | 0.84 | none | none |
| `trends-having-on-partition-column` | 11.5 MB | 11.5 MB (high) | 1.00 | none | none |
| `trends-cte-read-twice` | 49.3 MB | 55.6 MB (medium) | 1.13 | none | none |
| `trends-self-join-two-days` | 71.3 MB | 66.1 MB (medium) | 0.93 | none | none |
| `trends-union-of-days` | 76.5 MB | 68.2 MB (medium) | 0.89 | none | none |
| `pageviews-one-article` | 810.5 MB | 10.5 MB to 10 GB (medium) | 12.30 ** | none | none |
| `pageviews-title-without-wiki` | 6.7 GB | 10.5 MB to 6.7 GB (medium) | 1.01 | SCN011 | SCN011 |
| `pageviews-lower-title` | 1.7 GB | 10.5 MB to 10 GB (medium) | 5.95 ** | SCN004 | SCN004 |
| `pageviews-no-partition-filter` | rejected: can be used for partition elimination | none | - | SCN003 | SCN003 |
| `pageviews-cast-datehour` | rejected: can be used for partition elimination | none | - | SCN004 | SCN004 |
| `pageviews-open-ended-range` | 22.8 GB | 10.5 MB to 948.2 GB (medium) | 41.67 ** | none | none |
| `pageviews-timestamp-trunc` | 810.5 MB | 10.5 MB to 10 GB (medium) | 12.30 ** | none | none |
| `pageviews-hours-of-one-day` | 810.5 MB | 10.5 MB to 10 GB (medium) | 12.30 ** | none | none |
| `pageviews-top-titles-week` | 3.6 GB | 10.5 MB to 67.4 GB (medium) | 18.83 ** | none | none |
| `pageviews-by-wiki` | 4.7 GB | 6.7 GB (medium) | 1.44 | none | none |
| `pageviews-preview` | 10 GB | 10 GB (medium) | 1.00 | SCN005 | SCN005 |
| `pageviews-extract-date` | 786.4 MB | 10.5 MB to 6.7 GB (medium) | 8.55 ** | none | none |
| `deps-one-package` | 35.7 MB | 10.5 MB to 15.9 GB (medium) | 446.68 ** | none | none |
| `deps-no-snapshot-filter` | 7.8 GB | 10.5 MB to 1.5 TB (medium) | 194.28 ** | SCN003 | SCN003 |
| `deps-name-without-system` | 165.7 MB | 10.5 MB to 8.8 GB (medium) | 53.06 ** | SCN011 | SCN011 |
| `deps-lower-name` | 308.3 MB | 10.5 MB to 15.9 GB (medium) | 51.66 ** | SCN004 | SCN004 |
| `deps-preview` | 57.4 GB | 10.5 MB to 119.7 GB (medium) | 2.09 | SCN005 | SCN005 |
| `deps-struct-field` | 45.1 MB | 10.5 MB to 24.9 GB (medium) | 552.74 ** | none | none |
| `pypi-one-day-one-project` | 842 MB | 10.5 MB to 267 GB (medium) | 317.14 ** | none | none |
| `pypi-lower-project` | 98.5 GB | 267 GB (medium) | 2.71 | SCN004 | SCN004 |
| `pypi-no-partition-filter` | 564.9 GB | 10.5 MB to 338 TB (medium) | 598.31 ** | SCN003 | SCN003 |
| `pypi-last-week` | 6 GB | 10.5 MB to 1.9 TB (medium) | 325.00 ** | none | none |
| `pypi-first-week-of-month` | 5 GB | 10.5 MB to 1.6 TB (medium) | 317.42 ** | none | none |
| `pypi-struct-field` | 1.2 GB | 10.5 MB to 496 GB (medium) | 408.49 ** | none | none |
| `ga4-first-week` | 10.5 MB | 11.5 MB (medium) | 1.10 | none | none |
| `ga4-event-date-instead-of-suffix` | 99.6 MB | 418.4 MB (medium) | 4.20 ** | SCN003 | SCN003 |
| `ga4-suffix-like` | 22 MB | 84.9 MB (medium) | 3.86 ** | none | none |
| `ga4-narrower-wildcard` | 15.7 MB | 56.6 MB (medium) | 3.60 ** | none | none |
| `ga4-parse-date-suffix` | 10.5 MB | 10.5 MB to 209.7 MB (low) | 20.00 ** | none | none |
| `ga4-preview-one-shard` | 21 MB | 21 MB (medium) | 1.00 | none | none |
| `ga4-preview-every-shard` | 3.6 GB | 3.6 GB (medium) | 1.00 | SCN003, SCN005 | SCN003, SCN005 |
| `ga4-struct-field` | 10.5 MB | 56.6 MB (medium) | 5.40 ** | none | none |
| `ga4-suffix-not-equal` | 56.6 MB | 208.7 MB (medium) | 3.69 ** | SCN003 | SCN003 |
| `ga4-suffix-from-format-date` | 0 B | 0 B (high) | 1.00 | none | none |
| `thelook-order-totals` | 21 MB | 21 MB (low) | 1.00 | none | none |
| `thelook-users-preview` | 19.9 MB | 19.9 MB (medium) | 1.00 | none | none |
| `thelook-events-preview` | 386.9 MB | 386.9 MB (medium) | 1.00 | none | none |
| `thelook-monthly-average` | 10.5 MB | 10.5 MB (low) | 1.00 | none | none |
| `thelook-orders-by-country` | 21 MB | 21 MB (low) | 1.00 | none | none |
| `mix-trending-articles` | 1.5 GB | 54.5 MB to 8.1 GB (medium) | 5.37 ** | none | none |
| `mix-trends-unfiltered` | 2.6 GB | 1 GB to 9.1 GB (medium) | 3.45 ** | SCN003 | SCN003 |
| `mix-pageviews-unfiltered` | rejected: can be used for partition elimination | none | - | SCN003 | SCN003 |
