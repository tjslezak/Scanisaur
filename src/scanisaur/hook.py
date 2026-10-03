"""``scanisaur hook``: check the SQL in an agent's tool call before the tool runs it.

An agent harness runs this command before each matching tool call and passes the call as
JSON on standard input. The SQL goes to a running ``scanisaur serve`` over a local socket,
which answers in about a millisecond. With no server, the check runs here instead, which
costs the engine's import time. If that fails too, the call goes ahead with a warning:
Scanisaur being down never stops an agent's work.

This module imports only the standard library until it needs the engine, because the
harness waits for it on every call.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shlex
import socket
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

#: Bump on any change to the request or response a client could depend on.
PROTOCOL_VERSION = 1
#: The same name :data:`scanisaur.config.CONFIG_FILE` holds; that module is too slow to import.
CONFIG_FILE = "scanisaur.yaml"
#: Tool names that carry SQL, as shell-style patterns, unless --tool says otherwise.
DEFAULT_TOOLS = ("*execute_sql*",)
#: Claude Code's tool for shell commands; a ``bq query`` command in it is checked too.
SHELL_TOOLS = ("Bash",)
#: Argument names that hold the SQL of a matching tool.
SQL_ARGUMENTS = ("sql", "query")
#: The characters shlex splits out as shell operators.
_SHELL_OPERATOR_CHARS = frozenset("();<>|&")
CONNECT_TIMEOUT = 0.05
READ_TIMEOUT = 2.0
#: The longest response line accepted from the server.
MAX_RESPONSE = 1 << 20

JsonObject = dict[str, Any]
#: The agent harness a hook answers.
HookFormat = Literal["claude", "cursor"]


class FindingJson(TypedDict):
    """A finding as the check result's JSON carries it (see ``engine.result.Finding``)."""

    rule: str
    severity: str
    message: str
    fix: NotRequired[str | None]
    line: NotRequired[int | None]
    column: NotRequired[int | None]


class CheckJson(TypedDict):
    """The parts of a check result's JSON a hook reads. The pydantic model isn't used
    here because importing it would slow every hook call."""

    verdict: Literal["pass", "warn", "block"]
    findings: list[FindingJson]
    #: The catalog snapshot the check ran against; set by ``scanisaur serve``.
    snapshot_id: NotRequired[str | None]


def socket_path(catalog: Path, config: Path | None) -> Path:
    """Where ``serve`` listens for the given catalog and policy file.

    ``serve`` and ``hook`` run with the same arguments, so they compute the same path;
    any other combination gets its own server. The hash keeps the path short: macOS
    allows 104 bytes.
    """
    key = f"{catalog.resolve()}\0{'' if config is None else config.resolve()}"
    name = hashlib.sha256(key.encode()).hexdigest()[:16] + ".sock"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "scanisaur" / name
    user = os.getuid() if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"scanisaur-{user}" / name


def policy_file(config: Path | None) -> Path | None:
    """The policy file in effect: the one given, else ``scanisaur.yaml`` here, if any."""
    if config is None and Path(CONFIG_FILE).is_file():
        return Path(CONFIG_FILE)
    return config


def extract_sql(call: JsonObject, tools: tuple[str, ...] = DEFAULT_TOOLS) -> str | None:
    """The SQL in a tool call, or None when there is none.

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
            return None
    if name is None and isinstance(call.get("command"), str):
        return bq_query_sql(call["command"])
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return None
    if name in SHELL_TOOLS:
        command = arguments.get("command")
        return bq_query_sql(command) if isinstance(command, str) else None
    if not any(fnmatch.fnmatchcase(name, pattern) for pattern in tools):
        return None
    for key in SQL_ARGUMENTS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def sql_from(sql: str = "", command: str = "") -> str | None:
    """The SQL handed to the ``scanisaur_hook`` MCP tool: as is, or from a shell command.

    An argument the hook's template couldn't fill arrives empty or as ``${...}``.
    """
    if sql.strip() and not _unfilled(sql):
        return sql
    if command.strip() and not _unfilled(command):
        return bq_query_sql(command)
    return None


def _unfilled(value: str) -> bool:
    value = value.strip()
    return value.startswith("${") and value.endswith("}")


def bq_query_sql(command: str) -> str | None:
    """The SQL of a ``bq query 'SELECT ...'`` command: its last argument that isn't a flag.

    Only the words of the ``bq`` command itself count, up to a pipe, ``&&``, ``;`` or
    redirect. Commands this can't read, such as SQL piped in from a file, aren't checked.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:  # an unclosed quote
        return None
    for segment in _commands(words):
        names = [Path(word).name for word in segment]
        if "bq" in names and "query" in segment[names.index("bq") + 1 :]:
            rest = segment[segment.index("query", names.index("bq") + 1) + 1 :]
            positional = [word for word in rest if not word.startswith("-")]
            return positional[-1] if positional else None
    return None


