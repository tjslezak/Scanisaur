"""SCN003, SCN004 and SCN011: filters that let BigQuery skip partitions, shards and blocks.

Which conditions prune was measured with dry runs on public tables (``docs/rules/``).
BigQuery prunes through ranges, ``IN`` lists, ``IS NULL`` and ``OR``s of them, and through
``DATE()``, ``TIMESTAMP_TRUNC``, ``DATE_TRUNC``, ``CAST(... AS DATE)``,
``EXTRACT(DATE|YEAR ...)``, date arithmetic and ``FORMAT_TIMESTAMP``. Only the functions
measured to defeat pruning are flagged; one that hasn't been measured is assumed to
prune, so the rules don't warn on a guess.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterator
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from scanisaur.catalog.model import PARTITIONDATE, PARTITIONTIME, TABLE_SUFFIX, Table
from scanisaur.engine.facts import Predicate, QueryFacts, TableFacts
from scanisaur.engine.parse import DIALECT
from scanisaur.engine.result import Finding, Severity
from scanisaur.engine.rules import CLUSTER_PREFIX, PARTITION_FILTER, PRUNING_DEFEATED

#: Returns the name of a function that stops pruning, given a node and its column's type.
Defeating = Callable[[exp.Expr, str], str | None]
#: A table's filter conditions, each parsed once.
Parsed = list[tuple[Predicate, exp.Expr]]
#: Which LIKE patterns limit what is read: none, any constant, or only a fixed prefix.
LikeMode = Literal["none", "any", "prefix"]

#: Comparisons whose ``usable`` facts BigQuery can prune with (used for hints).
_PRUNING_OPS = frozenset({"=", "<", "<=", ">", ">=", "between", "in"})
_COMPARISONS = (exp.EQ, exp.LT, exp.LTE, exp.GT, exp.GTE)
#: Reading all of a smaller table costs little, so its findings are info, not warnings.
_SMALL_TABLE_BYTES = 1 << 30
#: EXTRACT parts measured to stop pruning. DATE and YEAR still prune.
_EXTRACT_DEFEATS = frozenset({"DAY", "DAYOFWEEK", "DAYOFYEAR", "WEEK", "MONTH", "QUARTER"})
#: Column types a cast to STRING was measured to defeat pruning on.
_STRING_CAST_DEFEATS = frozenset({"DATE", "TIMESTAMP"})
#: Functions measured to stop clustering from skipping blocks. SUBSTR and LIKE still prune.
_CLUSTER_DEFEATS: dict[type[exp.Expr], str] = {
    exp.Lower: "LOWER",
    exp.Upper: "UPPER",
    exp.Trim: "TRIM",
}
_EXAMPLES = {
    "DATE": "{col} >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)",
    "DATETIME": "{col} >= DATETIME_SUB(CURRENT_DATETIME(), INTERVAL 7 DAY)",
    "TIMESTAMP": "{col} >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)",
}
_SUFFIX_EXAMPLE = "_TABLE_SUFFIX >= FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY))"
_PSEUDO_COLUMNS = {name.lower(): name for name in (TABLE_SUFFIX, PARTITIONTIME, PARTITIONDATE)}
_SIMPLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FULL_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def pruning_findings(facts: QueryFacts) -> list[Finding]:
    """SCN003, SCN004 and SCN011 findings for every table the query reads.

    A problem found at several references, such as two unfiltered UNION branches, is
    reported once, at the first.
    """
    candidates = [finding for table in facts.tables for finding in _table_findings(table)]
    candidates.sort(key=lambda f: (f.line or 0, f.column or 0, f.rule))
    unique: dict[tuple[str, str], Finding] = {}
    for finding in candidates:
        unique.setdefault((finding.rule, finding.message), finding)
    return list(unique.values())


def parsed_conditions(facts: TableFacts) -> Parsed:
    """The table's filter conditions, each parsed once; unparsable ones are left out."""
    return [(p, tree) for p in facts.predicates if (tree := _parse(p.sql)) is not None]


