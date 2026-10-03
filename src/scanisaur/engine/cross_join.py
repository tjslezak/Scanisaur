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

from scanisaur.engine.estimate import table_rows
from scanisaur.engine.facts import DerivedSource, Product, QueryFacts, TableFacts
from scanisaur.engine.result import Finding, Severity
from scanisaur.engine.rules import CROSS_JOIN

#: Rows of one group of sources: at most this many (None: any number), and whether that
#: count is the real one rather than an upper bound.
_Size = tuple[int | None, bool]


def cross_join_findings(
    facts: QueryFacts, now: datetime, *, warn_pairs: int, block_pairs: int
) -> list[Finding]:
    """One finding per SELECT whose sources nothing connects. It blocks from
    ``block_pairs`` pairs when every side's size is known, and otherwise warns from
    ``warn_pairs``, or whenever a side may hold any number of rows."""
    findings: dict[str, Finding] = {}
    for product in facts.products:
        finding = _finding(product, now, warn_pairs, block_pairs)
        if finding is not None:
            findings.setdefault(finding.message, finding)
    return list(findings.values())


def _finding(product: Product, now: datetime, warn_pairs: int, block_pairs: int) -> Finding | None:
    sizes = [_group_size(group, product, now) for group in product.groups]
    bounds = [bound for bound, _exact in sizes]
    pairs = math.prod(b for b in bounds if b is not None) if None not in bounds else None
    exact = all(is_exact for _bound, is_exact in sizes)
    if pairs is not None and pairs < warn_pairs:
        return None  # a small product, such as a cross join with a short date list
    severity = (
        Severity.BLOCK if exact and pairs is not None and pairs >= block_pairs else Severity.WARN
    )
    line, column = product.position
    if line is None:
        line, column = next(
            (m.position for g in product.groups for m in g if m.position[0] is not None),
            (None, None),
        )
    return Finding(
        rule=CROSS_JOIN,
        severity=severity,
        message=_message(product, sizes, pairs, exact),
        fix=_fix(product),
        line=line,
        column=column,
    )


def _group_size(
    group: tuple[TableFacts | DerivedSource, ...], product: Product, now: datetime
) -> _Size:
    sizes = [_member_size(member, product, now) for member in group]
    if len(sizes) == 1:
        return sizes[0]
    # Joins inside the group shape its rows, so its size is never exact.
    bounds = [bound for bound, _exact in sizes]
    return (None if None in bounds else max(b for b in bounds if b is not None)), False


def _member_size(member: TableFacts | DerivedSource, product: Product, now: datetime) -> _Size:
    if isinstance(member, DerivedSource):
        return member.rows, False  # a LIMIT bounds the rows; it doesn't give them
    # A comparison with another group, as in `a.ts < b.ts`, filters pairs, not this
    # source's rows.
    relating = {column for alias, column in product.relating if alias == member.alias}
    rows = table_rows(replace(member, linked=member.linked - relating), now)
    return (None, False) if rows is None else rows


def _message(product: Product, sizes: list[_Size], pairs: int | None, exact: bool) -> str:
    names = [_describe(group) for group in product.groups]
    simple = all(len(group) == 1 for group in product.groups)
    if product.inequality is not None:
        condition = f"`{product.inequality}`, which isn't an equality"
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
        unknown = [
            name for name, (bound, _exact) in zip(names, sizes, strict=True) if bound is None
        ]
        amount = f"how many isn't known, as {_join_words(unknown)} may hold any number of rows"
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
    return name[:-1] if name.endswith("s") else name


def _describe(group: tuple[TableFacts | DerivedSource, ...]) -> str:
    names = [_name(member) for member in group]
    return names[0] if len(names) == 1 else f"the join of {_join_words(names)}"


def _name(member: TableFacts | DerivedSource) -> str:
    if isinstance(member, DerivedSource):
        # sqlglot names a subquery written without an alias `_0`, `_1`, ...
        return "a subquery" if member.alias.startswith("_") else f"`{member.alias}`"
    table = member.table.name
    return f"`{member.alias}`" if member.alias == table else f"`{member.alias}` ({table})"


def _join_words(words: list[str], conjunction: str = "and") -> str:
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + f" {conjunction} " + words[-1]


def _count(n: int) -> str:
    """A count in words: 3.1 billion."""
    for size, word in (
        (10**15, "quadrillion"),
        (10**12, "trillion"),
        (10**9, "billion"),
        (10**6, "million"),
    ):
        if n >= size:
            return f"{n / size:.1f}".removesuffix(".0") + f" {word}"
    return f"{n:,}"
