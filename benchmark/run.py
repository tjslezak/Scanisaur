"""Dry-run benchmark: Scanisaur's cost estimates and rules against BigQuery's dry runs.

Run from the repository root:

    uv run python benchmark/run.py refresh --project PROJECT
        Snapshot the metadata of the tables in tables.yaml into catalog.yaml, then dry-run
        every query in queries.yaml into dry_runs.json. Needs the bq CLI, logged in. The
        metadata queries bill about 10 MB each; dry runs are free.

    uv run python benchmark/run.py report [--write]
        Compare Scanisaur with the recorded dry runs, without the network. --write saves
        the report to docs/benchmark.md.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp
from sqlglot.errors import SqlglotError

from scanisaur.catalog.fixtures import load_catalog
from scanisaur.catalog.model import Catalog
from scanisaur.engine.check import check
from scanisaur.engine.estimate import MIN_BILLED_BYTES
from scanisaur.engine.pruning import format_bytes
from scanisaur.engine.result import CheckResult, Estimate

HERE = Path(__file__).parent
TABLES = HERE / "tables.yaml"
QUERIES = HERE / "queries.yaml"
CATALOG = HERE / "catalog.yaml"
DRY_RUNS = HERE / "dry_runs.json"
REPORT = HERE.parent / "docs" / "benchmark.md"
#: The rules the queries' `expect` lists cover.
RULES = ("SCN003", "SCN004", "SCN005", "SCN011")
#: Findings that mean Scanisaur couldn't check the query, as when the snapshot lacks a column.
FAILURES = ("SCN000", "SCN001")
_MIB = 2**20
#: Partition ID lengths by granularity; an INT64 partition column means integer ranges.
_GRANULARITIES = {4: "YEAR", 6: "MONTH", 8: "DAY", 10: "HOUR"}
_SPECIAL = ("__NULL__", "__UNPARTITIONED__")
_LABEL = "--label=purpose:scanisaur-benchmark"


class BqError(RuntimeError):
    """bq failed, or BigQuery rejected the query."""


@dataclass(frozen=True)
class Query:
    id: str
    sql: str
    expect: tuple[str, ...] = ()


@dataclass
class DryRuns:
    measured_at: datetime
    #: Bytes processed by query ID, or the error BigQuery gave.
    bytes: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


# Loading ---------------------------------------------------------------------------------


def load_queries(path: Path) -> list[Query]:
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    queries = [
        Query(e["id"], " ".join(e["sql"].split()), tuple(e.get("expect", ()))) for e in entries
    ]
    ids = [q.id for q in queries]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"query IDs are used more than once: {duplicates}")
    return queries


def load_dry_runs(path: Path) -> DryRuns:
    data = json.loads(path.read_text(encoding="utf-8"))
    return DryRuns(
        measured_at=datetime.fromisoformat(data["measured_at"]),
        bytes={k: int(v) for k, v in data.get("bytes", {}).items()},
        errors=dict(data.get("errors", {})),
    )


def save_dry_runs(runs: DryRuns, path: Path) -> None:
    data = {
        "measured_at": runs.measured_at.isoformat(timespec="seconds"),
        "bytes": dict(sorted(runs.bytes.items())),
        "errors": dict(sorted(runs.errors.items())),
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# bq --------------------------------------------------------------------------------------


def _bq(project: str, args: list[str]) -> str:
    command = ["bq", f"--project_id={project}", "--format=json", "--quiet", *args]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise BqError(message[-1] if message else f"bq exited with {result.returncode}")
    return result.stdout


def bq_rows(project: str, sql: str) -> list[dict[str, Any]]:
    """The rows of a query, as bq prints them: every value a string or None."""
    out = _bq(project, ["query", "--use_legacy_sql=false", "--max_rows=1000000", _LABEL, sql])
    rows: list[dict[str, Any]] = json.loads(out) if out.strip() else []
    return rows


def bq_dry_run(project: str, sql: str) -> int:
    """The bytes BigQuery would process for the query."""
    out = _bq(project, ["query", "--use_legacy_sql=false", "--dry_run", _LABEL, sql])
    return parse_dry_run(out)


def parse_dry_run(out: str) -> int:
    """bq prints the job as JSON with --format=json; older versions print a sentence."""
    try:
        job = json.loads(out)
    except json.JSONDecodeError:
        found = re.search(r"process (\d+) bytes", out)
        if found is None:
            raise BqError(f"can't read the dry run's bytes from: {out.strip()[:200]}") from None
        return int(found.group(1))
    statistics = job.get("statistics", {})
    value = statistics.get("query", {}).get(
        "totalBytesProcessed", statistics.get("totalBytesProcessed")
    )
    if value is None:
        raise BqError("the dry run returned no totalBytesProcessed")
    return int(value)


# Snapshot --------------------------------------------------------------------------------


def snapshot(project: str, names: list[str]) -> dict[str, Any]:
    """A catalog fixture for ``names``, read from BigQuery's metadata views."""
    return {"tables": [_table_spec(project, name) for name in names]}


