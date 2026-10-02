"""SCN003 and SCN004: filters that let BigQuery skip partitions, shards and clustered blocks.

Which functions stop BigQuery from pruning was measured with dry runs on public tables
(``docs/rules/scn004.md``). BigQuery still prunes through ``DATE()``, ``TIMESTAMP_TRUNC``,
``DATE_TRUNC``, ``CAST(... AS DATE)``, ``EXTRACT(DATE|YEAR ...)``, date arithmetic and
``FORMAT_TIMESTAMP``, so only the functions measured to defeat it are flagged. A function
that hasn't been measured is assumed to prune: the rules don't warn on a guess.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from scanisaur.catalog.model import PARTITIONDATE, PARTITIONTIME, TABLE_SUFFIX, Table
from scanisaur.engine.facts import Predicate, QueryFacts, TableFacts
from scanisaur.engine.parse import DIALECT
from scanisaur.engine.result import Finding, Severity
from scanisaur.engine.rules import PARTITION_FILTER, PRUNING_DEFEATED

#: Comparisons BigQuery can use to skip partitions when the other side is a constant.
_PRUNING_OPS = frozenset({"=", "<", "<=", ">", ">=", "between", "in"})
#: Reading all of a smaller table costs little, so its findings are info, not warnings.
_SMALL_TABLE_BYTES = 1 << 30
#: EXTRACT parts measured to stop pruning. DATE and YEAR still prune.
_EXTRACT_DEFEATS = frozenset({"DAY", "DAYOFWEEK", "DAYOFYEAR", "WEEK", "MONTH", "QUARTER"})
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
_NOT_NULL = re.compile(r"NOT (.+) IS NULL")


def pruning_findings(facts: QueryFacts) -> list[Finding]:
    """SCN003 and SCN004 findings for every table the query reads.

    A problem found at several references, such as two unfiltered UNION branches, is
    reported once, at the first.
    """
    candidates = [finding for table in facts.tables for finding in _table_findings(table)]
    candidates.sort(key=lambda f: (f.line or 0, f.column or 0, f.rule))
    unique: dict[tuple[str, str], Finding] = {}
    for finding in candidates:
        unique.setdefault((finding.rule, finding.message), finding)
    return list(unique.values())


def _table_findings(facts: TableFacts) -> Iterator[Finding]:
    table = facts.table
    if table.is_wildcard:
        yield from _shards(facts)
    elif table.partitioning is not None:
        yield from _partitions(facts)
    yield from _clustering(facts)


def _partitions(facts: TableFacts) -> Iterator[Finding]:
    table = facts.table
    partitioning = table.partitioning
    assert partitioning is not None
    required = partitioning.required
    if not required and not _reads_data(facts):
        return  # e.g. COUNT(*): BigQuery answers from metadata and reads nothing
    names = _partition_names(table)
    relevant = [p for p in facts.predicates if p.column in names and p.clause != "qualify"]
    if any(_prunes(p, table) for p in relevant):
        return
    shown = _shown_partition_column(table, relevant)
    wrapped = next((p for p in relevant if _usable(p) and _defeating(p, table)), None)
    subject = "the query"
    if wrapped is not None:
        message = (
            f"`{_display(wrapped.sql)}` wraps the partition column `{shown}` in "
            f"{_defeating(wrapped, table)}, so BigQuery can't use it to skip partitions "
            f"of `{table.qualified_name}`"
        )
        rule, fix = PRUNING_DEFEATED, _rewrite_fix(wrapped, shown, table)
    else:
        if relevant:
            problem = _why_not(relevant[0], shown, "partitions")
        else:
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
        joiner = "; the query" if wrapped is not None else f", so {subject}"
        message += f"{joiner} reads every partition{_size(table)}.{_hint(facts, names)}"
        severity = _severity(table)
    line, column = facts.position
    yield Finding(rule=rule, severity=severity, message=message, fix=fix, line=line, column=column)


def _shards(facts: TableFacts) -> Iterator[Finding]:
    table = facts.table
    if facts.name and len(facts.name) > len(table.name):
        return  # a narrower wildcard such as events_2026* already limits the shards
    if not _reads_data(facts):
        return  # e.g. MAX(_TABLE_SUFFIX) reads only shard names
    suffix = TABLE_SUFFIX.lower()
    relevant = [p for p in facts.predicates if p.column == suffix and p.clause != "qualify"]
    if any(_limits_shards(p) for p in relevant):
        return
    if relevant:
        problem, subject = _why_not(relevant[0], TABLE_SUFFIX, "shards"), "the query"
    else:
        problem, subject = "the query doesn't filter on `_TABLE_SUFFIX`", "it"
    hint = _hint(facts, frozenset({suffix}), "shards")
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


def _clustering(facts: TableFacts) -> Iterator[Finding]:
    table = facts.table
    for name in table.clustering:
        column = name.lower()
        relevant = [
            p
            for p in facts.predicates
            if p.column == column and _usable(p) and p.clause != "qualify"
        ]
        if any(p.wrapper is None for p in relevant):
            continue  # a plain filter on the column already uses the clustering
        for predicate in relevant:
            wrapped = _wrapped(predicate)
            function = _cluster_defeating(wrapped)
            if wrapped is None or function is None:
                continue
            line, position = facts.position
            yield Finding(
                rule=PRUNING_DEFEATED,
                severity=_severity(table),
                message=(
                    f"`{_display(predicate.sql)}` wraps the cluster column `{name}` in "
                    f"{function}, so BigQuery can't use the clustering of "
                    f"`{table.qualified_name}` to skip blocks."
                ),
                fix=(
                    f"Compare `{name}` itself, for example `{_unwrap(predicate, wrapped)}`, "
                    "if the stored values are written that way."
                ),
                line=line,
                column=position,
            )
            break


def _reads_data(facts: TableFacts) -> bool:
    """True unless the reference reads only pseudo-columns, or nothing at all."""
    pseudo = {name.lower() for name in facts.table.pseudo_columns}
    return facts.star or bool(facts.columns - pseudo)


def _partition_names(table: Table) -> frozenset[str]:
    """Lowercased names a filter can prune this table's partitions with."""
    partitioning = table.partitioning
    assert partitioning is not None
    if partitioning.column is not None:
        return frozenset({partitioning.column.lower()})
    return frozenset(name.lower() for name in table.pseudo_columns)


