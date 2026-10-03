"""Compare audit output, elapsed time and traced peak memory with a Git baseline.

Run: uv run python -m benchmark.audit --baseline-ref claude/m4-audit-cmd --runs 1000
Inputs are synthetic. Both implementations consume fresh generators of the same jobs.
"""

from __future__ import annotations

import argparse
import gc
import json
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import cast

from scanisaur.audit.log import Decision
from scanisaur.audit.report import Report, audit
from scanisaur.catalog import Catalog, Column, Partition, Partitioning, Table
from scanisaur.catalog.connectors import QueryRun
from scanisaur.engine.check import Policy, fingerprint
from scanisaur.engine.result import Verdict

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
WAREHOUSE = "bigquery:p:US"
GIB = 2**30
POLICY = Policy(warn_bytes=100 * GIB, block_bytes=None)
Audit = Callable[..., Report]


def catalog() -> Catalog:
    return Catalog(
        (
            Table("p", "d", "events", (Column("kind", "STRING"), Column("id", "INT64"))),
            Table("p", "d", "users", (Column("email", "STRING"), Column("id", "INT64"))),
            Table(
                "p",
                "d",
                "daily",
                (Column("day", "DATE"), Column("value", "INT64")),
                size_bytes=430 * GIB,
                row_count=430 * GIB // 16,
                partitioning=Partitioning("day", "DAY"),
                partitions=(
                    Partition("20261003", 10 * GIB),
                    Partition("20261004", 400 * GIB),
                    Partition("20261005", 20 * GIB),
                ),
            ),
        ),
        "p",
        "d",
    )


def history(count: int, *, unique: bool = False) -> Iterator[QueryRun]:
    for i in range(count):
        value = i if unique else i % 3
        sqls = (
            "SELECT 1",
            f"SELECT * FROM p.d.events WHERE kind = '{value}'",
            f"SELECT id FROM p.d.users WHERE email = '{value}@example.com' LIMIT 5",
            "SELECT SUM(value) FROM p.d.daily WHERE day = CURRENT_DATE()",
            "DELETE FROM p.d.users WHERE TRUE",
        )
        # Cross day boundaries and include exact duplicate start times.
        yield QueryRun(
            str(i),
            NOW + timedelta(minutes=(i % 300) * 15),
            "agent@x",
            sqls[i % len(sqls)],
            None if i % 7 == 0 else (i % 5) * GIB,
            "stopped" if i % 11 == 0 else "invalidQuery" if i % 13 == 0 else None,
        )


def decisions(count: int) -> Iterator[Decision]:
    for i in range(count):
        yield Decision(
            time=NOW + timedelta(seconds=(i % 300) * 900 - (i % 3) * 60),
            check_id=str(i),
            query=fingerprint("SELECT 1"),
            shape="s_unused",
            source="cli",
            warehouse=None if i % 19 == 0 else "bigquery:other:US" if i % 17 == 0 else WAREHOUSE,
            verdict=(Verdict.PASS, Verdict.WARN, Verdict.BLOCK)[i % 3],
        )


def run_report(implementation: Audit, count: int, unique: bool, top: int = 10) -> Report:
    return implementation(
        history(count, unique=unique),
        catalog(),
        decisions(count),
        since=NOW,
        warehouse=WAREHOUSE,
        policy=POLICY,
        top=top,
    )


def measure(implementation: Audit, count: int, unique: bool) -> tuple[Report, float, int]:
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        report = run_report(implementation, count, unique)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return report, elapsed, peak


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--runs", type=int, default=1000)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    source = subprocess.check_output(
        ["git", "show", f"{args.baseline_ref}:src/scanisaur/audit/report.py"],
        text=True,
    )
    baseline = ModuleType("_audit_baseline")
    sys.modules[baseline.__name__] = baseline
    exec(compile(source, "<audit baseline>", "exec"), baseline.__dict__)
    reference = cast(Audit, baseline.audit)
    for unique in (False, True):
        old, old_time, old_peak = measure(reference, args.runs, unique)
        new, new_time, new_peak = measure(audit, args.runs, unique)
        if old.model_dump(mode="json") != new.model_dump(mode="json"):
            raise AssertionError("optimized report differs from baseline")
        print(
            json.dumps(
                {
                    "workload": "unique literals" if unique else "repeated SQL",
                    "runs": args.runs,
                    "equal_reports": True,
                    "baseline_seconds": round(old_time, 3),
                    "optimized_seconds": round(new_time, 3),
                    "baseline_peak_bytes": old_peak,
                    "optimized_peak_bytes": new_peak,
                }
            )
        )


if __name__ == "__main__":
    main()
