"""Per-table facts for one resolved statement.

Rules and the cost estimator read these facts instead of walking the syntax tree. They
are built from ``resolve()``'s qualified tree and follow what BigQuery's planner does:

- A filter on a CTE, subquery or UNION column reaches the table under it, so it can
  prune partitions there.
- A table counts only the columns its readers use, so ``SELECT *`` in a CTE whose
  reader picks one column reads one column.
- A CTE is followed once per reference, because BigQuery evaluates each reference.
- A struct field is read on its own: ``device.category``, or ``i.item_name`` through
  ``UNNEST(items) AS i``, reads that field rather than the whole column, as BigQuery
  bills it (measured in issue #22).
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Literal, NamedTuple

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, find_all_in_scope, traverse_scope

from scanisaur.catalog.model import Catalog, Table
from scanisaur.engine.parse import DIALECT, SqlParseError, describe, parse, position, resolvable
from scanisaur.engine.resolve import Resolution, ResolveError, TableKey, resolve, stars_of

ComparisonOp = Literal["=", "<", "<=", ">", ">=", "between", "in", "other"]
Clause = Literal["where", "on", "having", "qualify"]

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
#: Names that need no backticks.
_PLAIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class FactsError(ValueError):
    """The statement can't be described: it doesn't parse, resolve, or read anything."""


class TooComplexError(FactsError):
    """The query reads its CTEs in too many different ways to analyze."""


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
    #: The table name as the query wrote it. It differs from ``table.name`` when a narrower
    #: wildcard such as ``events_2026*`` reads part of the ``events_*`` family.
    name: str = ""
    #: Where the query reads the table, as (line, column). Not compared, so identical facts
    #: read from several places still merge, keeping the first place.
    position: tuple[int | None, int | None] = field(default=(None, None), compare=False)
    #: Columns whose values something other than these predicates may limit: a join such
    #: as ``p.wiki = w.wiki``, a correlated ``EXISTS``, ``INTERSECT``, or a reader's filter
    #: that can't move down to the table, as one above a ``LIMIT`` can't.
    linked: frozenset[str] = frozenset()
    #: The struct fields read, as paths from the column such as ("device", "category"); a
    #: column read whole is its name alone. Every column in ``columns`` starts a path.
    #: None when every column is read whole.
    paths: frozenset[tuple[str, ...]] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DerivedSource:
    """A CTE or subquery joined as a source."""

    alias: str
    #: At most this many rows, from its LIMIT or its shape; None when that isn't known.
    rows: int | None
    position: tuple[int | None, int | None] = field(default=(None, None), compare=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class Product:
    """A SELECT whose sources fall into groups that no equality in WHERE, ON or USING
    connects, so joining them pairs every row of one group with every row of another."""

    #: Each group of sources the equalities connect, in FROM order. A source with at most
    #: one row, and an UNNEST, which belongs to the source whose array it reads, are left
    #: out.
    groups: tuple[tuple[TableFacts | DerivedSource, ...], ...]
    #: A condition that does relate the groups but isn't an equality between two sources,
    #: as `a.ts < b.ts`, so BigQuery compares every pair; None when nothing relates them.
    inequality: str | None
    #: Where the second group is joined.
    position: tuple[int | None, int | None] = field(default=(None, None), compare=False)
    #: Tables whose rows something other than the conditions relating sources limits: a
    #: correlated subquery, INTERSECT, or a reader's filter that can't move down.
    limited: frozenset[str] = frozenset()
    #: Tables whose arrays this SELECT flattens with UNNEST, which changes their rows.
    flattened: frozenset[str] = frozenset()
    #: The SELECT's LIMIT, when nothing in it needs every pair first (ORDER BY, GROUP BY,
    #: DISTINCT, an aggregate, a window), so BigQuery stops once it has that many rows.
    limit: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyedSource:
    """A table, CTE or subquery that a SELECT matches to another by equal columns."""

    alias: str
    #: The catalog table's name; None for a CTE or subquery.
    table: str | None
    #: Its columns, or a CTE or subquery's output columns.
    columns: frozenset[str]
    #: Sets of columns unique in it; None when that isn't known.
    keys: tuple[frozenset[str], ...] | None
    #: Columns this SELECT compares with one value, as ``i.status = 'Complete'``. They
    #: count toward a key: ``(order_id, line)`` is unique per order when ``line = 1``.
    fixed: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyMatch:
    """The equalities in WHERE and ON between the columns of two sources."""

    left: str
    right: str
    #: The columns of each side the equalities compare.
    left_columns: frozenset[str]
    right_columns: frozenset[str]
    #: The first such equality, as written.
    condition: str
    #: Where the later of the two sources is written.
    position: tuple[int | None, int | None] = field(default=(None, None), compare=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class Aggregation:
    """``SUM``, ``AVG``, ``COUNT`` or ``COUNTIF`` of one source's columns, without
    ``DISTINCT``: each of its rows counts as many times as the join repeats it."""

    sql: str
    #: SUM, AVG, COUNT or COUNTIF.
    function: str
    source: str
    position: tuple[int | None, int | None] = field(default=(None, None), compare=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyedJoin:
    """A SELECT that matches sources by equal columns, for SCN007: when the columns
    matched aren't a unique key of a side, each row of the other repeats once per match."""

    sources: tuple[KeyedSource, ...]
    matches: tuple[KeyMatch, ...]
    aggregations: tuple[Aggregation, ...]


@dataclass(frozen=True, slots=True)
class QueryFacts:
    tables: tuple[TableFacts, ...]
    joins: tuple[Join, ...]
    outer_limit: int | None
    outer_aggregated: bool
    products: tuple[Product, ...] = ()
    keyed_joins: tuple[KeyedJoin, ...] = ()


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
    # Visits of one SELECT give equal products; separate SELECTs, such as two UNION
    # branches alike, stay apart by where they are written.
    products = tuple({(p, p.position): p for p in walk.products}.values())
    keyed = tuple(walk.keyed_joins)  # one per SELECT, however often it is visited
    return QueryFacts(walk.table_facts(), tuple(walk.joins), limit, aggregated, products, keyed)


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
    #: Output columns whose values the reader may limit in ways that aren't filters here:
    #: a join, a correlated subquery, or a filter that can't move below this scope.
    linked: frozenset[str] = frozenset()
    #: The fields of output columns the reader reads, as paths from the column's name; a
    #: column read whole is its name alone. None: every column it reads, whole.
    fields: frozenset[tuple[str, ...]] | None = None


