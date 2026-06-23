"""Tests for the MCP server wrapper."""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from link_coroner.forensics.probe import ProbeConfig, ProbeResult, Verdict
from link_coroner.mcp_server import (
    PROTOCOL_VERSION,
    SERVER_NAME,
    TOOL_DEFS,
    MCPServer,
)
from link_coroner.wayback import WaybackSnapshot

# ---- helpers ---------------------------------------------------------------------


def _alive_result(url: str = "https://example.com/", status: int = 200) -> ProbeResult:
    return ProbeResult(
        url=url,
        verdict=Verdict.ALIVE,
        reason=f"HTTP_{status}",
        status_code=status,
        elapsed_ms=12,
        final_url=url,
    )


def _dead_result(url: str = "https://gone.example/", status: int = 404) -> ProbeResult:
    return ProbeResult(
        url=url,
        verdict=Verdict.DEAD,
        reason=f"HTTP_{status}",
        status_code=status,
        elapsed_ms=22,
        final_url=url,
    )


def _make_server(
    probe_results: list[ProbeResult] | None = None,
    snapshots: dict[str, WaybackSnapshot] | None = None,
) -> MCPServer:
    async def fake_probe(urls, cfg: ProbeConfig):
        if probe_results is None:
            return [_alive_result(u) for u in urls]
        # Map by URL if possible.
        by_url = {r.url: r for r in probe_results}
        out = []
        for u in urls:
            if u in by_url:
                out.append(by_url[u])
            else:
                out.append(probe_results[0])
        return out

    async def fake_resurrect(urls):
        if snapshots is not None:
            return snapshots
        return {u: WaybackSnapshot(url=u, snapshot_url=None, timestamp=None) for u in urls}

    return MCPServer(probe=fake_probe, resurrect=fake_resurrect)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---- tool definitions ------------------------------------------------------------


def test_tool_defs_are_well_formed():
    names = {t["name"] for t in TOOL_DEFS}
    assert names == {"autopsy_url", "autopsy_urls", "find_replacement"}
    for tool in TOOL_DEFS:
        assert "description" in tool and tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema


# ---- initialize / tools list -----------------------------------------------------


def test_initialize_handshake():
    server = _make_server()
    resp = _run(
        server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
    )
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert resp["result"]["serverInfo"]["name"] == SERVER_NAME
    assert "tools" in resp["result"]["capabilities"]


def test_tools_list_returns_all_tools():
    server = _make_server()
    resp = _run(
        server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    )
    tools = resp["result"]["tools"]
    assert {t["name"] for t in tools} == {"autopsy_url", "autopsy_urls", "find_replacement"}


def test_notifications_return_none():
    server = _make_server()
    resp = _run(
        server.handle_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
    )
    assert resp is None


# ---- tools/call --------------------------------------------------------------


def test_autopsy_url_success():
    server = _make_server(probe_results=[_alive_result("https://example.com/")])
    resp = _run(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "autopsy_url",
                    "arguments": {"url": "https://example.com/"},
                },
            }
        )
    )
    assert resp["result"]["isError"] is False
    payload = resp["result"]["structuredContent"]
    assert payload["url"] == "https://example.com/"
    assert payload["verdict"] == "ALIVE"
    assert payload["cause"] == "ALIVE"
    assert payload["is_alive"] is True
    # Also serialised in text content for clients that don't parse structured.
    text = resp["result"]["content"][0]["text"]
    assert "ALIVE" in text


def test_autopsy_url_dead_reports_cause():
    server = _make_server(probe_results=[_dead_result("https://gone.example/")])
    resp = _run(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "autopsy_url",
                    "arguments": {"url": "https://gone.example/"},
                },
            }
        )
    )
    payload = resp["result"]["structuredContent"]
    assert payload["verdict"] == "DEAD"
    assert payload["cause"] == "HTTP_4XX"
    assert payload["is_alive"] is False
    assert payload["cause_blurb"]


def test_autopsy_url_requires_url():
    server = _make_server()
    resp = _run(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "autopsy_url", "arguments": {}},
            }
        )
    )
    assert "error" in resp
    assert resp["error"]["code"] == -32602


