"""Minimal Language Server Protocol (LSP) server for link-coroner.

Implements just enough of the LSP for editors (VSCode, Neovim, Helix) to
underline dying links live in markdown documents:

* ``initialize`` / ``initialized`` / ``shutdown`` / ``exit`` lifecycle
* ``textDocument/didOpen`` / ``didChange`` / ``didSave`` syncs (full sync)
* ``textDocument/publishDiagnostics`` notifications (push)
* ``textDocument/hover`` — cause-of-death + Wayback suggestion popovers
* ``textDocument/codeAction`` — "Replace with Wayback snapshot" quick fix

Transport is stdio with ``Content-Length`` framing (vanilla JSON-RPC 2.0).
We intentionally do NOT depend on ``pygls`` so the package stays small;
the protocol surface we need is tiny enough to implement directly.

Probes reuse :mod:`link_coroner.forensics.probe` and the SQLite
:class:`~link_coroner.cache.ProbeCache` so the editor experience benefits
from the same forensics as the CLI.

The class is structured so tests can drive it synchronously by feeding
JSON-RPC frames into :meth:`LinkCoronerLanguageServer.dispatch` and
inspecting the captured outbound messages — no real stdio loop required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .cache import ProbeCache
from .diagnosis import cause_blurb, diagnose
from .forensics.probe import ProbeConfig, ProbeResult, Verdict, probe_urls
from .scanner.extractors import _URL_RE  # reuse the project-wide URL regex
from .wayback import WaybackSnapshot, lookup_snapshot

log = logging.getLogger("link_coroner.lsp")

# LSP DiagnosticSeverity constants (mirrors the LSP spec).
SEVERITY_ERROR = 1
SEVERITY_WARNING = 2
SEVERITY_INFORMATION = 3
SEVERITY_HINT = 4

DEFAULT_DEBOUNCE_SECONDS = 0.6


@dataclass(slots=True)
class _UrlHit:
    """One URL occurrence inside a document, with its LSP range."""

    url: str
    start_line: int
    start_char: int
    end_line: int
    end_char: int

    def to_range(self) -> dict[str, Any]:
        return {
            "start": {"line": self.start_line, "character": self.start_char},
            "end": {"line": self.end_line, "character": self.end_char},
        }


@dataclass(slots=True)
class _DocState:
    uri: str
    text: str
    version: int = 0
    last_results: dict[str, ProbeResult] = field(default_factory=dict)
    last_snapshots: dict[str, WaybackSnapshot] = field(default_factory=dict)
    last_hits: list[_UrlHit] = field(default_factory=list)
    debounce_task: asyncio.Task[None] | None = None


def find_url_hits(text: str) -> list[_UrlHit]:
    """Locate every URL in ``text`` and translate to LSP line/character ranges.

    LSP positions are 0-indexed line + UTF-16 code-unit character, but for
    pure-ASCII URLs UTF-16 == code points, so we treat character as the
    column in the line. Markdown URLs aren't typically multi-byte anyway.
    """
    hits: list[_UrlHit] = []
    # Precompute line start offsets to translate absolute matches to (line, col).
    line_starts: list[int] = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)

    def _to_pos(offset: int) -> tuple[int, int]:
        # Binary search for the line.
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo, offset - line_starts[lo]

    for match in _URL_RE.finditer(text):
        raw = match.group(0)
        start = match.start()
        # Mirror the trimming done by scanner.extractors.extract_urls so the
        # diagnostic range covers exactly the URL the prober will see.
        trimmed = raw
        while trimmed and trimmed[-1] in ".,;:!?\u2026":
            trimmed = trimmed[:-1]
        while trimmed.endswith(")") and trimmed.count("(") < trimmed.count(")"):
            trimmed = trimmed[:-1]
        if not trimmed:
            continue
        end = start + len(trimmed)
        sl, sc = _to_pos(start)
        el, ec = _to_pos(end)
        hits.append(_UrlHit(trimmed, sl, sc, el, ec))
    return hits


def severity_for(verdict: Verdict) -> int | None:
    """Map an autopsy verdict to an LSP DiagnosticSeverity, or None for ALIVE."""
    if verdict is Verdict.DEAD:
        return SEVERITY_ERROR
    if verdict is Verdict.UNREACHABLE:
        return SEVERITY_WARNING
    return None  # ALIVE — no diagnostic emitted.


def _uri_to_path(uri: str) -> Path | None:
    """Best-effort conversion from a ``file://`` URI to a :class:`Path`."""
    if not uri.startswith("file://"):
        return None
    parts = urlsplit(uri)
    return Path(unquote(parts.path))


