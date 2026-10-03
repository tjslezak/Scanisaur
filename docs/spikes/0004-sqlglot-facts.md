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

It also extracts join facts, and the `LIMIT` and aggregation of the effective outermost query.

> **Update (issue [#6](https://github.com/tjslezak/Scanisaur/issues/6)):** the prototype took a tree qualified with `expand_stars=False` and special-cased CTEs and pseudo-columns per scope. Facts now come from `resolve()`'s tree: the catalog completes every table name, `qualify()` expands stars, and pseudo-columns are attributed to their table. `facts_from_sql()` (or `extract()` on a `Resolution`) is the only entry point. Filters and column lists follow CTEs, subqueries and `UNION` branches down to the base tables, the way BigQuery's planner pushes them. The tables below describe the current behavior.

**Speed:** parse + resolve + extract takes a median of 2.2 ms and a p95 of 3.4 ms per query (27 golden-corpus queries × 30 runs); a ten-CTE join takes 18 ms. The 50 ms engine budget leaves plenty of room.

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
| `FROM a, b, c WHERE b.x = c.x` | A condition links a join only to sources before it: the join to `b` has no condition, the join to `c` does |
| Outer joins | `ON` filters only the side that isn't preserved (`LEFT JOIN u ON u.x = 1` filters `u`, not the left side) |
| `UNNEST` joins | Marked `unnest`, never treated as a cartesian join |
| CTEs | Followed once per reference, each with its own filters; identical facts merge with their scans added; unused CTEs count zero |
| Filters on CTE, subquery or `UNION` columns | Rewritten onto the base table (`b.day = 'd'` becomes `DATE(events.event_ts) = 'd'`). They pass `GROUP BY` keys, and windows only on `PARTITION BY` keys; they stop at `LIMIT`, `QUALIFY`, aggregates, subqueries and recursive CTEs |
| Filters on an outer join's NULL-filled side | Kept only if they reject NULLs: `u.country = 'US'` filters `u`; `u.id IS NULL` (an anti-join) doesn't |
| Whole-row references (`TO_JSON_STRING(t)`, `ARRAY_AGG(e)`) | Every column of `t` is read; `qualify()` turns them into `exp.TableColumn` nodes |
| Subqueries in output columns nobody reads, or inside `UNNEST(...)` | Not scanned, as BigQuery drops them; a subquery inside a read `UNNEST` is |
| `SELECT *` in a CTE or subquery | Recorded as `star`, but `columns` holds only what the readers use; `UNION ALL` prunes by position, `DISTINCT` and `UNION DISTINCT` read every column |
| Derived tables, correlated `EXISTS`, `UNION ALL` branches | Facts per branch; correlated columns count for the outer table |
| Wildcard tables and `_TABLE_SUFFIX`, `_PARTITIONDATE` | Pseudo-columns attributed by `resolve()`, including in joins |
| `QUALIFY` | Predicates kept but labeled, since they don't prune |
| Struct field access (`e.device.category`) | Reported as the top-level column `device` |
| Pipe syntax (`FROM ... \|> WHERE ... \|> AGGREGATE ...`) | `sqlglot` rewrites it into CTEs; pass-through wrappers are skipped when finding the outermost query |
| `INSERT ... SELECT` | Facts for the source tables; the statement guard (SCN002) handles the write |

## Gaps the engine must handle

| Construct | What happens now | Plan |
| --- | --- | --- |
| Unknown table | `qualify()` accepts it silently and fails later on its columns | Done: `resolve.py` looks up tables first (SCN001) |
| `INFORMATION_SCHEMA` queries | `qualify()` can't resolve the columns | Done: views are skipped; they have no table facts |
| Multi-statement scripts (`DECLARE`, `SET`) | `parse()` returns several statements; script variables look like unknown columns | Done: SCN000 per policy |
| A CTE read with different filters at every level | Each set of filters is visited separately; identical reads merge, so ordinary chains stay linear | Over 1,000 visits beyond one per scope raises `FactsError` |
| Query parameters (`@run_date`) | Constant, with the value `@run_date` | Estimator treats `=` as one unknown partition and ranges as unknown, with medium confidence |
| Typed literals (`DATE '2026-09-01'`) | Values arrive as `CAST('2026-09-01' AS DATE)` | Estimator evaluates them with `sqlglot` |
| `TABLESAMPLE SYSTEM (n PERCENT)` | Not captured | Add a sample percentage to `TableFacts` in M1; it reduces bytes |
| `OR` across different columns | `other`, so no pruning is assumed | Acceptable for v0.1: overestimates are safer than underestimates |

## Open questions for the BigQuery spikes

- **Which wrappers defeat partition pruning?** `DATE(ts)`, `CAST(...)` and `TIMESTAMP_TRUNC(ts, DAY)` all come through as wrappers. Before SCN004 ships, dry runs on a partitioned table must show which of them actually prevent pruning ([#1](https://github.com/tjslezak/Scanisaur/issues/1), [#3](https://github.com/tjslezak/Scanisaur/issues/3)). *Answered:* dry runs showed that most wrappers still prune; [SCN004](../rules/scn004.md) lists what was measured.
- **Are only referenced struct fields billed?** If BigQuery bills only the referenced leaf fields of a `STRUCT`, the estimator needs field-level facts instead of the top-level column.

## sqlglot 30 notes for contributors

- `exp.Expr` is the base node type; `parse_one` returns it. Annotate with `exp.Expr`, not `exp.Expression`.
- Set-operation branches are `Scope.set_operation_scopes` (formerly `union_scopes`).
- `SELECT * EXCEPT (...)` stores the excluded columns in `Star.args["except_"]`.
- `qualify()` turns BigQuery pseudo-columns (`_TABLE_SUFFIX`, `_PARTITIONTIME`, `_PARTITIONDATE`) into `exp.Pseudocolumn` nodes, a `Column` subclass that `Scope.columns` leaves out; find them by node type.
- `qualify()` rewrites `GROUP BY 1` into a reference to the output alias, so `GROUP BY`, `ORDER BY`, `HAVING` and `QUALIFY` can name output columns.
- `qualify()` expands stars and drops their `EXCEPT` and `REPLACE` lists, so `resolve()` first records them in each `SELECT` node's `meta`, which travels with the node (`stars_of()`).
- `qualify()` turns a whole-row reference such as `TO_JSON_STRING(t)` into an `exp.TableColumn`; functions sqlglot doesn't know, including user-defined aggregates, parse as `exp.Anonymous`.
