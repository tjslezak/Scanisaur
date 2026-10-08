"""Check one SQL statement against a catalog and a policy."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sqlglot import exp

from scanisaur.catalog.model import Catalog, Table
from scanisaur.engine.cross_join import cross_join_findings
from scanisaur.engine.estimate import estimate
from scanisaur.engine.facts import FactsError, QueryFacts, TooComplexError, extract
from scanisaur.engine.fan_out import fan_out_findings
from scanisaur.engine.parse import (
    DIALECT,
    SqlParseError,
    classify,
    describe,
    parse,
    resolvable,
)
from scanisaur.engine.pruning import pruning_findings
from scanisaur.engine.resolve import Resolution, ResolveError, resolve
from scanisaur.engine.result import CheckResult, Estimate, Finding, Severity, verdict_for
from scanisaur.engine.rules import (
    PARTITION_FILTER,
    PRUNING_DEFEATED,
    UNANALYZABLE,
    WRITE_STATEMENT,
)
from scanisaur.engine.scan_threshold import scan_threshold_findings
from scanisaur.engine.select_star import select_star_findings
from scanisaur.engine.type_mismatch import type_mismatch_findings
from scanisaur.engine.unbounded import unbounded_result_findings

#: Fixes for SQL that can't be analyzed.
_SEND_ONE = "Send one complete SQL query."
_BY_HAND = "Check the table and column names by hand before running it."
_TOO_DEEP = "The SQL is nested too deeply to analyze."
_FLATTEN = "Move nested subqueries into WITH clauses, or nest fewer expressions."

#: Crockford base32, lowercase: sortable and unambiguous to read aloud.
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_RANDOM_BITS = 40
#: 100 bits: collisions are negligible for any one project's query history.
_FINGERPRINT_DIGITS = 20
#: A tracking tag at the start or end of the SQL, where agents add it.
_LEADING_TAG = re.compile(r"\A\s*/\*\s*scanisaur:[0-9a-z_]+\s*\*/")
_TRAILING_TAG = re.compile(r"/\*\s*scanisaur:[0-9a-z_]+\s*\*/\s*\Z")
#: Every kind of constant sqlglot parses, each replaced by ``?`` in a shape.
_LITERALS = (
    exp.Literal,
    exp.RawString,
    exp.ByteString,
    exp.HexString,
    exp.BitString,
    exp.National,
    exp.UnicodeString,
    exp.Boolean,
    exp.Null,
)
#: A value left in a shape: a quoted string or a hex number.
_VALUE = re.compile(r"['\"]|\b0[xX][0-9a-fA-F]+\b")
#: For SQL that doesn't parse: a quoted string, a comment, or a number.
_TOKEN = re.compile(
    r"""(?P<string>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""
    r"|(?P<comment>/\*.*?\*/|(?:--|#)[^\n]*)"
    r"|(?P<number>\b\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\b)",
    re.DOTALL,
)

#: A rule's severity set by policy, or "off" to drop its findings.
RuleSetting = Literal["off", "info", "warn", "block"]


@dataclass(frozen=True, slots=True)
class Policy:
    #: Block write, DDL and export statements (SCN002).
    read_only: bool = True
    #: When SQL can't be analyzed (SCN000): warn and let it run ("open"), or block ("closed").
    fail_mode: Literal["open", "closed"] = "open"
    #: On-demand price in US dollars per TiB billed; None for capacity (Editions) pricing,
    #: which gets estimates in bytes only.
    price_per_tib: float | None = 6.25
    #: SCN006 warns from this many pairs of rows, and blocks from this many when every
    #: side's size is known. Slot time grows by about 10 seconds per billion pairs (#23).
    cross_join_warn_pairs: int = 10**8
    cross_join_block_pairs: int = 10**10
    #: SCN009 warns when a query returns at least this many rows of a table, every row it
    #: reads, with no LIMIT or aggregate.
    unbounded_result_rows: int = 10_000
    #: SCN010 warns or blocks when the low end of the estimate reaches this many bytes
    #: billed (docs/rules/scn010.md). None turns that level off.
    warn_bytes: int | None = 100 * 2**30
    block_bytes: int | None = 2**40
    #: Severity overrides by rule ID, applied to every finding last.
    rules: Mapping[str, RuleSetting] = field(default_factory=dict, hash=False)