def build_diagnostics(
    hits: Iterable[_UrlHit],
    results: dict[str, ProbeResult],
    snapshots: dict[str, WaybackSnapshot] | None = None,
) -> list[dict[str, Any]]:
    """Build the ``Diagnostic[]`` payload for a document."""
    snapshots = snapshots or {}
    diags: list[dict[str, Any]] = []
    for hit in hits:
        result = results.get(hit.url)
        if result is None:
            continue
        sev = severity_for(result.verdict)
        if sev is None:
            continue
        cause = diagnose(result)
        message_parts = [f"{cause.value}: {cause_blurb(cause)}"]
        if result.status_code is not None:
            message_parts.append(f"(HTTP {result.status_code})")
        snap = snapshots.get(hit.url)
        if snap and snap.snapshot_url:
            message_parts.append(f"Wayback: {snap.snapshot_url}")
        diags.append(
            {
                "range": hit.to_range(),
                "severity": sev,
                "source": "link-coroner",
                "code": cause.value,
                "message": " ".join(message_parts),
            }
        )
    return diags


def build_hover(
    hit: _UrlHit,
    result: ProbeResult | None,
    snapshot: WaybackSnapshot | None,
) -> dict[str, Any] | None:
    """Render a hover card (markdown) for a URL at ``hit``."""
    if result is None:
        return None
    cause = diagnose(result)
    lines = [
        f"**link-coroner** — `{hit.url}`",
        "",
        f"- **Verdict:** {result.verdict.value}",
        f"- **Cause:** {cause.value} — {cause_blurb(cause)}",
    ]
    if result.status_code is not None:
        lines.append(f"- **HTTP:** {result.status_code}")
    if snapshot and snapshot.snapshot_url:
        lines.append(f"- **Wayback:** [{snapshot.snapshot_url}]({snapshot.snapshot_url})")
    return {
        "contents": {"kind": "markdown", "value": "\n".join(lines)},
        "range": hit.to_range(),
    }