class _Reading(NamedTuple):
    """How one visit of a SELECT is read: what its paths and its UNNESTs depend on."""

    select: exp.Select
    scope: Scope
    needed: frozenset[str] | None
    fields: frozenset[tuple[str, ...]] | None
    #: Output columns read whole whatever the reader reads of them: those GROUP BY or
    #: ORDER BY name; None when DISTINCT or GROUP BY ALL reads every one whole.
    whole: frozenset[str] | None
    #: Columns only in output columns nobody reads, which BigQuery drops.
    skipped: frozenset[int]


_Sources = tuple[
    dict[str, Table], dict[str, Scope], dict[str, str], dict[str, tuple[int | None, int | None]]
]


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
        # Each UNNEST, with the scope that reads its elements and their alias there.
        self._unnests = {
            id(node): (scope, alias)
            for scope in scopes
            for alias, (node, _source) in scope.selected_sources.items()
            if isinstance(node, exp.Unnest)
        }
        self._unnest_fields_seen: dict[tuple[int, object], frozenset[tuple[str, ...]] | None] = {}
        self.joins: list[Join] = []
        self.products: list[Product] = []
        self.keyed_joins: list[KeyedJoin] = []
        self._keys: dict[int, tuple[frozenset[str], ...] | None] = {}
        self._schedule(scopes[-1], None, (), 1)

    def run(self) -> None:
        while self._queue:
            _position, key = heapq.heappop(self._queue)
            scope = self._scopes[key]
            for item, runs in self._pending.pop(key).values():
                self._visits += 1
                if self._visits > self._budget:
                    raise TooComplexError("the query reads its CTEs in too many ways to analyze")
                self._visit(scope, item, runs)

    def table_facts(self) -> tuple[TableFacts, ...]:
        """Identical facts from several paths become one entry with their scans added,
        at the earliest position any of them is written."""
        scans: dict[TableFacts, int] = {}
        first: dict[TableFacts, TableFacts] = {}
        for facts in self._visited:
            unscanned = replace(facts, scans=0)
            scans[unscanned] = scans.get(unscanned, 0) + facts.scans
            seen = first.get(unscanned)
            if seen is None or _sort_key(facts.position) < _sort_key(seen.position):
                first[unscanned] = facts
        return tuple(replace(first[facts], scans=count) for facts, count in scans.items())

    def _schedule(
        self,
        scope: Scope,
        needed: frozenset[str] | None,
        pushed: Iterable[_Pushed],
        runs: int,
        linked: frozenset[str] = frozenset(),
        fields: frozenset[tuple[str, ...]] | None = None,
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
        found = bucket.get(key)
        if found is None:
            bucket[key] = (_Item(needed, pushed, linked, fields), runs)
            return
        # Links and fields don't split visits: readers that differ only in them share one
        # visit, which reads what any of them does.
        item, count = found
        merged = None if item.fields is None or fields is None else item.fields | fields
        bucket[key] = (item._replace(linked=item.linked | linked, fields=merged), count + runs)

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
                self._schedule(child, None, (), runs, item.linked)

    def _visit_set_operation(
        self, scope: Scope, operation: exp.SetOperation, item: _Item, runs: int
    ) -> None:
        recursive = _is_recursive(scope)
        pushed, linked = item.pushed, item.linked
        if recursive or _limited(operation):
            # The filter runs on rows the recursion or the LIMIT produced.
            pushed, linked = (), linked | _filtered_names(item.pushed)
        names = self._output_names(operation)
        if not isinstance(operation, exp.Union):  # INTERSECT and EXCEPT match other rows
            linked = linked | frozenset(names)
        # UNION DISTINCT, INTERSECT and EXCEPT compare whole rows, so every column is read.
        prunes = (
            isinstance(operation, exp.Union)
            and not operation.args.get("distinct")
            and not recursive
        )
        needed, fields = item.needed, item.fields
        if needed is not None:  # ORDER BY on the UNION reads its columns in every branch
            ordered = _named_in(operation.args.get("order"), set(names))
            needed = needed | ordered
            if fields is not None:
                fields = fields | {(name,) for name in ordered}
        for branch in scope.set_operation_scopes:
            branch_names = self._output_names(branch.expression)
            if operation.args.get("by_name"):  # BY NAME and CORRESPONDING match by name
                renamed = {name: name for name in names if name in branch_names}
            else:
                renamed = dict(zip(names, branch_names, strict=False))
            branch_needed = branch_fields = None
            if needed is not None and prunes:
                branch_needed = frozenset(renamed[n] for n in needed if n in renamed)
                if fields is not None:
                    branch_fields = frozenset(
                        (renamed[path[0]], *path[1:]) for path in fields if path[0] in renamed
                    )
            branch_pushed = [
                p._replace(condition=_rename(p.condition, p.alias, renamed)) for p in pushed
            ]
            branch_linked = frozenset(renamed[n] for n in linked if n in renamed)
            self._schedule(branch, branch_needed, branch_pushed, runs, branch_linked, branch_fields)

    def _visit_select(self, scope: Scope, select: exp.Select, item: _Item, runs: int) -> None:
        tables, derived, names, positions = self._sources(scope)
        nullable = _null_supplying(select)
        predicates: dict[str, list[Predicate]] = {alias: [] for alias in tables}
        pushdown: dict[str, list[_Pushed]] = {alias: [] for alias in derived}
        inherited, dropped = _inherited(select, item)
        linked = _Links(tables.keys() | derived.keys())
        # The same, less conditions that relate sources in WHERE or ON: they filter pairs
        # of rows, not one source's rows, which SCN006 needs to know.
        limits = _Links(tables.keys() | derived.keys())
        producers = _producers(select, item.linked | dropped)
        linked.add(producers)
        limits.add(producers)
        conditions = [*_conditions(select), *((clause, c, None) for clause, c in inherited)]
        for clause, condition, filtered in conditions:
            local = _local_columns(condition)
            correlated = list(_correlated(condition, linked.sources, local))
            linked.add(correlated)
            limits.add(correlated)
            owner = _filtered_source(condition, local, filtered, nullable)
            if owner in tables:
                predicate = _classify(condition, clause)
                if predicate is not None:
                    predicates[owner].append(predicate)
                    continue
            elif owner in derived and clause != "qualify":
                pushdown[owner].append(_Pushed(clause, condition, owner))
                continue
            # Not a filter on one source, as a join condition or a filter that runs too late
            # isn't, but it may still limit the values of the columns it reads.
            linked.add(local)
            if clause not in ("where", "on") or len({c.table for c in local}) < 2:
                limits.add(local)

        named = _named_by_clauses(select)
        reads = _read_columns(scope, select, item.needed, named)
        group = select.args.get("group")
        everything = select.args.get("distinct") or (group is not None and group.args.get("all"))
        reading = _Reading(
            select,
            scope,
            item.needed,
            item.fields,
            whole=None if everything else named,
            skipped=_columns_in(reads.unused),
        )
        paths: dict[str, set[tuple[str, ...]]] = {}
        for column in reads.columns:
            paths.setdefault(column.table, set()).update(self._read_paths(column, reading))
        stars = stars_of(select)
        visited: dict[str, TableFacts] = {}
        for alias, table in tables.items():
            starred = [s for s in stars if s.qualifier.lower() in ("", alias.lower())]
            table_paths: frozenset[tuple[str, ...]] | None = None
            if alias in reads.whole_rows:
                columns = frozenset(column.name.lower() for column in table.columns)
            else:
                columns = frozenset(c.name.lower() for c in reads.columns if c.table == alias)
                if any(len(path) > 1 for path in paths.get(alias, ())):
                    table_paths = frozenset(paths[alias])
            visited[alias] = TableFacts(
                table=table,
                alias=alias,
                columns=columns,
                star=bool(starred),
                star_except=frozenset(e.name.lower() for s in starred for e in s.excepted),
                predicates=tuple(predicates[alias]),
                scans=runs,
                linked=linked.of(alias),
                name=names[alias],
                position=positions[alias],
                paths=table_paths,
            )
            self._visited.append(visited[alias])
        limited = frozenset(alias for alias in visited if limits.of(alias))
        product = _product(scope, select, visited, derived, limited)
        if product is not None:
            self.products.append(product)
        if id(scope) not in self._joined:
            self._joined.add(id(scope))
            self.joins.extend(_joins(scope, select, tables))
            keyed = self._keyed_join(scope, select, tables, derived, positions)
            if keyed is not None:
                self.keyed_joins.append(keyed)

        for alias, source in derived.items():
            links = linked.of(alias)
            if _is_recursive(source):
                self._schedule(source, None, (), runs, links | _filtered_names(pushdown[alias]))
            elif alias in reads.whole_rows:
                self._schedule(source, None, pushdown[alias], runs, links)
            else:
                used = frozenset(c.name.lower() for c in reads.columns if c.table == alias)
                fields = frozenset(paths.get(alias, ()))
                self._schedule(source, used, pushdown[alias], runs, links, fields)
        for child in [*scope.subquery_scopes, *scope.udtf_scopes]:
            # BigQuery drops output columns nobody reads, with any subquery inside them.
            if not any(_within(child.expression, projection) for projection in reads.unused):
                self._schedule(child, _needed_by(child), (), runs)

    def _read_paths(self, column: exp.Column, reading: _Reading) -> set[tuple[str, ...]]:
        """What reading ``column`` here reads of it: the struct fields written after it,
        and those the reader reads of the output column it is, or of an array's elements
        through UNNEST."""
        top, chain = _field_chain(column)
        path = (column.name.lower(), *chain)
        parent = top.parent
        if isinstance(parent, exp.Unnest):
            inner = self._unnest_fields(parent, reading)
        else:
            projection = parent if isinstance(parent, exp.Alias) else top
            inner = None
            if projection.parent is reading.select and projection.arg_key == "expressions":
                inner = _output_fields(projection.alias_or_name.lower(), reading)
        if inner is None:
            return {path}
        return {(*path, *field) for field in inner}

    def _unnest_fields(
        self, unnest: exp.Unnest, reading: _Reading
    ) -> frozenset[tuple[str, ...]] | None:
        """The fields of an array's elements the query reads through ``unnest``, as paths
        within an element; None when it reads whole elements, or that isn't known. Fields
        only in output columns nobody reads don't count."""
        found = self._unnests.get(id(unnest))
        if found is None or len(unnest.expressions) != 1:
            return None
        scope, alias = found
        # In this SELECT, what is dropped depends on how this visit is read; in a
        # subquery, such as `(SELECT value FROM UNNEST(params) ...)`, on how it is used.
        key = (id(unnest), reading.needed if scope is reading.scope else "subquery")
        if key not in self._unnest_fields_seen:
            if scope is reading.scope:
                skipped = reading.skipped
            else:
                skipped = _skipped_columns(scope.expression, _needed_by(scope))
            self._unnest_fields_seen[key] = _element_fields(unnest, scope, alias, skipped)
        return self._unnest_fields_seen[key]

    def _keyed_join(
        self,
        scope: Scope,
        select: exp.Select,
        tables: Mapping[str, Table],
        derived: Mapping[str, Scope],
        positions: Mapping[str, tuple[int | None, int | None]],
    ) -> KeyedJoin | None:
        """The equalities between this SELECT's sources, what is unique in each, and the
        aggregates that would count a source's rows once per match."""
        members = {*tables, *derived}
        if len(members) < 2:
            return None
        written = dict(positions)
        for alias in derived:
            written[alias] = _written_at(scope.selected_sources[alias][0])
        pairs, fixed = _equalities(select, members, written)
        if not pairs:
            return None
        order = list(scope.selected_sources)
        sources = sorted(
            (self._keyed_source(alias, tables, derived, fixed[alias]) for alias in members),
            key=lambda source: order.index(source.alias),
        )
        return KeyedJoin(
            sources=tuple(sources),
            matches=_key_matches(pairs, written),
            aggregations=tuple(_aggregations(select, members)),
        )

    def _keyed_source(
        self,
        alias: str,
        tables: Mapping[str, Table],
        derived: Mapping[str, Scope],
        fixed: set[str],
    ) -> KeyedSource:
        """A catalog table's columns and declared keys, or a CTE or subquery's output
        columns and the keys its query implies."""
        table = tables.get(alias)
        if table is None:
            return KeyedSource(
                alias=alias,
                table=None,
                columns=frozenset(self._output_names(derived[alias].expression)),
                keys=self._source_keys(derived[alias]),
                fixed=frozenset(fixed),
            )
        return KeyedSource(
            alias=alias,
            table=table.name,
            columns=frozenset(c.name.lower() for c in table.columns),
            keys=_lowered(table.keys),
            fixed=frozenset(fixed),
        )

    def _source_keys(self, scope: Scope) -> tuple[frozenset[str], ...] | None:
        """Sets of a CTE or subquery's output columns that are unique in its rows: the
        GROUP BY columns, every column under DISTINCT, any set at all for a single row,
        or the keys of the one table or CTE it selects from. None when not known."""
        if id(scope) in self._keys:
            return self._keys[id(scope)]
        self._keys[id(scope)] = None  # a recursive CTE reaches itself
        keys = self._compute_keys(scope)
        self._keys[id(scope)] = keys
        return keys

    def _compute_keys(self, scope: Scope) -> tuple[frozenset[str], ...] | None:
        select = scope.expression
        if not isinstance(select, exp.Select):
            return None  # a UNION may repeat rows across its branches
        if _row_bound(select) == 1:
            return (frozenset(),)
        # resolve() has expanded * and replaced output aliases in GROUP BY by what they name.
        if select.args.get("distinct") is not None:
            return (frozenset(n.lower() for n in select.named_selects),)
        group = select.args.get("group")
        if group is not None:
            return _group_keys(select, group)
        return self._passed_through_keys(scope, select)

    def _passed_through_keys(
        self, scope: Scope, select: exp.Select
    ) -> tuple[frozenset[str], ...] | None:
        """The keys of the one table or CTE a SELECT reads, under its output names, when
        the SELECT neither joins nor groups: each of its rows is one row of that source."""
        if len(scope.selected_sources) != 1 or select.args.get("joins"):
            return None
        ((alias, (_node, source)),) = scope.selected_sources.items()
        if isinstance(source, exp.Table):
            table = self._references.get((source.catalog, source.db, source.name))
            inner = None if table is None else _lowered(table.keys)
        elif isinstance(source, Scope) and isinstance(source.expression, exp.Query):
            inner = self._source_keys(source)
        else:
            return None
        if inner is None:
            return None
        return _renamed_keys(select, alias, inner)

    def _sources(self, scope: Scope) -> _Sources:
        """Catalog tables and derived sources (CTEs, subqueries) by alias, with the name
        and position each table is written with."""
        tables: dict[str, Table] = {}
        derived: dict[str, Scope] = {}
        names: dict[str, str] = {}
        positions: dict[str, tuple[int | None, int | None]] = {}
        for alias, (_node, source) in scope.selected_sources.items():
            if isinstance(source, exp.Table):
                table = self._references.get((source.catalog, source.db, source.name))
                if table is not None:  # INFORMATION_SCHEMA views and table functions aren't
                    tables[alias] = table
                    names[alias] = source.name
                    # A name completed from the catalog's defaults has no position of its
                    # own; the table name inside it does.
                    where = position(source)
                    positions[alias] = where if where[0] is not None else position(source.this)
            elif isinstance(source, Scope) and isinstance(source.expression, exp.Query):
                derived[alias] = source
        return tables, derived, names, positions


def _product(
    scope: Scope,
    select: exp.Select,
    tables: Mapping[str, TableFacts],
    derived: Mapping[str, Scope],
    limited: frozenset[str],
) -> Product | None:
    """The groups of sources this SELECT joins with no equality connecting them, when
    there is more than one. Only WHERE and ON count (resolve() turns USING into ON):
    HAVING and QUALIFY run after the join. BigQuery reorders joins, so a condition written
    later still connects."""
    members: dict[str, TableFacts | DerivedSource] = dict(tables)
    for alias, source in derived.items():
        bound = _row_bound(source.expression)
        if bound is None or bound > 1:  # one row pairs with each row once
            node = scope.selected_sources[alias][0]
            members[alias] = DerivedSource(alias=alias, rows=bound, position=_written_at(node))
    if len(members) < 2:
        return None
    owners = _unnest_owners(scope, members.keys())

    def sources_of(node: exp.Expr) -> frozenset[str]:
        """The members a node reads. A column of an outer query, in a correlated subquery,
        counts as a source of its own: conditions on it relate members through it."""
        found: set[str] = set()
        for column in _local_columns(node):
            if column.table in members:
                found.add(column.table)
            elif column.table in owners:
                found |= owners[column.table]
            elif column.table not in scope.selected_sources:
                found.add(f"^{column.table}")
        return frozenset(found)

    conditions = [(c, s) for c in _join_conditions(select) if len(s := sources_of(c)) >= 2]
    equal, related = _Groups(members), _Groups(members)
    for condition, sources in conditions:
        related.connect(sources)
        if _is_equality(condition, sources_of):
            equal.connect(sources)
    order = list(scope.selected_sources)
    groups = equal.groups(order)
    if len(groups) < 2:
        return None
    group_of = {member: index for index, group in enumerate(groups) for member in group}
    # A condition between different groups, not one inside a keyed group.
    between = (
        condition
        for condition, sources in conditions
        if len({group_of[s] for s in sources if s in group_of}) > 1
    )
    inequality = next(between, None)
    return Product(
        groups=tuple(tuple(members[alias] for alias in group) for group in groups),
        inequality=(
            _shown(inequality)
            if inequality is not None and len(related.groups(order)) == 1
            else None
        ),
        position=members[groups[1][0]].position,
        limited=limited,
        flattened=frozenset(owner for found in owners.values() for owner in found),
        limit=_early_limit(select),
    )


_Pairs = dict[tuple[str, str], tuple[set[str], set[str], exp.Expr]]


def _equalities(
    select: exp.Select,
    members: set[str],
    written: Mapping[str, tuple[int | None, int | None]],
) -> tuple[_Pairs, dict[str, set[str]]]:
    """The columns that equalities in WHERE and ON match between two sources, by the pair
    of sources in written order with the first such equality, and the columns of each
    source compared with one value."""
    pairs: _Pairs = {}
    fixed: dict[str, set[str]] = {alias: set() for alias in members}
    for condition in _join_conditions(select):
        if not isinstance(condition, exp.EQ):
            continue
        left, right = _bare_column(condition.left), _bare_column(condition.right)
        if left is not None and right is not None:
            if left.table in members and right.table in members and left.table != right.table:
                a, b = sorted((left, right), key=lambda c: _sort_key(written[c.table]))
                found = pairs.setdefault((a.table, b.table), (set(), set(), condition))
                found[0].add(a.name.lower())
                found[1].add(b.name.lower())
            continue
        for column, other in ((left, condition.right), (right, condition.left)):
            if column is not None and column.table in members and _is_constant(other):
                fixed[column.table].add(column.name.lower())
    return pairs, fixed


def _key_matches(
    pairs: _Pairs, written: Mapping[str, tuple[int | None, int | None]]
) -> tuple[KeyMatch, ...]:
    return tuple(
        KeyMatch(
            left=a,
            right=b,
            left_columns=frozenset(a_columns),
            right_columns=frozenset(b_columns),
            condition=_shown(condition),
            position=written[b],
        )
        for (a, b), (a_columns, b_columns, condition) in pairs.items()
    )


def _group_keys(select: exp.Select, group: exp.Group) -> tuple[frozenset[str], ...] | None:
    """The GROUP BY columns under their output names, when every one is output."""
    if not group.expressions:
        return None  # GROUP BY ALL
    outputs: dict[exp.Expr, str] = {}
    for projection in select.expressions:
        outputs.setdefault(projection.unalias(), projection.alias_or_name.lower())
    key: set[str] = set()
    for expression in group.expressions:
        if expression not in outputs:
            # Left out of the output, rows that differ only in it repeat; ROLLUP,
            # CUBE and GROUPING SETS add rows.
            return None
        key.add(outputs[expression])
    return (frozenset(key),)


def _renamed_keys(
    select: exp.Select, alias: str, inner: tuple[frozenset[str], ...]
) -> tuple[frozenset[str], ...]:
    """The keys of the source ``alias`` that the SELECT outputs, under their output names."""
    renamed: dict[str, str] = {}
    for projection in select.expressions:
        column = projection.unalias()
        if isinstance(column, exp.Column) and column.table == alias:
            renamed.setdefault(column.name.lower(), projection.alias_or_name.lower())
    # A column WHERE holds to one value needn't be output: `(order_id, line)` with
    # `line = 1` leaves `order_id` unique.
    fixed = _fixed_columns(select, alias)
    return tuple(
        frozenset(renamed[name] for name in key - fixed)
        for key in inner
        if all(name in renamed for name in key - fixed)
    )


def _bare_column(node: exp.Expr) -> exp.Column | None:
    """The column a side of a comparison is, through parentheses; None for anything else."""
    while isinstance(node, exp.Paren):
        node = node.this
    return node if isinstance(node, exp.Column) and not isinstance(node.this, exp.Star) else None


def _lowered(keys: tuple[tuple[str, ...], ...] | None) -> tuple[frozenset[str], ...] | None:
    if keys is None:
        return None
    return tuple(frozenset(name.lower() for name in key) for key in keys)


#: Aggregates that count each input row: a row the join repeats counts again.
_REPEATED = {exp.Sum: "SUM", exp.Avg: "AVG", exp.Count: "COUNT", exp.CountIf: "COUNTIF"}


def _aggregations(select: exp.Select, members: set[str]) -> Iterator[Aggregation]:
    """SUM, AVG, COUNT and COUNTIF of one source's columns in the SELECT list and HAVING, without
    DISTINCT, that aren't themselves the function of an OVER clause. ``COUNT(*)`` counts
    the join's rows, as intended."""
    having = select.args.get("having")
    for node in [*select.expressions, *([having] if having is not None else [])]:
        for aggregate in find_all_in_scope(node, exp.AggFunc):
            function = _REPEATED.get(type(aggregate))
            if function is None or _is_window_function(aggregate):
                continue
            if isinstance(aggregate.this, exp.Distinct | exp.Star):
                continue
            owners = {column.table for column in _local_columns(aggregate)}
            if len(owners) == 1 and owners <= members:
                yield Aggregation(
                    sql=_shown(aggregate),
                    function=function,
                    source=owners.pop(),
                    position=_written_at(aggregate),
                )


def _is_window_function(aggregate: exp.AggFunc) -> bool:
    """True for the function of an OVER clause, as `SUM(x) OVER ()`, which doesn't collapse
    rows. An aggregate inside one, as `SUM(SUM(x)) OVER ()` or in its ORDER BY, does."""
    node: exp.Expr = aggregate
    while isinstance(node.parent, exp.IgnoreNulls | exp.RespectNulls):
        node = node.parent
    return isinstance(node.parent, exp.Window) and node.arg_key == "this"


def _fixed_columns(select: exp.Select, alias: str) -> frozenset[str]:
    """The columns of ``alias`` that the conditions compare with one value."""
    fixed: set[str] = set()
    for condition in _join_conditions(select):
        if not isinstance(condition, exp.EQ):
            continue
        for side, other in ((condition.left, condition.right), (condition.right, condition.left)):
            column = _bare_column(side)
            if column is not None and column.table == alias and _is_constant(other):
                fixed.add(column.name.lower())
    return frozenset(fixed)


def _join_conditions(select: exp.Select) -> list[exp.Expr]:
    """Each conjunct of WHERE and of every ON, nested joins such as
    ``JOIN (b JOIN c ON ...) ON ...`` included."""
    where = select.args.get("where")
    conditions = list(_conjuncts(where.this)) if where is not None else []
    for join in find_all_in_scope(select, exp.Join):
        conditions += _conjuncts(join.args.get("on"))
    return conditions


def _unnest_owners(scope: Scope, members: Iterable[str]) -> dict[str, frozenset[str]]:
    """Each UNNEST's alias, with the members whose arrays it reads: through another UNNEST
    too, as in ``UNNEST(i.tags)`` over ``UNNEST(e.items) AS i``. One of a literal or a
    generated array, such as GENERATE_DATE_ARRAY, belongs to none."""
    names = set(members)
    owners: dict[str, frozenset[str]] = {}
    for alias, (node, _source) in scope.selected_sources.items():  # in FROM order
        if isinstance(node, exp.Unnest):
            found: set[str] = set()
            for column in node.find_all(exp.Column):
                if column.table in names:
                    found.add(column.table)
                else:
                    found |= owners.get(column.table, frozenset())
            owners[alias] = frozenset(found)
    return owners


def _early_limit(select: exp.Select) -> int | None:
    """The SELECT's LIMIT, when nothing in it needs every row first, so BigQuery stops
    once it has that many: measured, `CROSS JOIN ... LIMIT 10` took 0.14 slot-seconds
    where the whole product took 160 (#23)."""
    limit_node = select.args.get("limit")
    value = limit_node.expression if limit_node is not None else None
    if not (isinstance(value, exp.Literal) and value.is_int):
        return None
    if any(select.args.get(k) for k in ("order", "group", "distinct", "having", "qualify")):
        return None
    if _select_aggregated(select) or any(p.find(exp.Window) for p in select.expressions):
        return None
    return int(value.this)


def _shown(condition: exp.Expr) -> str:
    """A condition as an agent would write it: qualified, without needless backticks."""
    shown = condition.copy()
    for identifier in shown.find_all(exp.Identifier):
        if _PLAIN_NAME.fullmatch(identifier.name):
            identifier.set("quoted", False)
    return shown.sql(dialect=DIALECT)


def _written_at(node: exp.Expr) -> tuple[int | None, int | None]:
    """Where a node is written: its own position, or the first one found inside it, as
    for a subquery in parentheses."""
    for inner in node.walk():
        where = position(inner)
        if where[0] is not None:
            return where
    return None, None


class _Groups:
    """Sources joined into groups by the conditions that connect them (union-find)."""

    def __init__(self, members: Iterable[str]) -> None:
        self._members = list(members)
        self._parent = {member: member for member in self._members}

    def _root(self, member: str) -> str:
        while self._parent[member] != member:
            member = self._parent[member]
        return member

    def connect(self, members: Iterable[str]) -> None:
        """Join these into one group. A name not given at the start, such as an outer
        query's source, joins groups without being one."""
        roots = [self._root(self._parent.setdefault(m, m)) for m in members]
        for root in roots[1:]:
            self._parent[root] = roots[0]

    def groups(self, order: list[str]) -> list[list[str]]:
        """The groups, each in FROM order, ordered by their first member."""
        ranked = [m for m in order if m in self._members]
        ranked += [m for m in self._members if m not in ranked]
        groups: dict[str, list[str]] = {}
        for member in ranked:
            groups.setdefault(self._root(member), []).append(member)
        return list(groups.values())


def _is_equality(condition: exp.Expr, sources_of: Callable[[exp.Expr], frozenset[str]]) -> bool:
    """True for an equality between two sources, which BigQuery joins by matching values.
    An OR of them, or an IN list of them, counts too: measured, an OR took 0.7
    slot-seconds where `<` took 155 (#23)."""
    return all(
        any(_equates(node, sources_of) for node in _conjuncts(branch))
        for branch in _operands(condition, exp.Or)
    )


def _equates(node: exp.Expr, sources_of: Callable[[exp.Expr], frozenset[str]]) -> bool:
    """True when each side reads exactly one source, a different one: with two sources on
    one side, as `a.x + b.y = c.z`, BigQuery must pair a with b first."""
    match node:
        case exp.EQ(left=left, right=right):
            sides = [sources_of(left), sources_of(right)]
        case exp.In(this=left, expressions=[_, *_] as items) if node.args.get("query") is None:
            sides = [sources_of(left), *map(sources_of, items)]
        case _:
            return False
    first, *others = sides
    return len(first) == 1 and all(len(side) == 1 and side != first for side in others)


def _row_bound(query: exp.Expr) -> int | None:
    """At most how many rows a CTE or subquery returns, when its shape says: one for an
    aggregate without GROUP BY or a SELECT without FROM, one per branch for a UNION ALL of
    those, or its LIMIT."""
    limit_node = query.args.get("limit")
    value = limit_node.expression if limit_node is not None else None
    limit = int(value.this) if isinstance(value, exp.Literal) and value.is_int else None
    shape: int | None = None
    if isinstance(query, exp.Select):
        if query.args.get("from_") is None or (
            query.args.get("group") is None and _select_aggregated(query)
        ):
            shape = 1
    elif isinstance(query, exp.Union) and not query.args.get("distinct"):
        left, right = _row_bound(query.left), _row_bound(query.right)
        if left is not None and right is not None and limit_node is None:
            shape = left + right
    bounds = [b for b in (limit, shape) if b is not None]
    return min(bounds) if bounds else None


def _sort_key(position: tuple[int | None, int | None]) -> tuple[float, float]:
    """Order positions as written; an unknown position sorts last."""
    line, column = position
    return (line if line is not None else float("inf"), column if column is not None else 0)


def _joins_in_order(select: exp.Select) -> Iterator[tuple[exp.Join, frozenset[str]]]:
    """Each join, with the sources joined before it."""
    earlier = {_from_alias(select)}
    for join in select.args.get("joins") or []:
        yield join, frozenset(earlier)
        earlier.add(join.alias_or_name)


def _conditions(select: exp.Select) -> Iterator[tuple[Clause, exp.Expr, frozenset[str] | None]]:
    """Each conjunct of WHERE, ON, HAVING and QUALIFY, with the sources it can filter
    (None: any). Only HAVING conditions on grouping columns filter: BigQuery applies them
    before grouping, so they can prune partitions."""
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
    yield from (("having", c, None if grouping else frozenset()) for c, grouping in _having(select))
    qualify = select.args.get("qualify")
    if qualify is not None:
        yield from (("qualify", c, None) for c in _conjuncts(qualify.this))


def _having(select: exp.Select) -> Iterator[tuple[exp.Expr, bool]]:
    """Each HAVING conjunct, and whether it tests only plain GROUP BY columns, without
    aggregates."""
    having, group = select.args.get("having"), select.args.get("group")
    if having is None:
        return
    keys: set[tuple[str, str]] = set()
    # Rolled-up rows add NULL keys the filter also sees, so nothing filters before them.
    if group is not None and not any(
        group.args.get(k) for k in ("rollup", "cube", "grouping_sets")
    ):
        keys = {_key(c) for c in group.expressions if isinstance(c, exp.Column)}
    for condition in _conjuncts(having.this):
        columns = _local_columns(condition)
        grouping = bool(columns) and all(_key(c) in keys for c in columns)
        yield condition, grouping and condition.find(exp.AggFunc, exp.Window) is None


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
    """True when the condition is never true for NULL, as a plain comparison isn't. A
    function such as COALESCE can turn the column's NULL into a match; one that only sees
    constants, such as ``IFNULL(@wiki, 'en')``, can't."""
    if not isinstance(condition, _NULL_REJECTING):
        return False
    return not any(node.find(exp.Column) for node in condition.find_all(*_NULL_TOLERANT))


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


def _needed_by(child: Scope) -> frozenset[str] | None:
    """The output columns of a subquery that its reader uses: none for ``EXISTS``, which
    only asks whether a row exists, so ``EXISTS (SELECT * ...)`` reads only what its own
    conditions use; all of them otherwise."""
    query = child.expression
    parent = query.parent
    while isinstance(parent, exp.Subquery):
        parent = parent.parent
    return frozenset() if isinstance(parent, exp.Exists) else None


def _within(node: exp.Expr, ancestor: exp.Expr) -> bool:
    parent = node.parent
    while parent is not None:
        if parent is ancestor:
            return True
        parent = parent.parent
    return False


def _field_chain(column: exp.Column) -> tuple[exp.Expr, tuple[str, ...]]:
    """The outermost struct field access on ``column``, as ``e.device.web_info.browser``
    on ``e.device``, and the fields it goes through."""
    node: exp.Expr = column
    names: list[str] = []
    while isinstance(node.parent, exp.Dot) and node.parent.this is node:
        field_name = node.parent.expression
        if not isinstance(field_name, exp.Identifier):
            break
        names.append(field_name.name.lower())
        node = node.parent
    return node, tuple(names)


def _output_fields(name: str, reading: _Reading) -> frozenset[tuple[str, ...]] | None:
    """The fields of output column ``name`` the reader reads; None when it reads the
    whole column."""
    fields, whole = reading.fields, reading.whole
    if fields is None or whole is None or name in whole:
        return None
    inner = frozenset(path[1:] for path in fields if path[0] == name)
    if not inner or () in inner:
        return None
    return inner


def _element_fields(
    unnest: exp.Unnest, scope: Scope, alias: str, skipped: frozenset[int]
) -> frozenset[tuple[str, ...]] | None:
    """The element fields that ``scope`` reads of ``unnest``, which it names ``alias``."""
    table_alias = unnest.args.get("alias")
    names = table_alias.columns if isinstance(table_alias, exp.TableAlias) else []
    # `UNNEST(items) AS i` names each element i; without a name, a struct element's
    # fields are read as columns.
    element = names[0].name.lower() if names else None
    offset = unnest.args.get("offset")
    position = offset.name.lower() if isinstance(offset, exp.Expr) else None
    fields: set[tuple[str, ...]] = set()
    for column in scope.columns:
        if column.table != alias or isinstance(column, exp.Pseudocolumn) or id(column) in skipped:
            continue
        name = column.name.lower()
        if name == position:
            continue  # WITH OFFSET: the element's position, not its data
        _top, chain = _field_chain(column)
        if element is None:
            fields.add((name, *chain))
        elif name == element and chain:
            fields.add(chain)
        else:
            return None  # the whole element, as in TO_JSON_STRING(i)
    if any(n.name == alias for n in find_all_in_scope(scope.expression, exp.TableColumn)):
        return None
    select = scope.expression
    for projection in select.expressions if isinstance(select, exp.Select) else []:
        # A star qualify() couldn't expand (duplicate names) reads whole elements.
        if isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column)
            and isinstance(projection.this, exp.Star)
            and projection.table == alias
        ):
            return None
    return frozenset(fields) or None


