"""SCN005: ``SELECT *`` that reads every column of a large table.

BigQuery bills every column a query reads in every partition it touches, so ``SELECT *``
costs the whole width of the table there. ``LIMIT`` doesn't change that on an unclustered
table: one partition of Google Trends billed 99.6 MB with ``LIMIT 10`` against 98.6 MB
without it, and 46.8 MB for the two columns the question needed (issue #16).
"""

from __future__ import annotations

from datetime import datetime

from scanisaur.engine.estimate import table_estimate
from scanisaur.engine.facts import QueryFacts, TableFacts
from scanisaur.engine.pruning import format_bytes
from scanisaur.engine.result import Finding, Severity
from scanisaur.engine.rules import SELECT_STAR

#: Below this many bytes billed, reading every column costs little, so the rule stays quiet.
LARGE_BYTES = 1 << 30
_FIX = (
    "Select only the columns you need. For column names and types, use "
    "`scanisaur_schema_describe`, which reads no data."
)


def select_star_findings(
    facts: QueryFacts,
    now: datetime,
    *,
    sampled: frozenset[str] = frozenset(),
    rejected: bool = False,
) -> list[Finding]:
    """One finding per large table that ``SELECT *`` reads in full, at the first place the
    query does. ``sampled`` names tables read with TABLESAMPLE, which reads a sample of
    blocks; ``rejected`` means BigQuery would refuse the query, so no amount is given."""
    limit = facts.outer_limit
    if limit == 0:
        return []  # LIMIT 0 returns only the schema
    preview = limit is not None and not facts.outer_aggregated
    full_reads: dict[str, list[TableFacts]] = {}
    for table_facts in sorted(facts.tables, key=lambda t: (t.position[0] or 0, t.position[1] or 0)):
        name = table_facts.table.qualified_name
        if name not in sampled and _reads_every_column(table_facts):
            full_reads.setdefault(name, []).append(table_facts)
    findings = []
    for references in full_reads.values():
        finding = _finding(references, limit if preview else None, now, rejected)
        if finding is not None:
            findings.append(finding)
    return findings


def _reads_every_column(facts: TableFacts) -> bool:
    """True when ``*`` reaches the result, so every column it selects is read. A reader
    that uses only some of the columns makes BigQuery read only those."""
    table = facts.table
    if not facts.star or table.kind in ("VIEW", "EXTERNAL"):
        return False  # a view reads other tables; an external table bills its files
    names = {column.name.lower() for column in table.columns}
    kept = names - facts.star_except
    return bool(kept) and kept <= facts.columns


def _finding(
    references: list[TableFacts], limit: int | None, now: datetime, rejected: bool
) -> Finding | None:
    first = references[0]
    table = first.table
    cost = table_estimate(references, now)
    if cost is not None and cost[1] < LARGE_BYTES:
        return None
    partitioned = table.partitioning is not None or table.is_wildcard
    # Clustering lets a LIMIT stop the scan early, so the amount is only an upper bound.
    stops_early = limit is not None and bool(table.clustering)
    amount = ""
    if cost is not None and not rejected:
        low, high, _confidence = cost
        unknown = partitioned and not table.partitions and low != high
        if not unknown:  # without a partition list, a filter keeps an unknown share
            bound = "about" if low == high and not stops_early else "up to"
            amount = f", {bound} {format_bytes(high)}"
    excepted = [c.name for c in table.columns if c.name.lower() in first.star_except]
    star = f"`* EXCEPT ({', '.join(excepted)})`" if excepted else "`*`"
    read = f"{len(table.columns) - len(excepted)} of the {len(table.columns)} columns"
    if not excepted:
        read = "every column"
    where = ""
    if partitioned:
        where = f" in the {'shards' if table.is_wildcard else 'partitions'} the query touches"
    subject = f"selecting {star} reads {read} of `{table.qualified_name}`{where}{amount}"
    if limit is not None and not stops_early:
        message = f"`LIMIT {limit}` doesn't reduce the bytes billed: {subject}."
    else:
        message = f"S{subject[1:]}."
    line, column = first.position
    return Finding(
        rule=SELECT_STAR,
        severity=Severity.WARN,
        message=message,
        fix=_FIX,
        line=line,
        column=column,
    )
