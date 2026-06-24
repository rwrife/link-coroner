"""Live-site URL discovery via sitemap.xml + robots.txt.

When `link-coroner scan --site https://example.com` is invoked, we
discover URLs from the deployed site rather than the local filesystem.

Discovery order:
1. Fetch ``<site>/robots.txt`` and parse Disallow rules + Sitemap: hints
   + Crawl-delay. Robots failure is non-fatal — we proceed with no rules.
2. Try each ``Sitemap:`` hint, then ``<site>/sitemap.xml``. Recurse into
   sitemap-index documents. URLs blocked by robots are dropped.
3. If no sitemap entries are discovered, fall back to a one-hop crawl
   of the homepage: extract same-host ``<a href>`` links.

The discovered URLs are returned in document order, deduped, and
ready to be handed to the existing autopsy probe pipeline.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

# Cap how many sitemap documents we'll fetch in one run (DoS guard).
_MAX_SITEMAPS = 50
# Cap how many URLs we'll surface from one site.
_MAX_URLS = 5000

_HREF_RE = re.compile(r"""<a\s[^>]*href\s*=\s*["']([^"'#]+)["']""", re.IGNORECASE)
_SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_IS_SITEMAP_INDEX_RE = re.compile(r"<sitemapindex\b", re.IGNORECASE)


@dataclass(slots=True)
class RobotsRules:
    """Parsed subset of robots.txt that we care about."""

    disallowed: tuple[str, ...] = ()
    crawl_delay: float | None = None
    sitemaps: tuple[str, ...] = ()

    def allows(self, url: str) -> bool:
        path = urlsplit(url).path or "/"
        for rule in self.disallowed:
            if rule and path.startswith(rule):
                return False
        return True


def parse_robots(text: str, *, user_agent: str = "link-coroner") -> RobotsRules:
    """Parse a robots.txt body.

    We honor the union of the wildcard (``User-agent: *``) group and any
    group matching ``user_agent`` (case-insensitive substring).
    """
    groups: list[tuple[list[str], list[str], float | None]] = []
    sitemaps: list[str] = []
    current_agents: list[str] = []
    current_disallow: list[str] = []
    current_delay: float | None = None

    def flush() -> None:
        if current_agents:
            groups.append((current_agents[:], current_disallow[:], current_delay))

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()
        if field_name == "user-agent":
            # Starting a new group resets only when we already saw a directive.
            if current_disallow or current_delay is not None:
                flush()
                current_agents = []
                current_disallow = []
                current_delay = None
            current_agents.append(value.lower())
        elif field_name == "disallow":
            current_disallow.append(value)
        elif field_name == "crawl-delay":
            try:
                current_delay = float(value)
            except ValueError:
                pass
        elif field_name == "sitemap":
            sitemaps.append(value)
    flush()

    ua = user_agent.lower()
    disallowed: list[str] = []
    delay: float | None = None
    for agents, dis, d in groups:
        match = any(a == "*" or (a and a in ua) for a in agents)
        if not match:
            continue
        disallowed.extend(dis)
        if d is not None and (delay is None or d > delay):
            delay = d

    return RobotsRules(
        disallowed=tuple(disallowed),
        crawl_delay=delay,
        sitemaps=tuple(sitemaps),
    )


@dataclass(slots=True)
class SiteDiscovery:
    """Result of a site discovery run."""

    urls: list[str] = field(default_factory=list)
    sitemaps_seen: list[str] = field(default_factory=list)
    robots: RobotsRules = field(default_factory=RobotsRules)
    used_fallback: bool = False


def _same_host(a: str, b: str) -> bool:
    return (urlsplit(a).hostname or "").lower() == (urlsplit(b).hostname or "").lower()


def _normalize_site(site: str) -> str:
    """Return a normalized base URL like ``https://example.com``."""
    if not site:
        raise ValueError("site URL is required")
    if "://" not in site:
        site = "https://" + site
    parts = urlsplit(site)
    if not parts.hostname:
        raise ValueError(f"invalid site URL: {site!r}")
    # Strip path/query/fragment — we always operate from the root.
    return f"{parts.scheme}://{parts.netloc}"


def _extract_sitemap_locs(body: str) -> list[str]:
    return [m.group(1).strip() for m in _SITEMAP_LOC_RE.finditer(body)]


def _extract_homepage_links(base: str, body: str) -> Iterable[str]:
    for match in _HREF_RE.finditer(body):
        href = match.group(1).strip()
        if not href or href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        url = urljoin(base + "/", href)
        if not url.startswith(("http://", "https://")):
            continue
        if _same_host(url, base):
            yield url


async def _safe_fetch(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        resp = await client.get(url)
    except httpx.HTTPError:
        return None
    return resp


async def discover_site(
    site: str,
    *,
    client: httpx.AsyncClient | None = None,
    user_agent: str = "link-coroner",
    max_urls: int = _MAX_URLS,
    timeout: float = 10.0,
) -> SiteDiscovery:
    """Discover URLs for a live site via sitemap + robots, with HTML fallback."""
    base = _normalize_site(site)
    discovery = SiteDiscovery()
    seen: set[str] = set()

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        )

    try:
        # 1. robots.txt
        robots_resp = await _safe_fetch(client, f"{base}/robots.txt")
        if robots_resp is not None and 200 <= robots_resp.status_code < 300:
            discovery.robots = parse_robots(robots_resp.text, user_agent=user_agent)

        # 2. sitemap discovery (queue with cycle guard)
        queue: list[str] = []
        for hint in discovery.robots.sitemaps:
            queue.append(hint)
        queue.append(f"{base}/sitemap.xml")

        visited: set[str] = set()
        while queue and len(discovery.sitemaps_seen) < _MAX_SITEMAPS:
            sm = queue.pop(0)
            if sm in visited:
                continue
            visited.add(sm)
            resp = await _safe_fetch(client, sm)
            if resp is None or resp.status_code >= 400:
                continue
            discovery.sitemaps_seen.append(sm)
            body = resp.text
            locs = _extract_sitemap_locs(body)
            if _IS_SITEMAP_INDEX_RE.search(body):
                # Sitemap index — enqueue its <loc>s as more sitemaps.
                for loc in locs:
                    if loc not in visited and _same_host(loc, base):
                        queue.append(loc)
                continue
            for loc in locs:
                if not loc.startswith(("http://", "https://")):
                    continue
                if not discovery.robots.allows(loc):
                    continue
                if loc in seen:
                    continue
                seen.add(loc)
                discovery.urls.append(loc)
                if len(discovery.urls) >= max_urls:
                    return discovery

        if discovery.urls:
            return discovery

        # 3. fallback — fetch homepage and pull same-host <a href> links.
        discovery.used_fallback = True
        home = await _safe_fetch(client, base + "/")
        if home is None or home.status_code >= 400:
            return discovery
        for url in _extract_homepage_links(base, home.text):
            if not discovery.robots.allows(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            discovery.urls.append(url)
            if len(discovery.urls) >= max_urls:
                break
        # Always include the homepage itself.
        root = base + "/"
        if discovery.robots.allows(root) and root not in seen:
            discovery.urls.insert(0, root)
        return discovery
    finally:
        if owns_client:
            await client.aclose()
