"""SCN008: comparisons between values of different types, in a join or a filter.

BigQuery converts some types into others to compare them, and refuses the rest. Measured
with free dry runs and literal-only queries on 2026-10-03 (``docs/rules/scn008.md``):

- STRING against a number, DATE or TIMESTAMP against each other, DATETIME against
  TIMESTAMP, BYTES against STRING and BOOL against INT64 fail with "No matching signature".
- A date compared with a TIMESTAMP or DATETIME means midnight, so
  ``created_at <= '2024-09-30'`` leaves out the rest of September 30, and
  ``created_at = '2024-09-30'`` matches only midnight. The query runs; the answer is wrong.
- INT64 compared with FLOAT64 is converted to FLOAT64, which holds whole numbers exactly
  only up to 2^53: ``9007199254740993 = 9007199254740992.0`` is true.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, timedelta
from typing import NamedTuple

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.scope import traverse_scope

from scanisaur.engine.facts import shown_sql, written_at
from scanisaur.engine.parse import DIALECT
from scanisaur.engine.resolve import Resolution, sqlglot_schema
from scanisaur.engine.result import Finding, Severity
from scanisaur.engine.rules import TYPE_MISMATCH

_NUMBERS = frozenset({"INT64", "NUMERIC", "BIGNUMERIC", "FLOAT64"})
#: Types a string literal converts to, when its text reads as one.
_FROM_STRING_LITERAL = frozenset({"DATE", "DATETIME", "TIMESTAMP", "TIME"})
_KNOWN = _NUMBERS | _FROM_STRING_LITERAL | {"STRING", "BYTES", "BOOL"}
_MOMENTS = frozenset({"DATETIME", "TIMESTAMP"})
#: Truncating to these units, or coarser ones, leaves midnight.
_FINE_UNITS = frozenset({"HOUR", "MINUTE", "SECOND", "MILLISECOND", "MICROSECOND"})
_DATE_ONLY = re.compile(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*")
_EQUALITIES = ("=", "!=", "IN", "IS NOT DISTINCT FROM")
_OPERATORS: dict[type[exp.Expr], str] = {
    exp.EQ: "=",
    exp.NEQ: "!=",
    exp.NullSafeEQ: "IS NOT DISTINCT FROM",
    exp.LT: "<",
    exp.LTE: "<=",
    exp.GT: ">",
    exp.GTE: ">=",
}
_FLIPPED = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}
#: sqlglot writes `DATE '2026-09-30'` as a CAST, `CURRENT_DATE()` without parentheses...
_TYPED_LITERAL = re.compile(r"CAST\('([^'\\]*)' AS (DATE|DATETIME|TIMESTAMP|TIME)\)")
_BARE_CURRENT = re.compile(r"\bCURRENT_(DATE|DATETIME|TIMESTAMP|TIME)\b(?!\()")
#: ...and quotes the number in `INTERVAL 7 DAY`.
_QUOTED_INTERVAL = re.compile(r"\bINTERVAL '(-?\d+)'")


class _Pair(NamedTuple):
    """Two sides of one comparison, with the operator as ``left op right``."""

    left: exp.Expr
    op: str
    right: exp.Expr


def type_mismatch_findings(resolution: Resolution, *, rows_read: bool = True) -> list[Finding]:
    """One finding per comparison in WHERE, ON, HAVING or QUALIFY whose sides have
    different types that BigQuery either refuses to compare or compares in a way that
    changes the answer. With ``rows_read`` False (``LIMIT 0``) only refusals are reported:
    no rows come back to be wrong."""
    if resolution.qualified is None:
        return []
    try:
        tree = annotate_types(
            resolution.qualified.copy(),
            schema=sqlglot_schema(resolution.references),
            dialect=DIALECT,
        )
        scopes = traverse_scope(tree)
    except SqlglotError:
        return []  # types can't be worked out, so nothing is claimed about them
    findings: dict[tuple[int | None, int | None, str], Finding] = {}
    for scope in scopes:
        for comparison in _comparisons(scope.expression):
            finding = _check(comparison, rows_read)
            if finding is not None:
                findings.setdefault((finding.line, finding.column, finding.message), finding)
    return list(findings.values())


def _comparisons(select: exp.Expr) -> Iterator[exp.Expr]:
    """Comparisons in a SELECT's WHERE, join ON, HAVING and QUALIFY, leaving subqueries
    to their own scope."""
    if not isinstance(select, exp.Select):
        return
    clauses = [select.args.get(key) for key in ("where", "having", "qualify")]
    clauses += [join.args.get("on") for join in select.args.get("joins") or ()]
    for clause in clauses:
        if clause is None:
            continue
        stack: list[exp.Expr] = [clause]
        while stack:
            node = stack.pop()
            if isinstance(node, exp.Query):
                continue
            if isinstance(node, (*_OPERATORS, exp.Between, exp.In)):
                yield node
            stack.extend(reversed(list(node.iter_expressions())))


def _pairs(node: exp.Expr) -> list[_Pair]:
    if isinstance(node, exp.Between):
        return [
            _Pair(node.this, ">=", node.args["low"]),
            _Pair(node.this, "<=", node.args["high"]),
        ]
    if isinstance(node, exp.In):
        if node.args.get("query") is not None or node.args.get("unnest") is not None:
            return []
        return [_Pair(node.this, "IN", value) for value in node.expressions]
    return [_Pair(node.this, _OPERATORS[type(node)], node.expression)]


def _check(node: exp.Expr, rows_read: bool) -> Finding | None:
    pairs = [(pair, _type(pair.left), _type(pair.right)) for pair in _pairs(node)]
    for pair, left, right in pairs:
        if left is not None and right is not None and not _comparable(pair, left, right):
            return _finding(node, _refused(node, pair, left, right))
    if not rows_read:
        return None
    for pair, left, right in pairs:
        if left is None or right is None:
            continue
        found = _midnight(node, pair, left, right) or _float(node, pair, left, right)
        if found is not None:
            return _finding(node, found)
    return None


def _finding(node: exp.Expr, text: tuple[str, str]) -> Finding:
    message, fix = text
    line, column = written_at(node)
    return Finding(
        rule=TYPE_MISMATCH,
        severity=Severity.WARN,
        message=message,
        fix=fix,
        line=line,
        column=column,
    )


def _type(node: exp.Expr) -> str | None:
    """The BigQuery type of an expression, when it is a plain scalar type."""
    if isinstance(node, exp.Null) or node.type is None:
        return None
    name = node.type.sql(dialect=DIALECT).upper()
    name = name.partition("(")[0].strip()  # NUMERIC(10, 2)
    return name if name in _KNOWN else None


def _comparable(pair: _Pair, left: str, right: str) -> bool:
    if left == right or (left in _NUMBERS and right in _NUMBERS):
        return True
    if {left, right} == {"DATE", "DATETIME"}:
        return True  # the DATE becomes midnight of that day
    # A string literal converts to a date or time type; a malformed one fails differently.
    if left == "STRING" and _string_literal(pair.left) and right in _FROM_STRING_LITERAL:
        return True
    return right == "STRING" and _string_literal(pair.right) and left in _FROM_STRING_LITERAL


def _string_literal(node: exp.Expr) -> bool:
    return isinstance(node, exp.Literal) and node.is_string


def _refused(node: exp.Expr, pair: _Pair, left: str, right: str) -> tuple[str, str]:
    message = (
        f"BigQuery refuses `{_show(node)}`: it doesn't convert between {left} and {right} "
        "to compare them, so the query fails before it reads anything."
    )
    return message, _refused_fix(pair, left, right)


def _refused_fix(pair: _Pair, left: str, right: str) -> str:
    sides = {left: pair.left, right: pair.right}
    if "STRING" in sides and (left in _NUMBERS or right in _NUMBERS):
        text, number = sides["STRING"], sides[left if left != "STRING" else right]
        if isinstance(number, exp.Literal):
            return f"Quote the value, as `'{number.name}'`, if the column holds text."
        if _string_literal(text):
            return f"Write the number without quotes, as `{text.name}`."
        return (
            f"Convert one side to the other's type, as `CAST({_show(number)} AS STRING)`, "
            f"or `SAFE_CAST({_show(text)} AS INT64)` if the text holds whole numbers."
        )
    if {left, right} == {"DATE", "TIMESTAMP"}:
        moment, day = _show(sides["TIMESTAMP"]), _show(sides["DATE"])
        return (
            f"Compare days with `DATE({moment})`, or convert the date with "
            f"`TIMESTAMP({day})`, which is midnight UTC."
        )
    if {left, right} == {"DATETIME", "TIMESTAMP"}:
        moment, local = _show(sides["TIMESTAMP"]), _show(sides["DATETIME"])
        return (
            f"Convert one side: `DATETIME({moment})` reads the timestamp in UTC, and "
            f"`TIMESTAMP({local})` reads the datetime as UTC."
        )
    return "Convert one side with `CAST` so both sides have the same type."


def _midnight(node: exp.Expr, pair: _Pair, left: str, right: str) -> tuple[str, str] | None:
    """A date compared with a moment in a way that depends on the time of day."""
    if left in _MOMENTS and _date_only(pair.right, right, left):
        moment, op, day, kind = pair.left, pair.op, pair.right, left
    elif right in _MOMENTS and _date_only(pair.left, left, right):
        moment, op, day, kind = pair.right, _FLIPPED.get(pair.op, pair.op), pair.left, right
    else:
        return None
    if op in (">=", "<") or _at_midnight(moment):
        return None  # from midnight on, or before it: whole days either way
    shown, column = _show(node), _show(moment)
    zone = " UTC" if kind == "TIMESTAMP" else ""
    opening = f"`{shown}` compares {kind} `{column}` with a date, which means midnight{zone}"
    after = _next_day(day)
    if isinstance(node, exp.Between) and op == "<=":
        low = _show(node.args["low"])
        return (
            f"{opening}, so the range ends at the start of its last day and leaves out "
            "the rest of it.",
            f"Use a half-open range: `{column} >= {low} AND {column} < {after}`.",
        )
    if op == "<=":
        return (
            f"{opening}, so it leaves out the rest of that day.",
            f"Compare with the next day instead: `{column} < {after}`.",
        )
    if op == ">":
        return (
            f"{opening}, so it keeps the rest of that day too.",
            f"Start from the next day instead: `{column} >= {after}`.",
        )
    keeps = "drops only rows at exactly midnight" if op == "!=" else "matches only midnight"
    return (
        f"{opening}, so it {keeps}, not the whole day.",
        f"Compare days instead: `DATE({column})` in place of `{column}`.",
    )


def _date_only(node: exp.Expr, kind: str, other: str) -> bool:
    """A date compared with a moment: a DATE value against a DATETIME (BigQuery refuses
    one against a TIMESTAMP), or a string literal holding just a date."""
    if kind == "DATE":
        return other == "DATETIME"
    return _string_literal(node) and _literal_date(node) is not None


def _literal_date(node: exp.Expr) -> date | None:
    """The date a string literal holds, when it holds just a real one."""
    match = _DATE_ONLY.fullmatch(node.name)
    if match is None:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None  # such as '2026-02-30', which BigQuery refuses to convert


def _at_midnight(node: exp.Expr) -> bool:
    """True when a moment always falls at midnight: truncated to a day or coarser, or
    made from a DATE."""
    if isinstance(node, (exp.TimestampTrunc, exp.DatetimeTrunc, exp.DateTrunc)):
        unit = node.args.get("unit")
        name = unit.this if isinstance(unit, exp.WeekStart) else unit
        return isinstance(name, exp.Expr) and name.name.upper() not in _FINE_UNITS
    if isinstance(node, (exp.Cast, exp.Timestamp, exp.TsOrDsToDatetime)):
        return _type(node.this) == "DATE"
    return False


def _next_day(day: exp.Expr) -> str:
    value = _literal_date(day) if _string_literal(day) else None
    if value is not None:
        return f"'{(value + timedelta(days=1)).isoformat()}'"
    return f"DATE_ADD({_show(day)}, INTERVAL 1 DAY)"


def _float(node: exp.Expr, pair: _Pair, left: str, right: str) -> tuple[str, str] | None:
    """An equality between FLOAT64 and an exact number that both come from data."""
    if pair.op not in _EQUALITIES or "FLOAT64" not in (left, right):
        return None
    exact = right if left == "FLOAT64" else left
    if exact not in ("INT64", "NUMERIC", "BIGNUMERIC"):
        return None
    if any(_constant(side) for side in (pair.left, pair.right)):
        return None
    floating = pair.left if left == "FLOAT64" else pair.right
    if exact == "INT64":
        why = (
            "It holds whole numbers exactly only up to 2^53 (9,007,199,254,740,992), so "
            "larger IDs that differ can match"
        )
    else:
        why = f"It can't hold every {exact} value exactly, so values that differ can match"
    return (
        f"`{_show(node)}` compares {exact} with FLOAT64, so BigQuery converts both sides "
        f"to FLOAT64. {why}.",
        f"Compare exact values: `CAST({_show(floating)} AS {exact})`.",
    )


def _show(node: exp.Expr) -> str:
    """An expression as an agent would write it."""
    text = _TYPED_LITERAL.sub(r"\2 '\1'", shown_sql(node))
    text = _QUOTED_INTERVAL.sub(r"INTERVAL \1", text)
    return _BARE_CURRENT.sub(r"CURRENT_\1()", text)


def _constant(node: exp.Expr) -> bool:
    return not any(True for _ in node.find_all(exp.Column))
