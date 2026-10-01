"""Per-table facts for one resolved statement.

Rules and the cost estimator read these facts instead of walking the syntax tree. They
are built from ``resolve()``'s qualified tree and follow what BigQuery's planner does:

- A filter on a CTE, subquery or UNION column reaches the table under it, so it can
  prune partitions there.
- A table counts only the columns its readers use, so ``SELECT *`` in a CTE whose
  reader picks one column reads one column.
- A CTE is followed once per reference, because BigQuery evaluates each reference.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Literal, NamedTuple

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, find_all_in_scope, traverse_scope

from scanisaur.catalog.model import Catalog, Table
from scanisaur.engine.parse import DIALECT, SqlParseError, describe, parse, resolvable
from scanisaur.engine.resolve import Resolution, ResolveError, TableKey, resolve, stars_of

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
#: Node types that need parentheses when they replace a column inside another operator.
_OPERATORS = (exp.Binary, exp.Unary, exp.Predicate)
#: Clauses that can name a SELECT's output columns by alias.
_ALIAS_CLAUSES = ("group", "order", "having", "qualify")
#: Readers that filter a CTE differently each get their own visit of it. Past this
#: many visits beyond one per scope, give up rather than risk exponential work.
_MAX_REVISITS = 1_000
#: Conditions that are never true when their column is NULL...
_NULL_REJECTING = (
    exp.EQ,
    exp.NEQ,
    exp.LT,
    exp.LTE,
    exp.GT,
    exp.GTE,
    exp.Between,
    exp.In,
    exp.Like,
)
#: ...unless one of these turns a NULL into a value.
_NULL_TOLERANT = (exp.Coalesce, exp.If, exp.Case, exp.Is)


class FactsError(ValueError):
    """The statement can't be described: it doesn't parse, resolve, or read anything."""


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
    #: The condition as it applies to the table; a filter written on a CTE column is
    #: shown in terms of the table under it.
    sql: str


@dataclass(frozen=True, slots=True)
class Join:
    target: str
    target_kind: Literal["table", "unnest", "derived"]
    side: str | None
    kind: str | None
    #: True when a condition in ON or WHERE relates the target to a source before it.
    has_condition: bool
    #: Equality pairs such as ("e.user_id", "u.user_id"), from ON or from WHERE.
    keys: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class TableFacts:
    table: Table
    #: The table's alias in the SELECT that reads it.
    alias: str
    #: Columns read: those the query's output, filters, joins or grouping actually use.
    columns: frozenset[str]
    #: The SELECT that reads the table selects it with ``*``...
    star: bool
    #: ...except these columns (BigQuery ``SELECT * EXCEPT (...)``).
    star_except: frozenset[str]
    predicates: tuple[Predicate, ...]
    #: How many times the table is scanned with these facts; a CTE referenced twice
    #: is scanned twice.
    scans: int


@dataclass(frozen=True, slots=True)
class QueryFacts:
    tables: tuple[TableFacts, ...]
    joins: tuple[Join, ...]
    outer_limit: int | None
    outer_aggregated: bool


def facts_from_sql(sql: str, catalog: Catalog) -> QueryFacts:
    """Parse and resolve one statement against ``catalog``, then extract its facts."""
    try:
        statements = parse(sql, DIALECT)
    except SqlParseError as error:
        raise FactsError(f"the SQL could not be parsed: {error.message}") from error
    if len(statements) != 1:
        raise FactsError(f"expected one statement, found {len(statements)}")
    (tree,) = statements
    target = resolvable(tree)
    if target is None:
        raise FactsError(f"{describe(tree)} statements have no query to describe")
    try:
        resolution = resolve(target, catalog, DIALECT)
    except ResolveError as error:
        raise FactsError(f"names could not be resolved: {error}") from error
    return extract(resolution)


def extract(resolution: Resolution) -> QueryFacts:
    """Extract facts from a statement that resolved without findings."""
    if resolution.findings:
        raise FactsError(resolution.findings[0].message)
    if resolution.qualified is None:  # pragma: no cover - resolve() always reports why
        raise FactsError("the statement did not resolve")
    scopes = traverse_scope(resolution.qualified)
    if not scopes:
        raise FactsError("the statement is not a query")
    walk = _Walk(resolution.references, scopes)
    walk.run()
    limit, aggregated = _outer_shape(_effective_root(scopes[-1]).expression)
    return QueryFacts(walk.table_facts(), tuple(walk.joins), limit, aggregated)


class _Pushed(NamedTuple):
    """A filter written on a CTE or subquery's columns, to apply to what's under it."""

    clause: Clause
    #: The condition, with the derived source's columns qualified by ``alias``.
    condition: exp.Expr
    alias: str


class _Item(NamedTuple):
    """One way a scope is reached: the output columns its reader uses (None: all of
    them) and the reader's filters on those columns."""

    needed: frozenset[str] | None
    pushed: tuple[_Pushed, ...]


