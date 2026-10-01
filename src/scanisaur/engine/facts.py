"""Per-table facts extracted from a qualified SQL syntax tree.

Rules and the cost estimator read these facts instead of walking the syntax tree
themselves. The input must already be qualified (``sqlglot.optimizer.qualify``
with ``expand_stars=False``), so every column carries the alias of its source.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

#: BigQuery pseudo-columns that sqlglot leaves unqualified.
PSEUDO_COLUMNS = frozenset({"_table_suffix", "_partitiontime", "_partitiondate"})

ComparisonOp = Literal["=", "<", "<=", ">", ">=", "between", "in", "other"]
Clause = Literal["where", "on", "qualify"]

_COMPARISONS: dict[type[exp.Expr], ComparisonOp] = {
    exp.EQ: "=",
    exp.LT: "<",
    exp.LTE: "<=",
    exp.GT: ">",
    exp.GTE: ">=",
}
_FLIPPED: dict[ComparisonOp, ComparisonOp] = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}


@dataclass(frozen=True, slots=True)
class TableRef:
    project: str | None
    dataset: str | None
    name: str
    alias: str

    @property
    def qualified_name(self) -> str:
        return ".".join(part for part in (self.project, self.dataset, self.name) if part)


@dataclass(frozen=True, slots=True)
class Predicate:
    """A filter condition that involves columns of exactly one table reference."""

    column: str
    op: ComparisonOp
    #: SQL of the compared value(s); empty unless ``constant``.
    values: tuple[str, ...]
    #: True when compared only against constants (literals, ``CURRENT_DATE()`` arithmetic).
    constant: bool
    #: Function wrapping the column, such as ``DATE`` or ``CAST``; None for a bare column.
    wrapper: str | None
    clause: Clause
    sql: str


@dataclass(frozen=True, slots=True)
class Join:
    target: str
    target_kind: Literal["table", "unnest", "derived"]
    side: str | None
    kind: str | None
    has_condition: bool
    #: Equality pairs such as ("e.user_id", "u.user_id"), from ON or from WHERE.
    keys: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class TableFacts:
    table: TableRef
    #: Columns read from the table; with ``star`` the query reads every column...
    columns: frozenset[str]
    star: bool
    #: ...except these (BigQuery ``SELECT * EXCEPT (...)``).
    star_except: frozenset[str]
    predicates: tuple[Predicate, ...]
    #: How many times the table is scanned; a CTE referenced twice counts twice.
    scans: int


@dataclass(frozen=True, slots=True)
class QueryFacts:
    tables: tuple[TableFacts, ...]
    joins: tuple[Join, ...]
    outer_limit: int | None
    outer_aggregated: bool


def extract(expression: exp.Expr, dialect: str = "bigquery") -> QueryFacts:
    """Extract facts from a qualified query (SELECT, set operation, or pipe syntax)."""
    scopes = traverse_scope(expression)
    if not scopes:
        raise ValueError("expression is not a query")
    scans = _scan_counts(scopes)

    tables: list[TableFacts] = []
    joins: list[Join] = []
    for scope in scopes:
        if scans[id(scope)] == 0 or not isinstance(scope.expression, exp.Select):
            continue
        tables.extend(_table_facts(scope, scans[id(scope)], dialect))
        joins.extend(_joins(scope))

    root = _effective_root(scopes[-1])
    limit, aggregated = _outer_shape(root.expression)
    return QueryFacts(tuple(tables), tuple(joins), limit, aggregated)


def _scan_counts(scopes: list[Scope]) -> Counter[int]:
    """Count how many times each scope runs: CTEs once per reference, unused CTEs zero."""
    counts: Counter[int] = Counter({id(scopes[-1]): 1})
    for scope in reversed(scopes):  # traverse_scope yields children first
        runs = counts[id(scope)]
        for _node, source in scope.selected_sources.values():
            if isinstance(source, Scope):
                counts[id(source)] += runs
        for child in [*scope.subquery_scopes, *scope.set_operation_scopes]:
            counts[id(child)] += runs
    return counts


def _table_sources(scope: Scope) -> dict[str, exp.Table]:
    return {
        alias: source
        for alias, (_node, source) in scope.selected_sources.items()
        if isinstance(source, exp.Table)
    }


def _table_facts(scope: Scope, scans: int, dialect: str) -> Iterator[TableFacts]:
    sources = _table_sources(scope)
    if not sources:
        return
    lone_alias = next(iter(sources)) if len(sources) == 1 else None
    starred = _starred_aliases(scope, sources)
    predicates = list(_predicates(scope, sources, lone_alias, dialect))

    for alias, table in sources.items():
        columns = frozenset(
            column.name.lower()
            for column in scope.columns
            if _owner(column, lone_alias) == alias and column.name.lower() not in PSEUDO_COLUMNS
        )
        yield TableFacts(
            table=TableRef(
                project=table.catalog or None,
                dataset=table.db or None,
                name=table.name,
                alias=alias,
            ),
            columns=columns,
            star=alias in starred,
            star_except=starred.get(alias, frozenset()),
            predicates=tuple(p for owner, p in predicates if owner == alias),
            scans=scans,
        )


def _owner(column: exp.Column, lone_alias: str | None) -> str | None:
    """The alias a column belongs to; unqualified pseudo-columns go to a scope's only table."""
    if column.table:
        return column.table
    if column.name.lower() in PSEUDO_COLUMNS:
        return lone_alias
    return None