def _commands(words: list[str]) -> list[list[str]]:
    """Split shell words into simple commands at operators such as ``|``, ``&&`` and ``>``.

    A redirect's target, the word after ``>`` or ``<``, is dropped with it.
    """
    commands: list[list[str]] = [[]]
    skip_next = False
    for word in words:
        if skip_next:
            skip_next = False
        elif word and set(word) <= _SHELL_OPERATOR_CHARS:
            skip_next = word[0] in "<>"
            commands.append([])
        else:
            commands[-1].append(word)
    return commands


def private_dir(path: Path) -> bool:
    """True when ``path`` is a directory of the current user that no one else can use.

    The socket's directory can be in the shared temp directory. If another user made it
    first, they could answer checks with their own verdicts, so it isn't trusted.
    """
    try:
        info = path.stat()
    except OSError:
        return False
    owned = hasattr(os, "getuid") and info.st_uid == os.getuid()
    return owned and stat.S_ISDIR(info.st_mode) and info.st_mode & 0o077 == 0


def request_check(path: Path, sql: str) -> CheckJson | None:
    """Ask the server at ``path``; None when no server answers."""
    if not hasattr(socket, "AF_UNIX") or not private_dir(path.parent):
        return None  # Windows has no local sockets in v0.1
    request = json.dumps({"v": PROTOCOL_VERSION, "sql": sql}).encode() + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(CONNECT_TIMEOUT)
            client.connect(str(path))
            client.settimeout(READ_TIMEOUT)
            client.sendall(request)
            response = _read_line(client)
    except OSError:  # includes timeouts and a missing socket file
        return None
    try:
        answer = json.loads(response)
    except ValueError:
        return None
    if not isinstance(answer, dict) or answer.get("verdict") not in ("pass", "warn", "block"):
        return None
    return cast(CheckJson, answer)


def _read_line(client: socket.socket) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while size < MAX_RESPONSE:
        chunk = client.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if chunk.endswith(b"\n"):
            break
    return b"".join(chunks)


def check_here(sql: str, catalog: Path, config: Path | None) -> CheckJson:
    """Check without a server. Imports the engine, so it costs a few hundred ms."""
    from scanisaur.catalog.fixtures import load_catalog
    from scanisaur.config import load_policy
    from scanisaur.engine.check import DEFAULT_POLICY, check

    policy = DEFAULT_POLICY if config is None else load_policy(config)
    result = check(sql, load_catalog(catalog), policy=policy)
    return cast(CheckJson, result.model_dump(mode="json"))


def claude_output(result: CheckJson) -> JsonObject | None:
    """Claude Code's ``PreToolUse`` answer for a check result.

    Pass says nothing. Warn adds the findings to the agent's context. Block denies the
    call, with the findings and fixes as the reason. Neither ever answers "allow", which
    would skip the user's own permission rules.
    """
    verdict = result.get("verdict")
    if verdict == "block":
        decision = {"permissionDecision": "deny", "permissionDecisionReason": _explain(result)}
    elif verdict == "warn":
        decision = {"additionalContext": _explain(result)}
    else:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", **decision}}


