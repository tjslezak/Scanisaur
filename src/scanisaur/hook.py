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
import sys
import tempfile
from pathlib import Path
from typing import Any

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
CONNECT_TIMEOUT = 0.05
READ_TIMEOUT = 2.0
#: The longest response line accepted from the server.
MAX_RESPONSE = 1 << 20

JsonObject = dict[str, Any]


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
    """The SQL in a Claude Code ``PreToolUse`` payload, or None when there is none."""
    name = call.get("tool_name")
    arguments = call.get("tool_input")
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


def bq_query_sql(command: str) -> str | None:
    """The SQL of a ``bq query 'SELECT ...'`` command: its last argument that isn't a flag.

    Commands this can't read, such as SQL piped in from a file, aren't checked.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for i, word in enumerate(words[:-1]):
        if Path(word).name == "bq" and "query" in words[i + 1 :]:
            rest = words[words.index("query", i + 1) + 1 :]
            positional = [w for w in rest if not w.startswith("-")]
            return positional[-1] if positional else None
    return None


def request_check(path: Path, sql: str) -> JsonObject | None:
    """Ask the server at ``path``; None when no server answers."""
    if not hasattr(socket, "AF_UNIX"):
        return None  # Windows: no local sockets in v0.1
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
    return answer if isinstance(answer, dict) and "verdict" in answer else None


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


def check_here(sql: str, catalog: Path, config: Path | None) -> JsonObject:
    """Check without a server. Imports the engine, so it costs a few hundred ms."""
    from scanisaur.catalog.fixtures import load_catalog
    from scanisaur.config import load_policy
    from scanisaur.engine.check import DEFAULT_POLICY, check

    policy = DEFAULT_POLICY if config is None else load_policy(config)
    result: JsonObject = check(sql, load_catalog(catalog), policy=policy).model_dump(mode="json")
    return result


def claude_output(result: JsonObject) -> JsonObject | None:
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


def unchecked_output(reason: str) -> JsonObject:
    """The answer when the SQL couldn't be checked: let it run, and say so."""
    context = f"Scanisaur couldn't check this SQL ({reason}), so it ran unchecked."
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": context}}


def _explain(result: JsonObject) -> str:
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
            print(json.dumps(unchecked_output(str(error))))
            return 0
    output = claude_output(result)
    if output is not None:
        print(json.dumps(output))
    return 0
