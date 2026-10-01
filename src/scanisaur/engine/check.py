"""Check one SQL statement against a catalog and a policy."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Literal

from sqlglot import exp

from scanisaur.catalog.model import Catalog, Table
from scanisaur.engine.parse import (
    DIALECT,
    SqlParseError,
    classify,
    describe,
    parse,
    resolvable,
)
from scanisaur.engine.resolve import ResolveError, resolve
from scanisaur.engine.result import CheckResult, Finding, Severity, verdict_for
from scanisaur.engine.rules import UNANALYZABLE, WRITE_STATEMENT

#: Writes whose names are checked when the policy allows writes: the query they run,
#: and an INSERT's target. Names in other writes (UPDATE, MERGE, ...) aren't checked yet.
_CHECKED_WRITES = (exp.Insert, exp.Create, exp.Export)

#: Fixes for SQL that can't be analyzed.
_SEND_ONE = "Send one complete SQL query."
_BY_HAND = "Check the table and column names by hand before running it."

#: Crockford base32, lowercase: sortable and unambiguous to read aloud.
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_RANDOM_BITS = 40


@dataclass(frozen=True, slots=True)
class Policy:
    #: Block write, DDL and export statements (SCN002).
    read_only: bool = True
    #: When SQL can't be analyzed (SCN000): warn and let it run ("open"), or block ("closed").
    fail_mode: Literal["open", "closed"] = "open"


DEFAULT_POLICY = Policy()


def check(
    sql: str,
    catalog: Catalog,
    *,
    policy: Policy = DEFAULT_POLICY,
    check_id: str | None = None,
) -> CheckResult:
    check_id = check_id or new_check_id()
    findings, tables = _analyze(sql, catalog, policy)
    return CheckResult(
        check_id=check_id,
        tag=tag_for(check_id),
        verdict=verdict_for(findings),
        findings=tuple(findings),
        tables=tuple(table.qualified_name for table in tables),
    )


def _analyze(sql: str, catalog: Catalog, policy: Policy) -> tuple[list[Finding], tuple[Table, ...]]:
    try:
        statements = parse(sql, DIALECT)
    except SqlParseError as error:
        message = f"The SQL could not be parsed: {error.message}."
        return [
            _unanalyzable(policy, message, "Fix the syntax error.", error.line, error.column)
        ], ()
    if not statements:
        return [_unanalyzable(policy, "There is no SQL statement to check.", _SEND_ONE)], ()

    # Writes are blocked wherever they are, even next to statements that can't be checked.
    findings = _writes_blocked(statements) if policy.read_only else []
    if len(statements) > 1:
        message = f"Found {len(statements)} statements; send one statement per check."
        fix = "Check each statement on its own."
        return [*findings, _unanalyzable(policy, message, fix)], ()
    if findings:
        return findings, ()

    (tree,) = statements
    kind = classify(tree)
    if kind == "other":
        message = f"{describe(tree)} statements can't be checked."
        return [_unanalyzable(policy, message, _SEND_ONE)], ()
    if kind == "write" and not isinstance(tree, _CHECKED_WRITES):
        message = f"Names in {describe(tree)} statements aren't checked yet."
        return [_unanalyzable(policy, message, _BY_HAND)], ()
    try:
        resolution = resolve(resolvable(tree), catalog, DIALECT)
    except ResolveError as error:
        message = f"Names could not be resolved: {error}."
        return [_unanalyzable(policy, message, _BY_HAND)], ()
    return list(resolution.findings), resolution.tables


def _writes_blocked(statements: list[exp.Expr]) -> list[Finding]:
    """One SCN002 finding per kind of write statement."""
    names = dict.fromkeys(describe(tree) for tree in statements if classify(tree) == "write")
    return [
        Finding(
            rule=WRITE_STATEMENT,
            severity=Severity.BLOCK,
            message=f"{name} statements can change data, schema or access, "
            "and this policy is read-only.",
            fix="Run a read-only SELECT instead.",
        )
        for name in names
    ]


def _unanalyzable(
    policy: Policy, message: str, fix: str, line: int | None = None, column: int | None = None
) -> Finding:
    return Finding(
        rule=UNANALYZABLE,
        severity=Severity.BLOCK if policy.fail_mode == "closed" else Severity.WARN,
        message=message,
        fix=fix,
        line=line,
        column=column,
    )


def new_check_id(now_ms: int | None = None, randomness: int | None = None) -> str:
    """A time-sortable ID: 48 bits of milliseconds and 40 random bits, in base32."""
    millis = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    random = secrets.randbits(_RANDOM_BITS) if randomness is None else randomness
    value = (millis << _RANDOM_BITS) | random
    digits = []
    for _ in range(18):  # 88 bits in 5-bit digits, fixed width so IDs sort by time
        value, digit = divmod(value, 32)
        digits.append(_ALPHABET[digit])
    return "chk_" + "".join(reversed(digits))


def tag_for(check_id: str) -> str:
    """The SQL comment an agent adds to the executed query."""
    return f"/* scanisaur:{check_id} */"
