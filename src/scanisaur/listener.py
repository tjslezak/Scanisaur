"""The server side of ``scanisaur hook``: answer checks on a local socket.

Each connection sends one JSON line, ``{"v": 1, "sql": "..."}``, and gets one line back:
the full check result, or ``{"error": "..."}``. ``scanisaur serve`` runs the listener next
to its MCP session, so hooks and the agent's own tool calls share one catalog and policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
from collections.abc import AsyncIterator
from pathlib import Path

from scanisaur.catalog.source import CatalogSource
from scanisaur.engine.check import Policy, check
from scanisaur.hook import PROTOCOL_VERSION, private_dir

#: The longest request line accepted, which bounds the SQL a hook can send.
MAX_REQUEST = 8 << 20

log = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def hook_listener(
    path: Path, source: CatalogSource, policy: Policy
) -> AsyncIterator[asyncio.Server | None]:
    """Listen on ``path`` while the block runs; yields None when another server has it.

    Several MCP clients can each start ``serve`` for the same project. The first one to
    bind answers the hooks; the others serve their MCP session only.
    """
    if not hasattr(socket, "AF_UNIX"):
        yield None
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not private_dir(path.parent):
        log.warning("not answering hooks: %s isn't private to this user", path.parent)
        yield None
        return
    if await _in_use(path):
        yield None
        return
    path.unlink(missing_ok=True)  # left behind by a server that didn't shut down cleanly

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            writer.write(_respond(line, source, policy).encode() + b"\n")
            await writer.drain()
        except (OSError, ValueError) as error:  # ValueError: a line over MAX_REQUEST
            log.warning("hook request failed: %s", error)
        finally:
            writer.close()

    try:
        server = await asyncio.start_unix_server(answer, path=str(path), limit=MAX_REQUEST)
    except OSError as error:  # another server bound it first
        log.warning("not answering hooks on %s: %s", path, error)
        yield None
        return
    path.chmod(0o600)
    log.info("answering hooks on %s", path)
    try:
        yield server
    finally:
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)


def _respond(line: bytes, source: CatalogSource, policy: Policy) -> str:
    try:
        request = json.loads(line)
    except ValueError:
        return json.dumps({"error": "the request isn't JSON"})
    if not isinstance(request, dict) or request.get("v") != PROTOCOL_VERSION:
        return json.dumps({"error": f"expected protocol version {PROTOCOL_VERSION}"})
    sql = request.get("sql")
    if not isinstance(sql, str):
        return json.dumps({"error": "sql must be a string"})
    snapshot = source.current()
    result = check(sql, snapshot.catalog, policy=policy)
    return result.model_copy(update={"snapshot_id": snapshot.snapshot_id}).model_dump_json()


async def _in_use(path: Path) -> bool:
    """True when a server is answering on ``path``."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 0.1)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True
