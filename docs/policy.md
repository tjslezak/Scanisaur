# Policy file: `scanisaur.yaml`

The policy decides what a check blocks, warns about or lets through. `scanisaur check` reads it from `scanisaur.yaml` in the working directory, or from the file given with `--config`. Without either, it uses the defaults below.

```yaml
pricing:
  model: on_demand        # or editions: estimates in bytes, no dollars
  usd_per_tib: 6.25
policy:
  read_only: true         # block writes, DDL and exports (SCN002)
  fail_mode: open         # SQL that can't be analyzed (SCN000): warn (open) or block (closed)
  warn_bytes: 100GiB      # SCN010; a number of bytes, a size such as 500 GB, or off
  block_bytes: 1TiB
  cross_join_warn_pairs: 100000000      # SCN006
  cross_join_block_pairs: 10000000000
  unbounded_result_rows: 10000          # SCN009
  rules:                  # per-rule severity: off, info, warn or block
    SCN005: off
```

Every key is optional. `profile` and `planner` are accepted, so a whole project file loads, but nothing reads them yet. `warehouse`, `cache` and `keys` are described under [Warehouse, cache and keys](#warehouse-cache-and-keys). Any other key, an unknown rule ID, or a `warn_bytes` set larger than `block_bytes` is an error, and `scanisaur check` exits with 2.

## Warehouse, cache and keys

```yaml
warehouse:
  type: bigquery          # or duckdb
  project: acme-analytics
  location: US            # default US
  billing_project: acme-billing           # optional; the warehouse project by default
  include_datasets: [analytics, marts]    # optional; every dataset when left out
  exclude_datasets: [scratch]             # optional
cache:
  ttl: 6h                 # a snapshot older than this is refreshed; 30m, 1d, 90s or seconds
  path: ~/scanisaur.sqlite                # optional; the user cache directory by default
keys:                     # column sets unique in a table, for SCN007
  acme-analytics.marts.orders: [[order_id]]
```

A DuckDB warehouse takes `path`, the database file, instead of `project`, `location` and `billing_project`. A relative path is read from the directory that holds `scanisaur.yaml`. BigQuery credentials come from Application Default Credentials and never from this file.

BigQuery doesn't enforce primary keys and most tables declare none, so `keys` lists the column sets you know are unique. Each table name must be `project.dataset.table`.

## Sizes

Decimal units (`KB`, `MB`, `GB`, `TB`, `PB`) are powers of 1,000, and binary units (`KiB` to `PiB`) powers of 1,024, in any case and with or without a space. BigQuery's on-demand price is per TiB.

## Rule overrides

`policy.rules` sets the severity of every finding of a rule, after the rule has run: `off` drops them, and `info`, `warn` or `block` replaces their severity. It applies to every rule, SCN002 included, so it can loosen as well as tighten. A query that BigQuery would reject still gets no estimate when its SCN003 block is overridden.

## Flags

The flags of `scanisaur check` change the file's policy for one run: `--allow-writes` turns `read_only` off, `--fail-closed` sets `fail_mode: closed`, and `--capacity-pricing` drops dollars.
