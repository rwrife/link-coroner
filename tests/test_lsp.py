"""Tests for the LSP server (link_coroner.lsp)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from link_coroner.cache import ProbeCache
from link_coroner.forensics.probe import ProbeResult, Verdict
from link_coroner.lsp import (
    LinkCoronerLanguageServer,
    build_code_actions,
    build_diagnostics,
    build_hover,
    find_url_hits,
    severity_for,
)
from link_coroner.wayback import WaybackSnapshot


def test_find_url_hits_locates_position():
    text = "intro line\nsee http://dead.example/path for details\nand https://ok.example."
    hits = find_url_hits(text)
    urls = [h.url for h in hits]
    assert urls == ["http://dead.example/path", "https://ok.example"]
    assert hits[0].start_line == 1
    assert hits[0].start_char == 4
    assert hits[0].end_char == 4 + len("http://dead.example/path")
    assert hits[1].start_line == 2


def test_severity_mapping():
    assert severity_for(Verdict.DEAD) == 1
    assert severity_for(Verdict.UNREACHABLE) == 2
    assert severity_for(Verdict.ALIVE) is None


def test_build_diagnostics_skips_alive_and_includes_wayback():
    text = "a http://dead.example/x b https://ok.example c"
    hits = find_url_hits(text)
    results = {
        "http://dead.example/x": ProbeResult(
            "http://dead.example/x", Verdict.DEAD, "HTTP_404", status_code=404
        ),
        "https://ok.example": ProbeResult(
            "https://ok.example", Verdict.ALIVE, "HTTP_200", status_code=200
        ),
    }
    snaps = {
        "http://dead.example/x": WaybackSnapshot(
            url="http://dead.example/x",
            snapshot_url="https://web.archive.org/web/2020/http://dead.example/x",
            timestamp="20200101000000",
        )
    }
    diags = build_diagnostics(hits, results, snaps)
    assert len(diags) == 1
    diag = diags[0]
    assert diag["severity"] == 1
    assert diag["source"] == "link-coroner"
    assert diag["code"] == "HTTP_4XX"
    assert "Wayback" in diag["message"]
    assert diag["range"]["start"]["character"] == 2


def test_build_hover_and_code_actions():
    text = "see http://dead.example end"
    hits = find_url_hits(text)
    hit = hits[0]
    result = ProbeResult("http://dead.example", Verdict.DEAD, "HTTP_404", status_code=404)
    snap = WaybackSnapshot(
        url="http://dead.example",
        snapshot_url="https://web.archive.org/web/2021/http://dead.example",
        timestamp="20210101000000",
    )
    hover = build_hover(hit, result, snap)
    assert hover is not None
    assert "Wayback" in hover["contents"]["value"]
    assert hover["range"]["start"]["line"] == 0

    actions = build_code_actions("file:///x.md", [hit], {hit.url: snap})
    assert len(actions) == 1
    edit = actions[0]["edit"]["changes"]["file:///x.md"][0]
    assert edit["newText"] == snap.snapshot_url
    assert edit["range"] == hit.to_range()

    # Out-of-range query yields nothing.
    no_actions = build_code_actions(
        "file:///x.md",
        [hit],
        {hit.url: snap},
        requested_range={
            "start": {"line": 5, "character": 0},
            "end": {"line": 5, "character": 1},
        },
    )
    assert no_actions == []


def _make_server(snapshot_url: str | None = None) -> LinkCoronerLanguageServer:
    async def fake_probe(urls: list[str]) -> list[ProbeResult]:
        out: list[ProbeResult] = []
        for u in urls:
            if "dead" in u:
                out.append(ProbeResult(u, Verdict.DEAD, "HTTP_404", status_code=404))
            else:
                out.append(ProbeResult(u, Verdict.ALIVE, "HTTP_200", status_code=200))
        return out

    async def fake_snapshot(url: str) -> WaybackSnapshot:
        return WaybackSnapshot(
            url=url,
            snapshot_url=snapshot_url,
            timestamp="20240101000000" if snapshot_url else None,
        )

    return LinkCoronerLanguageServer(
        probe=fake_probe,
        snapshot_lookup=fake_snapshot,
        debounce_seconds=0.0,
    )


def test_initialize_handshake_advertises_capabilities():
    server = _make_server()
    response = asyncio.run(
        server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    )
    assert response is not None
    caps = response["result"]["capabilities"]
    assert caps["hoverProvider"] is True
    assert caps["codeActionProvider"]["codeActionKinds"] == ["quickfix"]
    assert caps["textDocumentSync"]["change"] == 1


def test_did_open_publishes_diagnostics():
    server = _make_server(snapshot_url="https://web.archive.org/web/2024/http://dead.example")

    async def scenario():
        await server.dispatch(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        await server.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": "file:///doc.md",
                        "languageId": "markdown",
                        "version": 1,
                        "text": "Check http://dead.example and https://alive.example.",
                    }
                },
            }
        )
        state = server.documents["file:///doc.md"]
        if state.debounce_task is not None:
            await state.debounce_task

    asyncio.run(scenario())
    diags = server.diagnostics_for("file:///doc.md")
    assert len(diags) == 1
    assert diags[0]["severity"] == 1
    assert "Wayback" in diags[0]["message"]


def test_hover_returns_payload_for_dead_link():
    server = _make_server(snapshot_url="https://web.archive.org/web/2024/http://dead.example")

    async def scenario():
        await server.dispatch(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        await server.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": "file:///hover.md",
                        "languageId": "markdown",
                        "version": 1,
                        "text": "see http://dead.example end",
                    }
                },
            }
        )
        state = server.documents["file:///hover.md"]
        if state.debounce_task is not None:
            await state.debounce_task
        return await server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "textDocument/hover",
                "params": {
                    "textDocument": {"uri": "file:///hover.md"},
                    "position": {"line": 0, "character": 6},
                },
            }
        )

    response = asyncio.run(scenario())
    assert response is not None
    assert response["result"] is not None
    payload = response["result"]
    assert payload["contents"]["kind"] == "markdown"
    assert "http://dead.example" in payload["contents"]["value"]


def test_did_save_writes_to_cache(tmp_path: Path):
    db = tmp_path / "cache.sqlite"

    async def fake_probe(urls):
        return [ProbeResult(u, Verdict.DEAD, "HTTP_404", status_code=404) for u in urls]

    async def fake_snapshot(url):
        return WaybackSnapshot(url=url, snapshot_url=None, timestamp=None)

    server = LinkCoronerLanguageServer(
        probe=fake_probe,
        snapshot_lookup=fake_snapshot,
        cache_db=db,
        debounce_seconds=0.0,
    )

    async def scenario():
        await server.dispatch(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        await server.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": "file:///save.md",
                        "languageId": "markdown",
                        "version": 1,
                        "text": "ref http://dead.example/x",
                    }
                },
            }
        )
        await server.dispatch(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didSave",
                "params": {"textDocument": {"uri": "file:///save.md"}},
            }
        )

    asyncio.run(scenario())
    with ProbeCache(db) as cache:
        events = cache.all_events()
    assert any(e.url == "http://dead.example/x" for e in events)


def test_shutdown_and_unknown_method():
    server = _make_server()
    shutdown = asyncio.run(
        server.dispatch({"jsonrpc": "2.0", "id": 5, "method": "shutdown"})
    )
    assert shutdown is not None
    assert shutdown["result"] is None

    err = asyncio.run(
        server.dispatch({"jsonrpc": "2.0", "id": 6, "method": "textDocument/foo"})
    )
    assert err is not None and err["error"]["code"] == -32601