def _table_spec(project: str, name: str) -> dict[str, Any]:
    owner, dataset, table = name.split(".")
    ref = f"`{owner}.{dataset}`"
    wildcard = table.endswith("*")
    where = f"STARTS_WITH(table_id, '{table[:-1]}')" if wildcard else f"table_id = '{table}'"
    sizes = bq_rows(
        project, f"SELECT table_id, row_count, size_bytes FROM {ref}.__TABLES__ WHERE {where}"
    )
    if not sizes:
        raise BqError(f"{name} doesn't exist or isn't readable")
    # A wildcard family's columns are its newest shard's.
    source = max(row["table_id"] for row in sizes) if wildcard else table
    columns = bq_rows(
        project,
        "SELECT column_name, data_type, is_partitioning_column, clustering_ordinal_position "
        f"FROM {ref}.INFORMATION_SCHEMA.COLUMNS WHERE table_name = '{source}' "
        "ORDER BY ordinal_position",
    )
    spec: dict[str, Any] = {
        "name": name,
        "rows": sum(int(row["row_count"]) for row in sizes),
        "bytes": sum(int(row["size_bytes"]) for row in sizes),
        "columns": {row["column_name"]: _type(row["data_type"]) for row in columns},
    }
    clustering = sorted(
        (int(row["clustering_ordinal_position"]), row["column_name"])
        for row in columns
        if row.get("clustering_ordinal_position") not in (None, "")
    )
    if clustering:
        spec["clustering"] = [column for _position, column in clustering]
    if wildcard:
        prefix = table[:-1]
        spec["partitions"] = {
            row["table_id"][len(prefix) :]: int(row["size_bytes"]) for row in sizes
        }
        return spec
    partitions = bq_rows(
        project,
        "SELECT partition_id, total_logical_bytes "
        f"FROM {ref}.INFORMATION_SCHEMA.PARTITIONS WHERE table_name = '{table}'",
    )
    # An unpartitioned table has one row, with a NULL partition ID.
    listed = {
        str(row["partition_id"]): int(row["total_logical_bytes"] or 0)
        for row in partitions
        if row.get("partition_id") is not None
    }
    if listed:
        options = bq_rows(
            project,
            f"SELECT option_value FROM {ref}.INFORMATION_SCHEMA.TABLE_OPTIONS "
            f"WHERE table_name = '{table}' AND option_name = 'require_partition_filter'",
        )
        spec["partitioning"] = _partitioning(columns, listed, options)
        spec["partitions"] = dict(sorted(listed.items()))
    return spec


def _partitioning(
    columns: list[dict[str, Any]], partitions: dict[str, int], options: list[dict[str, Any]]
) -> dict[str, Any]:
    column = next((c for c in columns if c.get("is_partitioning_column") == "YES"), None)
    lengths = {len(pid) for pid in partitions if pid not in _SPECIAL}
    if column is not None and column["data_type"] == "INT64":
        granularity = "RANGE"
    else:
        granularity = _GRANULARITIES.get(max(lengths, default=8), "DAY")
    result: dict[str, Any] = {"granularity": granularity}
    if column is not None:
        result["column"] = column["column_name"]
    if any(str(row.get("option_value")).lower() == "true" for row in options):
        result["required"] = True
    return result


def _type(data_type: str) -> str:
    """The column's type, or JSON (also variable-width) when the fixture can't read it."""
    try:
        exp.DataType.build(data_type, dialect="bigquery")
    except SqlglotError:
        return "JSON"
    return data_type


