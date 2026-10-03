"""``scanisaur audit``: replay checks over query history and report what they flag (OSS-19).

Each execution is checked at its start time, against the current catalog. A run counts as
checked when the decision log has a check of the same query in the hour before it
(docs/spikes/0002-job-history-comments.md).
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from operator import attrgetter

from pydantic import BaseModel, ConfigDict

from scanisaur.audit.log import Decision
from scanisaur.catalog.connectors.base import QueryRun
from scanisaur.catalog.model import Catalog
from scanisaur.engine.check import DEFAULT_POLICY, Policy, check, fingerprint, shape
from scanisaur.engine.result import CheckResult, Severity, Verdict

#: A check counts for a run when it came at most this long before it.
CHECK_WINDOW = timedelta(hours=1)
#: Allowance for the local clock running behind the warehouse's.
CLOCK_SKEW = timedelta(minutes=1)


class Total(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    runs: int
    #: Sum of known billed bytes; incomplete when unknown_billing_runs is nonzero.
    bytes_billed: int
    unknown_billing_runs: int = 0


class FlaggedShape(BaseModel):
    """Runs of one query shape that the replayed check warned about or blocked."""

    model_config = ConfigDict(frozen=True)

    #: The shape's SQL, with ``?`` for every literal value.
    sql: str
    verdict: Verdict
    rules: tuple[str, ...]
    runs: int
    #: Sum of known billed bytes; incomplete when unknown_billing_runs is nonzero.
    bytes_billed: int
    unknown_billing_runs: int = 0
    #: Runs with no check of the query in the log just before them.
    unchecked_runs: int


class Report(BaseModel):
    model_config = ConfigDict(frozen=True)

    since: datetime
    runs: int
    #: Sum of known billed bytes; incomplete when unknown_billing_runs is nonzero.
    bytes_billed: int
    unknown_billing_runs: int = 0
    flagged_runs: int
    flagged_bytes: int
    flagged_unknown_billing_runs: int = 0
    #: Failed attempts, including cancellations, separate from successful runs.
    failed_runs: int = 0
    failed_bytes: int = 0
    failed_unknown_billing_runs: int = 0
    failed_after_block: int = 0
    unchecked_runs: int
    #: Runs whose latest check before them blocked the query.
    ran_after_block: int
    #: The most expensive first.
    flagged: tuple[FlaggedShape, ...]
    tables: tuple[Total, ...]
    rules: tuple[Total, ...]


@dataclass(slots=True)
class _Counts:
    runs: int = 0
    bytes_billed: int = 0
    unknown_billing_runs: int = 0

    def add(self, run: QueryRun) -> None:
        self.runs += 1
        self.bytes_billed += run.bytes_billed or 0
        self.unknown_billing_runs += run.bytes_billed is None


@dataclass(slots=True)
class _Shape(_Counts):
    verdict: Verdict = Verdict.WARN
    rules: set[str] = field(default_factory=set)
    unchecked_runs: int = 0


def audit(
    runs: Iterable[QueryRun],
    catalog: Catalog,
    decisions: Iterable[Decision],
    *,
    since: datetime,
    warehouse: str,
    policy: Policy = DEFAULT_POLICY,
    top: int = 10,
) -> Report:
    """Replay checks over ``runs`` once and summarize them, ``top`` lines per list."""
    logged = _by_query(decisions, warehouse)
    # Per-audit, bounded cache: shapes depend on SQL only, verdicts also on run time.
    cached_shape = lru_cache(maxsize=1024)(shape)
    successful, failed, flagged = _Counts(), _Counts(), _Counts()
    unchecked = after_block = failed_after_block = 0
    shapes: defaultdict[str, _Shape] = defaultdict(_Shape)
    tables: defaultdict[str, _Counts] = defaultdict(_Counts)
    rules: defaultdict[str, _Counts] = defaultdict(_Counts)
    for run in runs:
        verdict = _logged(logged.get(fingerprint(run.sql), []), run)
        if run.error_reason is not None:
            failed.add(run)
            failed_after_block += verdict is Verdict.BLOCK
            continue
        successful.add(run)
        unchecked += verdict is None
        after_block += verdict is Verdict.BLOCK
        result = check(run.sql, catalog, policy=policy, now=run.started)
        for name in result.tables:
            tables[name].add(run)
        if result.verdict is Verdict.PASS:
            continue
        flagged.add(run)
        flagging_rules = _rules(result)
        for rule in flagging_rules:
            rules[rule].add(run)
        group = shapes[cached_shape(run.sql)]
        group.add(run)
        group.unchecked_runs += verdict is None
        group.rules.update(flagging_rules)
        if result.verdict is Verdict.BLOCK:
            group.verdict = Verdict.BLOCK
    return Report(
        since=since,
        runs=successful.runs,
        bytes_billed=successful.bytes_billed,
        unknown_billing_runs=successful.unknown_billing_runs,
        failed_runs=failed.runs,
        failed_bytes=failed.bytes_billed,
        failed_unknown_billing_runs=failed.unknown_billing_runs,
        failed_after_block=failed_after_block,
        flagged_runs=flagged.runs,
        flagged_bytes=flagged.bytes_billed,
        flagged_unknown_billing_runs=flagged.unknown_billing_runs,
        unchecked_runs=unchecked,
        ran_after_block=after_block,
        flagged=_flagged_shapes(shapes)[:top],
        tables=_totals(tables)[:top],
        rules=_totals(rules)[:top],
    )


def _by_query(decisions: Iterable[Decision], warehouse: str) -> dict[str, list[Decision]]:
    by_query: defaultdict[str, list[Decision]] = defaultdict(list)
    for entry in decisions:
        if entry.warehouse == warehouse:
            by_query[entry.query].append(entry)
    for entries in by_query.values():
        entries.sort(key=lambda entry: entry.time)
    return by_query


def _logged(entries: list[Decision], run: QueryRun) -> Verdict | None:
    """The verdict of the latest check in the window before ``run``."""
    earliest, latest = run.started - CHECK_WINDOW, run.started + CLOCK_SKEW
    index = bisect_right(entries, latest, key=attrgetter("time")) - 1
    if index >= 0 and entries[index].time >= earliest:
        return entries[index].verdict
    return None


def _rules(result: CheckResult) -> list[str]:
    flagging = {Severity.WARN, Severity.BLOCK}
    return sorted({f.rule for f in result.findings if f.severity in flagging})


def _flagged_shapes(groups: Mapping[str, _Shape]) -> tuple[FlaggedShape, ...]:
    shapes = [
        FlaggedShape(
            sql=sql,
            verdict=group.verdict,
            rules=tuple(sorted(group.rules)),
            runs=group.runs,
            bytes_billed=group.bytes_billed,
            unknown_billing_runs=group.unknown_billing_runs,
            unchecked_runs=group.unchecked_runs,
        )
        for sql, group in groups.items()
    ]
    return tuple(
        sorted(shapes, key=lambda s: (s.unknown_billing_runs == 0, -s.bytes_billed, -s.runs, s.sql))
    )


def _totals(groups: Mapping[str, _Counts]) -> tuple[Total, ...]:
    totals = [
        Total(
            name=name,
            runs=group.runs,
            bytes_billed=group.bytes_billed,
            unknown_billing_runs=group.unknown_billing_runs,
        )
        for name, group in groups.items()
    ]
    return tuple(
        sorted(
            totals, key=lambda t: (t.unknown_billing_runs == 0, -t.bytes_billed, -t.runs, t.name)
        )
    )