def _named_by_clauses(select: exp.Select) -> frozenset[str]:
    """Output columns that GROUP BY, ORDER BY, HAVING or QUALIFY name by alias, as in
    ``GROUP BY day`` where ``day`` is an output column."""
    aliases = {p.alias_or_name.lower() for p in select.expressions}
    named: set[str] = set()
    for key in _ALIAS_CLAUSES:
        clause = select.args.get(key)
        if clause is not None:
            named |= {
                c.name.lower()
                for c in find_all_in_scope(clause, exp.Column)
                if not c.table and c.name.lower() in aliases
            }
    return frozenset(named)


def _columns_in(projections: Iterable[exp.Expr]) -> frozenset[int]:
    return frozenset(id(n) for p in projections for n in p.find_all(exp.Column, exp.TableColumn))


def _skipped_columns(expression: exp.Expr, needed: frozenset[str] | None) -> frozenset[int]:
    """Columns only in the output columns of ``expression`` that a reader needing
    ``needed`` doesn't use."""
    if needed is None or not isinstance(expression, exp.Select) or expression.args.get("distinct"):
        return frozenset()
    return _columns_in(_unused(expression, needed, _named_by_clauses(expression)))


def _from_alias(select: exp.Select) -> str:
    from_ = select.args.get("from_")
    return from_.this.alias_or_name if isinstance(from_, exp.From) else ""


