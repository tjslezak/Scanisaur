"""Resolve every table and column a statement names against the catalog (SCN001).

Tables are looked up first, because sqlglot's ``qualify()`` silently accepts unknown
tables. ``qualify()`` then expands stars and attaches each column it can to a source,
with its own validation off; every column is checked here instead, so that all
unknown or ambiguous columns are reported at once, each in terms the agent wrote.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, find_all_in_scope, traverse_scope

from scanisaur.catalog.model import PARTITIONDATE, PARTITIONTIME, TABLE_SUFFIX, Catalog, Table
from scanisaur.engine.parse import position
from scanisaur.engine.result import Finding, Severity
from scanisaur.engine.rules import UNKNOWN_IDENTIFIER

_PSEUDO_COLUMNS = frozenset({TABLE_SUFFIX, PARTITIONTIME, PARTITIONDATE})
#: qualify() raises this, even with validation off, when a USING column isn't on both sides.
_USING_ERROR = re.compile(r"Cannot automatically join: (\S+)")

#: Suggestions must be this similar to the unknown name, and within the band of the best.
_MIN_SIMILARITY = 0.6
_SIMILARITY_BAND = 0.08
_MAX_SUGGESTIONS = 3
#: How many of a CTE's columns a fix lists before eliding the rest.
_MAX_LISTED = 8
#: Fixes that point the agent at the schema tools when nothing else fits.
_DESCRIBE = "List the columns with scanisaur_schema_describe."
_SEARCH = "Find the table with scanisaur_schema_search."

_Key = tuple[str, str, str]


class ResolveError(Exception):
    """qualify() failed for a reason that isn't an unknown name."""


@dataclass(frozen=True, slots=True)
class Resolution:
    #: The statement qualified against the catalog, with stars expanded. None when a
    #: table is unknown, or for INFORMATION_SCHEMA queries, whose columns aren't checked.
    qualified: exp.Expr | None
    #: Catalog tables the statement reads, each listed once, in the order written.
    tables: tuple[Table, ...]
    findings: tuple[Finding, ...]


@dataclass(frozen=True, slots=True)
class _Source:
    """Something a scope selects from: a catalog table, a CTE, a subquery or UNNEST."""

    #: How findings name it: a catalog table's full name, a CTE or subquery name in
    #: backticks, or "the subquery" when the query didn't name it.
    shown: str
    #: Its column names; None when they can't be known, as for UNNEST.
    columns: tuple[str, ...] | None
    pseudo_columns: frozenset[str] = frozenset()
    in_catalog: bool = False

    def has(self, name: str) -> bool:
        lowered = name.lower()
        return self.columns is not None and any(c.lower() == lowered for c in self.columns)


#: The sources a column can name: its own scope's first, then each enclosing scope a
#: correlated subquery can see.
_Visible = list[dict[str, _Source]]


def resolve(tree: exp.Expr, catalog: Catalog, dialect: str) -> Resolution:
    tree = tree.copy()  # table names are completed in place below
    try:
        references = [node for scope in traverse_scope(tree) for node in _table_nodes(scope)]
    except OptimizeError as error:
        raise ResolveError(str(error)) from error
    references.sort(key=_position_key)

    by_reference: dict[_Key, Table] = {}
    findings: list[Finding] = []
    metadata_query = False
    for node in references:
        if node.name.upper().startswith("INFORMATION_SCHEMA."):
            metadata_query = True
            continue
        table = catalog.find(node.name, node.db or None, node.catalog or None)
        if table is None:
            findings.append(_unknown_table(node, catalog))
        else:
            by_reference[(table.project, table.dataset, node.name)] = table
            _complete_name(node, table)
    tables = tuple({table.qualified_name: table for table in by_reference.values()}.values())
    if findings or metadata_query:
        return Resolution(None, tables, _unique(findings))

    try:
        qualified = qualify(
            tree,
            schema=_schema(by_reference),
            dialect=dialect,
            validate_qualify_columns=False,
            allow_partial_qualification=True,
        )
    except OptimizeError as error:
        finding = _using_error(error, tree, tables)
        if finding is None:
            raise ResolveError(str(error)) from error
        return Resolution(None, tables, (finding,))
    return Resolution(qualified, tables, _unique(_column_findings(qualified, by_reference)))


