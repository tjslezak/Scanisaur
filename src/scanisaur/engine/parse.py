"""Parse SQL and classify statements before anything is looked up in the catalog."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from scanisaur.errors import ScanisaurError

#: The SQL dialect Scanisaur checks. Table and column matching follow BigQuery's rules.
DIALECT = "bigquery"

#: ``query`` reads data; ``write`` changes data, schema or access, exports data, or runs SQL
#: that isn't visible here (CALL, EXECUTE IMMEDIATE);
#: ``other`` can't be analyzed (scripting, transactions, unsupported commands).
StatementKind = Literal["query", "write", "other"]

_QUERIES = (exp.Select, exp.SetOperation, exp.Subquery)  # a Subquery: (SELECT ...)
#: Writes whose names are checked when the policy allows writes: the query they run,
#: and an INSERT's target. Names in other writes (UPDATE, MERGE, ...) aren't checked yet.
_CHECKED_WRITES = (exp.Insert, exp.Create)
_WRITES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.TruncateTable,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Grant,
    exp.Revoke,
    exp.Export,
    exp.LoadData,
    exp.Copy,
    exp.Execute,
    exp.ExecuteSql,
)
#: sqlglot keeps statements it can't fully parse (CREATE SNAPSHOT TABLE, ALTER SCHEMA,
#: CALL, EXECUTE IMMEDIATE) as a raw Command; its first keyword says whether it can write.
#: CALL and EXECUTE IMMEDIATE run SQL that isn't visible here, so they count as writes.
_WRITE_COMMANDS = frozenset(
    {
        "ALTER",
        "CALL",
        "COPY",
        "CREATE",
        "DELETE",
        "DROP",
        "EXECUTE",
        "EXPORT",
        "GRANT",
        "INSERT",
        "LOAD",
        "MERGE",
        "RENAME",
        "REPLACE",
        "REVOKE",
        "TRUNCATE",
        "UNDROP",
        "UPDATE",
    }
)
#: Statement names where sqlglot's node name differs from the SQL keyword.
_NAMES = {
    "TRUNCATETABLE": "TRUNCATE TABLE",
    "EXPORT": "EXPORT DATA",
    "LOADDATA": "LOAD DATA",
    "TRANSACTION": "BEGIN TRANSACTION",
}
#: sqlglot error messages embed token reprs such as ``<Token token_type: ..., text: WHERE, ...>``.
_TOKEN_REPR = re.compile(r"<Token token_type: [^,]+, text: (.*?), line: .*?>")


class SqlParseError(ScanisaurError):
    def __init__(self, message: str, line: int | None = None, column: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.column = column


def parse(sql: str, dialect: str) -> list[exp.Expr]:
    """Parse SQL into statements, raising SqlParseError with the error position."""
    with _quiet_sqlglot():
        try:
            trees = sqlglot.parse(sql, read=dialect)
        except ParseError as error:
            detail: dict[str, Any] = error.errors[0] if error.errors else {}
            line, column = detail.get("line"), detail.get("col")
            highlight = detail.get("highlight") or ""
            if isinstance(column, int) and highlight:
                column -= len(highlight) - 1  # sqlglot reports the token's last column
            message = _TOKEN_REPR.sub(r"'\1'", detail.get("description", str(error)))
            raise SqlParseError(message, line, column) from error
        except TokenError as error:
            raise SqlParseError(str(error)) from error
    return [tree for tree in trees if tree is not None]


def resolvable(tree: exp.Expr) -> exp.Expr | None:
    """The part of a statement whose names are checked: a query, an INSERT or CREATE
    with the query it runs, or EXPORT DATA's query. None when names aren't checked."""
    if isinstance(tree, exp.Export):
        query = tree.this
        return query if isinstance(query, exp.Expr) else None
    if isinstance(tree, (*_QUERIES, *_CHECKED_WRITES)):
        return tree
    return None


def classify(tree: exp.Expr) -> StatementKind:
    if isinstance(tree, _QUERIES):
        return "query"
    if isinstance(tree, _WRITES):
        return "write"
    if isinstance(tree, exp.Command) and str(tree.this).upper() in _WRITE_COMMANDS:
        return "write"
    return "other"


def describe(tree: exp.Expr) -> str:
    """A short statement name for messages, such as ``INSERT`` or ``CREATE TABLE``."""
    if isinstance(tree, exp.Command):
        return str(tree.this).upper()
    name = _NAMES.get(tree.key.upper(), tree.key.upper())
    kind = tree.args.get("kind")
    if isinstance(tree, exp.Create | exp.Drop | exp.Alter) and isinstance(kind, str):
        return f"{name} {kind.upper()}"
    return name


def position(node: exp.Expr) -> tuple[int | None, int | None]:
    """The 1-based line and start column of a node in the source SQL.

    Columns point at the column name; tables at the first part of the reference.
    """
    anchor = node.parts[0] if isinstance(node, exp.Table) and node.parts else node.this
    meta: Mapping[str, Any] = anchor.meta if isinstance(anchor, exp.Expr) else node.meta
    line, end_column = meta.get("line"), meta.get("col")
    start, end = meta.get("start"), meta.get("end")
    if not isinstance(line, int) or not isinstance(end_column, int):
        return None, None
    if isinstance(start, int) and isinstance(end, int):
        return line, end_column - (end - start)  # sqlglot reports the token's last column
    return line, end_column


@contextmanager
def _quiet_sqlglot() -> Iterator[None]:
    """sqlglot logs a warning when it falls back to a generic Command; findings say it instead."""
    logger = logging.getLogger("sqlglot")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous)