DEFAULT_POLICY = Policy()


def check(
    sql: str,
    catalog: Catalog,
    *,
    policy: Policy = DEFAULT_POLICY,
    check_id: str | None = None,
    now: datetime | None = None,
) -> CheckResult:
    """``now`` evaluates ``CURRENT_DATE()`` and the like in the cost estimate."""
    return _check(sql, catalog, _parse_sql(sql), policy=policy, check_id=check_id, now=now)


def check_with_shape(
    sql: str,
    catalog: Catalog,
    *,
    policy: Policy = DEFAULT_POLICY,
    check_id: str | None = None,
    now: datetime | None = None,
) -> tuple[CheckResult, str]:
    """Check and fingerprint the query's shape using a single parse.

    Checking copies the tree before resolving names. Shaping consumes the original
    only after checking, so literals and source positions remain intact during analysis.
    """
    statements = _parse_sql(sql)
    result = _check(sql, catalog, statements, policy=policy, check_id=check_id, now=now)
    return result, "s_" + _digest(_shape(sql, statements))


_ParseResult = list[exp.Expr] | SqlParseError | RecursionError


def _parse_sql(sql: str) -> _ParseResult:
    try:
        return parse(sql, DIALECT)
    except (SqlParseError, RecursionError) as error:
        return error


def _check(
    sql: str,
    catalog: Catalog,
    statements: _ParseResult,
    *,
    policy: Policy,
    check_id: str | None,
    now: datetime | None,
) -> CheckResult:
    check_id = check_id or new_check_id()
    now = now or datetime.now(UTC)
    cost: Estimate | None = None
    try:
        findings, tables, analyzed = _analyze(statements, catalog, policy, now)
        if analyzed is not None and not _rejected(findings) and not _unseen_reads(analyzed[0]):
            resolution, facts = analyzed
            cost = estimate(facts, now, policy.price_per_tib, _sampled(resolution))
    except RecursionError:
        # sqlglot and the rules walk the tree recursively. BigQuery accepts SQL nested
        # deeper than Python's stack allows (about 46 parentheses or 51 CASEs at the
        # default limit), so this is SQL that can't be analyzed, not SQL that is wrong.
        findings = [_unanalyzable(policy, _TOO_DEEP, _FLATTEN)]
        tables, cost = (), None
    if cost is not None:
        findings += scan_threshold_findings(
            cost, warn_bytes=policy.warn_bytes, block_bytes=policy.block_bytes
        )
    findings = _overridden(findings, policy.rules)
    return CheckResult(
        check_id=check_id,
        tag=tag_for(sql),
        verdict=verdict_for(findings),
        findings=tuple(findings),
        tables=tuple(table.qualified_name for table in tables),
        estimate=cost,
    )


def _unparsed(policy: Policy, error: SqlParseError) -> Finding:
    message = f"The SQL could not be parsed: {error.message}."
    return _unanalyzable(policy, message, "Fix the syntax error.", error.line, error.column)


_Analysis = tuple[list[Finding], tuple[Table, ...], tuple[Resolution, QueryFacts] | None]


def _analyze(
    statements: _ParseResult, catalog: Catalog, policy: Policy, now: datetime
) -> _Analysis:
    if isinstance(statements, RecursionError):
        raise statements
    if isinstance(statements, SqlParseError):
        return [_unparsed(policy, statements)], (), None
    if not statements:
        return [_unanalyzable(policy, "There is no SQL statement to check.", _SEND_ONE)], (), None

    # Writes are blocked wherever they are, even next to statements that can't be checked.
    findings = _writes_blocked(statements) if policy.read_only else []
    if len(statements) > 1:
        message = f"Found {len(statements)} statements; send one statement per check."
        fix = "Check each statement on its own."
        return [*findings, _unanalyzable(policy, message, fix)], (), None
    if findings:
        return findings, (), None

    (tree,) = statements
    kind = classify(tree)
    if kind == "other":
        message = f"{describe(tree)} statements can't be checked."
        return [_unanalyzable(policy, message, _SEND_ONE)], (), None
    target = resolvable(tree)
    if target is None:
        message = f"Names in {describe(tree)} statements aren't checked yet."
        return [_unanalyzable(policy, message, _BY_HAND)], (), None
    try:
        resolution = resolve(target, catalog, DIALECT)
    except ResolveError as error:
        message = f"Names could not be resolved: {error}."
        return [_unanalyzable(policy, message, _BY_HAND)], (), None
    if resolution.findings:
        return list(resolution.findings), resolution.tables, None
    findings, facts = _rule_findings(resolution, policy, now, returns_rows=kind == "query")
    return findings, resolution.tables, None if facts is None else (resolution, facts)