def partition_conditions(facts: TableFacts, parsed: Parsed) -> list[exp.Expr]:
    """Every condition on the partition column, its pseudo-columns, or a wildcard's
    ``_TABLE_SUFFIX``, whether or not it can prune; QUALIFY runs too late for any."""
    table = facts.table
    if table.is_wildcard:
        return [tree for _p, tree in _conditions(parsed, frozenset({TABLE_SUFFIX.lower()}))]
    if table.partitioning is None:
        return []
    return [tree for _p, tree in _conditions(parsed, partition_names(table))]


def defeats_pruning(tree: exp.Expr, table: Table) -> bool:
    """True when the condition wraps the partition column in a function measured to stop
    BigQuery from skipping partitions, such as ``CAST(day AS STRING)``."""
    names = partition_names(table)
    return _first_defeating(tree, names, table, _partition_defeating) is not None


def skips_blocks(facts: TableFacts, parsed: Parsed) -> bool:
    """True when a filter on a cluster column may let BigQuery skip clustered blocks."""
    table = facts.table
    for name in table.clustering:
        names = frozenset({name.lower()})
        if _filtered(_conditions(parsed, names), names, table, _cluster_defeating):
            return True
    return False


def _table_findings(facts: TableFacts) -> Iterator[Finding]:
    table = facts.table
    parsed = parsed_conditions(facts)
    if table.is_wildcard:
        yield from _shards(facts, parsed)
    elif table.partitioning is not None:
        yield from _partitions(facts, parsed)
    yield from _clustering(facts, parsed)
    yield from _cluster_prefix(facts, parsed)


def _partitions(facts: TableFacts, parsed: Parsed) -> Iterator[Finding]:
    table = facts.table
    partitioning = table.partitioning
    assert partitioning is not None
    required = partitioning.required
    if not required and not _reads_data(facts):
        return  # e.g. COUNT(*): BigQuery answers from metadata and reads nothing
    names = partition_names(table)
    relevant = _conditions(parsed, names)
    if any(_limits(tree, names, table, "none", _partition_defeating) for _p, tree in relevant):
        return
    defeated = next(
        (
            (predicate, tree, function)
            for predicate, tree in relevant
            if (function := _defeated_by(tree, names, table)) is not None
        ),
        None,
    )
    subject = "the query"
    if defeated is not None:
        predicate, tree, function = defeated
        shown = _shown_partition_column(table, predicate)
        message = (
            f"`{_render(tree)}` wraps the partition column `{shown}` in {function}, so BigQuery "
            f"can't use it to skip partitions of `{table.qualified_name}`"
        )
        rule, fix = PRUNING_DEFEATED, _rewrite_fix(tree, predicate, shown, table)
    else:
        if relevant:
            predicate, tree = relevant[0]
            shown = _shown_partition_column(table, predicate)
            problem = _why_not(tree, shown, "partitions")
        else:
            shown = _shown_partition_column(table, None)
            problem, subject = "the query doesn't filter on it", "it"
        message = f"`{table.qualified_name}` is partitioned by `{shown}`, but {problem}"
        rule, fix = PARTITION_FILTER, f"Add a filter on `{shown}`, {_example(table, shown)}."
    if required:
        message += (
            f". BigQuery rejects queries on this table unless a filter on `{shown}` "
            "can limit the partitions."
        )
        severity = Severity.BLOCK
    else:
        joiner = "; the query" if defeated is not None else f", so {subject}"
        message += f"{joiner} reads every partition{_size(table)}.{_hint(facts, names)}"
        severity = _severity(table)
    line, column = facts.position
    yield Finding(rule=rule, severity=severity, message=message, fix=fix, line=line, column=column)


