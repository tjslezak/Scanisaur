"""Behavioral comparisons with reports captured from the corrected PR #43 baseline."""

import json
import random
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from benchmark.audit import NOW, WAREHOUSE, run_report

from scanisaur.audit.log import Decision
from scanisaur.audit.report import CHECK_WINDOW, CLOCK_SKEW, _logged, audit
from scanisaur.catalog import Catalog
from scanisaur.catalog.connectors import QueryRun
from scanisaur.engine.result import Verdict


@pytest.mark.parametrize("case_index", range(12))
def test_report_matches_corrected_baseline(case_index: int) -> None:
    cases = json.loads((Path(__file__).parent / "fixtures" / "reports.json").read_text())
    case = cases[case_index]
    report = run_report(audit, case["count"], case["unique"], case["top"])
    assert report.model_dump(mode="json") == case["report"]


def test_binary_search_matches_window_scan() -> None:
    rng = random.Random(43)
    entries = [
        Decision(
            time=NOW + timedelta(seconds=rng.randrange(-7200, 7200)),
            check_id=str(i),
            query="q_same",
            shape="s_same",
            source="cli",
            warehouse=WAREHOUSE,
            verdict=rng.choice(list(Verdict)),
        )
        for i in range(500)
    ]
    # Explicit inclusive boundaries and equal-time ordering.
    for i, seconds in enumerate((-3601, -3600, 60, 60, 61)):
        entries.append(
            Decision(
                time=NOW + timedelta(seconds=seconds),
                check_id=f"boundary{i}",
                query="q_same",
                shape="s_same",
                source="cli",
                warehouse=WAREHOUSE,
                verdict=Verdict.BLOCK if i % 2 else Verdict.PASS,
            )
        )
    entries.sort(key=lambda entry: entry.time)
    for seconds in [0, -14400, 14400, *(rng.randrange(-7200, 7200) for _ in range(200))]:
        run = QueryRun("job", NOW + timedelta(seconds=seconds), None, "SELECT 1", 0)
        found = [
            entry
            for entry in entries
            if run.started - CHECK_WINDOW <= entry.time <= run.started + CLOCK_SKEW
        ]
        assert _logged(entries, run) == (found[-1].verdict if found else None)
    assert _logged([], QueryRun("empty", NOW, None, "SELECT 1", 0)) is None


def test_history_is_consumed_only_once() -> None:
    class Once:
        def __init__(self) -> None:
            self.iterations = 0

        def __iter__(self) -> Iterator[QueryRun]:
            self.iterations += 1
            assert self.iterations == 1
            yield QueryRun("job", NOW, None, "SELECT 1", 0)

    history = Once()
    report = audit(history, Catalog(()), [], since=NOW, warehouse=WAREHOUSE)
    assert report.runs == 1