def _table_nodes(scope: Scope) -> Iterator[exp.Table]:
    """Physical tables a scope reads; CTE references and UNNEST are not tables."""
    for _node, source in scope.selected_sources.values():
        if isinstance(source, exp.Table):
            yield source


def _complete_name(node: exp.Table, table: Table) -> None:
    """Write the project and dataset the defaults filled in.

    qualify() lowercases table names without a dataset, taking them for possible CTE
    names; a complete name keeps its case, as BigQuery table names are case-sensitive.
    """
    if not node.db:
        node.set("db", exp.to_identifier(table.dataset))
    if not node.catalog:
        node.set("catalog", exp.to_identifier(table.project))


def _schema(by_reference: dict[_Key, Table]) -> dict[str, object]:
    """A sqlglot schema for the referenced tables only, keyed by the names as written."""
    schema: dict[str, dict[str, dict[str, dict[str, str]]]] = {}
    for (project, dataset, name), table in by_reference.items():
        columns = {column.name: column.type for column in table.columns}
        schema.setdefault(project, {}).setdefault(dataset, {})[name] = columns
    return dict(schema)


def _column_findings(qualified: exp.Expr, by_reference: dict[_Key, Table]) -> Iterator[Finding]:
    for scope in traverse_scope(qualified):
        if isinstance(scope.expression, exp.SetOperation):
            continue  # each branch of a UNION is a scope of its own
        visible = _visible(scope, by_reference)
        for column in _own_columns(scope):
            if column.name.upper() in _PSEUDO_COLUMNS:
                continue
            if column.table:
                finding = _check_qualified(column, visible)
            else:
                finding = _check_unqualified(column, visible)
            if finding is not None:
                yield finding
        yield from _pseudo_columns(scope, visible)


def _visible(scope: Scope, by_reference: dict[_Key, Table]) -> _Visible:
    levels: _Visible = []
    current: Scope | None = scope
    while current is not None:
        levels.append(_sources(current, by_reference))
        current = _outer(current)
    return levels


def _sources(scope: Scope, by_reference: dict[_Key, Table]) -> dict[str, _Source]:
    sources: dict[str, _Source] = {}
    for alias, (node, source) in scope.selected_sources.items():
        if isinstance(source, exp.Table):
            table = by_reference.get((source.catalog, source.db, source.name))
            if table is None:  # every table was resolved before qualify(); a bug if not
                raise ResolveError(f"table `{source.sql()}` lost its catalog entry")
            names = tuple(column.name for column in table.columns)
            shown = f"`{table.qualified_name}`"
            sources[alias] = _Source(shown, names, table.pseudo_columns, in_catalog=True)
        elif isinstance(source, Scope) and isinstance(source.expression, exp.Query):
            selects = tuple(source.expression.named_selects)
            columns = None if "*" in selects else selects
            sources[alias] = _Source(_derived_name(node, alias), columns)
        else:
            sources[alias] = _Source(f"`{alias}`", None)
    return sources


def _derived_name(node: exp.Expr, alias: str) -> str:
    if isinstance(node, exp.Table):
        return f"`{node.name}`"  # a CTE, by its name rather than its alias
    # A derived table's node is its query; the alias is on the enclosing Subquery.
    subquery = node if isinstance(node, exp.Subquery) else node.parent
    written = subquery.args.get("alias") if isinstance(subquery, exp.Subquery) else None
    # qualify() names unnamed subqueries; a name it made up has no position in the SQL.
    identifier = written.this if isinstance(written, exp.TableAlias) else None
    if isinstance(identifier, exp.Identifier) and identifier.meta:
        return f"`{alias}`"
    return "the subquery"


def _outer(scope: Scope) -> Scope | None:
    """The enclosing scope whose sources a correlated reference in ``scope`` can name."""
    if not scope.can_be_correlated:
        return None
    inner, outer = scope, scope.parent
    while outer is not None and isinstance(outer.expression, exp.SetOperation):
        inner, outer = outer, outer.parent  # a UNION's branches see what the UNION sees
    return None if inner.is_cte else outer