def _shards(facts: TableFacts, parsed: Parsed) -> Iterator[Finding]:
    table = facts.table
    if facts.name and len(facts.name) > len(table.name):
        return  # a narrower wildcard such as events_2026* already limits the shards
    if not _reads_data(facts):
        return  # e.g. DISTINCT _TABLE_SUFFIX reads only shard names
    names = frozenset({TABLE_SUFFIX.lower()})
    relevant = _conditions(parsed, names)
    # BigQuery checks any constant filter on _TABLE_SUFFIX against the shard names, wrapped
    # or not: PARSE_DATE, CAST, SUBSTR and LIKE were measured.
    if any(_limits(tree, names, table, "any", None) for _p, tree in relevant):
        return
    if relevant:
        problem, subject = _why_not(relevant[0][1], TABLE_SUFFIX, "shards"), "the query"
    else:
        problem, subject = "the query doesn't filter on `_TABLE_SUFFIX`", "it"
    hint = _hint(facts, names, "shards")
    line, column = facts.position
    yield Finding(
        rule=PARTITION_FILTER,
        severity=_severity(table),
        message=(
            f"`{table.qualified_name}` matches every shard, but {problem}, "
            f"so {subject} reads all of them{_size(table)}.{hint}"
        ),
        fix=(
            f"Add a filter on `_TABLE_SUFFIX`, for example `{_SUFFIX_EXAMPLE}`, "
            "or name fewer shards with a longer prefix."
        ),
        line=line,
        column=column,
    )


def _clustering(facts: TableFacts, parsed: Parsed) -> Iterator[Finding]:
    table = facts.table
    for name in table.clustering:
        names = frozenset({name.lower()})
        relevant = _conditions(parsed, names)
        if _filtered(relevant, names, table, _cluster_defeating):
            continue  # a filter on the column already uses the clustering
        for _predicate, tree in relevant:
            if not _limits(tree, names, table, "prefix", None):
                continue
            function = _first_defeating(tree, names, table, _cluster_defeating)
            if function is None:
                continue
            line, position = facts.position
            yield Finding(
                rule=PRUNING_DEFEATED,
                severity=_severity(table),
                message=(
                    f"`{_render(tree)}` wraps the cluster column `{name}` in {function}, so "
                    f"BigQuery can't use the clustering of `{table.qualified_name}` to skip blocks."
                ),
                fix=(
                    f"Compare `{name}` itself, for example "
                    f"`{_unwrap(tree, tuple(_CLUSTER_DEFEATS))}`, "
                    "if the stored values are written that way."
                ),
                line=line,
                column=position,
            )
            break


def _cluster_prefix(facts: TableFacts, parsed: Parsed) -> Iterator[Finding]:
    """SCN011: a filter on a later cluster column with none on the leading one.

    BigQuery sorts clustered data by the cluster columns in order, so without the leading
    column a filter on a later one may skip far fewer blocks: one pageviews day read 6.68 GB
    with `title` alone against 810 MB with `wiki` too. Both measured cases also changed the
    answer, because the result then covered every value of the leading column.
    """
    table = facts.table
    if len(table.clustering) < 2:
        return
    leading = table.clustering[0]
    lead = frozenset({leading.lower()})
    partition = table.partitioning.column if table.partitioning is not None else None
    if partition is not None and leading.lower() == partition.lower():
        return  # SCN003 asks for the partition filter
    # A wrapped filter on the leading column counts: SCN004 reports the function.
    if _filtered(_conditions(parsed, lead), lead, table, None):
        return
    # A join, a subquery, QUALIFY or an unmeasured shape such as `wiki IN UNNEST(@wikis)`
    # may still pick values of the leading column, and whether BigQuery then skips blocks
    # isn't known. Only exclusions such as `wiki != 'commons'` surely keep nearly every block.
    if lead & facts.linked:
        return
    on_leading = _conditions(parsed, lead, qualify=True)
    if on_leading and _column_type(table, leading.lower()) == "BOOL":
        return  # `active != TRUE` picks the one other value
    if not all(_excludes(tree, lead) for _p, tree in on_leading):
        return
    for name in table.clustering[1:]:
        if partition is not None and name.lower() == partition.lower():
            continue  # a filter on it is the partition filter, not a question about one value
        names = frozenset({name.lower()})
        if not any(_pins(tree, names) for _p, tree in _conditions(parsed, names)):
            continue
        order = ", then ".join(f"`{column}`" for column in table.clustering)
        line, column = facts.position
        yield Finding(
            rule=CLUSTER_PREFIX,
            severity=_severity(table),
            message=(
                f"`{table.qualified_name}` is clustered by {order}. The query filters "
                f"`{name}` but doesn't limit `{leading}` to particular values, so BigQuery may "
                f"skip far fewer blocks than it would with a `{leading}` filter as well."
            ),
            fix=(
                f"If the question is about particular `{leading}` values, "
                "filter on them with `=` or `IN`."
            ),
            line=line,
            column=column,
        )
        return


