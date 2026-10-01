"""Check one SQL statement against a catalog and a policy."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Literal

from scanisaur.catalog.model import Catalog, Table
from scanisaur.engine.parse import SqlParseError, classify, describe, parse
from scanisaur.engine.resolve import ResolveError, resolve
from scanisaur.engine.result import CheckResult, Finding, Severity, verdict_for
from scanisaur.engine.rules import UNANALYZABLE, WRITE_STATEMENT

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
    dialect: str = "bigquery",
    check_id: str | None = None,
) -> CheckResult:
    check_id = check_id or new_check_id()
    findings, tables = _analyze(sql, catalog, policy, dialect)
    return CheckResult(
        check_id=check_id,
        tag=tag_for(check_id),
        verdict=verdict_for(findings),
        findings=tuple(findings),
        tables=tuple(table.qualified_name for table in tables),
    )


def _analyze(
    sql: str, catalog: Catalog, policy: Policy, dialect: str
) -> tuple[list[Finding], tuple[Table, ...]]:
    try:
        statements = parse(sql, dialect)
    except SqlParseError as error:
        message = f"The SQL could not be parsed: {error.message}."
        return [_unanalyzable(policy, message, error.line, error.column)], ()
    if not statements:
        return [_unanalyzable(policy, "There is no SQL statement to check.")], ()
    if len(statements) > 1:
        message = f"Found {len(statements)} statements; send one statement per check."
        return [_unanalyzable(policy, message)], ()

    (tree,) = statements
    kind = classify(tree)
    if kind == "other":
        return [_unanalyzable(policy, f"{describe(tree)} statements can't be checked.")], ()
    if kind == "write" and policy.read_only:
        finding = Finding(
            rule=WRITE_STATEMENT,
            severity=Severity.BLOCK,
            message=f"{describe(tree)} statements can change data, schema or access, "
            "and this policy is read-only.",
            fix="Run a read-only SELECT instead.",
        )
        return [finding], ()
    try:
        resolution = resolve(tree, catalog, dialect)
    except ResolveError as error:
        return [_unanalyzable(policy, f"Names could not be resolved: {error}.")], ()
    return list(resolution.findings), resolution.tables


def _unanalyzable(
    policy: Policy, message: str, line: int | None = None, column: int | None = None
) -> Finding:
    return Finding(
        rule=UNANALYZABLE,
        severity=Severity.BLOCK if policy.fail_mode == "closed" else Severity.WARN,
        message=message,
        fix="Send one complete SQL query." if line is None else "Fix the syntax error.",
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