def _rule_findings(
    resolution: Resolution, policy: Policy, now: datetime, *, returns_rows: bool
) -> tuple[list[Finding], QueryFacts | None]:
    """Findings from the rules that read the qualified tree (SCN008) or per-table facts
    (SCN003 to SCN007, SCN009, SCN011), and the facts for the cost estimate.
    ``returns_rows`` is False for a statement that writes its result to a table."""
    try:
        facts = extract(resolution)
    except TooComplexError as error:
        mismatches = type_mismatch_findings(resolution)
        if not any(t.partitioning or t.clustering or t.is_wildcard for t in resolution.tables):
            return _ordered(mismatches), None  # no facts-based rule could apply
        message = f"Partition and cluster filters weren't checked: {error}."
        unchecked = _unanalyzable(policy, message, "Check those filters by hand.")
        return _ordered([unchecked, *mismatches]), None
    except FactsError:
        return [], None  # nothing is read, e.g. CREATE TABLE without a query
    if facts.outer_limit == 0:
        # Measured: BigQuery returns only the schema. It reads nothing, and doesn't require
        # a partition filter even on a table that needs one, so only a comparison it
        # refuses to compile still matters.
        return _ordered(type_mismatch_findings(resolution, rows_read=False)), facts
    pruning = pruning_findings(facts)
    star = select_star_findings(
        facts, now, sampled=_sampled(resolution), rejected=_rejected(pruning)
    )
    cross = cross_join_findings(
        facts,
        now,
        warn_pairs=policy.cross_join_warn_pairs,
        block_pairs=policy.cross_join_block_pairs,
        sampled=_sampled(resolution),
    )
    fan_out = fan_out_findings(facts)
    mismatches = type_mismatch_findings(resolution)
    unbounded: list[Finding] = []
    if returns_rows and not _rejected(pruning):
        unbounded = unbounded_result_findings(
            resolution,
            facts,
            now,
            warn_rows=policy.unbounded_result_rows,
            sampled=_sampled(resolution),
        )
    return _ordered([*pruning, *star, *cross, *fan_out, *mismatches, *unbounded]), facts


def _ordered(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.line or 0, f.column or 0, f.rule))


def _overridden(findings: list[Finding], rules: Mapping[str, RuleSetting]) -> list[Finding]:
    """Findings with the policy's per-rule severities; a rule set to "off" is dropped."""
    out = []
    for finding in findings:
        setting = rules.get(finding.rule)
        if setting is None:
            out.append(finding)
        elif setting != "off":
            out.append(finding.model_copy(update={"severity": Severity(setting)}))
    return out


def _rejected(findings: list[Finding]) -> bool:
    """True when BigQuery itself would reject the query, so it would bill nothing: a table
    requires a partition filter the query doesn't have (SCN003 and SCN004 block then)."""
    return any(
        f.severity is Severity.BLOCK and f.rule in (PARTITION_FILTER, PRUNING_DEFEATED)
        for f in findings
    )


def _unseen_reads(resolution: Resolution) -> bool:
    """True when the query reads data the facts don't follow: a table-valued function such
    as ``ML.PREDICT(MODEL m, TABLE t)``, or an ``INFORMATION_SCHEMA`` view."""
    tree = resolution.qualified
    if tree is None:
        return False
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            return True
        for part in (table.catalog, table.db, table.name):
            name = part.upper()
            if name == "INFORMATION_SCHEMA" or name.startswith("INFORMATION_SCHEMA."):
                return True
    return False


