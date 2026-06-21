"""Obituary digest webhooks (Slack / Discord).

Posts a short "newly deceased URLs since last run" digest to a Slack or
Discord incoming webhook. State is persisted as a JSON file so that each
run only reports *newly* deceased URLs (and, separately, URLs that came
back from the dead — "resurrected").

The networking surface is small and fully injectable so tests can stub it
without touching the network.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..diagnosis import Cause, diagnose
from ..forensics.probe import ProbeResult, Verdict
from ..wayback import WaybackSnapshot

DEFAULT_USER_AGENT = "link-coroner/0.1 (+https://github.com/rwrife/link-coroner)"


@dataclass(slots=True)
class DigestEntry:
    """A single line in the obituary digest."""

    url: str
    verdict: Verdict
    cause: Cause
    reason: str
    snapshot_url: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "verdict": self.verdict.value,
            "cause": self.cause.value,
            "reason": self.reason,
            "snapshot_url": self.snapshot_url,
        }


@dataclass(slots=True)
class ObituaryDigest:
    """Diffed digest payload covering one scan vs. the previous state."""

    newly_deceased: list[DigestEntry] = field(default_factory=list)
    resurrected: list[str] = field(default_factory=list)
    still_dead_count: int = 0
    total_scanned: int = 0
    generated_at: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.newly_deceased and not self.resurrected

    def to_dict(self) -> dict[str, object]:
        return {
            "newly_deceased": [e.to_dict() for e in self.newly_deceased],
            "resurrected": list(self.resurrected),
            "still_dead_count": self.still_dead_count,
            "total_scanned": self.total_scanned,
            "generated_at": self.generated_at,
        }


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def load_state(path: Path) -> set[str]:
    """Load the set of previously-known-dead URLs from ``path``.

    Returns an empty set if the file is missing or unreadable. We
    deliberately don't crash on a malformed state file — the worst case
    is that the next digest is a little noisier.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return set()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return set()
    if isinstance(data, dict):
        urls = data.get("dead_urls", [])
    else:
        urls = data
    if not isinstance(urls, list):
        return set()
    return {str(u) for u in urls if isinstance(u, str)}


def save_state(path: Path, dead_urls: Iterable[str]) -> None:
    """Persist the current set of dead URLs to ``path``."""
    payload = {
        "version": 1,
        "updated_at": _utc_now_iso(),
        "dead_urls": sorted(set(dead_urls)),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def build_digest(
    results: list[ProbeResult],
    *,
    previous_dead: Iterable[str] = (),
    snapshots: dict[str, WaybackSnapshot] | None = None,
) -> ObituaryDigest:
    """Diff ``results`` against ``previous_dead`` to build a digest."""
    snaps = snapshots or {}
    prev = set(previous_dead)

    current_dead: list[ProbeResult] = [
        r for r in results if r.verdict is not Verdict.ALIVE
    ]
    current_dead_urls = {r.url for r in current_dead}

    newly_deceased_results = [r for r in current_dead if r.url not in prev]
    resurrected = sorted(prev - current_dead_urls)

    entries: list[DigestEntry] = []
    for r in sorted(newly_deceased_results, key=lambda r: r.url):
        snap = snaps.get(r.url)
        entries.append(
            DigestEntry(
                url=r.url,
                verdict=r.verdict,
                cause=diagnose(r),
                reason=r.reason or "",
                snapshot_url=snap.snapshot_url if snap else None,
            )
        )

    return ObituaryDigest(
        newly_deceased=entries,
        resurrected=resurrected,
        still_dead_count=len(current_dead_urls & prev),
        total_scanned=len(results),
        generated_at=_utc_now_iso(),
    )


# ---- payload formatters -----------------------------------------------------


def _truncate(items: list[str], limit: int) -> tuple[list[str], int]:
    if len(items) <= limit:
        return items, 0
    return items[:limit], len(items) - limit


def render_slack_payload(digest: ObituaryDigest, *, max_entries: int = 20) -> dict[str, object]:
    """Build a Slack incoming-webhook JSON payload from a digest."""
    if digest.is_empty:
        text = "🪦 *link-coroner obituary digest*: no newly-deceased links since last run."
        return {"text": text, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}

    header = (
        f"🪦 *link-coroner obituary digest* — "
        f"{len(digest.newly_deceased)} newly deceased, "
        f"{len(digest.resurrected)} resurrected, "
        f"{digest.still_dead_count} still dead "
        f"(scanned {digest.total_scanned})."
    )
    blocks: list[dict[str, object]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
    ]

    shown, overflow = _truncate(digest.newly_deceased, max_entries)
    if shown:
        lines = ["*Newly deceased:*"]
        for e in shown:
            line = f"• `{e.cause.value}` <{e.url}|{e.url}> — {e.reason or 'no further details'}"
            if e.snapshot_url:
                line += f"\n    ↪ resurrect: <{e.snapshot_url}>"
            lines.append(line)
        if overflow:
            lines.append(f"_…and {overflow} more._")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

    if digest.resurrected:
        shown_r, overflow_r = _truncate(digest.resurrected, max_entries)
        rlines = ["*Resurrected since last run:*"]
        rlines += [f"• <{u}|{u}>" for u in shown_r]
        if overflow_r:
            rlines.append(f"_…and {overflow_r} more._")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(rlines)}})

    return {"text": header, "blocks": blocks}


