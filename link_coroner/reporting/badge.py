"""README badge generator (M-future, addresses issue #27).

Reads previously-rendered autopsy results (either a ``--from results.json``
file or rows from a :class:`~link_coroner.cache.ProbeCache`) and emits a
shields.io-compatible badge in one of three formats:

* ``svg``               – a self-contained shields.io-style SVG, ready to
  commit to ``docs/links.svg`` and referenced from a README.
* ``shields-endpoint``  – the JSON payload consumed by shields.io's
  `endpoint <https://shields.io/endpoint>`_ feature, so users can host
  this file (e.g. on GitHub Pages) and let shields.io render the badge.
* ``markdown``          – a ready-to-paste ``![link health](...)`` snippet.

Colour rules (worst-severity wins):

* **brightgreen** – zero deceased or suspicious links.
* **yellow**      – no deceased links, but at least one soft-404 / parked
  / otherwise suspicious entry.
* **red**         – at least one fully deceased link.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..diagnosis import Cause

__all__ = [
    "BadgeSummary",
    "summarize",
    "summarize_from_json",
    "render_svg",
    "render_shields_endpoint",
    "render_markdown",
    "color_for",
]


# Causes we treat as "dead" (counted toward the red bucket).
_DEAD_CAUSES: frozenset[str] = frozenset(
    {
        Cause.NXDOMAIN.value,
        Cause.DNS_FAILURE.value,
        Cause.CONN_REFUSED.value,
        Cause.TLS_EXPIRED.value,
        Cause.TLS_ERROR.value,
        Cause.HTTP_4XX.value,
        Cause.HTTP_5XX.value,
        Cause.TIMEOUT.value,
        Cause.REDIRECT_LOOP.value,
        Cause.BAD_URL.value,
    }
)

# Causes we treat as "suspicious" (counted toward the yellow bucket).
_SUSPICIOUS_CAUSES: frozenset[str] = frozenset(
    {Cause.SOFT_404.value, Cause.PARKED.value, Cause.UNKNOWN.value}
)


@dataclass(slots=True, frozen=True)
class BadgeSummary:
    """Aggregate counts used to pick the badge colour + message."""

    total: int
    alive: int
    dead: int
    suspicious: int

    @property
    def worst_color(self) -> str:
        return color_for(self)

    @property
    def message(self) -> str:
        if self.total == 0:
            return "no links"
        if self.dead == 0 and self.suspicious == 0:
            # 🪦 0 dead / N alive — keep terse for shields width.
            return f"🪦 0 dead / {self.alive} alive"
        parts: list[str] = []
        if self.dead:
            parts.append(f"🪦 {self.dead} dead")
        if self.suspicious:
            parts.append(f"⚠ {self.suspicious} suspicious")
        parts.append(f"{self.alive} alive")
        return " / ".join(parts)


def color_for(summary: BadgeSummary) -> str:
    """Pick the shields colour based on the worst severity in ``summary``."""
    if summary.dead > 0:
        return "red"
    if summary.suspicious > 0:
        return "yellow"
    return "brightgreen"


def _cause_of(item: dict[str, Any]) -> str:
    """Extract the canonical cause string from a results.json entry."""
    cause = item.get("cause")
    if isinstance(cause, str) and cause:
        return cause
    # Fallback: derive from verdict when cause is absent (older payloads).
    verdict = item.get("verdict")
    if verdict == "ALIVE":
        return Cause.ALIVE.value
    if verdict == "DEAD":
        return Cause.HTTP_4XX.value  # best-effort generic dead bucket
    return Cause.UNKNOWN.value


def summarize(items: Iterable[dict[str, Any]]) -> BadgeSummary:
    """Bucket each result into alive / dead / suspicious for the badge."""
    total = 0
    alive = 0
    dead = 0
    suspicious = 0
    for item in items:
        total += 1
        cause = _cause_of(item)
        if cause == Cause.ALIVE.value:
            alive += 1
        elif cause in _DEAD_CAUSES:
            dead += 1
        elif cause in _SUSPICIOUS_CAUSES:
            suspicious += 1
        else:
            # Unknown cause string — be conservative and call it suspicious
            # so the badge doesn't silently turn green on us.
            suspicious += 1
    return BadgeSummary(total=total, alive=alive, dead=dead, suspicious=suspicious)


def summarize_from_json(payload: str | bytes) -> BadgeSummary:
    """Parse a ``link-coroner autopsy --format json`` payload and summarise."""
    data = json.loads(payload)
    if not isinstance(data, list):
        raise ValueError("results.json must be a JSON array of autopsy entries")
    return summarize(data)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

# Hex colours pinned to the shields.io palette so a locally-rendered SVG
# is visually indistinguishable from a hosted shields.io badge.
_COLOR_HEX: dict[str, str] = {
    "brightgreen": "#4c1",
    "green": "#97ca00",
    "yellow": "#dfb317",
    "orange": "#fe7d37",
    "red": "#e05d44",
    "lightgrey": "#9f9f9f",
    "blue": "#007ec6",
}


def _color_hex(name: str) -> str:
    return _COLOR_HEX.get(name, name if name.startswith("#") else "#9f9f9f")


# Rough character width estimate (px) for shields-style 11px Verdana.
# Shields itself ships a per-glyph table; this approximation keeps the
# badge readable without pulling in font metrics. Wide enough for emoji.
def _text_width(text: str) -> int:
    width = 0
    for ch in text:
        if ch.isupper():
            width += 8
        elif ch.isdigit():
            width += 7
        elif ch in " ./":
            width += 4
        elif ord(ch) > 127:  # emoji / unicode — give it room
            width += 14
        else:
            width += 6
    # Padding on both sides.
    return width + 14


def render_svg(label: str, message: str, color: str) -> str:
    """Return a self-contained shields-style SVG (no external requests)."""
    color_hex = _color_hex(color)
    label_w = _text_width(label)
    msg_w = _text_width(message)
    total_w = label_w + msg_w

    # XML-escape the few characters we care about for SVG text nodes.
    def esc(value: str) -> str:
        return (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    label_safe = esc(label)
    message_safe = esc(message)

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{total_w}" height="20" role="img" '
        f'aria-label="{label_safe}: {message_safe}">'
        f"<title>{label_safe}: {message_safe}</title>"
        '<linearGradient id="s" x2="0" y2="100%">'
        '<stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
        '<stop offset="1" stop-opacity=".1"/>'
        "</linearGradient>"
        f'<clipPath id="r"><rect width="{total_w}" height="20" rx="3" fill="#fff"/></clipPath>'
        '<g clip-path="url(#r)">'
        f'<rect width="{label_w}" height="20" fill="#555"/>'
        f'<rect x="{label_w}" width="{msg_w}" height="20" fill="{color_hex}"/>'
        f'<rect width="{total_w}" height="20" fill="url(#s)"/>'
        "</g>"
        '<g fill="#fff" text-anchor="middle" '
        'font-family="Verdana,Geneva,DejaVu Sans,sans-serif" '
        'text-rendering="geometricPrecision" font-size="110">'
        f'<text aria-hidden="true" x="{label_w * 5}" y="150" fill="#010101" '
        f'fill-opacity=".3" transform="scale(.1)" textLength="{(label_w - 10) * 10}">'
        f"{label_safe}</text>"
        f'<text x="{label_w * 5}" y="140" transform="scale(.1)" '
        f'fill="#fff" textLength="{(label_w - 10) * 10}">{label_safe}</text>'
        f'<text aria-hidden="true" x="{(label_w + msg_w / 2) * 10}" y="150" '
        f'fill="#010101" fill-opacity=".3" transform="scale(.1)" '
        f'textLength="{(msg_w - 10) * 10}">{message_safe}</text>'
        f'<text x="{(label_w + msg_w / 2) * 10}" y="140" transform="scale(.1)" '
        f'fill="#fff" textLength="{(msg_w - 10) * 10}">{message_safe}</text>'
        "</g>"
        "</svg>"
    )


def render_shields_endpoint(label: str, message: str, color: str) -> str:
    """Return the shields.io endpoint-badge JSON payload."""
    payload = {
        "schemaVersion": 1,
        "label": label,
        "message": message,
        "color": color,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def render_markdown(
    label: str,
    message: str,
    color: str,
    *,
    endpoint_url: str | None = None,
    alt: str | None = None,
) -> str:
    """Return a ready-to-paste Markdown badge snippet.

    If ``endpoint_url`` is supplied (e.g. the raw URL of a hosted
    ``link-coroner.json``), produce a shields-endpoint-backed badge.
    Otherwise fall back to a static shields.io ``/badge/`` URL — note
    that shields' static URLs require ``-`` to be escaped as ``--``.
    """
    alt_text = alt or f"{label}: {message}"
    if endpoint_url:
        url = f"https://img.shields.io/endpoint?url={endpoint_url}"
    else:
        safe_label = label.replace("-", "--").replace(" ", "%20")
        safe_message = message.replace("-", "--").replace(" ", "%20")
        url = f"https://img.shields.io/badge/{safe_label}-{safe_message}-{color}"
    return f"![{alt_text}]({url})"
