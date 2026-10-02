"""The result of a check. Agents and tools depend on this shape (schema version 1).

Changes within version 1 are additive only; anything breaking bumps ``schema_version``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Severity(StrEnum):
    BLOCK = "block"
    WARN = "warn"
    INFO = "info"


class Verdict(StrEnum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: str
    severity: Severity
    message: str
    #: What the agent should change, when there is a concrete fix.
    fix: str | None = None
    #: 1-based position of the offending identifier or token in the SQL, when known.
    line: int | None = None
    column: int | None = None


class CheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    check_id: str
    #: SQL comment to include in the executed query, so it can be matched in query history.
    #: The same SQL always gets the same tag, so repeated queries can use BigQuery's cache.
    #: It identifies the query, not this check; ``check_id`` identifies the check.
    tag: str
    verdict: Verdict
    findings: tuple[Finding, ...] = ()
    #: Tables the query reads, as ``project.dataset.table``.
    tables: tuple[str, ...] = ()


def verdict_for(findings: tuple[Finding, ...] | list[Finding]) -> Verdict:
    severities = {finding.severity for finding in findings}
    if Severity.BLOCK in severities:
        return Verdict.BLOCK
    if Severity.WARN in severities:
        return Verdict.WARN
    return Verdict.PASS