def _starred_aliases(scope: Scope, sources: dict[str, exp.Table]) -> dict[str, frozenset[str]]:
    """Aliases selected with a star, mapped to the columns their ``EXCEPT`` leaves out."""
    select = scope.expression
    assert isinstance(select, exp.Select)
    starred: dict[str, frozenset[str]] = {}
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            excluded = _star_except(projection)
            starred.update(dict.fromkeys(sources, excluded))
        elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            if projection.table in sources:
                starred[projection.table] = _star_except(projection.this)
    return starred


def _star_except(star: exp.Star) -> frozenset[str]:
    return frozenset(column.name.lower() for column in star.args.get("except_") or [])


def _conjuncts(condition: exp.Expr | None) -> Iterator[exp.Expr]:
    if condition is None:
        return
    if isinstance(condition, exp.Paren):
        yield from _conjuncts(condition.this)
    elif isinstance(condition, exp.And):
        yield from _conjuncts(condition.left)
        yield from _conjuncts(condition.right)
    else:
        yield condition


def _clauses(scope: Scope) -> Iterator[tuple[Clause, exp.Expr]]:
    select = scope.expression
    where = select.args.get("where")
    if where is not None:
        yield from (("where", c) for c in _conjuncts(where.this))
    for join in select.args.get("joins") or []:
        yield from (("on", c) for c in _conjuncts(join.args.get("on")))
    qualify = select.args.get("qualify")
    if qualify is not None:
        yield from (("qualify", c) for c in _conjuncts(qualify.this))


def _local_columns(node: exp.Expr) -> list[exp.Column]:
    """Columns in ``node`` that belong to this scope, not to a nested subquery."""
    return [
        column
        for column in node.find_all(exp.Column)
        if not isinstance(column.this, exp.Star)
        and column.find_ancestor(exp.Select, exp.Subquery) is node.find_ancestor(exp.Select)
    ]


def _has_subquery(node: exp.Expr) -> bool:
    return node.find(exp.Select, exp.Subquery) is not None


def _is_constant(node: exp.Expr) -> bool:
    return not _local_columns(node) and not _has_subquery(node)


def _wrapper(side: exp.Expr) -> str | None:
    if isinstance(side, exp.Column):
        return None
    if isinstance(side, exp.Func):
        return side.sql_name()
    return side.key.upper()


def _predicates(
    scope: Scope, sources: dict[str, exp.Table], lone_alias: str | None, dialect: str
) -> Iterator[tuple[str, Predicate]]:
    for clause, condition in _clauses(scope):
        columns = _local_columns(condition)
        owners = {_owner(column, lone_alias) for column in columns}
        if len(owners) != 1:
            continue
        owner = owners.pop()
        if owner not in sources:
            continue
        predicate = _classify(condition, clause, dialect)
        if predicate is not None:
            yield owner, predicate