def _pins(node: exp.Expr, names: frozenset[str]) -> bool:
    """True when the condition confines the column to particular values with `=` or `IN`,
    the filters measured for SCN011. A range on a later cluster column is usually a time
    window over every value of the leading one, so it isn't reported, and neither is a
    function of the column such as `DATE(ts) = ...`, which is a range too. LOWER, UPPER and
    TRIM keep particular values; SCN004 reports them."""
    node = node.unnest()
    if isinstance(node, exp.Or):
        return all(_pins(child, names) for child in node.flatten())
    if isinstance(node, exp.And):
        return any(_pins(child, names) for child in node.flatten())
    if isinstance(node, exp.EQ):
        pairs = ((node.left, node.right), (node.right, node.left))
        return any(_plain(side, names) and is_constant(other) for side, other in pairs)
    if isinstance(node, exp.In):
        values = in_values(node)
        return bool(values) and all(map(is_constant, values)) and _plain(node.this, names)
    return False


def _plain(side: exp.Expr, names: frozenset[str]) -> bool:
    """The column itself, or the column under LOWER, UPPER or TRIM."""
    side = side.unnest()
    while isinstance(side, tuple(_CLUSTER_DEFEATS)):
        side = side.this.unnest()
    return isinstance(side, exp.Column) and _named(side, names)


def _excludes(node: exp.Expr, names: frozenset[str]) -> bool:
    """True when the condition only rules values out, as `wiki != 'commons'`,
    `wiki NOT IN (...)`, `wiki NOT LIKE ...` and `wiki IS NOT NULL` do."""
    node = node.unnest()
    if isinstance(node, exp.NEQ):
        for side, other in ((node.left, node.right), (node.right, node.left)):
            if _only(side, names) and (is_constant(other) or isinstance(other, exp.Subquery)):
                return True
        return False
    if isinstance(node, exp.Like) and node.args.get("negate"):  # how sqlglot holds NOT LIKE
        return _only(node.this, names)
    tested = node.this.unnest() if isinstance(node, exp.Not) else None
    if isinstance(tested, exp.Like) and tested.args.get("negate"):
        return False  # NOT (x NOT LIKE ...) is a LIKE
    if isinstance(tested, exp.In | exp.Like) or (
        isinstance(tested, exp.Is) and isinstance(tested.expression, exp.Null)
    ):
        return _only(tested.this, names)
    return False


def _conditions(parsed: Parsed, names: frozenset[str], *, qualify: bool = False) -> Parsed:
    """The conditions that name ``names`` outside a subquery. A fact records only the
    first column a condition names, so the condition itself is searched. QUALIFY runs
    too late to prune, so its conditions are left out unless ``qualify`` is set."""
    return [
        (predicate, tree)
        for predicate, tree in parsed
        if (qualify or predicate.clause != "qualify")
        and any(
            _named(column, names) and column.find_ancestor(exp.Query) is None
            for column in tree.find_all(exp.Column)
        )
    ]


