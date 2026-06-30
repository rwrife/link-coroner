"""`.coroner-ignore` quarantine config (issue #29).

A `.gitignore`-style file declaring URLs we expect to be dead (vendors that
403 to bots, intranet hosts, `https://example.invalid`, …). Each entry can
carry an optional `@ttl=<duration>` suffix; when the TTL elapses the URL is
re-probed so the coroner can shout `QUARANTINE_BROKEN_OUT` if the corpse
sat up.

Syntax
------

    # comments and blank lines are ignored
    https://example.invalid
    https://vendor.example.com/*  @ttl=30d
    *://intranet.local/*

Patterns use shell-style globbing (``fnmatch``) against the full URL, so
``https://vendor.example.com/*`` matches every path on that host, and
``*://example.com/foo`` matches both http and https.
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_IGNORE_FILENAME = ".coroner-ignore"
DEFAULT_STATE_FILENAME = ".coroner-ignore.state.json"

_TTL_RE = re.compile(r"@ttl\s*=\s*(?P<value>\S+)", re.IGNORECASE)
_DURATION_RE = re.compile(r"^(?P<num>\d+)(?P<unit>[smhdw])?$", re.IGNORECASE)

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 7 * 86400,
}


class IgnoreError(ValueError):
    """Raised for malformed `.coroner-ignore` entries."""


def parse_duration(text: str) -> int:
    """Parse a duration like ``30d`` / ``12h`` / ``900s`` into seconds.

    Bare numbers (e.g. ``86400``) are treated as seconds. Raises
    :class:`IgnoreError` for anything else.
    """
    m = _DURATION_RE.match(text.strip())
    if not m:
        raise IgnoreError(f"invalid duration: {text!r}")
    num = int(m.group("num"))
    unit = (m.group("unit") or "s").lower()
    return num * _UNIT_SECONDS[unit]


@dataclass(slots=True)
class IgnoreRule:
    """A single `.coroner-ignore` entry."""

    pattern: str
    ttl_seconds: int | None = None
    lineno: int = 0

    def matches(self, url: str) -> bool:
        return fnmatch.fnmatchcase(url, self.pattern)


@dataclass(slots=True)
class IgnoreFile:
    """Parsed `.coroner-ignore` contents."""

    rules: list[IgnoreRule] = field(default_factory=list)
    source: Path | None = None

    def match(self, url: str) -> IgnoreRule | None:
        """Return the first matching rule, or ``None``."""
        for rule in self.rules:
            if rule.matches(url):
                return rule
        return None

    def __bool__(self) -> bool:
        return bool(self.rules)


def parse_ignore_text(text: str, *, source: Path | None = None) -> IgnoreFile:
    """Parse `.coroner-ignore` text into an :class:`IgnoreFile`."""
    rules: list[IgnoreRule] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip inline comments (only when preceded by whitespace) so people
        # can write `https://x.example  # vendor bot block`.
        comment_idx = _find_inline_comment(line)
        if comment_idx is not None:
            line = line[:comment_idx].strip()
            if not line:
                continue

        ttl_seconds: int | None = None
        m = _TTL_RE.search(line)
        if m:
            ttl_seconds = parse_duration(m.group("value"))
            line = (line[: m.start()] + line[m.end() :]).strip()
        if not line:
            raise IgnoreError(f"line {lineno}: @ttl without a URL/pattern")
        rules.append(IgnoreRule(pattern=line, ttl_seconds=ttl_seconds, lineno=lineno))
    return IgnoreFile(rules=rules, source=source)


def _find_inline_comment(line: str) -> int | None:
    """Return index of an inline ``#`` comment if one exists, else ``None``."""
    for i, ch in enumerate(line):
        if ch == "#" and i > 0 and line[i - 1].isspace():
            return i
    return None


def load_ignore_file(path: Path) -> IgnoreFile:
    """Load `.coroner-ignore` from ``path``. Missing file → empty :class:`IgnoreFile`."""
    if not path.is_file():
        return IgnoreFile(source=path)
    return parse_ignore_text(path.read_text(encoding="utf-8"), source=path)


def discover_ignore_file(start: Path) -> Path:
    """Return the path where `.coroner-ignore` lives for ``start``.

    If ``start`` is a directory, look there. If it's a file, look in its
    parent directory. The returned path may not exist; callers should use
    :func:`load_ignore_file` which handles that.
    """
    if start.is_dir():
        return start / DEFAULT_IGNORE_FILENAME
    return start.parent / DEFAULT_IGNORE_FILENAME


