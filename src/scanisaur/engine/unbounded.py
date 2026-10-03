"""SCN009: a query that returns every row of a large table.

With no ``LIMIT``, aggregate or filter beyond whole partitions, the result holds every row
the query reads: a million rows from one day of Google Trends. Nobody reads that, and an
agent that asked for it fills its context with rows instead of the answer. The rule warns
only when the row count is known: a filter on any other column, an inner join or a
``DISTINCT`` may leave a handful of rows, so then it stays silent.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from scanisaur.engine.cross_join import format_count
from scanisaur.engine.estimate import table_rows
from scanisaur.engine.facts import QueryFacts, TableFacts, select_aggregated
from scanisaur.engine.parse import position
from scanisaur.engine.resolve import Resolution
from scanisaur.engine.result import Finding, Severity
from scanisaur.engine.rules import UNBOUNDED_RESULT

_FIX = (
    "Aggregate to get the answer instead of the rows, with `COUNT`, `SUM` or `GROUP BY`, "
    "or add a `LIMIT` to look at a sample."
)
#: Clauses after which a SELECT returns fewer rows than it reads, by an unknown amount.
_REDUCING = ("limit", "group", "distinct", "having", "qualify")


def unbounded_result_findings(
    resolution: Resolution,
    facts: QueryFacts,
    now: datetime,
    *,
    warn_rows: int,
    sampled: frozenset[str] = frozenset(),
) -> list[Finding]:
    """One finding when the query's result holds every row of tables it reads, at least
    ``warn_rows`` of them in all. ``sampled`` names tables read with TABLESAMPLE, whose
    rows are a fraction of their count."""
    if resolution.qualified is None:
        return []
    root = build_scope(resolution.qualified)
    if root is None:
        return []
    returned = _returned(root)
    if not returned:
        return []
    counts: list[tuple[exp.Table, int]] = []
    for node in returned:
        if _name(node) in sampled:
            return []
        count = _rows(node, facts, now)
        if count is None:
            return []  # some branch's rows aren't known, so neither is the total
        counts.append((node, count))
    total = sum(count for _node, count in counts)
    if total < warn_rows:
        return []
    names = list(dict.fromkeys(f"`{_name(node)}`" for node, _count in counts))
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    line, column = _where(counts[0][0])
    return [
        Finding(
            rule=UNBOUNDED_RESULT,
            severity=Severity.WARN,
            message=(
                f"The query returns every row it reads of {listed}, about "
                f"{format_count(total)} rows, with no `LIMIT` or aggregate: more than anyone "
                "reads, and more than fits in an agent's context."
            ),
            fix=_FIX,
            line=line,
            column=column,
        )
    ]


def _returned(scope: Scope) -> list[exp.Table]:
    """Catalog tables each of whose rows reaches the result at least once: read in FROM,
    through CTEs, subqueries, LEFT JOINs and UNION ALL branches, with nothing but filters
    on that table between it and the result. Empty when that isn't so."""
    expression = scope.expression
    if isinstance(expression, exp.SetOperation):
        if expression.args.get("distinct") or not isinstance(expression, exp.Union):
            return []  # UNION DISTINCT, INTERSECT and EXCEPT drop rows
        if any(expression.args.get(key) for key in _REDUCING):
            return []
        branches = [_returned(branch) for branch in scope.set_operation_scopes]
        if any(not tables for tables in branches):
            return []
        return [node for tables in branches for node in tables]
    if not isinstance(expression, exp.Select):
        return []
    if any(expression.args.get(key) for key in _REDUCING) or select_aggregated(expression):
        return []
    source = _from_alias(expression)
    if source is None or source not in scope.selected_sources:
        return []
    for join in expression.args.get("joins") or ():
        if join.args.get("side", "").upper() not in ("LEFT", "FULL"):
            return []  # an inner join or UNNEST may drop or repeat rows
    where = expression.args.get("where")
    if where is not None and not _on_source_only(where, source):
        return []  # a filter that involves another source isn't the table's own
    node, selected = scope.selected_sources[source]
    if isinstance(selected, Scope):
        return _returned(selected)
    if isinstance(selected, exp.Table) and isinstance(node, exp.Table):
        return [node]
    return []


def _from_alias(select: exp.Select) -> str | None:
    from_ = select.args.get("from_")
    if from_ is None or not isinstance(from_.this, exp.Table | exp.Subquery):
        return None
    return from_.this.alias_or_name or None


def _on_source_only(condition: exp.Expr, source: str) -> bool:
    if condition.find(exp.Query) is not None:
        return False  # EXISTS, IN (SELECT ...) and scalar subqueries
    return all(column.table == source for column in condition.find_all(exp.Column))


def _rows(node: exp.Table, facts: QueryFacts, now: datetime) -> int | None:
    """The rows a table reference reads, when that is known exactly: the fewest among the
    ways the query reads it there, as a CTE read twice with different filters."""
    where = _where(node)
    matches = [
        t for t in facts.tables if t.table.qualified_name == _name(node) and t.position == where
    ]
    counts = [_known(t, now) for t in matches]
    if not counts or None in counts:
        return None
    return min(c for c in counts if c is not None)


def _known(reference: TableFacts, now: datetime) -> int | None:
    # A LEFT JOIN's condition links the table's key without dropping its rows; what else
    # could limit them, a subquery or a filter on another source, _returned() rules out.
    measured = table_rows(replace(reference, linked=frozenset()), now)
    if measured is None or not measured[1]:
        return None
    return measured[0]


def _name(node: exp.Table) -> str:
    return f"{node.catalog}.{node.db}.{node.name}"


def _where(node: exp.Table) -> tuple[int | None, int | None]:
    """Where a table is written, as the facts record it."""
    where = position(node)
    return where if where[0] is not None else position(node.this)