def _filtered(
    conditions: Parsed, names: frozenset[str], table: Table, defeating: Defeating | None
) -> bool:
    """True when a condition confines the column to values, ranges or a prefix that
    BigQuery can skip clustered blocks with."""
    return any(_limits(tree, names, table, "prefix", defeating) for _p, tree in conditions)


def _limits(
    node: exp.Expr,
    names: frozenset[str],
    table: Table,
    like: LikeMode,
    defeating: Defeating | None,
) -> bool:
    """True when the condition confines the columns in ``names`` to constant values or
    ranges that BigQuery can skip data with. ``defeating`` names the functions that stop
    it; None means no function does."""
    node = node.unnest()
    if isinstance(node, exp.Or):  # every branch must be limited
        return all(_limits(child, names, table, like, defeating) for child in node.flatten())
    if isinstance(node, exp.And):  # one limited conjunct is enough
        return any(_limits(child, names, table, like, defeating) for child in node.flatten())
    side: exp.Expr | None = None
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
        side = node.this  # reads only the NULL partition, or the streaming buffer
    elif isinstance(node, _COMPARISONS):
        if is_constant(node.right):
            side = node.left
        elif is_constant(node.left):
            side = node.right
    elif isinstance(node, exp.Between):
        if is_constant(node.args["low"]) and is_constant(node.args["high"]):
            side = node.this
    elif isinstance(node, exp.In):
        values = in_values(node)
        if values and all(is_constant(value) for value in values):
            side = node.this
    elif isinstance(node, exp.Like) and _like_limits(node, like):
        side = node.this
    if not isinstance(side, exp.Expr) or not _only(side, names):
        return False
    return defeating is None or _first_defeating(side, names, table, defeating) is None


def _defeated_by(tree: exp.Expr, names: frozenset[str], table: Table) -> str | None:
    """The function stopping a filter that would otherwise limit the partitions."""
    if not _limits(tree, names, table, "any", None):
        return None
    return _first_defeating(tree, names, table, _partition_defeating)


def _first_defeating(
    tree: exp.Expr, names: frozenset[str], table: Table, defeating: Defeating
) -> str | None:
    """The innermost defeating function around one of ``names``."""
    for column in tree.find_all(exp.Column):
        if not _named(column, names):
            continue
        column_type = _column_type(table, column.name.lower())
        node = column.parent
        while node is not None and node is not tree.parent:
            name = defeating(node, column_type)
            if name is not None:
                return name
            node = node.parent
    return None


def _partition_defeating(node: exp.Expr, column_type: str) -> str | None:
    if isinstance(node, exp.Extract):
        unit = node.this
        if isinstance(unit, exp.WeekStart):  # EXTRACT(WEEK(MONDAY) FROM ...)
            part, shown = "WEEK", f"WEEK({unit.name.upper()})"
        else:
            part = shown = unit.name.upper()
        return f"EXTRACT({shown} FROM ...)" if part in _EXTRACT_DEFEATS else None
    if isinstance(node, exp.Cast) and node.to.is_type(*exp.DataType.TEXT_TYPES):
        if column_type in _STRING_CAST_DEFEATS:
            return f"{'SAFE_CAST' if isinstance(node, exp.TryCast) else 'CAST'}(... AS STRING)"
        return None
    if isinstance(node, exp.TimeToStr) and column_type == "DATE":
        return "FORMAT_DATE"  # FORMAT_TIMESTAMP on a TIMESTAMP column still prunes
    if column_type == "TIMESTAMP":
        if isinstance(node, exp.UnixSeconds | exp.UnixMillis):
            return node.sql_name()
        if isinstance(node, exp.TsOrDsToDatetime):
            return "DATETIME"
    return None


def _cluster_defeating(node: exp.Expr, _column_type: str) -> str | None:
    return _CLUSTER_DEFEATS.get(type(node))


def _only(side: exp.Expr, names: frozenset[str]) -> bool:
    """True when ``side`` reads only the columns in ``names``: `COALESCE(day, other) > x`
    depends on another column, so it can't prune."""
    columns = list(side.find_all(exp.Column))
    if not columns or side.find(exp.Query) is not None:
        return False
    return all(_named(column, names) for column in columns)