def _sampled(resolution: Resolution) -> frozenset[str]:
    """Tables read with TABLESAMPLE, as ``project.dataset.table``."""
    tree = resolution.qualified
    if tree is None:
        return frozenset()
    return frozenset(
        f"{table.catalog}.{table.db}.{table.name}"
        for table in tree.find_all(exp.Table)
        if table.args.get("sample") is not None
    )


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
    # 88 bits in 18 digits, fixed width so IDs sort by time.
    return "chk_" + _base32((millis << _RANDOM_BITS) | random, 18)


def fingerprint(sql: str) -> str:
    """Identify a query by its exact text.

    Tracking tags at the start or end, and the whitespace around the query, are left out,
    so re-checking tagged SQL gives the same fingerprint. Any other change counts, comments
    included: BigQuery serves cached results only for identical text, and a comment such as
    ``#legacySQL`` can change what the query means.
    """
    return "q_" + _digest(_untagged(sql))


def shape(sql: str) -> str:
    """The query on one line, without comments and with every literal value as ``?``.

    Queries that differ only in their constants share a shape, so the decision log and
    ``scanisaur audit`` can group them without keeping values such as email addresses.
    A list of constants after ``IN`` becomes one ``?``, whatever its length.
    """
    text = _untagged(sql)
    return _shape(text, _parse_sql(text))


def _shape(sql: str, statements: _ParseResult) -> str:
    if isinstance(statements, (SqlParseError, RecursionError)):
        return _rough_shape(_untagged(sql))
    try:
        shaped = "; ".join(
            statement.transform(_placeholder, copy=False).sql(dialect=DIALECT, comments=False)
            for statement in statements
        )
    except RecursionError:
        return _rough_shape(_untagged(sql))
    # A literal kind not in _LITERALS would keep its value: shape by pattern instead.
    return _rough_shape(_untagged(sql)) if _VALUE.search(shaped) else shaped


def shape_fingerprint(sql: str) -> str:
    """Identify a query's :func:`shape`: the same for queries that differ only in constants."""
    return "s_" + _digest(shape(sql))


def _untagged(sql: str) -> str:
    """``sql`` without tracking tags at its start or end, or whitespace around it."""
    text = sql
    while True:  # an agent may have added more than one tag
        untagged = _TRAILING_TAG.sub("", _LEADING_TAG.sub("", text))
        if untagged == text:
            return text.strip()
        text = untagged


def _digest(text: str) -> str:
    # surrogatepass: SQL decoded from JSON can hold an unpaired surrogate.
    data = text.encode("utf-8", "surrogatepass")
    digest = int.from_bytes(hashlib.sha256(data).digest(), "big")
    return _base32(digest >> (256 - 5 * _FINGERPRINT_DIGITS), _FINGERPRINT_DIGITS)


def _placeholder(node: exp.Expr) -> exp.Expr:
    if _is_constant(node):
        return exp.Placeholder()
    values = node.expressions if isinstance(node, exp.In) else []
    if values and all(_is_constant(value) for value in values):
        node.set("expressions", [exp.Placeholder()])
    return node


def _is_constant(node: exp.Expr) -> bool:
    """Literal values, including numbers represented by a unary minus."""
    return isinstance(node, _LITERALS) or (
        isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and node.this.is_number
    )


def _rough_shape(text: str) -> str:
    """A shape for SQL that doesn't parse: comments, strings and numbers removed by pattern."""
    text = _TOKEN.sub(lambda match: " " if match["comment"] else "?", text)
    return " ".join(text.split())


def tag_for(sql: str) -> str:
    """The SQL comment an agent adds at the start or end of the query it runs.

    It depends only on the SQL, so a repeated query keeps the same text and BigQuery can
    serve it from its cache; a tag unique to each check would make every run a cache miss.
    It identifies the query, not one check, and doesn't prove a check happened: audit
    matches it against Scanisaur's record of checks and their times.
    """
    return f"/* scanisaur:{fingerprint(sql)} */"


def _base32(value: int, digits: int) -> str:
    """The lowest ``5 * digits`` bits of ``value``, most significant digit first."""
    out = []
    for _ in range(digits):
        value, digit = divmod(value, 32)
        out.append(_ALPHABET[digit])
    return "".join(reversed(out))