def _inherited(
    select: exp.Select, item: _Item
) -> tuple[list[tuple[Clause, exp.Expr]], frozenset[str]]:
    """The reader's filters, rewritten in terms of this SELECT's own sources, and the
    output columns read by those that can't be: they still limit what the reader gets."""
    if _limited(select) or select.args.get("qualify"):
        # The reader's filter runs on rows these clauses already picked.
        return [], _filtered_names(item.pushed)
    pushable = _pushable(select)
    # A filter can move below window functions only if it keeps or drops whole
    # partitions: every column it reads must be in every window's PARTITION BY.
    windows = [w for p in select.expressions for w in p.find_all(exp.Window)]
    keys = [
        {_key(c) for c in w.args.get("partition_by") or [] if isinstance(c, exp.Column)}
        for w in windows
    ]
    kept: list[tuple[Clause, exp.Expr]] = []
    dropped: list[_Pushed] = []
    for pushed in item.pushed:
        translated = _translate(pushed.condition, pushed.alias, pushable)
        if translated is None or any(
            not {_key(c) for c in _local_columns(translated)} <= k for k in keys
        ):
            dropped.append(pushed)
        else:
            kept.append((pushed.clause, translated))
    return kept, _filtered_names(dropped)


def _filtered_names(pushed: Iterable[_Pushed]) -> frozenset[str]:
    """The output columns these filters read."""
    return frozenset(
        column.name.lower()
        for p in pushed
        for column in p.condition.find_all(exp.Column)
        if column.table == p.alias
    )