def cursor_output(result: CheckJson) -> JsonObject | None:
    """Cursor's answer for a check result: deny on block, findings for the agent on warn."""
    verdict = result.get("verdict")
    if verdict == "block":
        reason = _explain(result)
        return {"permission": "deny", "user_message": reason, "agent_message": reason}
    if verdict == "warn":
        return {"permission": "allow", "agent_message": _explain(result)}
    return None


def unchecked_output(reason: str, output_format: HookFormat = "claude") -> JsonObject:
    """The answer when the SQL couldn't be checked: let it run, and say so."""
    context = f"Scanisaur couldn't check this SQL ({reason}), so it ran unchecked."
    if output_format == "cursor":
        return {"permission": "allow", "agent_message": context}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": context}}


def _explain(result: CheckJson) -> str:
    findings = result.get("findings") or []
    lines = [f"Scanisaur: {result.get('verdict')}, {len(findings)} finding(s)."]
    for finding in findings:
        line = f"- {finding.get('rule')} ({finding.get('severity')}): {finding.get('message')}"
        if finding.get("fix"):
            line += f" Fix: {finding['fix']}"
        lines.append(line)
    if result.get("verdict") == "block":
        lines.append("Change the SQL as the fixes say, then run it again.")
    return "\n".join(lines)


def adk_callback(
    catalog: Path, config: Path | None = None, tools: tuple[str, ...] = DEFAULT_TOOLS
) -> Callable[[Any, dict[str, Any], Any], JsonObject | None]:
    """A Google ADK ``before_tool_callback`` that checks SQL tool calls.

    On block, it returns the findings as the tool's result, so the tool doesn't run and
    the agent reads the fixes. Otherwise it returns None and the tool runs. ADK can't
    attach findings to a call it lets run, so warnings aren't shown.
    """
    config = policy_file(config)
    path = socket_path(catalog, config)

    def before_tool_callback(
        tool: Any, args: dict[str, Any], tool_context: Any
    ) -> JsonObject | None:
        sql = extract_sql({"tool_name": getattr(tool, "name", None), "tool_input": args}, tools)
        if sql is None:
            return None
        result = request_check(path, sql)
        if result is None:
            try:
                result = check_here(sql, catalog, config)
            except (OSError, ValueError) as error:
                import logging  # only on this path: the hook command doesn't need it

                logging.getLogger(__name__).warning("SQL ran unchecked: %s", error)
                return None  # fail open, as the command does
        return {"error": _explain(result)} if result.get("verdict") == "block" else None

    return before_tool_callback


def main(argv: list[str] | None = None) -> int:
    """Read one tool call from stdin and print the harness's answer. Always exits 0: a
    decision is in the JSON, so a crash here can never block the agent."""
    parser = argparse.ArgumentParser(
        prog="scanisaur hook",
        description="Check the SQL in an agent's tool call. Give the same --catalog and "
        "--config as `scanisaur serve`.",
    )
    parser.add_argument("--catalog", "-c", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--format",
        choices=("claude", "cursor"),
        default="claude",
        help="The harness that runs the hook (default: claude, for Claude Code).",
    )
    parser.add_argument(
        "--tool",
        action="append",
        help=f"Tool name pattern that carries SQL (default: {', '.join(DEFAULT_TOOLS)}).",
    )
    args = parser.parse_args(argv)

    try:
        call = json.load(sys.stdin)
    except ValueError as error:
        print(f"scanisaur hook: input isn't JSON: {error}", file=sys.stderr)
        return 0
    sql = extract_sql(call, tuple(args.tool or DEFAULT_TOOLS)) if isinstance(call, dict) else None
    if sql is None:
        return 0

    config = policy_file(args.config)
    result = request_check(socket_path(args.catalog, config), sql)
    if result is None:
        try:
            result = check_here(sql, args.catalog, config)
        except (OSError, ValueError) as error:  # a missing or bad catalog or policy file
            print(f"scanisaur hook: {error}", file=sys.stderr)
            print(json.dumps(unchecked_output(str(error), args.format)))
            return 0
    output = cursor_output(result) if args.format == "cursor" else claude_output(result)
    if output is not None:
        print(json.dumps(output))
    return 0