def _shown_partition_column(table: Table, relevant: list[Predicate]) -> str:
    partitioning = table.partitioning
    assert partitioning is not None
    if partitioning.column is not None:
        return partitioning.column
    if relevant:  # an ingestion-time table filtered on _PARTITIONDATE: name what was used
        return _PSEUDO_COLUMNS.get(relevant[0].column, relevant[0].column)
    return PARTITIONTIME


def _usable(predicate: Predicate) -> bool:
    """A comparison with constants, which BigQuery can use if nothing wraps the column."""
    return predicate.constant and predicate.op in _PRUNING_OPS


def _prunes(predicate: Predicate, table: Table) -> bool:
    return _usable(predicate) and _defeating(predicate, table) is None


def _limits_shards(predicate: Predicate) -> bool:
    """BigQuery evaluates any constant filter on ``_TABLE_SUFFIX`` against the shard names,
    wrapped or not (``PARSE_DATE``, ``CAST``, ``SUBSTR`` and ``LIKE`` were measured)."""
    if predicate.constant:
        return True
    condition = _parse(predicate.sql)
    if condition is None:
        return False
    columns = {column.name.upper() for column in condition.find_all(exp.Column)}
    return columns == {TABLE_SUFFIX} and condition.find(exp.Query) is None


def _why_not(predicate: Predicate, shown: str, what: str) -> str:
    condition = _parse(predicate.sql)
    if condition is not None and condition.find(exp.Query) is not None:
        return f"comparing `{shown}` with a subquery can't limit which {what} are read"
    return f"`{_display(predicate.sql)}` can't limit which {what} are read"


def _defeating(predicate: Predicate, table: Table) -> str | None:
    """The function, as written, that stops BigQuery pruning this partition column."""
    if predicate.wrapper is None:
        return None
    wrapped = _wrapped(predicate)
    if wrapped is None:
        return None
    column_type = _column_type(table, predicate.column)
    for node in _functions(wrapped):
        name = _partition_defeating(node, column_type)
        if name is not None:
            return name
    return None