def write_catalog(spec: dict[str, Any], measured_at: datetime, path: Path) -> None:
    header = (
        f"# Metadata snapshot for the dry-run benchmark, taken {measured_at:%Y-%m-%d %H:%M} UTC\n"
        "# by `benchmark/run.py refresh`. Don't edit it by hand.\n"
    )
    path.write_text(header + yaml.safe_dump(spec, sort_keys=False, width=100), encoding="utf-8")


def refresh(project: str) -> None:
    names = yaml.safe_load(TABLES.read_text(encoding="utf-8"))["tables"]
    measured_at = datetime.now(UTC)
    print(f"Reading metadata for {len(names)} tables...", file=sys.stderr)
    write_catalog(snapshot(project, names), measured_at, CATALOG)
    runs = DryRuns(measured_at=measured_at)
    queries = load_queries(QUERIES)
    for number, query in enumerate(queries, start=1):
        print(f"Dry run {number}/{len(queries)}: {query.id}", file=sys.stderr)
        try:
            runs.bytes[query.id] = bq_dry_run(project, query.sql)
        except BqError as error:
            runs.errors[query.id] = str(error)
    save_dry_runs(runs, DRY_RUNS)


# Report ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    query: Query
    result: CheckResult
    #: What BigQuery would bill, from the dry run; None when it rejected the query.
    billed: int | None
    error: str | None
    #: The tables the query reads, each billed at least 10 MiB.
    tables: int = 1

    @property
    def found(self) -> tuple[str, ...]:
        shown = RULES + FAILURES
        return tuple(sorted({f.rule for f in self.result.findings if f.rule in shown}))

    @property
    def rules_match(self) -> bool:
        return set(self.found) == set(self.query.expect)

    @property
    def ratio(self) -> float | None:
        """The estimate's high end over the bill; None when either is missing."""
        estimate = self.result.estimate
        if estimate is None or self.billed is None:
            return None
        if self.billed == 0:
            return 1.0 if estimate.bytes_high == 0 else math.inf
        return estimate.bytes_high / self.billed

    @property
    def within_3x(self) -> bool:
        return self.ratio is not None and 1 / 3 <= self.ratio <= 3

    @property
    def in_range(self) -> bool:
        estimate = self.result.estimate
        if estimate is None or self.billed is None:
            return False
        # Each table's bytes are rounded up to a MiB, which a dry run's total doesn't show.
        slack = _MIB * self.tables
        return estimate.bytes_low - slack <= self.billed <= estimate.bytes_high + slack