def build_code_actions(
    uri: str,
    hits: Iterable[_UrlHit],
    snapshots: dict[str, WaybackSnapshot],
    requested_range: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build "Replace with Wayback snapshot" code actions for URLs in range."""
    actions: list[dict[str, Any]] = []
    for hit in hits:
        if requested_range is not None and not _ranges_overlap(hit.to_range(), requested_range):
            continue
        snap = snapshots.get(hit.url)
        if not snap or not snap.snapshot_url:
            continue
        actions.append(
            {
                "title": f"Replace with Wayback snapshot ({snap.snapshot_url})",
                "kind": "quickfix",
                "edit": {
                    "changes": {
                        uri: [
                            {
                                "range": hit.to_range(),
                                "newText": snap.snapshot_url,
                            }
                        ]
                    }
                },
            }
        )
    return actions


def _ranges_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True if LSP ranges ``a`` and ``b`` overlap (or touch)."""

    def _pos(p: dict[str, Any]) -> tuple[int, int]:
        return p["line"], p["character"]

    a_start, a_end = _pos(a["start"]), _pos(a["end"])
    b_start, b_end = _pos(b["start"]), _pos(b["end"])
    return not (a_end < b_start or b_end < a_start)


# ---- Server ---------------------------------------------------------------------

Prober = Callable[[list[str]], Awaitable[list[ProbeResult]]]
SnapshotLookup = Callable[[str], Awaitable[WaybackSnapshot]]


async def _default_prober(urls: list[str]) -> list[ProbeResult]:
    return await probe_urls(urls, config=ProbeConfig())


class LinkCoronerLanguageServer:
    """A tiny LSP server implementing just the link-coroner surface.

    ``probe`` and ``snapshot_lookup`` are injectable so tests can replace
    networking entirely.
    """

    def __init__(
        self,
        *,
        probe: Prober | None = None,
        snapshot_lookup: SnapshotLookup | None = None,
        cache_db: Path | None = None,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        self._probe = probe or _default_prober
        self._snapshot_lookup = snapshot_lookup or lookup_snapshot
        self._cache_db = cache_db
        self._debounce = debounce_seconds
        self._docs: dict[str, _DocState] = {}
        self._sent: list[dict[str, Any]] = []  # captured outbound messages
        self._shutdown_requested = False
        self._write_lock = asyncio.Lock()
        self._writer: Callable[[bytes], Awaitable[None]] | None = None

    # ---- public test/inspection helpers -----------------------------------

    @property
    def sent_messages(self) -> list[dict[str, Any]]:
        return list(self._sent)

    @property
    def documents(self) -> dict[str, _DocState]:
        return dict(self._docs)

    def diagnostics_for(self, uri: str) -> list[dict[str, Any]]:
        """Return the most recently published diagnostics for ``uri``."""
        for msg in reversed(self._sent):
            if (
                msg.get("method") == "textDocument/publishDiagnostics"
                and msg.get("params", {}).get("uri") == uri
            ):
                return msg["params"].get("diagnostics", [])
        return []

    # ---- dispatch ---------------------------------------------------------

    async def dispatch(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one inbound JSON-RPC message and return a response (or None)."""
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}

        try:
            if method == "initialize":
                return self._ok(msg_id, self._initialize_result())
            if method == "initialized":
                return None
            if method == "shutdown":
                self._shutdown_requested = True
                return self._ok(msg_id, None)
            if method == "exit":
                return None
            if method == "textDocument/didOpen":
                await self._on_did_open(params)
                return None
            if method == "textDocument/didChange":
                await self._on_did_change(params)
                return None
            if method == "textDocument/didSave":
                await self._on_did_save(params)
                return None
            if method == "textDocument/didClose":
                self._docs.pop(params.get("textDocument", {}).get("uri", ""), None)
                return None
            if method == "textDocument/hover":
                return self._ok(msg_id, self._on_hover(params))
            if method == "textDocument/codeAction":
                return self._ok(msg_id, self._on_code_action(params))
            if msg_id is not None:
                return self._err(msg_id, -32601, f"method not found: {method}")
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("LSP handler crashed for %s", method)
            if msg_id is not None:
                return self._err(msg_id, -32603, f"internal error: {exc}")
        return None

    # ---- handlers ---------------------------------------------------------

    def _initialize_result(self) -> dict[str, Any]:
        return {
            "capabilities": {
                "textDocumentSync": {
                    "openClose": True,
                    "change": 1,  # 1 = full document sync
                    "save": {"includeText": False},
                },
                "hoverProvider": True,
                "codeActionProvider": {"codeActionKinds": ["quickfix"]},
            },
            "serverInfo": {"name": "link-coroner", "version": "0.1"},
        }

    async def _on_did_open(self, params: dict[str, Any]) -> None:
        doc = params.get("textDocument", {})
        uri = doc.get("uri", "")
        if not uri:
            return
        state = _DocState(uri=uri, text=doc.get("text", ""), version=doc.get("version", 0))
        self._docs[uri] = state
        await self._schedule_probe(state)

    async def _on_did_change(self, params: dict[str, Any]) -> None:
        td = params.get("textDocument", {})
        uri = td.get("uri", "")
        state = self._docs.get(uri)
        if state is None:
            return
        state.version = td.get("version", state.version + 1)
        # Full-sync only: take the last content change's text.
        changes = params.get("contentChanges") or []
        if changes:
            state.text = changes[-1].get("text", state.text)
        await self._schedule_probe(state)

    async def _on_did_save(self, params: dict[str, Any]) -> None:
        uri = params.get("textDocument", {}).get("uri", "")
        state = self._docs.get(uri)
        if state is None:
            return
        # Force-refresh on save (bypass debounce).
        if state.debounce_task and not state.debounce_task.done():
            state.debounce_task.cancel()
        await self._run_probe_cycle(state)

    def _on_hover(self, params: dict[str, Any]) -> dict[str, Any] | None:
        uri = params.get("textDocument", {}).get("uri", "")
        state = self._docs.get(uri)
        if state is None:
            return None
        position = params.get("position") or {}
        line = position.get("line", -1)
        char = position.get("character", -1)
        for hit in state.last_hits:
            if hit.start_line == line and hit.start_char <= char <= hit.end_char:
                result = state.last_results.get(hit.url)
                snap = state.last_snapshots.get(hit.url)
                return build_hover(hit, result, snap)
        return None

    def _on_code_action(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        uri = params.get("textDocument", {}).get("uri", "")
        state = self._docs.get(uri)
        if state is None:
            return []
        rng = params.get("range")
        return build_code_actions(uri, state.last_hits, state.last_snapshots, rng)

    # ---- probe pipeline ---------------------------------------------------

    async def _schedule_probe(self, state: _DocState) -> None:
        if state.debounce_task and not state.debounce_task.done():
            state.debounce_task.cancel()
        state.debounce_task = asyncio.create_task(self._debounced_probe(state))

    async def _debounced_probe(self, state: _DocState) -> None:
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return
        await self._run_probe_cycle(state)

    async def _run_probe_cycle(self, state: _DocState) -> None:
        hits = find_url_hits(state.text)
        state.last_hits = hits
        urls = list({h.url for h in hits})
        if not urls:
            await self._publish_diagnostics(state.uri, [])
            return
        try:
            results = await self._probe(urls)
        except Exception:
            log.exception("probe failed for %s", state.uri)
            return
        results_by_url = {r.url: r for r in results}
        state.last_results = results_by_url

        if self._cache_db is not None:
            try:
                file_label = str(_uri_to_path(state.uri) or state.uri)
                url_to_file = {r.url: file_label for r in results}
                with ProbeCache(self._cache_db) as cache:
                    cache.record_probe_results(results, url_to_file=url_to_file)
            except Exception:  # pragma: no cover - cache errors shouldn't kill LSP
                log.exception("cache write failed")

        # Fetch wayback snapshots for non-alive URLs (best-effort).
        dead_urls = [r.url for r in results if r.verdict is not Verdict.ALIVE]
        snapshots: dict[str, WaybackSnapshot] = {}
        for url in dead_urls:
            try:
                snap = await self._snapshot_lookup(url)
            except Exception:
                continue
            if snap.snapshot_url:
                snapshots[url] = snap
        state.last_snapshots = snapshots

        diagnostics = build_diagnostics(hits, results_by_url, snapshots)
        await self._publish_diagnostics(state.uri, diagnostics)

    # ---- transport / framing ---------------------------------------------

    async def _publish_diagnostics(self, uri: str, diagnostics: list[dict[str, Any]]) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": diagnostics},
            }
        )

    async def _send(self, message: dict[str, Any]) -> None:
        self._sent.append(message)
        if self._writer is None:
            return
        body = json.dumps(message).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        async with self._write_lock:
            await self._writer(frame)

    def _ok(self, msg_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _err(self, msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }

    # ---- stdio loop -------------------------------------------------------

    async def serve_stdio(self) -> None:  # pragma: no cover - I/O loop
        """Run the LSP server over stdio. Blocks until ``exit`` is received."""
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

        writer_transport, writer_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout.buffer
        )
        writer = asyncio.StreamWriter(writer_transport, writer_protocol, None, loop)

        async def _write(buf: bytes) -> None:
            writer.write(buf)
            await writer.drain()

        self._writer = _write

        while True:
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if not line:
                    return
                if line in (b"\r\n", b"\n"):
                    break
                if b":" in line:
                    k, _, v = line.partition(b":")
                    headers[k.decode("ascii").strip().lower()] = v.decode("ascii").strip()
            length = int(headers.get("content-length", "0"))
            if length <= 0:
                continue
            body = await reader.readexactly(length)
            try:
                message = json.loads(body)
            except json.JSONDecodeError:
                continue
            response = await self.dispatch(message)
            if response is not None:
                await self._send(response)


async def run_stdio(cache_db: Path | None = None) -> None:  # pragma: no cover - I/O loop
    """Entry-point used by the ``link-coroner lsp`` CLI."""
    server = LinkCoronerLanguageServer(cache_db=cache_db)
    await server.serve_stdio()
