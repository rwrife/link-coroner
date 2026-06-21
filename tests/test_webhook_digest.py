"""Tests for the obituary digest webhook (Slack/Discord)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from link_coroner.cli import app
from link_coroner.forensics.probe import ProbeResult, Verdict
from link_coroner.reporting.webhook import (
    build_digest,
    detect_provider,
    load_state,
    post_digest,
    render_discord_payload,
    render_payload,
    render_slack_payload,
    save_state,
)
from link_coroner.wayback import WaybackSnapshot


def _result(url: str, verdict: Verdict = Verdict.DEAD, reason: str = "HTTP_404", status: int | None = 404) -> ProbeResult:
    return ProbeResult(url=url, verdict=verdict, reason=reason, status_code=status)


def test_detect_provider() -> None:
    assert detect_provider("https://hooks.slack.com/services/T/B/X") == "slack"
    assert detect_provider("https://discord.com/api/webhooks/123/abc") == "discord"
    assert detect_provider("https://discordapp.com/api/webhooks/1/2") == "discord"
    # fallback
    assert detect_provider("https://example.com/hook") == "slack"


def test_state_roundtrip(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    assert load_state(state_file) == set()
    save_state(state_file, ["https://a.example", "https://b.example", "https://a.example"])
    assert load_state(state_file) == {"https://a.example", "https://b.example"}
    # corrupt file → empty set, no crash
    state_file.write_text("not json", encoding="utf-8")
    assert load_state(state_file) == set()


def test_build_digest_diffs_against_previous() -> None:
    results = [
        _result("https://dead-new.example"),
        _result("https://dead-old.example"),
        ProbeResult(url="https://alive.example", verdict=Verdict.ALIVE, reason="OK", status_code=200),
    ]
    previous = {"https://dead-old.example", "https://now-alive.example"}
    snaps = {
        "https://dead-new.example": WaybackSnapshot(
            url="https://dead-new.example",
            snapshot_url="https://web.archive.org/web/20250101000000/https://dead-new.example",
            timestamp="20250101000000",
        ),
    }

    d = build_digest(results, previous_dead=previous, snapshots=snaps)

    assert [e.url for e in d.newly_deceased] == ["https://dead-new.example"]
    assert d.newly_deceased[0].snapshot_url is not None
    assert d.resurrected == ["https://now-alive.example"]
    assert d.still_dead_count == 1
    assert d.total_scanned == 3
    assert not d.is_empty


def test_build_digest_empty_when_nothing_changed() -> None:
    results = [
        _result("https://dead.example"),
    ]
    d = build_digest(results, previous_dead={"https://dead.example"})
    assert d.is_empty
    assert d.still_dead_count == 1


def test_slack_payload_includes_entries() -> None:
    d = build_digest(
        [_result("https://x.example")],
        previous_dead=set(),
        snapshots={
            "https://x.example": WaybackSnapshot(
                url="https://x.example",
                snapshot_url="https://web.archive.org/web/20240101/https://x.example",
                timestamp="20240101000000",
            )
        },
    )
    payload = render_slack_payload(d)
    assert "newly deceased" in payload["text"]
    text = json.dumps(payload)
    assert "https://x.example" in text
    assert "web.archive.org" in text


def test_slack_payload_empty_digest() -> None:
    d = build_digest([], previous_dead=set())
    payload = render_slack_payload(d)
    assert "no newly-deceased" in payload["text"]


def test_discord_payload_shape() -> None:
    d = build_digest(
        [_result("https://x.example"), _result("https://y.example", reason="NXDOMAIN", status=None)],
        previous_dead={"https://z.example"},
    )
    payload = render_discord_payload(d, max_entries=1)
    assert payload["username"] == "link-coroner"
    embeds = payload["embeds"]
    titles = [e["title"] for e in embeds]
    assert "Newly deceased" in titles
    assert "Resurrected since last run" in titles
    # max_entries=1 → overflow note for one of them
    desc = next(e["description"] for e in embeds if e["title"] == "Newly deceased")
    assert "and 1 more" in desc


def test_render_payload_dispatch() -> None:
    d = build_digest([], previous_dead=set())
    assert "text" in render_payload(d, "slack")
    assert "content" in render_payload(d, "discord")
    with pytest.raises(ValueError):
        render_payload(d, "teams")


@respx.mock
def test_post_digest_uses_httpx() -> None:
    url = "https://hooks.example.com/hook"
    route = respx.post(url).mock(return_value=httpx.Response(200, text="ok"))
    resp = post_digest(url, {"text": "hi"})
    assert resp.status_code == 200
    assert resp.body == "ok"
    assert route.called
    sent = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert sent == {"text": "hi"}


# ---- CLI integration --------------------------------------------------------


runner = CliRunner()


@respx.mock
def test_cli_digest_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("see https://dead.example for details", encoding="utf-8")
    state_file = tmp_path / "state.json"

    # Stub the probe layer so we don't hit the network.
    async def fake_probe(urls, config=None):  # type: ignore[no-untyped-def]
        return [_result(u) for u in urls]

    async def fake_resurrect(urls, **_kwargs):  # type: ignore[no-untyped-def]
        return {
            u: WaybackSnapshot(
                url=u,
                snapshot_url=f"https://web.archive.org/web/20240101/{u}",
                timestamp="20240101000000",
            )
            for u in urls
        }

    monkeypatch.setattr("link_coroner.cli.probe_urls", fake_probe)
    monkeypatch.setattr("link_coroner.cli.resurrect_many", fake_resurrect)

    result = runner.invoke(
        app,
        [
            "digest",
            str(repo),
            "--webhook-url",
            "https://hooks.slack.com/services/T/B/X",
            "--state-file",
            str(state_file),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "blocks" in payload
    # Dry-run must not persist state.
    assert not state_file.exists()


@respx.mock
def test_cli_digest_posts_and_updates_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("https://dead.example\n", encoding="utf-8")
    state_file = tmp_path / "state.json"

    async def fake_probe(urls, config=None):  # type: ignore[no-untyped-def]
        return [_result(u) for u in urls]

    async def fake_resurrect(urls, **_kwargs):  # type: ignore[no-untyped-def]
        return {}

    monkeypatch.setattr("link_coroner.cli.probe_urls", fake_probe)
    monkeypatch.setattr("link_coroner.cli.resurrect_many", fake_resurrect)

    webhook = "https://discord.com/api/webhooks/123/abc"
    route = respx.post(webhook).mock(return_value=httpx.Response(204, text=""))

    result = runner.invoke(
        app,
        [
            "digest",
            str(repo),
            "--webhook-url",
            webhook,
            "--state-file",
            str(state_file),
            "--no-resurrect",
        ],
    )
    assert result.exit_code == 0, result.output
    assert route.called
    sent = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert sent["username"] == "link-coroner"
    # State should now contain the dead URL.
    assert load_state(state_file) == {"https://dead.example"}

    # Second run: still dead, no new obituaries → no POST, state preserved.
    route.reset()
    result2 = runner.invoke(
        app,
        [
            "digest",
            str(repo),
            "--webhook-url",
            webhook,
            "--state-file",
            str(state_file),
            "--no-resurrect",
        ],
    )
    assert result2.exit_code == 0, result2.output
    assert not route.called
    assert load_state(state_file) == {"https://dead.example"}


@respx.mock
def test_cli_digest_webhook_failure_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("https://dead.example\n", encoding="utf-8")

    async def fake_probe(urls, config=None):  # type: ignore[no-untyped-def]
        return [_result(u) for u in urls]

    async def fake_resurrect(urls, **_kwargs):  # type: ignore[no-untyped-def]
        return {}

    monkeypatch.setattr("link_coroner.cli.probe_urls", fake_probe)
    monkeypatch.setattr("link_coroner.cli.resurrect_many", fake_resurrect)

    webhook = "https://hooks.slack.com/services/T/B/X"
    respx.post(webhook).mock(return_value=httpx.Response(500, text="boom"))

    result = runner.invoke(
        app,
        [
            "digest",
            str(repo),
            "--webhook-url",
            webhook,
            "--state-file",
            str(tmp_path / "state.json"),
            "--no-resurrect",
        ],
    )
    assert result.exit_code == 1
    assert "Webhook POST failed" in result.output