# ---------------------------------------------------------------------------
# State (per-URL last-probe timestamps for TTL re-checks)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IgnoreState:
    """Tracks when each quarantined URL was last probed."""

    last_checked: dict[str, float] = field(default_factory=dict)
    path: Path | None = None

    def needs_recheck(self, url: str, ttl_seconds: int | None, *, now: float | None = None) -> bool:
        """Return True if a TTL-bearing URL is due for a re-probe."""
        if ttl_seconds is None:
            return False
        last = self.last_checked.get(url)
        if last is None:
            return True
        current = now if now is not None else time.time()
        return (current - last) >= ttl_seconds

    def mark_checked(self, url: str, *, now: float | None = None) -> None:
        self.last_checked[url] = now if now is not None else time.time()


def load_ignore_state(path: Path) -> IgnoreState:
    """Load TTL bookkeeping state. Missing/corrupt → empty state."""
    if not path.is_file():
        return IgnoreState(path=path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return IgnoreState(path=path)
    if not isinstance(raw, dict):
        return IgnoreState(path=path)
    last = raw.get("last_checked", {})
    if not isinstance(last, dict):
        last = {}
    # Coerce values to float defensively.
    safe: dict[str, float] = {}
    for k, v in last.items():
        try:
            safe[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return IgnoreState(last_checked=safe, path=path)


def save_ignore_state(state: IgnoreState, path: Path | None = None) -> None:
    """Persist state JSON to ``path`` (falls back to ``state.path``)."""
    target = path or state.path
    if target is None:
        raise ValueError("save_ignore_state: no path given and state has none")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"last_checked": state.last_checked}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Classification helpers used by the CLI
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Quarantine:
    """Per-URL quarantine decision."""

    url: str
    rule: IgnoreRule
    recheck: bool  # True → probe anyway (TTL elapsed), False → skip


def classify_urls(
    urls: list[str],
    ignore: IgnoreFile,
    state: IgnoreState,
    *,
    now: float | None = None,
) -> tuple[list[str], list[Quarantine]]:
    """Split ``urls`` into (to-probe, quarantined).

    The first list contains URLs to probe normally (non-quarantined ones plus
    quarantined URLs whose TTL has elapsed). The second list mirrors every
    quarantined URL with its rule + whether it was scheduled for re-check.
    """
    to_probe: list[str] = []
    quarantined: list[Quarantine] = []
    for url in urls:
        rule = ignore.match(url)
        if rule is None:
            to_probe.append(url)
            continue
        recheck = state.needs_recheck(url, rule.ttl_seconds, now=now)
        quarantined.append(Quarantine(url=url, rule=rule, recheck=recheck))
        if recheck:
            to_probe.append(url)
    return to_probe, quarantined


# ---------------------------------------------------------------------------
# CLI helper: `link-coroner ignore add`
# ---------------------------------------------------------------------------


def append_ignore_entry(
    path: Path,
    entry: str,
    *,
    ttl: str | None = None,
) -> tuple[bool, str]:
    """Append ``entry`` to the `.coroner-ignore` at ``path``, dedup-aware.

    Returns ``(added, formatted_line)``. ``added`` is False when the entry
    already exists (idempotent). The TTL string is validated.
    """
    entry = entry.strip()
    if not entry:
        raise IgnoreError("entry must not be empty")
    if ttl is not None:
        parse_duration(ttl)  # validation only; raises on bad input
        formatted = f"{entry}  @ttl={ttl}"
    else:
        formatted = entry

    existing = load_ignore_file(path).rules if path.is_file() else []
    for rule in existing:
        if rule.pattern == entry:
            # Update TTL if the caller asked for one and the existing rule
            # has a different (or missing) TTL: rewrite the file in place.
            if ttl is not None and rule.ttl_seconds != parse_duration(ttl):
                _rewrite_with_updated_ttl(path, entry, ttl)
                return True, formatted
            return False, formatted

    path.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = path.is_file() and not path.read_text(encoding="utf-8").endswith("\n")
    with path.open("a", encoding="utf-8") as fh:
        if needs_newline:
            fh.write("\n")
        fh.write(formatted + "\n")
    return True, formatted


def _rewrite_with_updated_ttl(path: Path, entry: str, ttl: str) -> None:
    """Rewrite ``path`` replacing the TTL of the line whose pattern == ``entry``."""
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            out.append(raw)
            continue
        # Strip inline comment when comparing patterns.
        head = stripped
        comment_idx = _find_inline_comment(head)
        if comment_idx is not None:
            head = head[:comment_idx].strip()
        head_no_ttl = _TTL_RE.sub("", head).strip()
        if head_no_ttl == entry:
            out.append(f"{entry}  @ttl={ttl}")
        else:
            out.append(raw)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