def billed(processed: int, tables: int = 1) -> int:
    """Bytes processed as BigQuery bills them: rounded up to a MiB, with 10 MiB for each
    table read, unless nothing was read. Measured: a join of two tables that processed
    3.9 MB billed 20 MiB, and seven shards of a wildcard table billed 10 MiB."""
    if processed == 0:
        return 0
    return max(-(-processed // _MIB) * _MIB, MIN_BILLED_BYTES * tables)


def tables_read(sql: str) -> int:
    """How many tables the query names, counting a wildcard family once and CTEs not at
    all."""
    try:
        tree = sqlglot.parse_one(sql, dialect="bigquery")
    except SqlglotError:
        return 1
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    names = {
        ".".join(part for part in (table.catalog, table.db, table.name) if part).lower()
        for table in tree.find_all(exp.Table)
        if table.db or table.name.lower() not in ctes
    }
    return max(len(names), 1)


def outcomes(queries: list[Query], catalog: Catalog, runs: DryRuns) -> list[Outcome]:
    results = []
    for query in queries:
        result = check(query.sql, catalog, now=runs.measured_at)
        processed = runs.bytes.get(query.id)
        error = runs.errors.get(query.id)
        if processed is None and error is None:
            error = "not measured"
        tables = tables_read(query.sql)
        cost = None if processed is None else billed(processed, tables)
        results.append(Outcome(query, result, cost, error, tables))
    return results


def report(results: list[Outcome], runs: DryRuns) -> str:
    estimated = [o for o in results if o.ratio is not None]
    lines = [
        "# Dry-run benchmark",
        "",
        f"Scanisaur's cost estimates and rules against BigQuery dry runs, measured on "
        f"{runs.measured_at:%Y-%m-%d} with `benchmark/run.py` ({len(results)} queries on public "
        "tables, issue [#10](https://github.com/tjslezak/Scanisaur/issues/10)). Dry-run bytes are "
        "shown as BigQuery bills them: rounded up to a MiB, with at least 10 MiB for each table "
        "read.",
        "",
        "## Cost estimate",
        "",
        "| Confidence | Queries | High end within 3x of the bill | Range contains the bill |",
        "| --- | --- | --- | --- |",
    ]
    for confidence in ("high", "medium", "low"):
        group = [
            o for o in estimated if o.result.estimate and o.result.estimate.confidence == confidence
        ]
        lines.append(_estimate_row(confidence, group))
    lines.append(_estimate_row("**All**", estimated))
    # A range comes from what metadata can't show, mostly the blocks a cluster filter skips.
    single = [o for o in estimated if o.result.estimate and _single(o.result.estimate)]
    ranges = [o for o in estimated if o not in single]
    lines += [
        "",
        "| Estimate | Queries | High end within 3x of the bill | Range contains the bill |",
        "| --- | --- | --- | --- |",
        _estimate_row("One value", single),
        _estimate_row("A range", ranges),
    ]
    rejected = [o for o in results if o.billed is None and o.result.estimate is None]
    lines += [
        "",
        f"{len(rejected)} queries that BigQuery rejected have no estimate, as intended.",
        "",
        "## Rules",
        "",
    ]
    traps = [o for o in results if o.query.expect]
    fixes = [o for o in results if not o.query.expect]
    lines += [
        "| Queries | Count | As expected |",
        "| --- | --- | --- |",
        f"| Traps (a rule should fire) | {len(traps)} | {sum(o.rules_match for o in traps)} |",
        f"| Others (no rule should fire) | {len(fixes)} | {sum(o.rules_match for o in fixes)} |",
        "",
    ]
    misses = [o for o in results if not o.rules_match]
    if misses:
        lines += ["Mismatches:", ""]
        lines += [
            f"- `{o.query.id}`: expected {_rules(o.query.expect)}, found {_rules(o.found)}"
            for o in misses
        ]
        lines.append("")
    lines += [
        "## Every query",
        "",
        "| Query | Billed (dry run) | Estimate | High / billed | Rules expected | Rules found |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for o in results:
        lines.append(
            f"| `{o.query.id}` | {_billed(o)} | {_estimate(o)} | {_ratio(o)} | "
            f"{_rules(o.query.expect)} | {_rules(o.found)} |"
        )
    return "\n".join(lines) + "\n"


def _single(estimate: Estimate) -> bool:
    return estimate.bytes_low == estimate.bytes_high


def _estimate_row(label: str, group: list[Outcome]) -> str:
    if not group:
        return f"| {label} | 0 | - | - |"
    within = sum(o.within_3x for o in group)
    contains = sum(o.in_range for o in group)
    share = len(group)
    return (
        f"| {label} | {share} | {within} ({within / share:.0%}) | "
        f"{contains} ({contains / share:.0%}) |"
    )


def _billed(o: Outcome) -> str:
    if o.billed is None:
        return f"rejected: {o.error}" if o.error and o.error != "not measured" else "not measured"
    return format_bytes(o.billed)


def _estimate(o: Outcome) -> str:
    estimate = o.result.estimate
    if estimate is None:
        return "none"
    low, high = format_bytes(estimate.bytes_low), format_bytes(estimate.bytes_high)
    shown = low if low == high else f"{low} to {high}"
    return f"{shown} ({estimate.confidence})"


def _ratio(o: Outcome) -> str:
    ratio = o.ratio
    if ratio is None:
        return "-"
    if math.isinf(ratio):
        return "inf"
    return f"{ratio:.2f}" + ("" if o.within_3x else " **")


def _rules(rules: tuple[str, ...]) -> str:
    return ", ".join(rules) if rules else "none"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    refresh_parser = commands.add_parser(
        "refresh", help="snapshot metadata and dry-run every query"
    )
    refresh_parser.add_argument("--project", required=True, help="project that runs the queries")
    report_parser = commands.add_parser("report", help="compare Scanisaur with the dry runs")
    report_parser.add_argument("--write", action="store_true", help=f"save to {REPORT}")
    args = parser.parse_args(argv)
    if args.command == "refresh":
        refresh(args.project)
        return 0
    runs = load_dry_runs(DRY_RUNS)
    text = report(outcomes(load_queries(QUERIES), load_catalog(CATALOG), runs), runs)
    if args.write:
        REPORT.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
