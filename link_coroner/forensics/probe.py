"""Basic autopsy — DNS + HTTP probes that classify each URL.

M2 scope: produce an ``ALIVE | DEAD | UNREACHABLE`` verdict for each URL,
plus a short reason string. Fancy cause taxonomy + death certificates land
in M3; we deliberately keep this module small and replaceable.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

import httpx

from .soft404 import analyze_content, is_html

try:  # dnspython is optional at runtime — we degrade gracefully in tests.
    import dns.asyncresolver  # type: ignore[import-untyped]
    import dns.exception  # type: ignore[import-untyped]
    import dns.resolver  # type: ignore[import-untyped]

    _HAVE_DNS = True
except ImportError:  # pragma: no cover - exercised only without dnspython
    _HAVE_DNS = False


class Verdict(StrEnum):
    """Autopsy verdict — kept intentionally small for M2."""

    ALIVE = "ALIVE"
    DEAD = "DEAD"
    UNREACHABLE = "UNREACHABLE"


@dataclass(slots=True)
class ProbeResult:
    url: str
    verdict: Verdict
    reason: str
    status_code: int | None = None
    elapsed_ms: int | None = None
    final_url: str | None = None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["verdict"] = self.verdict.value
        return data


@dataclass(slots=True)
class ProbeConfig:
    concurrency: int = 16
    per_host_concurrency: int = 4
    timeout: float = 10.0
    user_agent: str = "link-coroner/0.1 (+https://github.com/rwrife/link-coroner)"
    verify_tls: bool = True
    follow_redirects: bool = True
    dns_timeout: float = 3.0
    # Status codes we treat as "alive" even though they're not 2xx. 401/403
    # mean the server is up and answering; for M2 that's enough.
    alive_status_codes: frozenset[int] = field(
        default_factory=lambda: frozenset({401, 403, 405, 429})
    )
    # M4: inspect body of HTML 2xx responses to catch soft-404 / parked pages.
    detect_soft_404: bool = True
    # Cap body read so giant HTML pages don't slow us down.
    max_body_bytes: int = 64 * 1024


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


async def _resolve_dns(host: str, timeout: float) -> tuple[bool, str]:
    """Return ``(ok, reason)``. ``ok=False`` means we should mark UNREACHABLE."""
    if not host or not _HAVE_DNS:
        # No host / no resolver available — let httpx try and decide.
        return True, ""
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = timeout
    resolver.timeout = timeout
    for rdtype in ("A", "AAAA"):
        try:
            await resolver.resolve(host, rdtype)
            return True, ""
        except dns.resolver.NXDOMAIN:
            return False, "NXDOMAIN"
        except dns.resolver.NoAnswer:
            continue
        except (dns.resolver.NoNameservers, dns.exception.Timeout):
            return False, "DNS_TIMEOUT"
        except dns.exception.DNSException as exc:  # pragma: no cover - defensive
            return False, f"DNS_ERROR:{exc.__class__.__name__}"
    return False, "DNS_NO_ANSWER"


def _classify_status(status: int, alive_codes: frozenset[int]) -> tuple[Verdict, str]:
    if 200 <= status < 400:
        return Verdict.ALIVE, f"HTTP_{status}"
    if status in alive_codes:
        return Verdict.ALIVE, f"HTTP_{status}"
    if 400 <= status < 600:
        return Verdict.DEAD, f"HTTP_{status}"
    return Verdict.UNREACHABLE, f"HTTP_{status}"  # pragma: no cover - very rare


async def _probe_one(
    url: str,
    client: httpx.AsyncClient,
    cfg: ProbeConfig,
    host_locks: dict[str, asyncio.Semaphore],
    global_sem: asyncio.Semaphore,
) -> ProbeResult:
    host = _host_of(url)
    if not host:
        return ProbeResult(url, Verdict.UNREACHABLE, "BAD_URL")

    async with global_sem:
        host_sem = host_locks.setdefault(host, asyncio.Semaphore(cfg.per_host_concurrency))
        async with host_sem:
            ok, reason = await _resolve_dns(host, cfg.dns_timeout)
            if not ok:
                return ProbeResult(url, Verdict.UNREACHABLE, reason)

            loop = asyncio.get_event_loop()
            started = loop.time()
            try:
                response = await client.head(url)
                if response.status_code in (405, 501) or (
                    400 <= response.status_code < 500 and response.status_code != 404
                ):
                    # Some servers refuse HEAD — retry with GET to be fair.
                    response = await client.get(url)
            except httpx.TimeoutException:
                return ProbeResult(url, Verdict.UNREACHABLE, "TIMEOUT")
            except httpx.TooManyRedirects:
                return ProbeResult(url, Verdict.DEAD, "REDIRECT_LOOP")
            except httpx.ConnectError as exc:
                return ProbeResult(url, Verdict.UNREACHABLE, f"CONN_ERROR:{exc}")
            except httpx.HTTPError as exc:
                return ProbeResult(url, Verdict.UNREACHABLE, f"HTTP_ERROR:{exc.__class__.__name__}")

            elapsed_ms = int((loop.time() - started) * 1000)
            verdict, reason = _classify_status(response.status_code, cfg.alive_status_codes)
            final_url = str(response.url) if str(response.url) != url else None

            # M4: peek at HTML bodies of 2xx responses to catch soft-404 / parked pages.
            if (
                cfg.detect_soft_404
                and verdict is Verdict.ALIVE
                and 200 <= response.status_code < 300
            ):
                soft = await _sniff_content(url, response, client, cfg)
                if soft is not None:
                    verdict = Verdict.UNREACHABLE
                    reason = soft

            return ProbeResult(
                url=url,
                verdict=verdict,
                reason=reason,
                status_code=response.status_code,
                elapsed_ms=elapsed_ms,
                final_url=final_url,
            )


async def _sniff_content(
    url: str,
    response: httpx.Response,
    client: httpx.AsyncClient,
    cfg: ProbeConfig,
) -> str | None:
    """Return a soft-404 / parked ``reason`` string, or ``None`` if the body is fine.

    ``response`` may be from a HEAD request (no body). If the content-type looks
    like HTML we issue a follow-up GET capped at ``cfg.max_body_bytes``.
    """
    content_type = response.headers.get("content-type")
    final_url = str(response.url)

    # Cheap parked-host check on the redirect target — no body needed.
    quick = analyze_content(b"", content_type=None, final_url=final_url)
    if quick.suspicious:
        return quick.reason

    if not is_html(content_type):
        return None

    body: str | bytes
    if response.request is not None and response.request.method.upper() == "GET":
        # We already have the body (HEAD fell back to GET upstream).
        body = response.content[: cfg.max_body_bytes] if response.content else b""
    else:
        try:
            follow = await client.get(url, headers={"Range": f"bytes=0-{cfg.max_body_bytes - 1}"})
        except httpx.HTTPError:
            return None
        body = follow.content[: cfg.max_body_bytes] if follow.content else b""
        content_type = follow.headers.get("content-type", content_type)
        final_url = str(follow.url)

    verdict = analyze_content(body, content_type=content_type, final_url=final_url)
    return verdict.reason if verdict.suspicious else None


async def probe_urls(
    urls: Iterable[str],
    *,
    config: ProbeConfig | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[ProbeResult]:
    """Probe an iterable of URLs concurrently and return one result per URL.

    Order matches input order. Duplicate URLs are de-duplicated; the same
    ``ProbeResult`` is returned for each occurrence.
    """
    cfg = config or ProbeConfig()
    # Preserve input order while de-duping.
    ordered: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        ordered.append(url)

    if not ordered:
        return []

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            timeout=cfg.timeout,
            follow_redirects=cfg.follow_redirects,
            verify=cfg.verify_tls,
            headers={"User-Agent": cfg.user_agent, "Accept": "*/*"},
            http2=False,
        )

    global_sem = asyncio.Semaphore(cfg.concurrency)
    host_locks: dict[str, asyncio.Semaphore] = defaultdict(
        lambda: asyncio.Semaphore(cfg.per_host_concurrency)
    )

    try:
        tasks = [
            asyncio.create_task(_probe_one(url, client, cfg, host_locks, global_sem))
            for url in ordered
        ]
        results = await asyncio.gather(*tasks)
    finally:
        if owns_client:
            await client.aclose()

    return list(results)
