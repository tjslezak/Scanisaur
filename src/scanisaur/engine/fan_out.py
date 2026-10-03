"""SCN007: a join on columns that aren't a unique key, which repeats the other side's rows.

When a join matches a row to several rows of the other side, the row appears once per
match. A count or sum over the repeated side is then multiplied, and the query returns
a wrong number with no error. A join that is a unique key of neither side can also give
more rows than either table. Metadata can't tell which columns are unique: BigQuery
doesn't enforce primary keys and most tables declare none, so the keys come from the
catalog's configuration, and a source whose keys aren't known is never reported.
"""

from __future__ import annotations

from collections import deque

from scanisaur.engine.facts import Aggregation, KeyedJoin, KeyedSource, KeyMatch, QueryFacts
from scanisaur.engine.result import Finding, Severity
from scanisaur.engine.rules import FAN_OUT


def fan_out_findings(facts: QueryFacts) -> list[Finding]:
    """At most one finding per SELECT: an aggregate that counts a source's rows once per
    match of another source, or else a join that is a unique key of neither side."""
    findings: dict[tuple[str, int | None, int | None], Finding] = {}
    for keyed in facts.keyed_joins:
        finding = _aggregate_finding(keyed) or _many_to_many_finding(keyed)
        if finding is not None:
            findings.setdefault((finding.message, finding.line, finding.column), finding)
    return list(findings.values())


def _unique(source: KeyedSource, columns: frozenset[str]) -> bool | None:
    """True when the columns, with those fixed to one value, hold a unique key of the
    source, so each row of the other side matches at most one row of it."""
    if source.keys is None:
        return None
    matched = columns | source.fixed
    return any(key <= matched for key in source.keys)


def _sides(match: KeyMatch, alias: str) -> tuple[str, frozenset[str], frozenset[str]]:
    """The other source of a match, the columns compared on it, and on ``alias``."""
    if match.left == alias:
        return match.right, match.right_columns, match.left_columns
    return match.left, match.left_columns, match.right_columns


def _aggregate_finding(keyed: KeyedJoin) -> Finding | None:
    sources = {source.alias: source for source in keyed.sources}
    for aggregation in keyed.aggregations:
        repeated = _repeating(keyed, sources, aggregation.source)
        if repeated is not None:
            other, columns = repeated
            return _aggregate(aggregation, sources[aggregation.source], sources[other], columns)
    return None


def _repeating(
    keyed: KeyedJoin, sources: dict[str, KeyedSource], start: str
) -> tuple[str, frozenset[str]] | None:
    """A source that matches each row of ``start`` more than once, with the columns it is
    matched on. A source matched on a unique key passes the question on to its own
    matches: each of its rows stands for one row of ``start``. So does one whose GROUP BY
    columns complete a key: each group holds one of its rows. One whose keys aren't
    known stops the search there."""
    seen = {start}
    queue = deque([start])
    while queue:
        alias = queue.popleft()
        for match in keyed.matches:
            if alias not in (match.left, match.right):
                continue
            other, columns, _own = _sides(match, alias)
            if other in seen:
                continue
            seen.add(other)
            unique = _unique(sources[other], columns | keyed.grouped.get(other, frozenset()))
            if unique is False:
                return other, columns
            if unique:
                queue.append(other)
    return None


def _many_to_many_finding(keyed: KeyedJoin) -> Finding | None:
    sources = {source.alias: source for source in keyed.sources}
    for match in keyed.matches:
        left, right = sources[match.left], sources[match.right]
        if (
            _unique(left, match.left_columns) is False
            and _unique(right, match.right_columns) is False
        ):
            line, column = match.position
            return Finding(
                rule=FAN_OUT,
                severity=Severity.WARN,
                message=(
                    f"{_name(left)} and {_name(right)} are matched on `{match.condition}`, "
                    "a unique key of neither, so each row of one pairs with every matching "
                    "row of the other: the result can hold more rows than both, and counts "
                    "or sums over it are multiplied."
                ),
                fix=_many_to_many_fix(match, left, right),
                line=line,
                column=column,
            )
    return None


def _aggregate(
    aggregation: Aggregation, source: KeyedSource, other: KeyedSource, columns: frozenset[str]
) -> Finding:
    matched = _columns(columns)
    message = (
        f"`{aggregation.sql}` counts each row of {_name(source)} once for every row of "
        f"{_name(other)} it matches, as {matched} isn't a unique key of `{other.alias}`. "
        "The result is wrong, and BigQuery gives no error."
    )
    fix = (
        f"Reduce `{other.alias}` to one row per {matched} before joining it, with GROUP BY "
        f"in a subquery, or compute `{aggregation.sql}` in a query that doesn't join "
        f"`{other.alias}`."
    )
    if aggregation.function == "COUNT":
        key = min(source.keys or (), key=lambda k: (len(k), sorted(k)), default=None)
        if key and len(key) == 1:
            distinct = f"COUNT(DISTINCT {source.alias}.{min(key)})"
            fix += f" To count rows of `{source.alias}`, use `{distinct}`."
    line, column = aggregation.position
    return Finding(
        rule=FAN_OUT,
        severity=Severity.WARN,
        message=message,
        fix=fix,
        line=line,
        column=column,
    )


def _many_to_many_fix(match: KeyMatch, left: KeyedSource, right: KeyedSource) -> str:
    """Name the columns that would complete a key of one side, when the other side has
    columns of the same names."""
    for side, columns, other in (
        (right, match.right_columns, left),
        (left, match.left_columns, right),
    ):
        for key in sorted(side.keys or (), key=lambda k: (len(k), sorted(k))):
            missing = sorted(key - columns - side.fixed)
            if missing and all(name in other.columns for name in missing):
                conditions = " AND ".join(
                    f"{side.alias}.{name} = {other.alias}.{name}" for name in missing
                )
                return (
                    f"Match on a unique key of one side, for example add `{conditions}`, or "
                    "reduce one side to one row per matched value with GROUP BY or DISTINCT "
                    "first."
                )
    return (
        "Match on a unique key of one side as well, or reduce one side to one row per "
        "matched value with GROUP BY or DISTINCT first."
    )


def _name(source: KeyedSource) -> str:
    if source.table is None:
        return "a subquery" if source.alias.startswith("_") else f"`{source.alias}`"
    if source.alias == source.table:
        return f"`{source.alias}`"
    return f"`{source.alias}` ({source.table})"


def _columns(columns: frozenset[str]) -> str:
    names = sorted(columns)
    if len(names) == 1:
        return f"`{names[0]}`"
    return "(" + ", ".join(f"`{name}`" for name in names) + ")"
