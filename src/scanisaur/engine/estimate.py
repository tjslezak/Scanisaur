"""Static estimate of the bytes a query is billed for under on-demand pricing.

BigQuery bills each column of each partition once per query, however many times the query
reads it: a CTE read twice, or a self-join on the same partitions, is billed as one read
(measured in issue #15). So for each table the estimate takes, partition by partition, the
union of the columns that every reference reads there, and never multiplies by ``scans``.
The unit is the leaf field: BigQuery bills ``device.category`` without the rest of
``device`` (measured in issue #22).

- **Columns:** fixed-width types use BigQuery's sizes. The leaf fields of variable-width
  columns (STRING, BYTES, JSON, GEOGRAPHY, ARRAY, and STRUCTs holding them) split the rest
  of the table equally, so a struct of ten fields counts ten times a STRING.
- **Partitions:** the filters SCN003 counts as pruning are evaluated against the catalog's
  partitions, or a wildcard family's shards. Without that list, a filtered table may cost
  anything up to all of it.
- **Clustering:** a filter on a cluster column may skip blocks that metadata can't see, so
  the table's estimate is an upper bound.
- **Minimum:** each table is billed at least 10 MiB once the query bills anything, even a
  table it reads no columns of or prunes to no partitions. A self-join is one table.
"""

from __future__ import annotations

import calendar
import functools
import math
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeVar, assert_never

from sqlglot import exp
from sqlglot.errors import SqlglotError

from scanisaur.catalog.model import PARTITIONDATE, PARTITIONTIME, TABLE_SUFFIX, Granularity, Table
from scanisaur.engine.facts import Predicate, QueryFacts, TableFacts
from scanisaur.engine.parse import DIALECT
from scanisaur.engine.pruning import (
    defeats_pruning,
    in_values,
    is_constant,
    parsed_conditions,
    partition_conditions,
    partition_names,
    skips_blocks,
)
from scanisaur.engine.result import Confidence, Estimate