class _Walk:
    """Visits scopes readers first, once per distinct way each one is reached.

    A CTE read the same way from several places is visited once and its scans are
    added up; a CTE read with different filters is visited once per set of filters.
    """

    def __init__(self, references: Mapping[TableKey, Table], scopes: list[Scope]) -> None:
        self._references = references
        # traverse_scope() yields children first. Reversed, every reader comes before
        # the CTEs, subqueries and UNION branches it reads.
        self._position = {id(scope): i for i, scope in enumerate(reversed(scopes))}
        self._scopes = {id(scope): scope for scope in scopes}
        self._queue: list[tuple[int, int]] = []
        self._pending: dict[int, dict[tuple[object, ...], tuple[_Item, int]]] = {}
        self._visited: list[TableFacts] = []
        self._joined: set[int] = set()
        self._visits = 0
        self._budget = len(scopes) + _MAX_REVISITS
        self._names: dict[int, list[str]] = {}
        self.joins: list[Join] = []
        self._schedule(scopes[-1], None, (), 1)

    def run(self) -> None:
        while self._queue:
            _position, key = heapq.heappop(self._queue)
            scope = self._scopes[key]
            for item, runs in self._pending.pop(key).values():
                self._visits += 1
                if self._visits > self._budget:
                    raise FactsError("the query reads its CTEs in too many ways to analyze")
                self._visit(scope, item, runs)

    def table_facts(self) -> tuple[TableFacts, ...]:
        """Identical facts from several paths become one entry with their scans added."""
        scans: dict[TableFacts, int] = {}
        for facts in self._visited:
            unscanned = replace(facts, scans=0)
            scans[unscanned] = scans.get(unscanned, 0) + facts.scans
        return tuple(replace(facts, scans=count) for facts, count in scans.items())

    def _schedule(
        self, scope: Scope, needed: frozenset[str] | None, pushed: Iterable[_Pushed], runs: int
    ) -> None:
        position = self._position.get(id(scope))
        if position is None:
            return  # not a scope of this statement, e.g. a recursive CTE's self-reference
        pushed = tuple(pushed)
        key = (needed, tuple((p.clause, p.alias, p.condition) for p in pushed))
        bucket = self._pending.get(id(scope))
        if bucket is None:
            bucket = self._pending[id(scope)] = {}
            heapq.heappush(self._queue, (position, id(scope)))
        item, count = bucket.get(key, (_Item(needed, pushed), 0))
        bucket[key] = (item, count + runs)

    def _output_names(self, expression: exp.Expr) -> list[str]:
        """A query's output column names. A long UNION chain shares its leftmost
        branch's names, so they are looked up once for the whole chain."""
        chain: list[exp.Expr] = []
        node = expression
        while id(node) not in self._names and isinstance(node, exp.SetOperation | exp.Subquery):
            chain.append(node)
            node = node.this  # the left operand, or the query in parentheses
        names = self._names.get(id(node))
        if names is None:
            names = [n.lower() for n in node.named_selects] if isinstance(node, exp.Query) else []
        for visited in [*chain, node]:
            self._names[id(visited)] = names
        return names

    def _visit(self, scope: Scope, item: _Item, runs: int) -> None:
        expression = scope.expression
        if isinstance(expression, exp.SetOperation):
            self._visit_set_operation(scope, expression, item, runs)
        elif isinstance(expression, exp.Select):
            self._visit_select(scope, expression, item, runs)
        else:  # parentheses around a query, UNNEST, table functions: only what's inside reads
            for child in [*scope.derived_table_scopes, *scope.subquery_scopes, *scope.udtf_scopes]:
                self._schedule(child, None, (), runs)

    def _visit_set_operation(
        self, scope: Scope, operation: exp.SetOperation, item: _Item, runs: int
    ) -> None:
        recursive = _is_recursive(scope)
        pushed = item.pushed
        if recursive or _limited(operation):
            pushed = ()  # the filter runs on rows the recursion or the LIMIT produced
        # UNION DISTINCT, INTERSECT and EXCEPT compare whole rows, so every column is read.
        prunes = (
            isinstance(operation, exp.Union)
            and not operation.args.get("distinct")
            and not recursive
        )
        names = self._output_names(operation)
        needed = item.needed
        if needed is not None:  # ORDER BY on the UNION reads its columns in every branch
            needed = needed | _named_in(operation.args.get("order"), set(names))
        for branch in scope.set_operation_scopes:
            branch_names = self._output_names(branch.expression)
            if operation.args.get("by_name"):  # BY NAME and CORRESPONDING match by name
                renamed = {name: name for name in names if name in branch_names}
            else:
                renamed = dict(zip(names, branch_names, strict=False))
            branch_needed = None
            if needed is not None and prunes:
                branch_needed = frozenset(renamed[n] for n in needed if n in renamed)
            branch_pushed = [
                p._replace(condition=_rename(p.condition, p.alias, renamed)) for p in pushed
            ]
            self._schedule(branch, branch_needed, branch_pushed, runs)

    def _visit_select(self, scope: Scope, select: exp.Select, item: _Item, runs: int) -> None:
        tables, derived = self._sources(scope)
        nullable = _null_supplying(select)
        predicates: dict[str, list[Predicate]] = {alias: [] for alias in tables}
        pushdown: dict[str, list[_Pushed]] = {alias: [] for alias in derived}
        inherited = [(clause, condition, None) for clause, condition in _inherited(select, item)]
        for clause, condition, filtered in [*_conditions(select), *inherited]:
            owners = {column.table for column in _local_columns(condition)}
            if len(owners) != 1:
                continue
            (owner,) = owners
            if filtered is not None and owner not in filtered:
                continue
            # On a side an outer join fills with NULLs, `u.id IS NULL` keeps the
            # unmatched rows rather than filtering u.
            if owner in nullable and not _null_rejecting(condition):
                continue
            if owner in tables:
                predicate = _classify(condition, clause)
                if predicate is not None:
                    predicates[owner].append(predicate)
            elif owner in derived and clause != "qualify":
                pushdown[owner].append(_Pushed(clause, condition, owner))

        reads = _read_columns(scope, select, item.needed)
        stars = stars_of(select)
        for alias, table in tables.items():
            starred = [s for s in stars if s.qualifier.lower() in ("", alias.lower())]
            if alias in reads.whole_rows:
                columns = frozenset(column.name.lower() for column in table.columns)
            else:
                columns = frozenset(c.name.lower() for c in reads.columns if c.table == alias)
            self._visited.append(
                TableFacts(
                    table=table,
                    alias=alias,
                    columns=columns,
                    star=bool(starred),
                    star_except=frozenset(e.name.lower() for s in starred for e in s.excepted),
                    predicates=tuple(predicates[alias]),
                    scans=runs,
                )
            )
        if id(scope) not in self._joined:
            self._joined.add(id(scope))
            self.joins.extend(_joins(scope, select, tables))

        for alias, source in derived.items():
            if _is_recursive(source):
                self._schedule(source, None, (), runs)
            elif alias in reads.whole_rows:
                self._schedule(source, None, pushdown[alias], runs)
            else:
                used = frozenset(c.name.lower() for c in reads.columns if c.table == alias)
                self._schedule(source, used, pushdown[alias], runs)
        for child in [*scope.subquery_scopes, *scope.udtf_scopes]:
            # BigQuery drops output columns nobody reads, with any subquery inside them.
            if not any(_within(child.expression, projection) for projection in reads.unused):
                self._schedule(child, None, (), runs)

    def _sources(self, scope: Scope) -> tuple[dict[str, Table], dict[str, Scope]]:
        """Catalog tables and derived sources (CTEs, subqueries) by alias."""
        tables: dict[str, Table] = {}
        derived: dict[str, Scope] = {}
        for alias, (_node, source) in scope.selected_sources.items():
            if isinstance(source, exp.Table):
                table = self._references.get((source.catalog, source.db, source.name))
                if table is not None:  # INFORMATION_SCHEMA views and table functions aren't
                    tables[alias] = table
            elif isinstance(source, Scope) and isinstance(source.expression, exp.Query):
                derived[alias] = source
        return tables, derived


