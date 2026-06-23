"""MCP (Model Context Protocol) server wrapper for link-coroner.

Exposes the autopsy + Wayback resurrection surface so AI agents can ask
"is this URL alive, and what should I cite instead?" inline during text
generation.

The server speaks line-delimited JSON-RPC 2.0 over a pair of streams
(stdin/stdout by default). It implements the small subset of MCP that
matters for tool use:

* ``initialize``  — handshake + capability advertisement.
* ``tools/list``  — describe the available tools.
* ``tools/call``  — invoke a tool by name with JSON arguments.

We intentionally do not pull in the official MCP SDK: keeping this
dependency-free means it works in tiny CI shells and is trivial to test.

All probe / wayback callables are injectable so tests can stub them
without touching the network.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from . import __version__
from .diagnosis import Cause, cause_blurb, diagnose
from .forensics.probe import ProbeConfig, ProbeResult, probe_urls
from .wayback import WaybackSnapshot, resurrect_many

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "link-coroner"

ProbeFn = Callable[[Iterable[str], ProbeConfig], Awaitable[list[ProbeResult]]]
ResurrectFn = Callable[[Iterable[str]], Awaitable[dict[str, WaybackSnapshot]]]


# ---- tool definitions ------------------------------------------------------------

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "autopsy_url",
        "description": (
            "Autopsy a single URL: classify it as ALIVE / DEAD / UNREACHABLE, "
            "name the cause of death, and report the HTTP status. Use this "
            "before citing a URL in generated text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to autopsy."},
                "timeout": {
                    "type": "number",
                    "description": "Probe timeout in seconds (default 10).",
                    "minimum": 0.1,
                    "maximum": 60,
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "autopsy_urls",
        "description": (
            "Autopsy a batch of URLs concurrently. Returns one verdict per URL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 256,
                    "description": "URLs to autopsy.",
                },
                "concurrency": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 64,
                    "description": "Concurrent probe workers (default 16).",
                },
                "timeout": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 60,
                },
            },
            "required": ["urls"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_replacement",
        "description": (
            "Ask the Wayback Machine for the closest archived snapshot of a "
            "URL — useful when autopsy reports the URL is dead and you need "
            "something to cite instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "include_time_of_death": {
                    "type": "boolean",
                    "description": "Also estimate when the URL stopped working.",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
]


# ---- result shaping --------------------------------------------------------------


def _probe_payload(result: ProbeResult) -> dict[str, Any]:
    cause = diagnose(result)
    data = result.to_dict()
    data["cause"] = cause.value
    data["cause_blurb"] = cause_blurb(cause)
    data["is_alive"] = cause is Cause.ALIVE
    return data


def _snapshot_payload(snap: WaybackSnapshot) -> dict[str, Any]:
    data = snap.to_dict()
    data["found"] = snap.snapshot_url is not None
    return data


# ---- dispatcher ------------------------------------------------------------------


@dataclass(slots=True)
class MCPServer:
    """Minimal MCP server with injectable probe + wayback callables."""

    probe: ProbeFn | None = None
    resurrect: ResurrectFn | None = None

    async def _do_probe(self, urls: list[str], cfg: ProbeConfig) -> list[ProbeResult]:
        if self.probe is not None:
            return await self.probe(urls, cfg)
        return await probe_urls(urls, config=cfg)

    async def _do_resurrect(self, urls: list[str]) -> dict[str, WaybackSnapshot]:
        if self.resurrect is not None:
            return await self.resurrect(urls)
        return await resurrect_many(urls)

    # -- tool implementations -------------------------------------------------

    async def tool_autopsy_url(self, args: dict[str, Any]) -> dict[str, Any]:
        url = args.get("url")
        if not isinstance(url, str) or not url:
            raise _ToolError("`url` must be a non-empty string.")
        timeout = float(args.get("timeout") or 10.0)
        cfg = ProbeConfig(timeout=timeout, concurrency=1, per_host_concurrency=1)
        results = await self._do_probe([url], cfg)
        if not results:
            raise _ToolError("probe returned no results")
        return _probe_payload(results[0])

    async def tool_autopsy_urls(self, args: dict[str, Any]) -> dict[str, Any]:
        urls = args.get("urls")
        if not isinstance(urls, list) or not urls or not all(
            isinstance(u, str) and u for u in urls
        ):
            raise _ToolError("`urls` must be a non-empty list of strings.")
        concurrency = int(args.get("concurrency") or 16)
        timeout = float(args.get("timeout") or 10.0)
        cfg = ProbeConfig(concurrency=concurrency, timeout=timeout)
        results = await self._do_probe(list(urls), cfg)
        payloads = [_probe_payload(r) for r in results]
        dead = sum(1 for p in payloads if not p["is_alive"])
        return {
            "results": payloads,
            "total": len(payloads),
            "dead": dead,
        }

    async def tool_find_replacement(self, args: dict[str, Any]) -> dict[str, Any]:
        url = args.get("url")
        if not isinstance(url, str) or not url:
            raise _ToolError("`url` must be a non-empty string.")
        snaps = await self._do_resurrect([url])
        snap = snaps.get(url) or WaybackSnapshot(url=url, snapshot_url=None, timestamp=None)
        return _snapshot_payload(snap)

    # -- request dispatch -----------------------------------------------------

    async def handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Handle a single JSON-RPC request and return the response (or ``None`` for notifications)."""
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}
        is_notification = "id" not in request

        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": __version__},
                }
            elif method in {"notifications/initialized", "initialized"}:
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOL_DEFS}
            elif method == "tools/call":
                result = await self._call_tool(params)
            elif method == "shutdown":
                result = None
            else:
                if is_notification:
                    return None
                return _error_response(req_id, -32601, f"method not found: {method}")
        except _ToolError as exc:
            if is_notification:
                return None
            return _error_response(req_id, -32602, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            if is_notification:
                return None
            return _error_response(req_id, -32603, f"internal error: {exc}")

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise _ToolError("`arguments` must be an object.")
        handler = {
            "autopsy_url": self.tool_autopsy_url,
            "autopsy_urls": self.tool_autopsy_urls,
            "find_replacement": self.tool_find_replacement,
        }.get(name)
        if handler is None:
            raise _ToolError(f"unknown tool: {name!r}")
        payload = await handler(args)
        text = json.dumps(payload, indent=2, sort_keys=True)
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": payload,
            "isError": False,
        }

    # -- stream loop ----------------------------------------------------------

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read newline-delimited JSON-RPC requests from ``reader`` and write
        responses to ``writer`` until EOF."""
        while True:
            line = await reader.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue
            try:
                request = json.loads(line_str)
            except json.JSONDecodeError as exc:
                response = _error_response(None, -32700, f"parse error: {exc}")
            else:
                if not isinstance(request, dict):
                    response = _error_response(None, -32600, "invalid request")
                else:
                    response = await self.handle_request(request)
            if response is None:
                continue
            writer.write((json.dumps(response) + "\n").encode("utf-8"))
            try:
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                break


class _ToolError(Exception):
    """Raised when an MCP tool call has bad arguments."""


def _error_response(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


# ---- stdio entry-point -----------------------------------------------------------


async def _stdio_streams() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    write_transport, write_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    writer = asyncio.StreamWriter(write_transport, write_protocol, None, loop)
    return reader, writer


async def run_stdio(server: MCPServer | None = None) -> None:
    """Run the MCP server bound to stdin/stdout (the standard MCP transport)."""
    srv = server or MCPServer()
    reader, writer = await _stdio_streams()
    try:
        await srv.serve(reader, writer)
    finally:
        try:
            writer.close()
        except Exception:  # pragma: no cover
            pass