def _producers(select: exp.Select, names: frozenset[str]) -> list[exp.Column]:
    """The columns the output columns ``names`` are computed from, including those a
    correlated subquery in them reads."""
    if not names:
        return []
    return [
        column
        for projection in select.expressions
        if projection.alias_or_name.lower() in names
        for column in projection.find_all(exp.Column)
    ]


class _Links:
    """Columns of each source whose values something other than a filter on that source
    may limit."""

    def __init__(self, sources: Iterable[str]) -> None:
        self._columns: dict[str, set[str]] = {alias: set() for alias in sources}

    @property
    def sources(self) -> Iterable[str]:
        return self._columns.keys()

    def add(self, columns: Iterable[exp.Column]) -> None:
        for column in columns:
            found = self._columns.get(column.table)
            if found is not None:
                found.add(column.name.lower())

    def of(self, alias: str) -> frozenset[str]:
        return frozenset(self._columns.get(alias, ()))


def _filtered_source(
    condition: exp.Expr,
    local: list[exp.Column],
    filtered: frozenset[str] | None,
    nullable: frozenset[str],
) -> str | None:
    """The one source the condition filters, if any. Outer joins count as limiting both
    sides: a WHERE filter on the other side can make them inner joins."""
    owners = {column.table for column in local}
    if len(owners) != 1:
        return None
    (owner,) = owners
    if filtered is not None and owner not in filtered:
        return None
    # On a side an outer join fills with NULLs, `u.id IS NULL` keeps the
    # unmatched rows rather than filtering u.
    if owner in nullable and not _null_rejecting(condition):
        return None
    return owner