def _joins_in_order(select: exp.Select) -> Iterator[tuple[exp.Join, frozenset[str]]]:
    """Each join, with the sources joined before it."""
    earlier = {_from_alias(select)}
    for join in select.args.get("joins") or []:
        yield join, frozenset(earlier)
        earlier.add(join.alias_or_name)


def _conditions(select: exp.Select) -> Iterator[tuple[Clause, exp.Expr, frozenset[str] | None]]:
    """Each conjunct of WHERE, ON and QUALIFY, with the sources it can filter (None: any)."""
    where = select.args.get("where")
    if where is not None:
        yield from (("where", c, None) for c in _conjuncts(where.this))
    for join, earlier in _joins_in_order(select):
        # An outer join's ON only decides which rows match; it filters the other side.
        filtered: frozenset[str] | None = {
            "LEFT": frozenset({join.alias_or_name}),
            "RIGHT": earlier,
            "FULL": frozenset(),
        }.get(join.side)
        yield from (("on", c, filtered) for c in _conjuncts(join.args.get("on")))
    qualify = select.args.get("qualify")
    if qualify is not None:
        yield from (("qualify", c, None) for c in _conjuncts(qualify.this))


def _null_supplying(select: exp.Select) -> frozenset[str]:
    """Sources an outer join fills with NULLs where they don't match."""
    nullable: set[str] = set()
    for join, earlier in _joins_in_order(select):
        if join.side in ("LEFT", "FULL"):
            nullable.add(join.alias_or_name)
        if join.side in ("RIGHT", "FULL"):
            nullable |= earlier
    return frozenset(nullable)


