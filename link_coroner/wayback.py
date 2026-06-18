"""Wayback Machine resurrection helpers (M5).

For every deceased URL, we'd like to:

1. Ask the Wayback Availability API for the closest snapshot.
2. (Optionally) bisect history to estimate the *time of death* — the
   point at which the last successful snapshot gave way to failure.
3. Surface a "resurrect" URL on the death certificate.
4. Let users patch dead URLs in-place (``link-coroner rewrite``).

The networking surface is intentionally tiny and fully injectable so the
unit tests can stub it via ``respx`` without touching the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime

import httpx

AVAILABILITY_API = "https://archive.org/wayback/available"
CDX_API = "https://web.archive.org/cdx/search/cdx"

DEFAULT_USER_AGENT = "link-coroner/0.1 (+https://github.com/rwrife/link-coroner)"


@dataclass(slots=True)
class WaybackSnapshot:
    """A single archived snapshot for a URL."""

    url: str
    """Original URL we asked about."""
    snapshot_url: str | None
    """Closest archived URL (``https://web.archive.org/web/...``), if any."""
    timestamp: str | None
    """Snapshot ``YYYYMMDDhhmmss`` timestamp, if any."""
    time_of_death: str | None = None
    """Estimated time of death (ISO-8601) from bisection, when computed."""

    @property
    def archived_at(self) -> datetime | None:
        if not self.timestamp:
            return None
        try:
            return datetime.strptime(self.timestamp, "%Y%m%d%H%M%S")
        except ValueError:
            return None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        archived = self.archived_at
        data["archived_at_iso"] = archived.isoformat() if archived else None
        return data


def _parse_availability(payload: object, url: str) -> WaybackSnapshot:
    """Pull the closest snapshot out of an Availability API response."""
    if not isinstance(payload, dict):
        return WaybackSnapshot(url=url, snapshot_url=None, timestamp=None)
    snapshots = payload.get("archived_snapshots") or {}
    closest = snapshots.get("closest") if isinstance(snapshots, dict) else None
    if not isinstance(closest, dict) or not closest.get("available"):
        return WaybackSnapshot(url=url, snapshot_url=None, timestamp=None)
    return WaybackSnapshot(
        url=url,
        snapshot_url=closest.get("url"),
        timestamp=closest.get("timestamp"),
    )


async def lookup_snapshot(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 8.0,
) -> WaybackSnapshot:
    """Query the Wayback Availability API for the closest snapshot of ``url``."""
    own_client = client is None
    client = client or httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": DEFAULT_USER_AGENT},
    )
    try:
        try:
            resp = await client.get(AVAILABILITY_API, params={"url": url})
        except httpx.HTTPError:
            return WaybackSnapshot(url=url, snapshot_url=None, timestamp=None)
        if resp.status_code != 200:
            return WaybackSnapshot(url=url, snapshot_url=None, timestamp=None)
        try:
            payload = resp.json()
        except ValueError:
            return WaybackSnapshot(url=url, snapshot_url=None, timestamp=None)
        return _parse_availability(payload, url)
    finally:
        if own_client:
            await client.aclose()


async def estimate_time_of_death(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 8.0,
    limit: int = 50,
) -> str | None:
    """Estimate the URL's time of death.

    Strategy (cheap, no real bisection on the live web): pull the most
    recent ``limit`` CDX entries and return the timestamp of the last
    one whose ``statuscode`` was 2xx/3xx. That's a strong proxy for the
    moment the URL stopped working.
    """
    own_client = client is None
    client = client or httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": DEFAULT_USER_AGENT},
    )
    try:
        params = {
            "url": url,
            "output": "json",
            "fl": "timestamp,statuscode",
            "limit": str(-abs(limit)),  # negative = newest first
        }
        try:
            resp = await client.get(CDX_API, params=params)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        try:
            rows = resp.json()
        except ValueError:
            return None
        if not isinstance(rows, list) or len(rows) < 2:
            return None
        # First row is the header.
        body = rows[1:]
        # Walk newest -> oldest until we find a healthy snapshot.
        for row in body:
            if not isinstance(row, list) or len(row) < 2:
                continue
            ts, status = str(row[0]), str(row[1])
            if status.startswith(("2", "3")):
                try:
                    return datetime.strptime(ts, "%Y%m%d%H%M%S").isoformat()
                except ValueError:
                    return None
        return None
    finally:
        if own_client:
            await client.aclose()


async def resurrect_many(
    urls: Iterable[str],
    *,
    concurrency: int = 8,
    timeout: float = 8.0,
    include_time_of_death: bool = True,
    client: httpx.AsyncClient | None = None,
) -> dict[str, WaybackSnapshot]:
    """Look up snapshots (and optional TOD) for a batch of URLs concurrently."""
    urls = list(dict.fromkeys(urls))  # dedupe, preserve order
    if not urls:
        return {}

    sem = asyncio.Semaphore(concurrency)
    own_client = client is None
    client = client or httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": DEFAULT_USER_AGENT},
    )

    async def _one(u: str) -> tuple[str, WaybackSnapshot]:
        async with sem:
            snap = await lookup_snapshot(u, client=client, timeout=timeout)
            if include_time_of_death and snap.snapshot_url:
                snap.time_of_death = await estimate_time_of_death(
                    u, client=client, timeout=timeout
                )
            return u, snap

    try:
        pairs = await asyncio.gather(*[_one(u) for u in urls])
        return dict(pairs)
    finally:
        if own_client:
            await client.aclose()