def render_discord_payload(
    digest: ObituaryDigest, *, max_entries: int = 20
) -> dict[str, object]:
    """Build a Discord webhook JSON payload from a digest."""
    if digest.is_empty:
        return {
            "username": "link-coroner",
            "content": "🪦 **obituary digest**: no newly-deceased links since last run.",
        }

    header = (
        f"🪦 **link-coroner obituary digest** — "
        f"{len(digest.newly_deceased)} newly deceased, "
        f"{len(digest.resurrected)} resurrected, "
        f"{digest.still_dead_count} still dead "
        f"(scanned {digest.total_scanned})."
    )

    embeds: list[dict[str, object]] = []
    if digest.newly_deceased:
        shown, overflow = _truncate(digest.newly_deceased, max_entries)
        lines = []
        for e in shown:
            line = f"`{e.cause.value}` [{e.url}]({e.url}) — {e.reason or 'no further details'}"
            if e.snapshot_url:
                line += f"\n↪ [resurrect snapshot]({e.snapshot_url})"
            lines.append(line)
        if overflow:
            lines.append(f"_…and {overflow} more._")
        # Discord caps description at 4096 chars; trim defensively.
        description = "\n".join(lines)[:4000]
        embeds.append(
            {
                "title": "Newly deceased",
                "description": description,
                "color": 0xB00020,
            }
        )

    if digest.resurrected:
        shown_r, overflow_r = _truncate(digest.resurrected, max_entries)
        rlines = [f"[{u}]({u})" for u in shown_r]
        if overflow_r:
            rlines.append(f"_…and {overflow_r} more._")
        embeds.append(
            {
                "title": "Resurrected since last run",
                "description": "\n".join(rlines)[:4000],
                "color": 0x2ECC71,
            }
        )

    return {"username": "link-coroner", "content": header, "embeds": embeds}


def render_payload(digest: ObituaryDigest, provider: str, *, max_entries: int = 20) -> dict[str, object]:
    """Render a digest payload for ``provider`` ('slack' | 'discord')."""
    p = provider.lower()
    if p == "slack":
        return render_slack_payload(digest, max_entries=max_entries)
    if p == "discord":
        return render_discord_payload(digest, max_entries=max_entries)
    raise ValueError(f"Unknown webhook provider: {provider!r} (expected 'slack' or 'discord')")


def detect_provider(webhook_url: str) -> str:
    """Best-effort provider detection from a webhook URL."""
    u = webhook_url.lower()
    if "discord.com" in u or "discordapp.com" in u:
        return "discord"
    if "hooks.slack.com" in u or "slack.com" in u:
        return "slack"
    # Default to Slack — its payload is the simpler "text-only" shape.
    return "slack"


# ---- transport --------------------------------------------------------------


@dataclass(slots=True)
class WebhookResponse:
    status_code: int
    body: str


def post_digest(
    webhook_url: str,
    payload: dict[str, object],
    *,
    timeout: float = 10.0,
    user_agent: str = DEFAULT_USER_AGENT,
    client: httpx.Client | None = None,
) -> WebhookResponse:
    """POST ``payload`` as JSON to ``webhook_url``.

    Pass ``client`` to inject a pre-configured (or mock) ``httpx.Client``;
    otherwise we create a short-lived one.
    """
    headers = {"User-Agent": user_agent, "Content-Type": "application/json"}
    if client is not None:
        resp = client.post(webhook_url, json=payload, headers=headers, timeout=timeout)
    else:
        with httpx.Client(timeout=timeout) as c:
            resp = c.post(webhook_url, json=payload, headers=headers)
    return WebhookResponse(status_code=resp.status_code, body=resp.text)