def _null_rejecting(condition: exp.Expr) -> bool:
    """True when the condition is never true for NULL, as a plain comparison isn't."""
    return isinstance(condition, _NULL_REJECTING) and condition.find(*_NULL_TOLERANT) is None


def _is_recursive(scope: Scope) -> bool:
    """A CTE that reads itself: no filter moves into it and all its columns are read."""
    cte = scope.expression.parent
    if not isinstance(cte, exp.CTE):
        return False
    with_ = cte.parent
    if not (isinstance(with_, exp.With) and with_.args.get("recursive")):
        return False
    name = cte.alias.lower()
    return any(not t.db and t.name.lower() == name for t in cte.this.find_all(exp.Table))


def _limited(query: exp.Expr) -> bool:
    """True when a LIMIT or OFFSET picks the query's rows, on the query itself or on
    parentheses around it, as in ``((SELECT ...) LIMIT 5)``."""
    node: exp.Expr | None = query
    while node is not None:
        if node.args.get("limit") or node.args.get("offset"):
            return True
        node = node.parent if isinstance(node.parent, exp.Subquery) else None
    return False


def _named_in(clause: exp.Expr | None, names: set[str]) -> frozenset[str]:
    """The output columns a clause such as ORDER BY names by alias."""
    if clause is None:
        return frozenset()
    return frozenset(
        c.name.lower()
        for c in find_all_in_scope(clause, exp.Column)
        if not c.table and c.name.lower() in names
    )


def _within(node: exp.Expr, ancestor: exp.Expr) -> bool:
    parent = node.parent
    while parent is not None:
        if parent is ancestor:
            return True
        parent = parent.parent
    return False


