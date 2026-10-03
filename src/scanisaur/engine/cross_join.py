"""SCN006: joins that pair every row of one side with every row of the other.

A join with nothing relating its sides, or only a comparison that isn't an equality,
makes BigQuery pair every row of one with every row of the other. The bill doesn't show
it: bytes billed stay the same, while slot time grows by about 10 slot-seconds per
billion pairs (measured in issue #23). Counts and sums over the result are multiplied,
and without an aggregate BigQuery fails with "Response too large to return".
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime
from typing import NamedTuple, assert_never

from scanisaur.engine.estimate import table_rows
from scanisaur.engine.facts import DerivedSource, Product, QueryFacts, TableFacts
from scanisaur.engine.result import Finding, Severity
from scanisaur.engine.rules import CROSS_JOIN

_Member = TableFacts | DerivedSource


class _Size(NamedTuple):
    """Rows of one group of sources."""

    #: At most this many; None when it may be any number.
    rows: int | None
    #: True when ``rows`` is the count itself rather than an upper bound.
    exact: bool


def cross_join_findings(
    facts: QueryFacts,
    now: datetime,
    *,
    warn_pairs: int,
    block_pairs: int,
    sampled: frozenset[str] = frozenset(),
) -> list[Finding]:
    """One finding per SELECT whose sources nothing connects. It blocks from
    ``block_pairs`` pairs when every side's size is known, and otherwise warns from
    ``warn_pairs``, or whenever a side may hold any number of rows. ``sampled`` names the
    tables read with TABLESAMPLE, whose rows are a fraction of their count."""
    findings: dict[tuple[str, int | None, int | None], Finding] = {}
    for product in facts.products:
        finding = _finding(product, now, warn_pairs, block_pairs, sampled)
        if finding is not None:  # several visits of one SELECT give one finding
            findings.setdefault((finding.message, finding.line, finding.column), finding)
    return list(findings.values())


def _finding(
    product: Product, now: datetime, warn_pairs: int, block_pairs: int, sampled: frozenset[str]
) -> Finding | None:
    sizes = [_group_size(group, product, now, sampled) for group in product.groups]
    bounds = [size.rows for size in sizes]
    pairs = None if None in bounds else math.prod(b for b in bounds if b is not None)
    exact = all(size.exact for size in sizes)
    if pairs is not None and pairs < warn_pairs:
        return None  # a small product, such as a cross join with a short date list
    blocks = exact and pairs is not None and pairs >= block_pairs and product.limit is None
    line, column = product.position
    if line is None:
        line, column = next(
            (m.position for g in product.groups for m in g if m.position[0] is not None),
            (None, None),
        )
    return Finding(
        rule=CROSS_JOIN,
        severity=Severity.BLOCK if blocks else Severity.WARN,
        message=_message(product, sizes, pairs, exact),
        fix=_fix(product),
        line=line,
        column=column,
    )


def _group_size(
    group: tuple[_Member, ...], product: Product, now: datetime, sampled: frozenset[str]
) -> _Size:
    if len(group) > 1:
        # The joins inside the group decide its rows: a key that isn't unique on either
        # side can give more rows than any member has.
        return _Size(None, exact=False)
    match group[0]:
        case DerivedSource(rows=rows):
            return _Size(rows, exact=False)  # a LIMIT bounds the rows; it doesn't give them
        case TableFacts(alias=alias, table=table) as member:
            if alias in product.flattened:
                return _Size(None, exact=False)  # UNNEST repeats or drops its rows
            # The conditions relating sources filter pairs, not this table's rows; what
            # else limits them, product.limited says.
            measured = table_rows(replace(member, linked=frozenset()), now)
            if measured is None:
                return _Size(None, exact=False)
            count, known = measured
            limited = alias in product.limited or table.qualified_name in sampled
            return _Size(count, exact=known and not limited)
        case other:
            assert_never(other)


def _message(product: Product, sizes: list[_Size], pairs: int | None, exact: bool) -> str:
    names = [_describe(group) for group in product.groups]
    simple = all(len(group) == 1 for group in product.groups)
    if product.inequality is not None:
        condition = f"`{product.inequality}`, which isn't an equality between two sources"
        if simple:
            opening = f"{_join_words(names)} are joined only by {condition}"
        else:
            opening = f"{names[1]} is joined to {names[0]} only by {condition}"
        opening += ", so BigQuery compares every pair"
    else:
        if simple:
            opening = f"{_join_words(names)} are joined with nothing relating them"
        elif len(names) == 2:
            opening = f"Nothing relates {names[1]} to {names[0]}"
        else:
            opening = f"Nothing relates {_join_words(names, 'or')} to one another"
        if len(names) == 2:
            opening += ", so every row of one pairs with every row of the other"
        else:
            opening += ", so every row of each pairs with every row of the others"
    if pairs is not None and exact:
        amount = f"about {_count(pairs)} pairs"
    elif pairs is not None:
        amount = f"up to about {_count(pairs)} pairs, fewer if filters or joins leave fewer rows"
    else:
        unknown = [name for name, size in zip(names, sizes, strict=True) if size.rows is None]
        amount = f"how many isn't known, as {_join_words(unknown)} may hold any number of rows"
    if product.limit is not None:
        return (
            f"{opening}: {amount}. The LIMIT {product.limit} stops BigQuery early, so compute "
            f"stays small, but the rows it returns are arbitrary pairs."
        )
    return (
        f"{opening}: {amount}. Bytes billed don't grow, but compute does, and counts or "
        "sums over the result are multiplied."
    )


def _fix(product: Product) -> str:
    key = _suggested_key(product)
    example = f", for example `{key}`" if key else ""
    if product.inequality is not None:
        return (
            f"Add an equality between the two sides alongside the comparison{example}, so "
            "BigQuery matches rows by it before comparing."
        )
    return (
        f"Add the condition that relates them{example}. If every pair is intended, keep "
        "one side small, such as a single-row subquery or a short list of dates."
    )


def _suggested_key(product: Product) -> str | None:
    """A join key the column names point to between the first two groups: a
    ``<table>_id`` column on one side and ``id`` on the other, or a shared ``*_id``."""
    first, second = product.groups[0], product.groups[1]
    for a in (m for m in first if isinstance(m, TableFacts)):
        for b in (m for m in second if isinstance(m, TableFacts)):
            for x, y in ((a, b), (b, a)):
                x_columns, y_columns = _columns(x), _columns(y)
                foreign = f"{_singular(y.table.name)}_id"
                if foreign in x_columns and "id" in y_columns:
                    return f"{x.alias}.{x_columns[foreign]} = {y.alias}.{y_columns['id']}"
            shared = sorted(n for n in _columns(a) if n.endswith("_id") and n in _columns(b))
            if shared:
                name = shared[0]
                return f"{a.alias}.{_columns(a)[name]} = {b.alias}.{_columns(b)[name]}"
    return None


def _columns(member: TableFacts) -> dict[str, str]:
    return {column.name.lower(): column.name for column in member.table.columns}


def _singular(name: str) -> str:
    name = name.lower()
    if name.endswith("ies"):
        return name[:-3] + "y"
    return name.removesuffix("s")


def _describe(group: tuple[_Member, ...]) -> str:
    names = [_name(member) for member in group]
    return names[0] if len(names) == 1 else f"the join of {_join_words(names)}"


def _name(member: _Member) -> str:
    match member:
        case DerivedSource(alias=alias) if alias.startswith("_"):
            return "a subquery"  # sqlglot names a subquery written without an alias `_0`
        case DerivedSource(alias=alias):
            return f"`{alias}`"
        case TableFacts(alias=alias, table=table) if alias == table.name:
            return f"`{alias}`"
        case TableFacts(alias=alias, table=table):
            return f"`{alias}` ({table.name})"
        case other:
            assert_never(other)


def _join_words(words: list[str], conjunction: str = "and") -> str:
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + f" {conjunction} " + words[-1]


_UNITS = ((10**6, "million"), (10**9, "billion"), (10**12, "trillion"), (10**15, "quadrillion"))


def _count(n: int) -> str:
    """A count in words, as 3.1 billion, moving up a unit when rounding reaches 1,000."""
    if n < 10**6:
        return f"{n:,}"
    for size, word in _UNITS:
        value = round(n / size, 1)
        if value < 1000 or word == _UNITS[-1][1]:
            return f"{value:.1f}".removesuffix(".0") + f" {word}"
    raise AssertionError("unreachable")  # pragma: no cover
