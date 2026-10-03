"""Find the SQL in an agent's tool call: a SQL tool's argument, or ``bq query`` commands
in a shell command.

Standard library only, like :mod:`scanisaur.hook`, which imports it on every hook call.
"""

from __future__ import annotations

import fnmatch
import json
import shlex
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]

#: Tool names that carry SQL, as shell-style patterns, unless --tool says otherwise.
DEFAULT_TOOLS = ("*execute_sql*",)
#: Claude Code's tool for shell commands; a ``bq query`` command in it is checked too.
SHELL_TOOLS = ("Bash",)
#: Argument names that hold the SQL of a matching tool.
SQL_ARGUMENTS = ("sql", "query")
#: The characters shlex splits out as shell operators.
_SHELL_OPERATOR_CHARS = frozenset("();<>|&")
#: ``bq query`` flags that take a value as the next word, as in ``--format json``.
_BQ_VALUE_FLAGS = frozenset(
    {
        "format", "n", "max_rows", "location", "project_id", "dataset_id",
        "destination_table", "parameter", "label", "job_id", "maximum_bytes_billed",
        "start_row", "connection_property", "reservation_id",
    }
)  # fmt: skip


def extract_sql(call: JsonObject, tools: tuple[str, ...] = DEFAULT_TOOLS) -> list[str]:
    """The SQL in a tool call: one query, several from a shell command, or none.

    Reads Claude Code's ``PreToolUse`` payload and Cursor's ``beforeMCPExecution`` and
    ``beforeShellExecution`` payloads: a tool name with its arguments (an object, or a
    JSON string of one), or a bare shell ``command``.
    """
    name = call.get("tool_name")
    arguments = call.get("tool_input")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return []
    if name is None and isinstance(call.get("command"), str):
        return bq_queries(call["command"])
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return []
    if name in SHELL_TOOLS:
        command = arguments.get("command")
        return bq_queries(command) if isinstance(command, str) else []
    if not any(fnmatch.fnmatchcase(name, pattern) for pattern in tools):
        return []
    for key in SQL_ARGUMENTS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return [value]
    return []


def sql_from(sql: str = "", command: str = "") -> list[str]:
    """The SQL handed to the ``scanisaur_hook`` MCP tool: as is, or from a shell command.

    An argument the hook's template couldn't fill arrives empty or as ``${...}``.
    """
    if sql.strip() and not _unfilled(sql):
        return [sql]
    if command.strip() and not _unfilled(command):
        return bq_queries(command)
    return []


def _unfilled(value: str) -> bool:
    value = value.strip()
    return value.startswith("${") and value.endswith("}")


def bq_queries(command: str) -> list[str]:
    """The SQL of each ``bq query 'SELECT ...'`` in a shell command line, in order.

    Only the words of each ``bq`` command count, up to a pipe, ``&&``, ``;`` or redirect.
    Commands this can't read, such as SQL piped in from a file, aren't checked.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:  # an unclosed quote
        return []
    return [sql for segment in _commands(words) if (sql := _bq_query(segment))]


def _bq_query(words: list[str]) -> str | None:
    """The SQL of one simple command, if it is ``bq ... query ... SQL``."""
    names = [Path(word).name for word in words]
    if "bq" not in names or "query" not in words[names.index("bq") + 1 :]:
        return None
    rest = iter(words[words.index("query", names.index("bq") + 1) + 1 :])
    positional = []
    for word in rest:
        if word.lstrip("-") in _BQ_VALUE_FLAGS:
            next(rest, None)  # the flag's value, as in ``--format json``
        elif not word.startswith("-"):
            positional.append(word)
    # A query is one quoted word with spaces in it; bq joins unquoted words into one.
    quoted = [word for word in positional if any(char.isspace() for char in word)]
    return quoted[-1] if quoted else " ".join(positional) or None


def _commands(words: list[str]) -> list[list[str]]:
    """Split shell words into simple commands at operators such as ``|``, ``&&`` and ``>``.

    A redirect's target, the word after ``>`` or ``<``, is dropped with it, and so is the
    file descriptor before it, as in ``2>&1``.
    """
    commands: list[list[str]] = [[]]
    skip_next = False
    for word in words:
        if skip_next:
            skip_next = False
        elif word and set(word) <= _SHELL_OPERATOR_CHARS:
            skip_next = word[0] in "<>"
            if skip_next and commands[-1] and commands[-1][-1].isdigit():
                commands[-1].pop()
            commands.append([])
        else:
            commands[-1].append(word)
    return commands