def _from_alias(select: exp.Select) -> str:
    from_ = select.args.get("from_")
    return from_.this.alias_or_name if isinstance(from_, exp.From) else ""


def _inherited(select: exp.Select, item: _Item) -> Iterator[tuple[Clause, exp.Expr]]:
    """The reader's filters, rewritten in terms of this SELECT's own sources."""
    if _limited(select) or select.args.get("qualify"):
        return  # the reader's filter runs on rows these clauses already picked
    pushable = _pushable(select)
    # A filter can move below window functions only if it keeps or drops whole
    # partitions: every column it reads must be in every window's PARTITION BY.
    windows = [w for p in select.expressions for w in p.find_all(exp.Window)]
    keys = [
        {_key(c) for c in w.args.get("partition_by") or [] if isinstance(c, exp.Column)}
        for w in windows
    ]
    for clause, condition, alias in item.pushed:
        translated = _translate(condition, alias, pushable)
        if translated is None:
            continue
        if any(not {_key(c) for c in _local_columns(translated)} <= k for k in keys):
            continue
        yield clause, translated


def _pushable(select: exp.Select) -> dict[str, exp.Expr]:
    """The output columns a reader's filter can be rewritten through, by name."""
    group = select.args.get("group")
    group_items = list(group.expressions) if group is not None else []
    if any(isinstance(g, exp.Rollup | exp.Cube | exp.GroupingSets) for g in group_items):
        return {}  # their total rows have NULL keys and read every input row
    projections = {p.alias_or_name.lower(): p.unalias() for p in select.expressions}
    if group is None and any(p.find(exp.AggFunc) for p in projections.values()):
        return {}  # one row for the whole input
    alias_keys = _named_in(group, set(projections))
    pushable: dict[str, exp.Expr] = {}
    for name, projection in projections.items():
        # An aggregate, a window, a subquery, or a function sqlglot doesn't know, such
        # as a user-defined aggregate, doesn't pass a filter through to its input.
        if projection.find(exp.AggFunc, exp.Window, exp.Query, exp.Anonymous):
            continue
        if group is not None and name not in alias_keys and projection not in group_items:
            continue  # only grouping keys pass GROUP BY
        pushable[name] = projection
    return pushable


def _key(column: exp.Column) -> tuple[str, str]:
    return column.table, column.name.lower()


def _translate(
    condition: exp.Expr, alias: str, projections: dict[str, exp.Expr]
) -> exp.Expr | None:
    """Replace ``alias.name`` with the expression that produces it; None when a column
    the filter reads has no pushable expression."""
    if any(
        column.table == alias and column.name.lower() not in projections
        for column in condition.find_all(exp.Column)
    ):
        return None

    def substitute(node: exp.Expr) -> exp.Expr:
        if not isinstance(node, exp.Column) or node.table != alias:
            return node
        new = projections[node.name.lower()].copy()
        if isinstance(new, _OPERATORS) and isinstance(node.parent, _OPERATORS):
            new = exp.paren(new, copy=False)
        return new

    return condition.transform(substitute)


def _rename(condition: exp.Expr, alias: str, names: dict[str, str]) -> exp.Expr:
    """Point a UNION column reference at the same position in one branch."""
    renamed = condition.copy()
    for column in renamed.find_all(exp.Column):
        if column.table == alias and column.name.lower() in names:
            column.set("this", exp.to_identifier(names[column.name.lower()]))
    return renamed


class _Reads(NamedTuple):
    #: The scope's columns, without those only in output columns nobody reads.
    columns: list[exp.Column]
    #: Sources used as a whole row, as in ``TO_JSON_STRING(t)``: all their columns are read.
    whole_rows: frozenset[str]
    #: Output columns nobody reads.
    unused: list[exp.Expr]