def _classify(condition: exp.Expr, clause: Clause, dialect: str) -> Predicate | None:
    sql = condition.sql(dialect=dialect)

    def make(side: exp.Expr, op: ComparisonOp, others: list[exp.Expr]) -> Predicate:
        column = side if isinstance(side, exp.Column) else side.find(exp.Column)
        assert column is not None
        constant = bool(others) and all(_is_constant(other) for other in others)
        values = tuple(other.sql(dialect=dialect) for other in others) if constant else ()
        return Predicate(column.name.lower(), op, values, constant, _wrapper(side), clause, sql)

    comparison = _COMPARISONS.get(type(condition))
    if comparison is not None and isinstance(condition, exp.Binary):
        left, right = condition.left, condition.right
        if _local_columns(left) and not _local_columns(right):
            return make(left, comparison, [right])
        if _local_columns(right) and not _local_columns(left):
            return make(right, _FLIPPED.get(comparison, comparison), [left])
        return None
    if isinstance(condition, exp.Between):
        return make(condition.this, "between", [condition.args["low"], condition.args["high"]])
    if isinstance(condition, exp.In):
        if condition.args.get("query") is not None:
            return make(condition.this, "in", [condition.args["query"]])
        return make(condition.this, "in", list(condition.expressions))
    if isinstance(condition, exp.Or):
        disjunction = _equality_disjunction(condition)
        if disjunction is not None:
            side, values = disjunction
            return make(side, "in", values)
    column = condition.find(exp.Column)
    if column is None:
        return None
    return Predicate(column.name.lower(), "other", (), False, None, clause, sql)


def _disjuncts(condition: exp.Expr) -> Iterator[exp.Expr]:
    if isinstance(condition, exp.Paren):
        yield from _disjuncts(condition.this)
    elif isinstance(condition, exp.Or):
        yield from _disjuncts(condition.left)
        yield from _disjuncts(condition.right)
    else:
        yield condition


def _equality_disjunction(
    condition: exp.Or,
) -> tuple[exp.Expr, list[exp.Expr]] | None:
    """Treat ``x = 'a' OR x = 'b'`` like ``x IN ('a', 'b')``, which BigQuery can prune."""
    side: exp.Expr | None = None
    values: list[exp.Expr] = []
    for disjunct in _disjuncts(condition):
        if not isinstance(disjunct, exp.EQ):
            return None
        left, right = disjunct.left, disjunct.right
        if _local_columns(left) and _is_constant(right):
            this, value = left, right
        elif _local_columns(right) and _is_constant(left):
            this, value = right, left
        else:
            return None
        if side is not None and this != side:
            return None
        side = this
        values.append(value)
    return (side, values) if side is not None else None


def _joins(scope: Scope) -> Iterator[Join]:
    select = scope.expression
    where = select.args.get("where")
    where_conjuncts = list(_conjuncts(where.this)) if where is not None else []
    for join in select.args.get("joins") or []:
        target = join.this
        alias = target.alias_or_name
        kind: Literal["table", "unnest", "derived"]
        if isinstance(target, exp.Unnest):
            kind = "unnest"
        elif isinstance(scope.sources.get(alias), exp.Table):
            kind = "table"
        else:
            kind = "derived"
        keys = [
            *_equality_keys(_conjuncts(join.args.get("on")), alias),
            *_equality_keys(where_conjuncts, alias),
        ]
        yield Join(
            target=alias,
            target_kind=kind,
            side=join.side or None,
            kind=join.kind or None,
            has_condition=join.args.get("on") is not None or bool(keys),
            keys=tuple(keys),
        )


def _equality_keys(
    conditions: Iterator[exp.Expr] | list[exp.Expr], alias: str
) -> Iterator[tuple[str, str]]:
    for condition in conditions:
        if not isinstance(condition, exp.EQ):
            continue
        left, right = condition.left, condition.right
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            continue
        if left.table == right.table or alias not in (left.table, right.table):
            continue
        yield (f"{left.table}.{left.name.lower()}", f"{right.table}.{right.name.lower()}")


def _effective_root(scope: Scope) -> Scope:
    """Skip pass-through wrappers such as ``SELECT * FROM (...)`` and pipe-syntax CTEs."""
    while True:
        select = scope.expression
        if not isinstance(select, exp.Select) or len(scope.selected_sources) != 1:
            return scope
        if any(select.args.get(key) for key in ("where", "group", "having", "qualify", "limit")):
            return scope
        if select.args.get("joins") or not all(
            isinstance(p, exp.Star | exp.Column) for p in select.expressions
        ):
            return scope
        ((_node, source),) = scope.selected_sources.values()
        if not isinstance(source, Scope):
            return scope
        scope = source


def _outer_shape(expression: exp.Expr) -> tuple[int | None, bool]:
    limit_node = expression.args.get("limit")
    limit: int | None = None
    if limit_node is not None:
        value = limit_node.expression
        if isinstance(value, exp.Literal) and value.is_int:
            limit = int(value.this)
    if not isinstance(expression, exp.Select):
        return limit, False
    aggregated = bool(expression.args.get("group")) or any(
        aggregate.find_ancestor(exp.Window) is None
        for projection in expression.expressions
        for aggregate in projection.find_all(exp.AggFunc)
    )
    return limit, aggregated
