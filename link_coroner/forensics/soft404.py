"""Soft-404 + parked-domain heuristics (M4).

A surprising number of dead links return HTTP 200 with a "page not found"
template, a parking page, or a domain-squatter ad. The probe layer
classifies these as ALIVE; this module sniffs the response body and
upgrades the verdict to "suspicious" with a specific cause.

Heuristics intentionally err on the conservative side — false positives
here are worse than misses, because we'll be telling users a working URL
is dead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

try:
    from selectolax.parser import HTMLParser  # type: ignore[import-untyped]

    _HAVE_SELECTOLAX = True
except ImportError:  # pragma: no cover - tests pin the dep
    _HAVE_SELECTOLAX = False


# Tiny bodies after stripping tags + whitespace are almost always 404 templates.
_TINY_BODY_CHARS = 240

# Soft-404 fingerprints — substrings checked case-insensitively against
# the rendered title + body text.
_SOFT_404_PATTERNS: tuple[str, ...] = (
    "page not found",
    "404 not found",
    "we couldn't find",
    "we couldnt find",
    "this page doesn't exist",
    "this page does not exist",
    "the page you requested",
    "the page you are looking for",
    "sorry, that page",
    "oops! that page",
    "page no longer exists",
    "content has moved",
    "nothing here",
    "doesn't seem to exist",
)

# Parker / squatter / for-sale fingerprints. Drawn from Sedo, GoDaddy,
# Bodis, HugeDomains, Afternic, Dan.com, Uniregistry, Above.com.
_PARKED_PATTERNS: tuple[str, ...] = (
    "domain is for sale",
    "this domain is for sale",
    "buy this domain",
    "the domain name",
    "make an offer",
    "domain may be for sale",
    "parked free, courtesy",
    "parked by",
    "this web page is parked",
    "this domain has expired",
    "expired domain",
    "renew now to keep",
    "sedo",
    "hugedomains",
    "afternic",
    "dan.com",
    "uniregistry",
    "godaddy",
    "bodis",
    "related searches",
    "sponsored listings",
)

# Hostname fingerprints — sometimes the redirect chain or final URL is
# the dead giveaway even when body text is sparse.
_PARKED_HOST_SUBSTRINGS: tuple[str, ...] = (
    "sedoparking.com",
    "hugedomains.com",
    "afternic.com",
    "dan.com",
    "uniregistry.com",
    "bodis.com",
    "above.com",
    "parkingcrew.net",
    "parklogic.com",
    "godaddy.com/sale",
    "buydomains.com",
)


_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


@dataclass(slots=True)
class ContentVerdict:
    """Outcome of analysing a successful 2xx response body."""

    suspicious: bool
    reason: str  # "" when not suspicious
    note: str = ""


def is_html(content_type: str | None) -> bool:
    if not content_type:
        return False
    ct = content_type.lower().split(";", 1)[0].strip()
    return ct in _HTML_CONTENT_TYPES


def _strip_html(body: str) -> tuple[str, str]:
    """Return ``(title, text)``. Falls back to a crude regex if selectolax missing."""
    if _HAVE_SELECTOLAX:
        tree = HTMLParser(body)
        title_node = tree.css_first("title")
        title = title_node.text(strip=True) if title_node else ""
        # Strip script/style first — they bloat the body and don't reflect content.
        for sel in ("script", "style", "noscript", "template"):
            for node in tree.css(sel):
                node.decompose()
        root = tree.body or tree.root
        text = root.text(separator=" ", strip=True) if root is not None else ""
        return title, text
    # Fallback: very small repos may not have selectolax in the env.
    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else ""
    no_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", no_scripts)
    text = re.sub(r"\s+", " ", text).strip()
    return title, text


def analyze_content(
    body: str | bytes,
    *,
    content_type: str | None = None,
    final_url: str | None = None,
) -> ContentVerdict:
    """Return a :class:`ContentVerdict` for a successful 2xx response.

    Order of checks:
      1. Parked-host fingerprints in the final URL (cheap, very high confidence).
      2. Parker / for-sale phrases in title or body text.
      3. Soft-404 phrases in title or body text.
      4. Suspiciously tiny body with a 200 status.
    """
    if final_url:
        haystack = final_url.lower()
        for fp in _PARKED_HOST_SUBSTRINGS:
            if fp in haystack:
                return ContentVerdict(True, "PARKED", note=f"final URL matches {fp!r}")

    if not is_html(content_type):
        # Non-HTML 200s (json, images, pdfs) are not soft-404 candidates.
        return ContentVerdict(False, "")

    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - decode should not raise with errors=replace
            return ContentVerdict(False, "")

    if not body or not body.strip():
        return ContentVerdict(True, "SOFT_404", note="empty 200 body")

    title, text = _strip_html(body)
    blob = f"{title}\n{text}".lower()

    for fp in _PARKED_PATTERNS:
        if fp in blob:
            return ContentVerdict(True, "PARKED", note=f"matched {fp!r}")

    for fp in _SOFT_404_PATTERNS:
        if fp in blob:
            return ContentVerdict(True, "SOFT_404", note=f"matched {fp!r}")

    if len(text) < _TINY_BODY_CHARS and any(
        kw in (title or "").lower() for kw in ("404", "not found", "error")
    ):
        return ContentVerdict(True, "SOFT_404", note="tiny body + 404-ish title")

    return ContentVerdict(False, "")
