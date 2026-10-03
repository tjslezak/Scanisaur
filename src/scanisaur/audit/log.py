"""The decision log: one JSON line per check, in a file per month (OSS-18).

An entry holds what a check decided, never literal values: findings keep their rule,
severity and position but not their messages, which can quote a filter's values. Raw
SQL is logged only when ``log.raw_sql`` is on.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import platformdirs
from pydantic import BaseModel, ConfigDict, ValidationError

from scanisaur.config import LogSettings
from scanisaur.engine.check import fingerprint, shape_fingerprint
from scanisaur.engine.result import CheckResult, Estimate, Severity, Verdict

logger = logging.getLogger(__name__)

#: Where a check came from.
Source = Literal["cli", "mcp", "hook"]


class LoggedFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: str
    severity: Severity
    line: int | None = None
    column: int | None = None


class Decision(BaseModel):
    """One log line. Changes within version 1 are additive only."""

    model_config = ConfigDict(frozen=True)

    v: Literal[1] = 1
    time: datetime
    check_id: str
    #: The query's exact-text fingerprint, as in its tag: matches runs in query history.
    query: str
    #: The literal-free fingerprint, shared by queries that differ only in constants.
    shape: str
    source: Source
    warehouse: str | None = None
    verdict: Verdict
    findings: tuple[LoggedFinding, ...] = ()
    tables: tuple[str, ...] = ()
    estimate: Estimate | None = None
    sql: str | None = None


def decision(
    result: CheckResult,
    sql: str,
    *,
    source: Source,
    warehouse: str | None = None,
    raw_sql: bool = False,
    shape_id: str | None = None,
    now: datetime | None = None,
) -> Decision:
    """The log entry for ``result``, the check of ``sql``."""
    return Decision(
        time=now or datetime.now(UTC),
        check_id=result.check_id,
        query=fingerprint(sql),
        shape=shape_id if shape_id is not None else shape_fingerprint(sql),
        source=source,
        warehouse=warehouse,
        verdict=result.verdict,
        findings=tuple(
            LoggedFinding(rule=f.rule, severity=f.severity, line=f.line, column=f.column)
            for f in result.findings
        ),
        tables=result.tables,
        estimate=result.estimate,
        sql=sql if raw_sql else None,
    )


def log_directory(settings: LogSettings) -> Path:
    if settings.path is not None:
        return settings.path.expanduser()
    return Path(platformdirs.user_state_dir("scanisaur")) / "log"


def append(directory: Path, entry: Decision) -> None:
    """Add ``entry`` to its month's file, raising OSError when it can't be written.

    One ``write`` on a file opened for appending, so lines from processes that log at
    the same time don't interleave.
    """
    directory.mkdir(parents=True, exist_ok=True)
    data = (entry.model_dump_json(exclude_none=True) + "\n").encode()
    fd = os.open(_month_file(directory, entry.time), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def read(directory: Path, since: datetime) -> Iterator[Decision]:
    """Entries from ``since`` on, in file order. Lines that don't parse are skipped."""
    first = _month_file(directory, since).name
    for path in sorted(directory.glob("*.jsonl")):
        if path.name >= first:
            yield from (entry for entry in _entries(path) if entry.time >= since)


def _entries(path: Path) -> Iterator[Decision]:
    with path.open(encoding="utf-8", errors="replace") as lines:
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                yield Decision.model_validate_json(line)
            except ValidationError:
                logger.warning("%s:%d: not a decision log entry, skipped", path, number)


def _month_file(directory: Path, time: datetime) -> Path:
    return directory / f"{time.astimezone(UTC):%Y-%m}.jsonl"