def _named(column: exp.Column, names: frozenset[str]) -> bool:
    """True when ``column`` is one of the table's columns ``names``. A parsed condition is
    qualified by the table's alias, so ``t.meta.status`` is a field of the ``meta`` struct,
    not the column ``status``."""
    return column.name.lower() in names and not column.args.get("db")


def is_constant(node: exp.Expr | None) -> bool:
    """True for a value that reads no column and runs no query, such as a literal,
    ``CURRENT_DATE()`` or a parameter."""
    return (
        isinstance(node, exp.Expr)
        and node.find(exp.Column) is None
        and node.find(exp.Query) is None
    )


def in_values(node: exp.In) -> list[exp.Expr]:
    """The values of ``x IN (...)`` or ``x IN UNNEST([...])``; empty for a subquery."""
    if node.args.get("query") is not None:
        return []
    unnest = node.args.get("unnest")
    if unnest is not None:
        arrays = unnest.expressions
        if len(arrays) == 1 and isinstance(arrays[0], exp.Array):
            return list(arrays[0].expressions)
        return []
    return list(node.expressions)


def _like_limits(node: exp.Like, like: LikeMode) -> bool:
    """Whether a LIKE limits what is read. Shard names are matched against any constant
    pattern; clustering needs a fixed start such as 'req%'. NOT LIKE keeps nearly all."""
    pattern = node.expression
    if like == "none" or node.args.get("negate") or not is_constant(pattern):
        return False  # NOT LIKE, like `!=`, rules out a few values
    if like == "any":
        return True
    return isinstance(pattern, exp.Literal) and pattern.is_string and pattern.name[:1] not in "%_"


def _reads_data(facts: TableFacts) -> bool:
    """True unless the reference reads only pseudo-columns, or nothing at all.

    ``columns`` already holds only what the readers use, so a ``SELECT *`` that only feeds a
    ``COUNT(*)`` reads nothing.
    """
    pseudo = {name.lower() for name in facts.table.pseudo_columns}
    return bool(facts.columns - pseudo)


def partition_names(table: Table) -> frozenset[str]:
    """Lowercased names a filter can prune this table's partitions with."""
    partitioning = table.partitioning
    assert partitioning is not None
    if partitioning.column is not None:
        return frozenset({partitioning.column.lower()})
    return frozenset(name.lower() for name in table.pseudo_columns)


def _shown_partition_column(table: Table, predicate: Predicate | None) -> str:
    """The partition column to name: on an ingestion-time table, the pseudo-column the
    reported filter used."""
    partitioning = table.partitioning
    assert partitioning is not None
    if partitioning.column is not None:
        return partitioning.column
    if predicate is not None:
        return _PSEUDO_COLUMNS.get(predicate.column, predicate.column)
    return PARTITIONTIME


def _why_not(tree: exp.Expr, shown: str, what: str) -> str:
    if tree.find(exp.Query) is not None:
        return f"comparing `{shown}` with a subquery can't limit which {what} are read"
    return f"`{_render(tree)}` can't limit which {what} are read"


def _rewrite_fix(tree: exp.Expr, predicate: Predicate, shown: str, table: Table) -> str:
    """Drop a cast to STRING where that keeps the meaning: a DATE column compared with
    whole dates, which BigQuery then reads as dates."""
    casts = [
        node
        for node in tree.find_all(exp.Cast)
        if isinstance(node.this, exp.Column) and node.to.is_type(*exp.DataType.TEXT_TYPES)
    ]
    literals = [node for node in tree.find_all(exp.Literal) if node.parent not in casts]
    if (
        casts
        and _column_type(table, predicate.column) == "DATE"
        and tree.find(exp.Like) is None
        and literals
        and all(node.is_string and _FULL_DATE.fullmatch(node.name) for node in literals)
    ):
        return f"Compare `{shown}` itself: `{_unwrap(tree, (exp.Cast,))}`."
    return f"Compare `{shown}` itself with a constant, {_example(table, shown)}."