def _pushable(select: exp.Select) -> dict[str, exp.Expr]:
    """The output columns a reader's filter can be rewritten through, by name."""
    group = select.args.get("group")
    group_items = list(group.expressions) if group is not None else []
    if any(isinstance(g, exp.Rollup | exp.Cube | exp.GroupingSets) for g in group_items):
        return {}  # their total rows have NULL keys and read every input row
    projections = {p.alias_or_name.lower(): p.unalias() for p in select.expressions}
    if group is None and _select_aggregated(select):
        return {}  # one row for the whole input; a window aggregate keeps every row
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


def _read_columns(
    scope: Scope, select: exp.Select, needed: frozenset[str] | None, named: frozenset[str]
) -> _Reads:
    columns = [c for c in scope.columns if not isinstance(c, exp.Pseudocolumn)]
    distinct = select.args.get("distinct")
    unused = [] if needed is None or distinct else _unused(select, needed, named)
    skipped = _columns_in(unused)
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


def _unused(select: exp.Select, needed: frozenset[str], named: frozenset[str]) -> list[exp.Expr]:
    """Output columns the reader doesn't use, and no clause of the SELECT names."""
    used = needed | named
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


def _correlated(
    condition: exp.Expr, sources: Iterable[str], local: list[exp.Column]
) -> Iterator[exp.Column]:
    """Columns of these sources that a subquery in the condition reads, as ``p.wiki`` in
    ``EXISTS (SELECT 1 FROM wikis w WHERE w.wiki = p.wiki)``. ``local`` holds the
    condition's own columns."""
    if condition.find(exp.Query) is None:
        return
    aliases = set(sources)
    own = {id(column) for column in local}
    for column in condition.find_all(exp.Column):
        if id(column) in own or column.table not in aliases:
            continue
        node = column.parent
        while node is not None and node is not condition:  # a nearer source of that name?
            if isinstance(node, exp.Select) and column.table in _source_aliases(node):
                break
            node = node.parent
        else:
            yield column


def _source_aliases(select: exp.Select) -> set[str]:
    return {_from_alias(select)} | {join.alias_or_name for join in select.args.get("joins") or []}


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