def _read_columns(scope: Scope, select: exp.Select, needed: frozenset[str] | None) -> _Reads:
    columns = [c for c in scope.columns if not isinstance(c, exp.Pseudocolumn)]
    unused = [] if needed is None or select.args.get("distinct") else _unused(select, needed)
    skipped = {
        id(node)
        for projection in unused
        for node in projection.find_all(exp.Column, exp.TableColumn)
    }
    # qualify() turns a whole-row reference such as TO_JSON_STRING(t) into a TableColumn.
    whole = {
        node.name
        for node in find_all_in_scope(select, exp.TableColumn)
        if node.name in scope.selected_sources and id(node) not in skipped
    }
    # A star qualify() couldn't expand (duplicate column names) reads whole rows too.
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            whole |= set(scope.selected_sources)
        elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            whole.add(projection.table)
    whole_rows = frozenset(whole)
    return _Reads([c for c in columns if id(c) not in skipped], whole_rows, unused)


def _unused(select: exp.Select, needed: frozenset[str]) -> list[exp.Expr]:
    """Output columns the reader doesn't use, and no clause of the SELECT names."""
    aliases = {p.alias_or_name.lower() for p in select.expressions}
    used = set(needed)
    for key in _ALIAS_CLAUSES:  # e.g. GROUP BY day, where day is an output column
        clause = select.args.get(key)
        if clause is not None:
            used |= {
                c.name.lower()
                for c in find_all_in_scope(clause, exp.Column)
                if not c.table and c.name.lower() in aliases
            }
    return [
        p
        for p in select.expressions
        if p.alias_or_name.lower() not in used and not p.find(exp.Star)
    ]


def _conjuncts(condition: exp.Expr | None) -> Iterator[exp.Expr]:
    return _operands(condition, exp.And)


def _operands(condition: exp.Expr | None, connector: type[exp.Connector]) -> Iterator[exp.Expr]:
    """The operands of a chain of ANDs or ORs, left to right, through parentheses.
    Iterative, so a chain of a thousand conditions doesn't exhaust the stack."""
    stack = [condition] if condition is not None else []
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Paren):
            stack.append(node.this)
        elif isinstance(node, connector):
            stack.extend((node.right, node.left))
        else:
            yield node


def _local_columns(node: exp.Expr) -> list[exp.Column]:
    """Columns in ``node`` itself, not in a subquery inside it."""
    return [c for c in find_all_in_scope(node, exp.Column) if not isinstance(c.this, exp.Star)]


def _is_constant(node: exp.Expr) -> bool:
    return not _local_columns(node) and node.find(exp.Query) is None


def _wrapper(side: exp.Expr) -> str | None:
    """The function around a column, ignoring parentheses and struct field access."""
    while isinstance(side, exp.Paren | exp.Dot):
        side = side.this
    if isinstance(side, exp.Column):
        return None
    if isinstance(side, exp.Func):
        return side.sql_name()
    return side.key.upper()


def _classify(condition: exp.Expr, clause: Clause) -> Predicate | None:
    sql = condition.sql(dialect=DIALECT)

    def other() -> Predicate | None:
        columns = _local_columns(condition)
        if not columns:
            return None
        return Predicate(columns[0].name.lower(), "other", (), False, None, clause, sql)

    def make(side: exp.Expr, op: ComparisonOp, others: list[exp.Expr]) -> Predicate | None:
        columns = _local_columns(side)
        if not columns:  # e.g. CURRENT_DATE() BETWEEN valid_from AND valid_to
            return other()
        constant = bool(others) and all(_is_constant(value) for value in others)
        values = tuple(value.sql(dialect=DIALECT) for value in others) if constant else ()
        return Predicate(columns[0].name.lower(), op, values, constant, _wrapper(side), clause, sql)

    comparison = _COMPARISONS.get(type(condition))
    if comparison is not None and isinstance(condition, exp.Binary):
        left, right = condition.left, condition.right
        if _local_columns(left) and not _local_columns(right):
            return make(left, comparison, [right])
        if _local_columns(right) and not _local_columns(left):
            return make(right, _FLIPPED.get(comparison, comparison), [left])
        return other()
    if isinstance(condition, exp.Between):
        return make(condition.this, "between", [condition.args["low"], condition.args["high"]])
    if isinstance(condition, exp.In):
        return make(condition.this, "in", _in_values(condition))
    if isinstance(condition, exp.Or):
        disjunction = _equality_disjunction(condition)
        if disjunction is not None:
            side, values = disjunction
            return make(side, "in", values)
    return other()


