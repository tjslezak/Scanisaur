# Dry-run benchmark

This checks Scanisaur against BigQuery itself, on public tables, using free dry runs. It is the first part of [#10](https://github.com/tjslezak/Scanisaur/issues/10). The agent benchmark, BigQuery's MCP server with and without Scanisaur, comes once Scanisaur has its own MCP server.

It measures two things:

- **Cost estimate:** for each query, Scanisaur's estimate against the bytes the dry run reports, shown as BigQuery bills them: rounded up to a MiB, with at least 10 MiB for each table read (a wildcard family counts as one). A dry run gives only a query's total, so for a query reading several tables the bill is a range, from every byte in one table to every table read, and the estimate is compared with the nearest end. The milestone target is at least 80% of queries within 3x.
- **Rules:** each query lists the rules that should fire (`expect` in `queries.yaml`). Traps should be caught, and their fixes should pass.

The results are in [docs/benchmark.md](../docs/benchmark.md).

## Files

| File | What it holds |
| --- | --- |
| `tables.yaml` | The public tables, from #10: required and optional partition filters, clustered or not, daily shards, and a small schema with no partitions |
| `queries.yaml` | About 60 queries, in trap-and-fix pairs |
| `catalog.yaml` | The tables' metadata: columns, sizes, partitioning, clustering and every partition's size. Generated |
| `dry_runs.json` | The bytes each query's dry run reported, or BigQuery's error. Generated |
| `run.py` | Builds the two generated files, and the report |

## Refresh the measurements

You need the `bq` CLI, logged in with `gcloud auth login`, and a project to run the queries in. From the repository root:

```bash
uv run python benchmark/run.py refresh --project YOUR_PROJECT
uv run python benchmark/run.py report --write
```

`refresh` rewrites `catalog.yaml` and `dry_runs.json`, taking the metadata and the dry runs together so they match. It writes both only once every dry run is done, so an interrupted refresh leaves them as they were. The metadata queries read `INFORMATION_SCHEMA` and bill about 10 MB each, under $0.01 in all. The dry runs are free. Every query carries the label `purpose:scanisaur-benchmark`.

`report` reads only the files, so it runs anywhere, including CI. It evaluates `CURRENT_DATE()` at the time the dry runs were taken.