_MIB = 2**20
#: BigQuery bills at least this much for each table a query reads, rounded up to a MiB.
MIN_BILLED_BYTES = 10 * _MIB
_TIB = 2**40
#: At most this many partitions past the newest one listed stand in for those written
#: since the catalog was read.
_MAX_NEWER = 100
_RANK: dict[Confidence, int] = {"low": 0, "medium": 1, "high": 2}
_T = exp.DataType.Type
#: Bytes per value of fixed-width types, from BigQuery's data size calculation. INTEGER,
#: SMALLINT and the other integer aliases are INT64; FLOAT is FLOAT64.
_WIDTHS: dict[object, int] = {
    _T.BIGINT: 8,
    _T.INT: 8,
    _T.SMALLINT: 8,
    _T.TINYINT: 8,
    _T.DOUBLE: 8,
    _T.FLOAT: 8,
    _T.DECIMAL: 16,  # NUMERIC
    _T.BIGDECIMAL: 32,  # BIGNUMERIC
    _T.BOOLEAN: 1,
    _T.DATE: 8,
    _T.TIME: 8,
    _T.TIMESTAMP: 8,  # DATETIME
    _T.TIMESTAMPTZ: 8,  # TIMESTAMP
    _T.INTERVAL: 16,
}
#: Partition ID formats by granularity.
_ID_FORMATS: dict[Granularity, str] = {
    "YEAR": "%Y",
    "MONTH": "%Y%m",
    "DAY": "%Y%m%d",
    "HOUR": "%Y%m%d%H",
}
#: Special partitions. On a column-partitioned table, __UNPARTITIONED__ holds values
#: outside the range BigQuery partitions; on an ingestion-time one, the streaming buffer.
_NULL, _UNPARTITIONED = "__NULL__", "__UNPARTITIONED__"
#: Far enough from Python's limits that date arithmetic on them still works.
_EARLIEST, _LATEST = datetime(2, 1, 1), datetime(9998, 12, 31, 23, 59, 59, 999999)
_OUT_OF_RANGE = [
    (_EARLIEST, datetime(1960, 1, 1) - timedelta(microseconds=1)),
    (datetime(2160, 1, 1), _LATEST),
]
_WEEKDAYS = {name.upper(): index for index, name in enumerate(calendar.day_name)}
_DATE_TYPES = (_T.DATE,)
_TIME_TYPES = (_T.DATE, _T.TIMESTAMP, _T.TIMESTAMPTZ)
_ADDS = (exp.DateAdd, exp.TimestampAdd, exp.DatetimeAdd)
_SUBS = (exp.DateSub, exp.TimestampSub, exp.DatetimeSub)
_TRUNCS = (exp.DateTrunc, exp.TimestampTrunc, exp.DatetimeTrunc)
_COMPARISONS: dict[type[exp.Expr], str] = {
    exp.EQ: "=",
    exp.NEQ: "!=",
    exp.LT: "<",
    exp.LTE: "<=",
    exp.GT: ">",
    exp.GTE: ">=",
}
_FLIPPED = {"=": "=", "!=": "!=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}
#: FORMAT_DATE directives that map straight onto strftime.
_FORMAT = re.compile(r"(?:%[YmdHF]|[^%])*")
_V = TypeVar("_V", str, datetime)
#: The units DATE_TRUNC and the like floor to.
TruncUnit = Literal["HOUR", "DAY", "WEEK", "MONTH", "QUARTER", "YEAR"]


@dataclass(frozen=True, slots=True)
class _Floor:
    """A step that truncates to ``unit``, as DATE_TRUNC does."""

    unit: TruncUnit
    #: The day a WEEK starts, as ``datetime.weekday()`` numbers it: Monday is 0.
    weekday: int = 0


@dataclass(frozen=True, slots=True)
class _Shift:
    """A step that adds ``amount`` units, as DATE_ADD does."""

    amount: int
    unit: str


_Step = _Floor | _Shift
_DAY = _Floor("DAY")
_TRUNC_UNITS: dict[str, _Floor] = {
    "HOUR": _Floor("HOUR"),
    "DAY": _DAY,
    "MONTH": _Floor("MONTH"),
    "QUARTER": _Floor("QUARTER"),
    "YEAR": _Floor("YEAR"),
    "ISOWEEK": _Floor("WEEK", _WEEKDAYS["MONDAY"]),
    "WEEK": _Floor("WEEK", _WEEKDAYS["SUNDAY"]),
}
#: A UTC offset in hours only, such as the `-07` in `10:00:00-07`. It follows a time, so
#: the day of a plain date such as `2026-09-30` isn't taken for one.
_SHORT_OFFSET = re.compile(r"(\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?[+-]\d{2})$")


def estimate(
    facts: QueryFacts,
    now: datetime,
    price_per_tib: float | None,
    sampled: frozenset[str] = frozenset(),
) -> Estimate | None:
    """The bytes billed for the tables in ``facts``. None when a table's size is unknown,
    or it is a view or an external table, which bill differently.

    ``now`` evaluates ``CURRENT_DATE()`` and the like. ``price_per_tib`` is the on-demand
    price in US dollars; None (capacity pricing) leaves out the dollars. ``sampled`` names
    the tables read with TABLESAMPLE, which bills only the blocks it picks.
    """
    by_table: dict[str, list[TableFacts]] = {}
    for table_facts in facts.tables:
        by_table.setdefault(table_facts.table.qualified_name, []).append(table_facts)
    if not all(_estimable(references[0].table) for references in by_table.values()):
        return None
    if facts.outer_limit == 0:  # measured: BigQuery returns the schema and reads nothing
        free = None if price_per_tib is None else 0.0
        return Estimate(bytes_low=0, bytes_high=0, confidence="high", usd_low=free, usd_high=free)
    now = _naive_utc(now)
    low = high = free = 0
    confidence: Confidence = "high"
    for name, references in by_table.items():
        result = _table_estimate(references, now)
        if result is None:
            return None
        table_low, table_high, table_confidence = result
        if name in sampled and table_high:
            table_low = MIN_BILLED_BYTES
            table_confidence = "low"
        free += not table_high
        low, high = low + table_low, high + table_high
        confidence = min(confidence, table_confidence, key=_RANK.__getitem__)
    if high:
        # Measured (#26): once a query bills anything, each table it references is billed
        # its minimum, even one it reads no columns of or prunes to no partitions. A query
        # that bills nothing, such as `SELECT COUNT(*)` on one table, stays free.
        low, high = low + free * MIN_BILLED_BYTES, high + free * MIN_BILLED_BYTES
    usd_low = usd_high = None
    if price_per_tib is not None:
        usd_low = round(low / _TIB * price_per_tib, 4)
        usd_high = round(high / _TIB * price_per_tib, 4)
    return Estimate(
        bytes_low=low,
        bytes_high=high,
        confidence=confidence,
        usd_low=usd_low,
        usd_high=usd_high,
    )


def _naive_utc(now: datetime) -> datetime:
    """``now`` in UTC without a time zone, as partition IDs and literals are read."""
    return now if now.tzinfo is None else now.astimezone(UTC).replace(tzinfo=None)


#: A leaf field: a column's name, then the struct fields down to the leaf.
_Leaf = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Read:
    """What one reference reads: its leaf fields, in the units it may and surely reads."""

    leaves: frozenset[_Leaf]
    high: frozenset[str]
    low: frozenset[str]
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class _Layout:
    """Each leaf field's share of a table's bytes, how well it is known, and each column's
    leaf fields."""

    shares: dict[_Leaf, float]
    confidence: dict[_Leaf, Confidence]
    leaves: dict[str, tuple[_Leaf, ...]]


#: Without a partition list, the whole table is one unit.
_WHOLE = "*"


def table_estimate(
    references: list[TableFacts], now: datetime
) -> tuple[int, int, Confidence] | None:
    """Billed bytes (low, high) and confidence for one table read by ``references``; None
    when it can't be estimated, as ``estimate`` says."""
    return _table_estimate(references, _naive_utc(now))


def _table_estimate(
    references: list[TableFacts], now: datetime
) -> tuple[int, int, Confidence] | None:
    """Billed bytes (low, high) and confidence for one table, over all its references."""
    table = references[0].table
    size = table.size_bytes
    if size is None or not _estimable(table):
        return None
    layout = _layout(table)
    units, domain = _units(table, size, now)
    high_leaves: dict[str, set[_Leaf]] = {}
    low_leaves: dict[str, set[_Leaf]] = {}
    confidence: Confidence = "high"
    for reference in references:
        read = _read(reference, layout, units, domain)
        if not read.leaves:
            continue  # e.g. COUNT(*), answered from metadata
        for unit in read.high:
            high_leaves.setdefault(unit, set()).update(read.leaves)
        for unit in read.low:
            low_leaves.setdefault(unit, set()).update(read.leaves)
        ranks = [read.confidence, *(layout.confidence[leaf] for leaf in read.leaves)]
        confidence = min(confidence, *ranks, key=_RANK.__getitem__)
    if not any(units[unit] for unit in high_leaves):
        return 0, 0, confidence  # nothing to read: no columns, or empty partitions
    low = billed_bytes(_scanned(low_leaves, units, layout.shares))
    return low, billed_bytes(_scanned(high_leaves, units, layout.shares)), confidence


def _estimable(table: Table) -> bool:
    """False for a table of unknown size, and for views and external tables, which bill
    differently."""
    return table.size_bytes is not None and table.kind not in ("VIEW", "EXTERNAL")


def billed_bytes(scanned: float) -> int:
    """BigQuery rounds each table's bytes up to a whole MiB, and bills at least 10 MiB."""
    return max(-int(-scanned // _MIB) * _MIB, MIN_BILLED_BYTES)


def _scanned(
    leaves: dict[str, set[_Leaf]], units: dict[str, int], shares: dict[_Leaf, float]
) -> float:
    """Each leaf field of each unit counted once, however many references read it. ``fsum``
    keeps the total, and so its rounding up to a MiB, the same in any order."""
    return math.fsum(
        units[unit] * math.fsum(shares[leaf] for leaf in read) for unit, read in leaves.items()
    )


def table_rows(reference: TableFacts, now: datetime) -> tuple[int, bool] | None:
    """The rows a reference reads: the table's row count times the share of its bytes in
    the partitions its filters keep. The flag says whether that count is known, which it
    is only when every filter on the table is a partition filter evaluated exactly: any
    other filter, a join or a subquery may leave far fewer rows. None without a row count
    or a size."""
    table = reference.table
    rows, size = table.row_count, table.size_bytes
    if rows is None or size is None or not _estimable(table):
        return None
    if size == 0:
        return rows, rows == 0  # an empty table holds no rows
    units, domain = _units(table, size, _naive_utc(now))
    high, _low, confidence = _partitions(reference, units, domain)
    kept = sum(units[unit] for unit in high)
    count = round(rows * min(kept / size, 1.0))
    return count, confidence == "high" and _only_partition_filters(reference)


def _only_partition_filters(reference: TableFacts) -> bool:
    """True when nothing but filters that prune partitions limits the reference's rows."""
    table = reference.table
    if reference.linked:
        return False  # a correlated subquery, INTERSECT or a filter above a LIMIT
    keys: frozenset[str] = frozenset()
    if table.is_wildcard:
        keys = frozenset({TABLE_SUFFIX.lower()})
    elif table.partitioning is not None:
        keys = partition_names(table)
    parsed = parsed_conditions(reference)
    if len(parsed) != len(reference.predicates):
        return False  # a condition that couldn't be parsed
    return all(
        predicate.column.lower() in keys
        and predicate.op != "other"
        and predicate.constant
        and not (table.partitioning is not None and defeats_pruning(tree, table))
        and _whole_partitions(predicate, table)
        for predicate, tree in parsed
    )


def _whole_partitions(predicate: Predicate, table: Table) -> bool:
    """True when the filter keeps or drops whole partitions, so the rows it keeps are
    theirs: a shard suffix, a pseudo-column holding each partition's start, or a DATE
    partitioned by day, as `day = '…'` or `DATE(ts) = '…'`. A finer filter, such as a
    five-second window in a daily partition, keeps fewer rows than its partitions hold."""
    column = predicate.column.lower()
    if table.is_wildcard or column in (PARTITIONTIME.lower(), PARTITIONDATE.lower()):
        return True
    partitioning = table.partitioning
    if partitioning is None or partitioning.granularity != "DAY":
        return False
    types = {c.name.lower(): c.type.upper() for c in table.columns}
    return types.get(column) == "DATE" or (predicate.wrapper or "").upper() == "DATE"


def _units(table: Table, size: int, now: datetime) -> tuple[dict[str, int], _Domain | None]:
    """The table's partitions by ID with their bytes, or the whole table as one unit
    without a partition list; and the values its partitions hold."""
    listed = bool(table.partitions) and (table.partitioning is not None or table.is_wildcard)
    domain = _domain(table, now) if table.partitioning is not None or table.is_wildcard else None
    units = {p.id: p.size_bytes for p in table.partitions} if listed else {_WHOLE: size}
    if listed and domain is not None:
        units |= _newer(table, domain)
    return units, domain


def _read(
    reference: TableFacts,
    layout: _Layout,
    units: dict[str, int],
    domain: _Domain | None,
) -> _Read:
    leaves = _leaves_read(reference, layout)
    if not leaves:
        return _Read(frozenset(), frozenset(), frozenset(), "high")
    high, low, confidence = _partitions(reference, units, domain)
    return _Read(leaves, high, low, confidence)


def _partitions(
    reference: TableFacts, units: dict[str, int], domain: _Domain | None
) -> tuple[frozenset[str], frozenset[str], Confidence]:
    """The units a reference may read, those it surely reads, and how well that is known."""
    table = reference.table
    parsed = parsed_conditions(reference)
    conditions = partition_conditions(reference, parsed)
    narrowed = table.is_wildcard and len(reference.name) > len(table.name)
    blocks = skips_blocks(reference, parsed)
    if _WHOLE in units or domain is None:
        whole = frozenset({_WHOLE})
        limited = domain is not None and not all(
            _keeps_all(tree, domain, table) for tree in conditions
        )
        if limited or narrowed:  # limits partitions, by how much isn't known
            return whole, frozenset(), "low"
        if blocks:
            return whole, frozenset(), "medium"
        return whole, whole, "high"
    if all(_keeps_all(tree, domain, table) for tree in conditions):
        # Unfiltered: the partitions listed. Newer ones matter only to a filter that
        # could pick them alone, such as `= CURRENT_DATE()`.
        units = {unit: size for unit, size in units.items() if not _is_newer(unit)}
    kept, exact, cap = _kept(reference, conditions, domain, units)
    high = frozenset(kept)
    if cap is not None and len(high) > cap:  # `= @day` keeps one partition, unknown which
        high = frozenset(sorted(high, key=lambda pid: (-units[pid], pid))[:cap])
    confidence: Confidence = "high" if exact else "medium" if cap is not None else "low"
    if blocks or any(_is_newer(unit) for unit in high):
        confidence = min(confidence, "medium", key=_RANK.__getitem__)
    known = frozenset(unit for unit in high if not _is_newer(unit))
    low = known if exact and not blocks else frozenset()
    return high, low, confidence


def _leaves_read(reference: TableFacts, layout: _Layout) -> frozenset[_Leaf]:
    """The leaf fields a reference reads: every leaf under each path it reads. A path past
    a leaf, as into a JSON value, reads that leaf; one the schema doesn't have reads the
    whole column."""
    paths = reference.paths
    if paths is None:
        paths = frozenset((column,) for column in reference.columns)
    read: set[_Leaf] = set()
    for path in paths:
        leaves = layout.leaves.get(path[0])
        if leaves is None:
            continue  # a pseudo-column such as _PARTITIONTIME, which is free
        under = [leaf for leaf in leaves if leaf[: len(path)] == path]
        if not under:
            under = [leaf for leaf in leaves if path[: len(leaf)] == leaf] or list(leaves)
        read.update(under)
    return frozenset(read)


def _layout(table: Table) -> _Layout:
    """Each leaf field's share of the table's bytes, and how well it is known.

    A fixed-width field, whether a column or a struct field outside an array, holds its
    width in bytes for every row. The variable-width fields split the rest equally, so a
    struct of ten STRINGs gets ten times a STRING's share. When fixed-width fields would
    fill the table (NULLs take no space), the split can't be known, so every field gets an
    equal share.
    """
    size = table.size_bytes or 0
    rows = table.row_count
    leaves: dict[str, tuple[_Leaf, ...]] = {}
    widths: dict[_Leaf, int | None] = {}
    for column in table.columns:
        name = column.name.lower()
        fields = _fields(column.type)
        leaves[name] = tuple((name, *path) for path, _width in fields)
        widths.update(zip(leaves[name], (width for _path, width in fields), strict=True))
    fixed = (
        {leaf: rows * w for leaf, w in widths.items() if w is not None} if rows is not None else {}
    )
    variable = [leaf for leaf, width in widths.items() if width is None]
    total = sum(fixed.values())
    if rows is None or size == 0 or (variable and total >= size):
        even = 1 / len(widths)
        return _Layout(dict.fromkeys(widths, even), dict.fromkeys(widths, "low"), leaves)
    if total > size:  # only fixed-width fields, smaller than their widths for NULLs
        shares = {leaf: fixed[leaf] / total for leaf in widths}
        return _Layout(shares, dict.fromkeys(widths, "medium"), leaves)
    rest = (size - total) / len(variable) if variable else 0.0
    shares = {leaf: fixed.get(leaf, rest) / size for leaf in widths}
    confidence: dict[_Leaf, Confidence] = {
        leaf: "high" if leaf in fixed else "medium" for leaf in widths
    }
    return _Layout(shares, confidence, leaves)


@functools.cache
def _fields(type_: str) -> tuple[tuple[tuple[str, ...], int | None], ...]:
    """A column type's leaf fields, as (path within the column, bytes per value or None
    for a variable width). A scalar is one field with an empty path; an ARRAY has its
    element's fields, all of variable width."""
    try:
        data_type = exp.DataType.build(type_, dialect=DIALECT)
    except SqlglotError:
        return (((), None),)
    return tuple(_leaf_fields(data_type, repeated=False))


def _leaf_fields(
    data_type: exp.DataType, *, repeated: bool
) -> Iterator[tuple[tuple[str, ...], int | None]]:
    if data_type.this == _T.ARRAY:
        element = data_type.expressions[0] if data_type.expressions else None
        if isinstance(element, exp.DataType):
            yield from _leaf_fields(element, repeated=True)
        else:
            yield (), None
        return
    if data_type.this == _T.STRUCT:
        kinds = [
            (field_.name.lower(), kind)
            for field_ in data_type.expressions
            if isinstance(field_, exp.ColumnDef)
            and isinstance(kind := field_.args.get("kind"), exp.DataType)
        ]
        if not kinds or len(kinds) != len(data_type.expressions):
            # A field without a name, as in STRUCT<INT64, STRING>, can't be read on its own.
            yield (), None
            return
        for name, kind in kinds:
            for path, width in _leaf_fields(kind, repeated=repeated):
                yield (name, *path), width
        return
    yield (), None if repeated else _WIDTHS.get(data_type.this)


# Partition filters --------------------------------------------------------------------

#: The values a partition can hold, as (first, last) pairs; empty for the NULL partition.
_Spans = list[tuple[datetime, datetime]]
#: Whether a partition, given by ID or shard suffix, may hold rows a condition keeps;
#: None when that isn't known.
_Test = Callable[[str], bool | None]
#: An OR of ANDs of `x <op> value`; None for a value not known here.
_Terms = list[list[tuple[str, datetime | None]]]


@dataclass(frozen=True, slots=True)
class _Domain:
    """How a table's partition IDs compare with filter values."""

    #: Lowercased names that filter partitions: the partition column, its pseudo-columns,
    #: or ``_TABLE_SUFFIX``.
    names: frozenset[str]
    #: Shards of a wildcard family, compared as strings.
    suffix: bool
    granularity: Granularity | None
    #: Pseudo-columns hold the partition's start, not a range of values.
    point: bool
    #: The partition column is a DATE, so a partition's last value is a whole day.
    dates: bool
    now: datetime
    _spans: dict[str, _Spans | None] = field(default_factory=dict, compare=False)

    def spans(self, pid: str) -> _Spans | None:
        """The values partition ``pid`` holds; None when its ID can't be read."""
        if pid not in self._spans:
            self._spans[pid] = self._read_spans(pid)
        return self._spans[pid]

    def _read_spans(self, pid: str) -> _Spans | None:
        if pid == _NULL:
            return []
        if pid == _UNPARTITIONED:
            return [] if self.point else list(_OUT_OF_RANGE)
        fmt = _ID_FORMATS.get(self.granularity) if self.granularity is not None else None
        if fmt is None:
            return None  # integer ranges
        try:
            start = datetime.strptime(pid, fmt)
        except ValueError:
            return None
        assert self.granularity is not None
        end = _shift(start, 1, self.granularity)
        if end is None:
            return None
        last = start if self.point else end - timedelta(microseconds=1)
        if self.dates:
            last = last.replace(hour=0, minute=0, second=0, microsecond=0)
        return [(start, last)]


def _domain(table: Table, now: datetime) -> _Domain:
    if table.is_wildcard:
        return _Domain(frozenset({"_table_suffix"}), True, None, False, False, now)
    partitioning = table.partitioning
    assert partitioning is not None
    names = partition_names(table)
    if partitioning.column is None:
        return _Domain(names, False, partitioning.granularity, True, False, now)
    column = table.column(partitioning.column)
    dates = column is not None and column.type.upper() == "DATE"
    return _Domain(names, False, partitioning.granularity, False, dates, now)


def _newer(table: Table, domain: _Domain) -> dict[str, int]:
    """Partitions that may have been written since the catalog was read, up to ``now``,
    each the size of the newest one listed."""
    granularity = domain.granularity
    fmt = _ID_FORMATS.get(granularity) if granularity is not None else None
    dated = [
        (spans[0][0], p)
        for p in table.partitions
        if p.id not in (_NULL, _UNPARTITIONED) and (spans := domain.spans(p.id))
    ]
    if fmt is None or granularity is None or not dated:
        return {}
    start, newest = max(dated, key=lambda pair: pair[0])
    newer: dict[str, int] = {}
    moment = _shift(start, 1, granularity)
    while moment is not None and moment <= domain.now and len(newer) < _MAX_NEWER:
        newer[_NEWER + moment.strftime(fmt)] = newest.size_bytes
        moment = _shift(moment, 1, granularity)
    return newer


_NEWER = "newer:"


def _is_newer(unit: str) -> bool:
    return unit.startswith(_NEWER)


def _kept(
    reference: TableFacts, conditions: list[exp.Expr], domain: _Domain, units: dict[str, int]
) -> tuple[set[str], bool, int | None]:
    """The partitions the conditions keep, whether that is exact, and at most how many an
    ``=`` or ``IN`` with values not known here keeps."""
    table = reference.table
    # In `events_2026*`, _TABLE_SUFFIX is what follows `events_2026`.
    prefix = reference.name[len(table.name) - 1 : -1] if table.is_wildcard else ""
    values = {
        unit: unit.removeprefix(_NEWER)[len(prefix) :]
        for unit in units
        if unit.removeprefix(_NEWER).startswith(prefix)
    }
    kept = set(values)
    exact, cap = True, None
    for tree in conditions:
        test = _compile(tree, domain, table)
        unknown = False
        for unit in list(kept):
            verdict = test(values[unit])
            if verdict is False:
                kept.discard(unit)
            elif verdict is None:
                unknown = True
        if unknown:
            exact = False
            count = _point_values(tree, domain)
            if count is not None:
                cap = count if cap is None else min(cap, count)
    return kept, exact, cap


def _compile(node: exp.Expr, domain: _Domain, table: Table) -> _Test:
    """A test of each partition against one condition, worked out once for all of them."""
    node = node.unnest()
    if isinstance(node, exp.Or | exp.And):
        tests = [_compile(child, domain, table) for child in node.flatten()]
        decisive = isinstance(node, exp.Or)  # one True decides an OR, one False an AND

        def combined(pid: str) -> bool | None:
            verdicts = [test(pid) for test in tests]
            if decisive in verdicts:
                return decisive
            return None if None in verdicts else not decisive

        return combined
    if _keeps_all(node, domain, table):
        return lambda _pid: True
    return _suffix_test(node, domain) if domain.suffix else _time_test(node, domain)


def _keeps_all(node: exp.Expr, domain: _Domain, table: Table) -> bool:
    """True for a condition BigQuery can't skip any partition or shard with."""
    node = node.unnest()
    if isinstance(node, exp.Or):  # one branch keeping everything keeps everything
        return any(_keeps_all(child, domain, table) for child in node.flatten())
    if isinstance(node, exp.And):
        return all(_keeps_all(child, domain, table) for child in node.flatten())
    if not _mentions(node, domain.names) or _dynamic(node, domain):
        return True
    not_null = isinstance(node, exp.Not) and isinstance(node.this.unnest(), exp.Is)
    if domain.suffix:
        return not_null  # shard names are never NULL
    # Measured not to prune: `!=`, IS NOT NULL, and functions such as CAST(... AS STRING).
    return isinstance(node, exp.NEQ) or not_null or defeats_pruning(node, table)


def _dynamic(node: exp.Expr, domain: _Domain) -> bool:
    """True when the condition compares with another column or a subquery, which BigQuery
    can't check against partitions before it reads them."""
    if node.find(exp.Query) is not None:
        return True
    columns = [c for c in node.find_all(exp.Column) if c.find_ancestor(exp.Query) is None]
    return any(column.name.lower() not in domain.names for column in columns)


def _time_test(node: exp.Expr, domain: _Domain) -> _Test:
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
        if _path(node.this, domain) != ():
            return lambda _pid: None
        return lambda pid: None if (spans := domain.spans(pid)) is None else not spans
    compared = None if isinstance(node, exp.Not) else _comparison(node)
    path = None if compared is None else _path(compared[0], domain)
    if compared is None or path is None:
        # No comparison ever holds on the NULL partition, whatever it compares.
        return lambda pid: False if domain.spans(pid) == [] else None
    terms: _Terms = [
        [(op, _time_value(value, domain.now)) for op, value in raw] for raw in compared[1]
    ]

    def test(pid: str) -> bool | None:
        spans = domain.spans(pid)
        if spans is None:
            return None
        verdicts: list[bool | None] = []
        for first, last in spans:
            low, high = _apply(path, first), _apply(path, last)
            if low is None or high is None:
                verdicts.append(None)
            else:
                verdicts.extend(_term(low, high, term) for term in terms)
        return _any(verdicts)

    return test


def _suffix_test(node: exp.Expr, domain: _Domain) -> _Test:
    negated = isinstance(node, exp.Not)
    inner = node.this.unnest() if negated else node
    if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
        return lambda _pid: negated  # shard names are never NULL
    if isinstance(inner, exp.Like):
        pattern = _string_value(inner.expression, domain.now)
        if pattern is None or _path(inner.this, domain) != ():
            return lambda _pid: None
        regex = _like(pattern)
        keep = negated == bool(inner.args.get("negate"))  # NOT (x NOT LIKE p) is a LIKE
        return lambda pid: (regex.fullmatch(pid) is not None) == keep
    compared = _comparison(inner)
    if (negated and not isinstance(inner, exp.In)) or compared is None:
        return lambda _pid: None
    if _path(compared[0], domain) != ():
        return lambda _pid: None
    terms = [[(op, _string_value(value, domain.now)) for op, value in raw] for raw in compared[1]]

    def test(pid: str) -> bool | None:
        held = _any([_term(pid, pid, term) for term in terms])
        return held if held is None or not negated else not held

    return test


def _term(low: _V, high: _V, term: list[tuple[str, _V | None]]) -> bool | None:
    """Whether values from ``low`` to ``high`` can satisfy every comparison in ``term``;
    None when only the comparisons with values not known here are left to decide."""
    if any(value is not None and not _holds(low, high, op, value) for op, value in term):
        return False
    return None if any(value is None for _op, value in term) else True


def _any(verdicts: list[bool | None]) -> bool | None:
    if True in verdicts:
        return True
    return None if None in verdicts else False


def _comparison(node: exp.Expr) -> tuple[exp.Expr, list[list[tuple[str, exp.Expr]]]] | None:
    """The side holding the partition column, and the condition on it as an OR of ANDs."""
    if isinstance(node, exp.Between):
        low, high = node.args["low"], node.args["high"]
        return node.this, [[(">=", low), ("<=", high)]]
    if isinstance(node, exp.In):
        values = in_values(node)
        return (node.this, [[("=", value)] for value in values]) if values else None
    op = _COMPARISONS.get(type(node))
    if op is None or not isinstance(node, exp.Binary):
        return None
    if is_constant(node.right):
        return node.left, [[(op, node.right)]]
    if is_constant(node.left):
        return node.right, [[(_FLIPPED[op], node.left)]]
    return None


def _holds(low: _V, high: _V, op: str, value: _V) -> bool:
    """Whether some value from ``low`` to ``high`` satisfies ``x <op> value``."""
    if op == "=":
        return low <= value <= high
    if op == "!=":
        return not low == value == high
    if op == "<":
        return low < value
    if op == "<=":
        return low <= value
    if op == ">":
        return high > value
    return high >= value


def _point_values(tree: exp.Expr, domain: _Domain) -> int | None:
    """How many partitions ``col = x`` or ``col IN (...)`` can keep, when it compares the
    partition column, or DATE() of it on daily partitions, with values not known here."""
    node = tree.unnest()
    compared = _comparison(node) if isinstance(node, exp.EQ | exp.In) else None
    if compared is None:
        return None
    path = _path(compared[0], domain)
    if path == () or (path == (_DAY,) and domain.granularity == "DAY"):
        return len(compared[1])
    return None


def _mentions(node: exp.Expr, names: frozenset[str]) -> bool:
    return any(
        column.name.lower() in names
        and not column.args.get("db")
        and column.find_ancestor(exp.Query) is None
        for column in node.find_all(exp.Column)
    )


def _path(node: exp.Expr, domain: _Domain) -> tuple[_Step, ...] | None:
    """The steps between the partition column and ``node``, innermost first: ``()`` for the
    column itself, ``(_Floor("DAY"),)`` for ``DATE(ts)``, ``(_Shift(1, "DAY"),)`` for
    ``DATE_ADD(d, INTERVAL 1 DAY)``. None for anything else, such as a function measured to stop
    pruning, or one with a time zone. Every step keeps the order of values."""
    node = node.unnest()
    if isinstance(node, exp.Column):
        named = node.name.lower() in domain.names and not node.args.get("db")
        return () if named else None
    if node.args.get("zone") is not None:
        return None
    inner: exp.Expr | None
    step: _Step | None
    if (isinstance(node, exp.Date) and not node.expressions) or (
        isinstance(node, exp.Cast) and node.to.is_type(*_DATE_TYPES)
    ):
        inner, step = node.this, _DAY
    elif isinstance(node, exp.Extract) and node.name.upper() == "DATE":
        inner, step = node.expression, _DAY
    elif isinstance(node, exp.Timestamp):
        inner, step = node.this, None  # TIMESTAMP(date) keeps pruning
    elif isinstance(node, _TRUNCS):
        inner, step = node.this, _trunc(node.args.get("unit"))
        if step is None:
            return None
    elif isinstance(node, _ADDS + _SUBS):
        shift = _interval(node)
        if shift is None:
            return None
        inner, step = node.this, _Shift(*shift)
    else:
        return None
    if not isinstance(inner, exp.Expr):
        return None
    path = _path(inner, domain)
    if path is None:
        return None
    return path if step is None else (*path, step)


def _apply(path: tuple[_Step, ...], value: datetime) -> datetime | None:
    result: datetime | None = value
    for step in path:
        if result is None:
            return None
        if isinstance(step, _Shift):
            result = _shift(result, step.amount, step.unit)
        else:
            result = _floor(result, step)
    return result


def _trunc(node: object) -> _Floor | None:
    """The step of DATE_TRUNC to ``node``'s unit; None for a unit this doesn't model."""
    if isinstance(node, exp.WeekStart):
        weekday = _WEEKDAYS.get(node.name.upper())
        return None if weekday is None else _Floor("WEEK", weekday)
    return _TRUNC_UNITS.get(node.name.upper()) if isinstance(node, exp.Expr) else None


def _floor(value: datetime, step: _Floor) -> datetime | None:
    unit = step.unit
    if unit == "HOUR":
        return value.replace(minute=0, second=0, microsecond=0)
    day = value.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "DAY":
        return day
    if unit == "WEEK":
        back = (day.weekday() - step.weekday) % 7
        try:
            return day - timedelta(days=back)
        except OverflowError:
            return None
    if unit == "MONTH":
        return day.replace(day=1)
    if unit == "QUARTER":
        return day.replace(month=(day.month - 1) // 3 * 3 + 1, day=1)
    if unit == "YEAR":
        return day.replace(month=1, day=1)
    assert_never(unit)


def _shift(value: datetime, amount: int, unit: str) -> datetime | None:
    """``value`` moved by ``amount`` units; None when the unit is unknown or the result
    falls outside the dates Python can hold."""
    step = {
        "MICROSECOND": timedelta(microseconds=1),
        "MILLISECOND": timedelta(milliseconds=1),
        "SECOND": timedelta(seconds=1),
        "MINUTE": timedelta(minutes=1),
        "HOUR": timedelta(hours=1),
        "DAY": timedelta(days=1),
        "WEEK": timedelta(weeks=1),
    }.get(unit)
    months = {"MONTH": 1, "QUARTER": 3, "YEAR": 12}.get(unit)
    try:
        if step is not None:
            return value + amount * step
        if months is None:
            return None
        year, month = divmod(value.year * 12 + value.month - 1 + amount * months, 12)
        day = min(value.day, calendar.monthrange(year, month + 1)[1])
        return value.replace(year=year, month=month + 1, day=day)
    except (OverflowError, ValueError):
        return None


# Constant values ----------------------------------------------------------------------


def _time_value(node: exp.Expr, now: datetime) -> datetime | None:
    """A constant date or timestamp, in UTC; None if it isn't one this can evaluate."""
    node = node.unnest()
    if isinstance(node, exp.Literal) and node.is_string:
        return _parse_time(node.name)
    if node.args.get("zone") is not None:
        return None
    if isinstance(node, exp.Cast) and node.to.is_type(*_TIME_TYPES):
        value = _time_value(node.this, now)
        if value is not None and node.to.is_type(*_DATE_TYPES):
            value = _floor(value, _DAY)
        return value
    if isinstance(node, exp.CurrentDate) and node.this is None:
        return _floor(now, _DAY)
    if isinstance(node, exp.CurrentTimestamp | exp.CurrentDatetime) and node.this is None:
        return now
    if isinstance(node, _ADDS + _SUBS):
        base, shift = _time_value(node.this, now), _interval(node)
        return None if base is None or shift is None else _shift(base, *shift)
    if isinstance(node, exp.Add | exp.Sub) and isinstance(node.expression, exp.Interval):
        base = _time_value(node.this, now)
        interval = node.expression
        amount, unit = _integer(interval.this), interval.args.get("unit")
        if base is None or amount is None or not isinstance(unit, exp.Expr):
            return None
        sign = -1 if isinstance(node, exp.Sub) else 1
        return _shift(base, sign * amount, unit.name.upper())
    if isinstance(node, _TRUNCS):
        base = _time_value(node.this, now)
        step = _trunc(node.args.get("unit"))
        return None if base is None or step is None else _floor(base, step)
    if isinstance(node, exp.TsOrDsToDate) or (isinstance(node, exp.Date) and not node.expressions):
        base = _time_value(node.this, now)
        return None if base is None else _floor(base, _DAY)
    if isinstance(node, exp.Timestamp):
        return _time_value(node.this, now)
    if isinstance(node, exp.DateFromParts):
        year, month, day = (_integer(node.args.get(key)) for key in ("year", "month", "day"))
        if year is None or month is None or day is None:
            return None
        try:
            return datetime(year, month, day)
        except (ValueError, OverflowError):
            return None
    return None


def _string_value(node: exp.Expr, now: datetime) -> str | None:
    """A constant string, such as a shard suffix or FORMAT_DATE('%Y%m%d', CURRENT_DATE())."""
    node = node.unnest()
    if isinstance(node, exp.Literal) and node.is_string:
        return node.name
    if isinstance(node, exp.TimeToStr) and node.args.get("zone") is None:
        fmt = node.args.get("format")
        value = _time_value(node.this, now)
        if not isinstance(fmt, exp.Literal) or value is None or not _FORMAT.fullmatch(fmt.name):
            return None
        return value.strftime(fmt.name.replace("%F", "%Y-%m-%d"))
    return None


def _parse_time(text: str) -> datetime | None:
    """A DATE, DATETIME or TIMESTAMP literal, as BigQuery reads one, in UTC."""
    text = re.sub(r"^(\d{4}-\d{2}-\d{2})[Tt]", r"\1 ", text.strip())
    if text.upper().endswith(" UTC"):
        text = text[:-4]
    text = _SHORT_OFFSET.sub(r"\1:00", text)
    try:
        value = datetime.fromisoformat(text)
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
    except (ValueError, OverflowError):
        return None
    return value


def _interval(node: exp.Expr) -> tuple[int, str] | None:
    """The signed amount and unit of DATE_ADD, TIMESTAMP_SUB and the like."""
    interval = node.expression
    if isinstance(interval, exp.Interval):  # DATE_SUB(d, INTERVAL '1' DAY) once rendered
        amount, unit = _integer(interval.this), interval.args.get("unit")
    else:
        amount, unit = _integer(interval), node.args.get("unit")
    if amount is None or not isinstance(unit, exp.Expr):
        return None
    return (-amount if isinstance(node, _SUBS) else amount), unit.name.upper()


def _integer(node: object) -> int | None:
    """An integer literal, as in ``INTERVAL 7 DAY`` or ``INTERVAL '-7' DAY``."""
    if isinstance(node, exp.Neg):
        inner = _integer(node.this)
        return None if inner is None else -inner
    if isinstance(node, exp.Literal):
        try:
            return int(node.name)
        except ValueError:
            return None
    return None


def _like(pattern: str) -> re.Pattern[str]:
    """A LIKE pattern as a regular expression; a backslash escapes the next character."""
    parts: list[str] = []
    chars = iter(pattern)
    for char in chars:
        if char == "\\":
            parts.append(re.escape(next(chars, "\\")))
        else:
            parts.append("." if char == "_" else ".*" if char == "%" else re.escape(char))
    return re.compile("".join(parts), re.DOTALL)
