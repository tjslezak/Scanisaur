"""``scanisaur audit``: replay checks over query history and report what they flag (OSS-19).

Each distinct query is checked once, against the current catalog. A run counts as
checked when the decision log has a check of the same query in the hour before it
(docs/spikes/0002-job-history-comments.md).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

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
    bytes_billed: int


class FlaggedShape(BaseModel):
    """Runs of one query shape that the replayed check warned about or blocked."""

    model_config = ConfigDict(frozen=True)

    #: The shape's SQL, with ``?`` for every literal value.
    sql: str
    verdict: Verdict
    rules: tuple[str, ...]
    runs: int
    bytes_billed: int
    #: Runs with no check of the query in the log just before them.
    unchecked_runs: int


class Report(BaseModel):
    model_config = ConfigDict(frozen=True)

    since: datetime
    runs: int
    bytes_billed: int
    flagged_runs: int
    flagged_bytes: int
    unchecked_runs: int
    #: Runs whose latest check before them blocked the query.
    ran_after_block: int
    #: The most expensive first.
    flagged: tuple[FlaggedShape, ...]
    tables: tuple[Total, ...]
    rules: tuple[Total, ...]


@dataclass(frozen=True, slots=True)
class _Run:
    run: QueryRun
    result: CheckResult
    shape: str
    #: The verdict of the latest logged check before the run, or None when unchecked.
    logged: Verdict | None


def audit(
    runs: Iterable[QueryRun],
    catalog: Catalog,
    decisions: Iterable[Decision],
    *,
    since: datetime,
    policy: Policy = DEFAULT_POLICY,
    top: int = 10,
) -> Report:
    """Replay checks over ``runs`` and summarize them, ``top`` lines per list."""
    replayed = _replay(runs, catalog, policy, _by_query(decisions))
    flagged = [r for r in replayed if r.result.verdict is not Verdict.PASS]
    return Report(
        since=since,
        runs=len(replayed),
        bytes_billed=sum(r.run.bytes_billed for r in replayed),
        flagged_runs=len(flagged),
        flagged_bytes=sum(r.run.bytes_billed for r in flagged),
        unchecked_runs=sum(r.logged is None for r in replayed),
        ran_after_block=sum(r.logged is Verdict.BLOCK for r in replayed),
        flagged=_flagged_shapes(flagged)[:top],
        tables=_totals((t, r) for r in replayed for t in r.result.tables)[:top],
        rules=_totals((rule, r) for r in flagged for rule in _rules(r.result))[:top],
    )


def _by_query(decisions: Iterable[Decision]) -> dict[str, list[Decision]]:
    by_query: defaultdict[str, list[Decision]] = defaultdict(list)
    for entry in decisions:
        by_query[entry.query].append(entry)
    for entries in by_query.values():
        entries.sort(key=lambda entry: entry.time)
    return by_query


def _replay(
    runs: Iterable[QueryRun], catalog: Catalog, policy: Policy, logged: dict[str, list[Decision]]
) -> list[_Run]:
    checked: dict[str, tuple[CheckResult, str]] = {}
    replayed = []
    for run in runs:
        query = fingerprint(run.sql)
        if query not in checked:
            result = check(run.sql, catalog, policy=policy, now=run.started)
            checked[query] = result, shape(run.sql)
        result, run_shape = checked[query]
        replayed.append(_Run(run, result, run_shape, _logged(logged.get(query, []), run)))
    return replayed


def _logged(entries: list[Decision], run: QueryRun) -> Verdict | None:
    """The verdict of the latest check in the window before ``run``."""
    earliest, latest = run.started - CHECK_WINDOW, run.started + CLOCK_SKEW
    found = [entry for entry in entries if earliest <= entry.time <= latest]
    return found[-1].verdict if found else None


def _rules(result: CheckResult) -> list[str]:
    flagging = {Severity.WARN, Severity.BLOCK}
    return sorted({f.rule for f in result.findings if f.severity in flagging})


def _flagged_shapes(flagged: list[_Run]) -> tuple[FlaggedShape, ...]:
    groups: defaultdict[str, list[_Run]] = defaultdict(list)
    for run in flagged:
        groups[run.shape].append(run)
    shapes = [
        FlaggedShape(
            sql=sql,
            verdict=_worst(run.result.verdict for run in runs),
            rules=tuple(sorted({rule for run in runs for rule in _rules(run.result)})),
            runs=len(runs),
            bytes_billed=sum(run.run.bytes_billed for run in runs),
            unchecked_runs=sum(run.logged is None for run in runs),
        )
        for sql, runs in groups.items()
    ]
    return tuple(sorted(shapes, key=lambda s: (-s.bytes_billed, -s.runs, s.sql)))


def _worst(verdicts: Iterable[Verdict]) -> Verdict:
    return Verdict.BLOCK if Verdict.BLOCK in set(verdicts) else Verdict.WARN


def _totals(pairs: Iterable[tuple[str, _Run]]) -> tuple[Total, ...]:
    runs: Counter[str] = Counter()
    billed: Counter[str] = Counter()
    for name, run in pairs:
        runs[name] += 1
        billed[name] += run.run.bytes_billed
    totals = [Total(name=n, runs=runs[n], bytes_billed=billed[n]) for n in runs]
    return tuple(sorted(totals, key=lambda t: (-t.bytes_billed, -t.runs, t.name)))