def _own_columns(scope: Scope) -> list[exp.Column]:
    """The scope's columns, without the correlated ones sqlglot adds from its subqueries."""
    children = (*scope.subquery_scopes, *scope.udtf_scopes, *scope.derived_table_scopes)
    inherited = {id(column) for child in children for column in child.external_columns}
    return [column for column in scope.columns if id(column) not in inherited]


def _check_qualified(column: exp.Column, visible: _Visible) -> Finding | None:
    alias, name = column.table, column.name
    source = next((level[alias] for level in visible if alias in level), None)
    if source is None:
        known = sorted({known for level in visible for known in level})
        return _finding(
            f"`{alias}.{name}` uses `{alias}`, which isn't a table or alias in this query.",
            _suggest(_closest(alias, known)) or f"Use one of: {_listing(known)}.",
            column,
        )
    if source.columns is None or source.has(name):
        return None
    # Another source having the column usually means the wrong alias, not a typo.
    elsewhere = [f"{a}.{name}" for level in visible for a, s in level.items() if s.has(name)]
    similar = [f"{alias}.{match}" for match in _closest(name, source.columns)]
    return _finding(
        f"Column `{alias}.{name}` does not exist in {source.shown}.",
        _suggest(elsewhere or similar) or _no_match([source]),
        column,
    )


def _check_unqualified(column: exp.Column, visible: _Visible) -> Finding | None:
    name = column.name
    for level in visible:
        if any(source.columns is None for source in level.values()):
            return None  # it may come from a source whose columns can't be known
        owners = [alias for alias, source in level.items() if source.has(name)]
        if len(owners) == 1:
            return None
        if owners:
            return _finding(
                f"Column `{name}` is ambiguous: it exists in {_listing(owners)}.",
                f"Qualify it, for example `{owners[0]}.{name}`.",
                column,
            )
    nearest = list(visible[0].values()) if visible else []
    candidates = [c for level in visible for s in level.values() for c in s.columns or ()]
    return _finding(
        f"Column `{name}` does not exist in {_where(nearest)}.",
        _suggest(_closest(name, candidates)) or _no_match(nearest),
        column,
    )


def _pseudo_columns(scope: Scope, visible: _Visible) -> Iterator[Finding]:
    """Attribute pseudo-columns to the one table that has them, or report them.

    qualify() turns them into ``Pseudocolumn`` nodes and leaves them out of
    ``scope.columns``, so they are found by node type.
    """
    for column in find_all_in_scope(scope.expression, exp.Pseudocolumn):
        name = column.name.upper()
        if name not in _PSEUDO_COLUMNS or column.table:
            continue
        owners = next(
            (
                owners
                for level in visible
                if (owners := [a for a, s in level.items() if name in s.pseudo_columns])
            ),
            [],
        )
        if len(owners) == 1:
            column.set("table", exp.to_identifier(owners[0]))
        elif owners:
            yield _finding(
                f"`{name}` is ambiguous: it exists on {_listing(owners)}.",
                f"Qualify it, for example `{owners[0]}.{name}`.",
                column,
            )
        elif name == TABLE_SUFFIX:
            yield _finding(
                f"`{name}` only exists on wildcard tables such as `events_*`, "
                "and this query reads none.",
                "Query the wildcard table, or remove the filter.",
                column,
            )
        else:
            yield _finding(
                f"`{name}` only exists on ingestion-time partitioned tables, "
                "and this query reads none.",
                "Filter on the table's partition column instead.",
                column,
            )


def _using_error(error: OptimizeError, tree: exp.Expr, tables: tuple[Table, ...]) -> Finding | None:
    match = _USING_ERROR.search(str(error))
    if match is None:
        return None
    name = match.group(1)
    node = next(
        (
            identifier
            for join in tree.find_all(exp.Join)
            for identifier in join.args.get("using") or ()
            if identifier.name.lower() == name.lower()
        ),
        None,
    )
    if node is None:
        return None
    owners = [f"`{table.qualified_name}`" for table in tables if table.column(name) is not None]
    if owners:
        fix = f"Only {', '.join(owners)} has `{name}`; join with ON on the matching columns."
    else:
        candidates = [column.name for table in tables for column in table.columns]
        fix = _suggest(_closest(name, candidates)) or _DESCRIBE
    return _finding(f"`USING ({name})` needs `{name}` on both sides of the join.", fix, node)


