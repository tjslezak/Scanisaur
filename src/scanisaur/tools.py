"""What the MCP tools do, as plain functions: no MCP, no I/O.

Each tool returns a pydantic model (its output schema) and :func:`summary` gives the one
line of text that goes with it. Responses are kept short, since every token of them is
spent in the agent's context: about 500 tokens for a check, at the 95th percentile.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TypeVar

from pydantic import BaseModel, ConfigDict

from scanisaur.catalog.model import Catalog, Table
from scanisaur.catalog.source import CatalogSource, Snapshot
from scanisaur.engine.check import DEFAULT_POLICY, Policy, check
from scanisaur.engine.pruning import format_bytes
from scanisaur.engine.result import CheckResult, Estimate, Severity, Verdict

#: Tables ``schema_search`` returns unless asked for more, and the most it returns.
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 50
#: Table and column hits read from the source per search, before grouping them by table.
_HITS_PER_SEARCH = 500
#: Tables one ``schema_describe`` call takes.
MAX_DESCRIBE_TABLES = 5
#: Columns shown per table; partition, cluster and key columns come first.
MAX_COLUMNS = 200
#: Findings ``check_sql`` returns; the rest are counted in ``omitted_findings``.
MAX_FINDINGS = 5

_N = TypeVar("_N", int, float)
_SEVERITY_ORDER = {Severity.BLOCK: 0, Severity.WARN: 1, Severity.INFO: 2}


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class TableMatch(_Out):
    table: str
    rows: int | None
    size_bytes: int | None
    #: For example ``event_date DAY, filter required``; None when not partitioned.
    partitioning: str | None
    clustering: tuple[str, ...]
    #: Columns that matched; empty when only the table's name or description did.
    columns: tuple[str, ...]


class SearchResponse(_Out):
    tables: tuple[TableMatch, ...]


class ColumnInfo(_Out):
    name: str
    type: str


class TableDescription(_Out):
    table: str
    kind: str
    rows: int | None
    size_bytes: int | None
    partitioning: str | None
    clustering: tuple[str, ...]
    #: Column sets known to be unique; None when not known.
    keys: tuple[tuple[str, ...], ...] | None
    description: str
    columns: tuple[ColumnInfo, ...]
    #: Columns left out because the table has more than the limit.
    omitted_columns: int


class DescribeResponse(_Out):
    tables: tuple[TableDescription, ...]
    #: Names that matched no table.
    unknown: tuple[str, ...]


def schema_search(
    query: str, source: CatalogSource, limit: int = DEFAULT_SEARCH_LIMIT
) -> SearchResponse:
    """The tables best matching ``query``, each with the columns that matched."""
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    catalog = source.current().catalog
    columns: dict[str, list[str]] = {}
    # Hits come best first, so the dict's insertion order ranks the tables.
    for hit in source.search(query, _HITS_PER_SEARCH):
        found = columns.setdefault(hit.table, [])
        if hit.column is not None:
            found.append(hit.column)
    matches = []
    for name, found in columns.items():
        table = _lookup(catalog, name)
        if table is None:
            continue
        matches.append(
            TableMatch(
                table=name,
                rows=table.row_count,
                size_bytes=table.size_bytes,
                partitioning=_partitioning(table),
                clustering=table.clustering,
                columns=tuple(found),
            )
        )
        if len(matches) == limit:
            break
    return SearchResponse(tables=tuple(matches))


def schema_describe(names: Sequence[str], catalog: Catalog) -> DescribeResponse:
    """Describe up to :data:`MAX_DESCRIBE_TABLES` tables, by full or partial name."""
    described, unknown = [], []
    for name in names[:MAX_DESCRIBE_TABLES]:
        table = _lookup(catalog, name)
        if table is None:
            unknown.append(name)
        else:
            described.append(_describe(table))
    return DescribeResponse(tables=tuple(described), unknown=tuple(unknown))


def check_sql(
    sql: str,
    snapshot: Snapshot,
    policy: Policy = DEFAULT_POLICY,
    *,
    now: datetime | None = None,
) -> CheckResult:
    """Check the SQL and keep the :data:`MAX_FINDINGS` most severe findings."""
    result = check(sql, snapshot.catalog, policy=policy, now=now)
    findings = result.findings
    if len(findings) > MAX_FINDINGS:
        # sorted() is stable, so equally severe findings keep the order of the SQL.
        ranked = sorted(range(len(findings)), key=lambda i: _SEVERITY_ORDER[findings[i].severity])
        findings = tuple(findings[i] for i in sorted(ranked[:MAX_FINDINGS]))
    return result.model_copy(
        update={
            "findings": findings,
            "omitted_findings": len(result.findings) - len(findings),
            "snapshot_id": snapshot.snapshot_id,
        }
    )


def summary(result: CheckResult) -> str:
    """One line for the agent, such as
    ``warn: 2 findings, 1.6-2.4 GB billed. Fix and check again, or run it with the tag.``"""
    count = len(result.findings) + result.omitted_findings
    line = f"{result.verdict.value}: {count} finding{'' if count == 1 else 's'}"
    if result.estimate is not None:
        line += f", {describe_estimate(result.estimate)}"
    if result.verdict is Verdict.BLOCK:
        return f"{line}. Apply the fixes and check again before running it."
    return f"{line}. Run it with {result.tag} at the start or end."


def describe_estimate(estimate: Estimate) -> str:
    """For example ``1.6-2.4 GB billed, $0.01-$0.02 (medium confidence)``."""
    shown = _span(estimate.bytes_low, estimate.bytes_high, format_bytes) + " billed"
    if estimate.usd_low is not None and estimate.usd_high is not None:
        shown += ", " + _span(estimate.usd_low, estimate.usd_high, _dollars)
    return f"{shown} ({estimate.confidence} confidence)"


def _span(low: _N, high: _N, show: Callable[[_N], str]) -> str:
    shown = show(low), show(high)
    return shown[0] if shown[0] == shown[1] else f"{shown[0]}-{shown[1]}"


def _dollars(value: float) -> str:
    return "<$0.01" if 0 < value < 0.005 else f"${value:.2f}"


def _lookup(catalog: Catalog, name: str) -> Table | None:
    """``table``, ``dataset.table`` or ``project.dataset.table``, backticks allowed."""
    parts = name.strip().strip("`").split(".")
    if not 1 <= len(parts) <= 3 or not all(parts):
        return None
    dataset = parts[-2] if len(parts) > 1 else None
    project = parts[-3] if len(parts) > 2 else None
    return catalog.find(parts[-1], dataset, project)


def _partitioning(table: Table) -> str | None:
    partitioning = table.partitioning
    if partitioning is None:
        return "_TABLE_SUFFIX, wildcard shards" if table.is_wildcard else None
    column = partitioning.column or "_PARTITIONTIME"
    required = ", filter required" if partitioning.required else ""
    return f"{column} {partitioning.granularity}{required}"


def _describe(table: Table) -> TableDescription:
    columns = table.columns
    if len(columns) > MAX_COLUMNS:
        important = {name.lower() for name in table.clustering}
        if table.partitioning is not None and table.partitioning.column is not None:
            important.add(table.partitioning.column.lower())
        for key in table.keys or ():
            important.update(name.lower() for name in key)
        first = [c for c in columns if c.name.lower() in important]
        rest = [c for c in columns if c.name.lower() not in important]
        columns = tuple((first + rest)[:MAX_COLUMNS])
    return TableDescription(
        table=table.qualified_name,
        kind=table.kind,
        rows=table.row_count,
        size_bytes=table.size_bytes,
        partitioning=_partitioning(table),
        clustering=table.clustering,
        keys=table.keys,
        description=table.description,
        columns=tuple(ColumnInfo(name=c.name, type=c.type) for c in columns),
        omitted_columns=len(table.columns) - len(columns),
    )