def test_autopsy_urls_batch():
    results = [
        _alive_result("https://a.example/"),
        _dead_result("https://b.example/"),
    ]
    server = _make_server(probe_results=results)
    resp = _run(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "autopsy_urls",
                    "arguments": {
                        "urls": ["https://a.example/", "https://b.example/"],
                    },
                },
            }
        )
    )
    payload = resp["result"]["structuredContent"]
    assert payload["total"] == 2
    assert payload["dead"] == 1
    assert {r["url"] for r in payload["results"]} == {
        "https://a.example/",
        "https://b.example/",
    }


def test_autopsy_urls_validates_input():
    server = _make_server()
    resp = _run(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "autopsy_urls", "arguments": {"urls": []}},
            }
        )
    )
    assert resp["error"]["code"] == -32602


def test_find_replacement_returns_snapshot():
    snap = WaybackSnapshot(
        url="https://gone.example/",
        snapshot_url="https://web.archive.org/web/20240101/https://gone.example/",
        timestamp="20240101000000",
        time_of_death="2024-01-01T00:00:00",
    )
    server = _make_server(snapshots={"https://gone.example/": snap})
    resp = _run(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "find_replacement",
                    "arguments": {"url": "https://gone.example/"},
                },
            }
        )
    )
    payload = resp["result"]["structuredContent"]
    assert payload["found"] is True
    assert payload["snapshot_url"].startswith("https://web.archive.org/")
    assert payload["time_of_death"] == "2024-01-01T00:00:00"


def test_find_replacement_no_snapshot_found():
    server = _make_server(
        snapshots={"https://nope.example/": WaybackSnapshot(
            url="https://nope.example/", snapshot_url=None, timestamp=None
        )}
    )
    resp = _run(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "find_replacement",
                    "arguments": {"url": "https://nope.example/"},
                },
            }
        )
    )
    payload = resp["result"]["structuredContent"]
    assert payload["found"] is False
    assert payload["snapshot_url"] is None


def test_unknown_tool_returns_error():
    server = _make_server()
    resp = _run(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "nope", "arguments": {}},
            }
        )
    )
    assert "error" in resp
    assert "unknown tool" in resp["error"]["message"]


def test_unknown_method_returns_error():
    server = _make_server()
    resp = _run(
        server.handle_request({"jsonrpc": "2.0", "id": 11, "method": "no/such"})
    )
    assert resp["error"]["code"] == -32601


# ---- stream serve loop -----------------------------------------------------------


class _MemoryWriter:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, data: bytes) -> None:
        self.buffer.write(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    def output(self) -> str:
        return self.buffer.getvalue().decode("utf-8")


def test_serve_handles_multiple_requests():
    server = _make_server(probe_results=[_alive_result("https://x.example/")])

    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(
            (
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
                + "\n"
                + json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "autopsy_url",
                            "arguments": {"url": "https://x.example/"},
                        },
                    }
                )
                + "\n"
                + "\n"  # blank line ignored
            ).encode("utf-8")
        )
        reader.feed_eof()
        writer = _MemoryWriter()
        await server.serve(reader, writer)  # type: ignore[arg-type]
        return writer.output()

    output = _run(go())
    lines = [json.loads(line) for line in output.splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["result"]["serverInfo"]["name"] == SERVER_NAME
    assert lines[1]["result"]["structuredContent"]["url"] == "https://x.example/"


def test_serve_handles_parse_error():
    server = _make_server()

    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(b"not json\n")
        reader.feed_eof()
        writer = _MemoryWriter()
        await server.serve(reader, writer)  # type: ignore[arg-type]
        return writer.output()

    output = _run(go())
    line = json.loads(output.strip())
    assert line["error"]["code"] == -32700


@pytest.mark.parametrize(
    "tool,args",
    [
        ("autopsy_url", {"url": ""}),
        ("autopsy_urls", {"urls": ["", ""]}),
        ("find_replacement", {"url": ""}),
    ],
)
def test_empty_string_rejected(tool, args):
    server = _make_server()
    resp = _run(
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args},
            }
        )
    )
    assert "error" in resp