def _unknown_table(node: exp.Table, catalog: Catalog) -> Finding:
    written = ".".join(part.name for part in node.parts)
    parts = len(node.parts)
    if parts < _parts_needed(catalog):
        fix = "Qualify the table as `project.dataset.table`."
    else:
        candidates = [_reference(table, catalog, parts) for table in catalog.tables]
        fix = _suggest(_closest(written, candidates)) or _SEARCH
    return _finding(f"Table `{written}` does not exist.", fix, node)


def _reference(table: Table, catalog: Catalog, parts: int) -> str:
    """How to write ``table`` with at least ``parts`` parts, and enough to resolve it."""
    forms = (table.name, f"{table.dataset}.{table.name}", table.qualified_name)
    return forms[max(parts, _parts_needed(catalog, table)) - 1]


def _parts_needed(catalog: Catalog, table: Table | None = None) -> int:
    """The fewest name parts that resolve ``table``, or that can resolve any table."""
    project, dataset = catalog.default_project, catalog.default_dataset
    if project is None or (table is not None and table.project != project):
        return 3
    if dataset is None or (table is not None and table.dataset != dataset):
        return 2
    return 1


def _finding(message: str, fix: str, node: exp.Expr) -> Finding:
    line, column = position(node)
    return Finding(
        rule=UNKNOWN_IDENTIFIER,
        severity=Severity.BLOCK,
        message=message,
        fix=fix,
        line=line,
        column=column,
    )


def _closest(name: str, candidates: Iterable[str]) -> list[str]:
    """The names closest to ``name``: only those nearly as close as the best, at most three."""
    by_lower = {candidate.lower(): candidate for candidate in candidates}
    target = name.lower()
    scored = sorted(
        ((difflib.SequenceMatcher(None, target, c).ratio(), c) for c in by_lower),
        key=lambda pair: (-pair[0], pair[1]),
    )
    if not scored or scored[0][0] < _MIN_SIMILARITY:
        # `id` for `user_id`: the name is a whole word of a longer one.
        words = [c for c in sorted(by_lower) if target in c.split("_")]
        return [by_lower[c] for c in words[:_MAX_SUGGESTIONS]]
    best = scored[0][0]
    close = [c for ratio, c in scored if ratio >= best - _SIMILARITY_BAND]
    return [by_lower[c] for c in close[:_MAX_SUGGESTIONS]]


def _suggest(names: Iterable[str]) -> str | None:
    shown = [f"`{name}`" for name in names]
    return "Did you mean " + " or ".join(shown) + "?" if shown else None


def _no_match(sources: Iterable[_Source]) -> str:
    """The fix when nothing is close: where the agent can find the real column names."""
    hints = []
    for source in sources:
        if source.in_catalog or source.columns is None:
            continue
        more = ", …" if len(source.columns) > _MAX_LISTED else ""
        hints.append(f"Columns of {source.shown}: {_listing(source.columns[:_MAX_LISTED])}{more}.")
    return " ".join(hints) or _DESCRIBE


def _where(sources: list[_Source]) -> str:
    names = [source.shown for source in sources]
    if not names:
        return "this query"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " or " + names[-1]


def _listing(names: Iterable[str]) -> str:
    return ", ".join(f"`{name}`" for name in names)


def _position_key(item: exp.Expr | Finding) -> tuple[int, int]:
    line, column = position(item) if isinstance(item, exp.Expr) else (item.line, item.column)
    return line or 0, column or 0


def _unique(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """One finding per problem, in the order written; a repeated unknown name is reported once."""
    seen: dict[tuple[str, str], Finding] = {}
    for finding in sorted(findings, key=_position_key):
        seen.setdefault((finding.rule, finding.message), finding)
    return tuple(seen.values())