def _unwrap(tree: exp.Expr, functions: tuple[type[exp.Expr], ...]) -> str:
    """The condition with each of ``functions`` replaced by its argument, so only the
    function that stops pruning goes: `SUBSTR(LOWER(x), 1, 3)` becomes `SUBSTR(x, 1, 3)`."""
    tree = tree.copy()
    for node in list(tree.find_all(*functions)):
        if isinstance(node.this, exp.Expr) and node.find(exp.Column) is not None:
            node.replace(node.this)
    return _render(tree)


def _example(table: Table, shown: str) -> str:
    partitioning = table.partitioning
    assert partitioning is not None
    if partitioning.granularity == "RANGE":
        return "limiting it to a range of values"
    if partitioning.column is None:
        column_type = "DATE" if shown == PARTITIONDATE else "TIMESTAMP"
    else:
        column_type = _column_type(table, shown.lower())
    template = _EXAMPLES.get(column_type, _EXAMPLES["TIMESTAMP"])
    return f"for example `{template.format(col=shown)}`"


def _hint(facts: TableFacts, names: frozenset[str], what: str = "partitions") -> str:
    """Name a filter on another date column: the usual mistake, such as filtering
    ``event_date`` on sharded tables, or ``week`` instead of ``refresh_date``."""
    for predicate in facts.predicates:
        if predicate.column in names or not predicate.constant:
            continue
        if predicate.op not in _PRUNING_OPS:
            continue
        column_type = _column_type(facts.table, predicate.column)
        name = predicate.column.lower()
        if column_type in _EXAMPLES or "date" in name or "time" in name:
            return f" The filter on `{predicate.column}` doesn't limit which {what} are read."
    return ""


def _column_type(table: Table, name: str) -> str:
    column = table.column(name)
    if column is None:  # a pseudo-column
        return "DATE" if name == PARTITIONDATE.lower() else "TIMESTAMP"
    return column.type.upper().split("<", 1)[0].strip()


@functools.lru_cache(maxsize=4096)
def _parse(sql: str) -> exp.Expr | None:
    """A predicate, parsed. Cached: the rules and the estimate parse the same ones, and
    neither changes the trees."""
    try:
        return sqlglot.parse_one(sql, dialect=DIALECT)
    except SqlglotError:
        return None


def _render(condition: exp.Expr) -> str:
    """A condition as an agent would write it: no table qualifiers or needless quotes."""
    condition = condition.copy()
    for column in condition.find_all(exp.Column):
        column.set("table", None)
        if column.name in _PSEUDO_COLUMNS:
            column.set("this", exp.to_identifier(_PSEUDO_COLUMNS[column.name]))
    for identifier in condition.find_all(exp.Identifier):
        if _SIMPLE_NAME.fullmatch(identifier.name):
            identifier.set("quoted", False)
    tested = condition.this if isinstance(condition, exp.Not) else None
    if isinstance(tested, exp.Is) and isinstance(tested.expression, exp.Null):
        return f"{tested.this.sql(dialect=DIALECT)} IS NOT NULL"
    return condition.sql(dialect=DIALECT)


def _severity(table: Table) -> Severity:
    small = table.size_bytes is not None and table.size_bytes < _SMALL_TABLE_BYTES
    return Severity.INFO if small else Severity.WARN


def _size(table: Table) -> str:
    if table.size_bytes is None:
        return ""
    return f" ({format_bytes(table.size_bytes)} in all)"


def format_bytes(size: int) -> str:
    """Bytes in decimal units with one decimal place, such as ``2.1 TB``."""
    value, unit = float(size), "B"
    for larger in ("KB", "MB", "GB", "TB", "PB"):
        if round(value, 1) < 1000:
            break
        value, unit = value / 1000, larger
    return f"{value:.1f}".removesuffix(".0") + f" {unit}"