def _partition_defeating(node: exp.Expr, column_type: str) -> str | None:
    if isinstance(node, exp.Extract):
        part = node.this.name.upper()
        return f"EXTRACT({part} FROM ...)" if part in _EXTRACT_DEFEATS else None
    if isinstance(node, exp.Cast) and node.to.is_type(*exp.DataType.TEXT_TYPES):
        return f"{'SAFE_CAST' if isinstance(node, exp.TryCast) else 'CAST'}(... AS STRING)"
    if isinstance(node, exp.TimeToStr) and column_type == "DATE":
        return "FORMAT_DATE"  # FORMAT_TIMESTAMP on a TIMESTAMP column still prunes
    if column_type == "TIMESTAMP":
        if isinstance(node, exp.UnixSeconds | exp.UnixMillis):
            return node.sql_name()
        if isinstance(node, exp.TsOrDsToDatetime):
            return "DATETIME"
    return None


def _cluster_defeating(wrapped: exp.Expr | None) -> str | None:
    if wrapped is None:
        return None
    for node in _functions(wrapped):
        name = _CLUSTER_DEFEATS.get(type(node))
        if name is not None:
            return name
    return None


def _functions(wrapped: exp.Expr) -> list[exp.Expr]:
    """The nodes between the column and the top of ``wrapped``, innermost first."""
    column = wrapped.find(exp.Column)
    if column is None or column is wrapped:
        return []
    nodes: list[exp.Expr] = []
    node = column.parent
    while node is not None:
        nodes.append(node)
        if node is wrapped:
            break
        node = node.parent
    return nodes


def _wrapped(predicate: Predicate) -> exp.Expr | None:
    """The side of the condition that holds the column, with whatever wraps it."""
    condition = _parse(predicate.sql)
    if condition is None:
        return None
    condition = condition.unnest()
    if isinstance(condition, exp.Or):  # x = 'a' OR x = 'b' counts as IN
        condition = next(condition.flatten()).unnest()
    if isinstance(condition, exp.Between | exp.In):
        side = condition.this
        return side if isinstance(side, exp.Expr) else None
    if isinstance(condition, exp.Binary):
        left, right = condition.left, condition.right
        return left if left.find(exp.Column) is not None else right
    return None


def _unwrap(predicate: Predicate, wrapped: exp.Expr) -> str:
    """The condition with the wrapping function replaced by the column inside it."""
    condition = _parse(predicate.sql)
    column = wrapped.find(exp.Column)
    assert condition is not None
    assert column is not None
    # Every copy, so `LOWER(x) = 'a' OR LOWER(x) = 'b'` becomes `x = 'a' OR x = 'b'`.
    for node in [n for n in condition.find_all(type(wrapped)) if n == wrapped]:
        node.replace(column.copy())
    return _render(condition)


def _rewrite_fix(predicate: Predicate, shown: str, table: Table) -> str:
    wrapped = _wrapped(predicate)
    # Dropping a cast to STRING keeps the meaning: BigQuery reads the literal as a date.
    if isinstance(wrapped, exp.Cast) and isinstance(wrapped.this, exp.Column):
        return f"Compare `{shown}` itself: `{_unwrap(predicate, wrapped)}`."
    return f"Compare `{shown}` itself with a constant, {_example(table, shown)}."


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
        if predicate.column in names or not _usable(predicate):
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


def _parse(sql: str) -> exp.Expr | None:
    try:
        return sqlglot.parse_one(sql, dialect=DIALECT)
    except SqlglotError:
        return None


def _display(sql: str) -> str:
    """A condition as an agent would write it: no table qualifiers or needless quotes."""
    condition = _parse(sql)
    return sql if condition is None else _render(condition)


def _render(condition: exp.Expr) -> str:
    condition = condition.copy()
    for column in condition.find_all(exp.Column):
        column.set("table", None)
        if column.name in _PSEUDO_COLUMNS:
            column.set("this", exp.to_identifier(_PSEUDO_COLUMNS[column.name]))
    for identifier in condition.find_all(exp.Identifier):
        if _SIMPLE_NAME.fullmatch(identifier.name):
            identifier.set("quoted", False)
    text = condition.sql(dialect=DIALECT)
    match = _NOT_NULL.fullmatch(text)
    return f"{match.group(1)} IS NOT NULL" if match else text


def _severity(table: Table) -> Severity:
    small = table.size_bytes is not None and table.size_bytes < _SMALL_TABLE_BYTES
    return Severity.INFO if small else Severity.WARN


def _size(table: Table) -> str:
    if table.size_bytes is None:
        return ""
    value, unit = float(table.size_bytes), "B"
    for larger in ("KB", "MB", "GB", "TB", "PB"):
        if value < 1000:
            break
        value, unit = value / 1000, larger
    shown = f"{value:.1f}".removesuffix(".0")
    return f" ({shown} {unit} in all)"