def _in_values(condition: exp.In) -> list[exp.Expr]:
    """The right-hand side of IN: a list, a subquery, or UNNEST of an array."""
    query = condition.args.get("query")
    if query is not None:
        return [query]
    unnest = condition.args.get("unnest")
    if unnest is not None:
        arrays = unnest.expressions
        if len(arrays) == 1 and isinstance(arrays[0], exp.Array):
            return list(arrays[0].expressions)
        return list(arrays)
    return list(condition.expressions)


def _disjuncts(condition: exp.Expr) -> Iterator[exp.Expr]:
    return _operands(condition, exp.Or)


def _equality_disjunction(condition: exp.Or) -> tuple[exp.Expr, list[exp.Expr]] | None:
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


def _joins(scope: Scope, select: exp.Select, tables: dict[str, Table]) -> Iterator[Join]:
    """Joins in FROM order. A condition links a join only to sources joined before it:
    in ``FROM a, b, c WHERE b.x = c.x`` the join to ``b`` has no condition."""
    where = select.args.get("where")
    where_conjuncts = list(_conjuncts(where.this)) if where is not None else []
    for join, earlier in _joins_in_order(select):
        target = join.alias_or_name
        kind: Literal["table", "unnest", "derived"]
        if isinstance(join.this, exp.Unnest):
            kind = "unnest"
        elif target in tables or isinstance(scope.sources.get(target), exp.Table):
            kind = "table"
        else:
            kind = "derived"
        links = [
            condition
            for condition in [*_conjuncts(join.args.get("on")), *where_conjuncts]
            if _links(condition, target, earlier)
        ]
        yield Join(
            target=target,
            target_kind=kind,
            side=join.side or None,
            kind=join.kind or None,
            has_condition=bool(links),
            keys=tuple(key for condition in links if (key := _equality_key(condition))),
        )


def _links(condition: exp.Expr, target: str, earlier: frozenset[str]) -> bool:
    """True when the condition relates ``target`` to sources joined before it, and only those."""
    owners = {column.table for column in _local_columns(condition)}
    others = owners - {target}
    return target in owners and bool(others) and others <= earlier


def _equality_key(condition: exp.Expr) -> tuple[str, str] | None:
    if not isinstance(condition, exp.EQ):
        return None
    left, right = condition.left, condition.right
    if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
        return None
    if left.table == right.table:
        return None
    return (f"{left.table}.{left.name.lower()}", f"{right.table}.{right.name.lower()}")


def _effective_root(scope: Scope) -> Scope:
    """Skip pass-through wrappers such as ``SELECT * FROM (...)`` and pipe-syntax CTEs."""
    while True:
        select = scope.expression
        if not isinstance(select, exp.Select) or len(scope.selected_sources) != 1:
            return scope
        if any(select.args.get(key) for key in ("where", "group", "having", "qualify", "limit")):
            return scope
        if select.args.get("joins") or not all(
            isinstance(p, exp.Star | exp.Column) or isinstance(p.unalias(), exp.Column)
            for p in select.expressions
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
    return limit, _aggregated(expression)


def _aggregated(expression: exp.Expr) -> bool:
    """True when the query returns one row per group rather than one per input row;
    for a set operation, when every branch does."""
    stack = [expression]
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Subquery):
            stack.append(node.this)
        elif isinstance(node, exp.SetOperation):
            stack.extend((node.left, node.right))
        elif not _select_aggregated(node):
            return False
    return True


def _select_aggregated(node: exp.Expr) -> bool:
    if not isinstance(node, exp.Select):
        return False
    if node.args.get("group"):
        return True
    # Aggregates inside scalar subqueries or window functions don't collapse the rows.
    return any(
        aggregate.find_ancestor(exp.Window) is None
        for projection in node.expressions
        for aggregate in find_all_in_scope(projection, exp.AggFunc)
    )
