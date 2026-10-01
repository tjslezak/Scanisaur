# Spike 0004: per-table facts with sqlglot

- **Issue:** [#4](https://github.com/tjslezak/Scanisaur/issues/4)
- **Date:** 2026-10-01
- **Status:** Done

## Question

Can `sqlglot` give the rules and the cost estimator everything they need about each table a query reads, without the engine walking the syntax tree in every rule?

## Answer

Yes. With `sqlglot` 30.21, a prototype extractor ([`src/scanisaur/engine/facts.py`](../../src/scanisaur/engine/facts.py)) builds, for every table reference:

- the columns read
- whether a star is used, and any `EXCEPT` columns
- filter predicates (operator, constant values, wrapping function, clause)
- how many times the table is scanned

It also extracts join facts, and the `LIMIT` and aggregation of the effective outermost query. The input is a tree already resolved by `qualify(..., expand_stars=False)`. All 35 corpus tests pass ([`tests/engine/test_facts.py`](../../tests/engine/test_facts.py)).

**Speed:** parse + qualify + extract takes a median of 1.9 ms and a p95 of 2.8 ms per query (22 corpus queries × 50 runs). The 50 ms engine budget leaves plenty of room.

## What works

| Construct | Behavior |
| --- | --- |
| Literal and `IN` filters, flipped comparisons (`'x' <= col`) | Normalized operator and constant values |
| `x = 'a' OR x = 'b'` on one column | Treated as `IN`, which BigQuery can prune |
| Relative dates (`DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)`) | Constant |
| Scalar or `IN` subquery on the right-hand side | Not constant, so no pruning is assumed |
| Function-wrapped columns (`DATE(ts)`, `CAST(...)`, `TIMESTAMP_TRUNC(...)`) | Wrapper recorded for SCN004 |
| `JOIN ... ON`, `USING`, comma joins with the condition in `WHERE` | Join keys extracted; `USING` arrives as `ON` |
| Comma join with no condition | `has_condition = False` (SCN006) |
| `UNNEST` joins | Marked `unnest`, never treated as a cartesian join |
| CTEs | Scan count follows references: used twice counts twice, unused counts zero |
| Derived tables, correlated `EXISTS`, `UNION ALL` branches | Facts per branch; correlated columns count for the outer table |
| Wildcard tables and `_TABLE_SUFFIX`, `_PARTITIONDATE` | Pseudo-columns attributed to the scope's only table |
| `QUALIFY` | Predicates kept but labeled, since they don't prune |
| Struct field access (`e.device.category`) | Reported as the top-level column `device` |
| Pipe syntax (`FROM ... \|> WHERE ... \|> AGGREGATE ...`) | `sqlglot` rewrites it into CTEs; pass-through wrappers are skipped when finding the outermost query |
| `INSERT ... SELECT` | Facts for the source tables; the statement guard (SCN002) handles the write |

## Gaps the engine must handle

| Construct | What happens now | Plan |
| --- | --- | --- |
| Unknown table | `qualify()` accepts it silently and fails later on its columns | `resolve.py` looks up tables first (already planned) |
| `INFORMATION_SCHEMA` queries | `qualify()` can't resolve the columns | Recognize them in `resolve.py` and pass them as metadata queries |
| Multi-statement scripts (`DECLARE`, `SET`) | `parse()` returns several statements; script variables look like unknown columns | v0.1: return "could not analyze" per policy |
| Query parameters (`@run_date`) | Constant, with the value `@run_date` | Estimator treats `=` as one unknown partition and ranges as unknown, with medium confidence |
| Typed literals (`DATE '2026-09-01'`) | Values arrive as `CAST('2026-09-01' AS DATE)` | Estimator evaluates them with `sqlglot` |
| `TABLESAMPLE SYSTEM (n PERCENT)` | Not captured | Add a sample percentage to `TableFacts` in M1; it reduces bytes |
| `OR` across different columns | `other`, so no pruning is assumed | Acceptable for v0.1: overestimates are safer than underestimates |

## Open questions for the BigQuery spikes

- **Which wrappers defeat partition pruning?** `DATE(ts)`, `CAST(...)` and `TIMESTAMP_TRUNC(ts, DAY)` all come through as wrappers. Before SCN004 ships, dry runs on a partitioned table must show which of them actually prevent pruning ([#1](https://github.com/tjslezak/Scanisaur/issues/1), [#3](https://github.com/tjslezak/Scanisaur/issues/3)).
- **Are only referenced struct fields billed?** If BigQuery bills only the referenced leaf fields of a `STRUCT`, the estimator needs field-level facts instead of the top-level column.

## sqlglot 30 notes for contributors

- `exp.Expr` is the base node type; `parse_one` returns it. Annotate with `exp.Expr`, not `exp.Expression`.
- Set-operation branches are `Scope.set_operation_scopes` (formerly `union_scopes`).
- `SELECT * EXCEPT (...)` stores the excluded columns in `Star.args["except_"]`.
- BigQuery pseudo-columns (`_TABLE_SUFFIX`, `_PARTITIONTIME`, `_PARTITIONDATE`) are left unqualified by `qualify()`.
