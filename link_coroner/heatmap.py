"""Link-rot heatmap reporting (issue #22).

Aggregates :class:`~link_coroner.cache.ProbeEvent` history into a
file/dir × week grid and emits ANSI, SVG, or HTML output.

The "rot intensity" for each (path, week) cell is the count of distinct
URLs first observed as DEAD/UNREACHABLE during that week. Multiple
probe rows for the same URL only count once — what matters is *when*
the link died, not how often we re-confirmed it.
"""

from __future__ import annotations

import html
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import PurePosixPath

from .cache import ProbeEvent, is_dead_verdict


def _week_start(ts: int) -> date:
    """Return the Monday of the ISO week containing ``ts`` (UTC)."""
    d = datetime.fromtimestamp(int(ts), tz=UTC).date()
    return d - timedelta(days=d.weekday())


def _bucket_path(file_path: str | None, depth: int) -> str:
    """Collapse ``file_path`` to the top ``depth`` segments.

    Files outside any directory (or unknown origin) collapse to a stable
    sentinel so they stay visible in the heatmap.
    """
    if not file_path:
        return "<unknown>"
    pp = PurePosixPath(file_path.replace("\\", "/"))
    parts = [p for p in pp.parts if p not in ("", "/")]
    if not parts:
        return "<unknown>"
    if len(parts) <= depth:
        return "/".join(parts)
    return "/".join(parts[:depth]) + "/"


@dataclass(slots=True)
class HeatmapStats:
    total_deaths: int = 0
    top_paths: list[tuple[str, int]] = field(default_factory=list)
    mtbf_per_host: list[tuple[str, float]] = field(default_factory=list)
    deaths_per_host: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class HeatmapGrid:
    weeks: list[date]
    paths: list[str]
    cells: dict[tuple[str, date], int]
    stats: HeatmapStats
    since_ts: int
    until_ts: int

    def max_intensity(self) -> int:
        return max(self.cells.values(), default=0)

    def cell(self, path: str, week: date) -> int:
        return self.cells.get((path, week), 0)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def build_grid(
    events: Iterable[ProbeEvent],
    *,
    since_ts: int,
    until_ts: int | None = None,
    path_depth: int = 2,
    top_paths_n: int = 5,
) -> HeatmapGrid:
    """Aggregate probe events into a heatmap grid.

    ``since_ts`` / ``until_ts`` are unix seconds (UTC). Events outside
    the window are ignored. The grid always covers every week in
    ``[since, until]`` so blanks render as empty cells.
    """
    if until_ts is None:
        until_ts = int(datetime.now(tz=UTC).timestamp())

    # First-death-per-URL across the window.
    first_death: dict[str, ProbeEvent] = {}
    deaths_per_host: Counter[str] = Counter()
    first_seen_per_host: dict[str, int] = {}
    last_seen_per_host: dict[str, int] = {}

    for ev in events:
        if ev.observed_at < since_ts or ev.observed_at > until_ts:
            continue
        if ev.host:
            first_seen_per_host.setdefault(ev.host, ev.observed_at)
            last_seen_per_host[ev.host] = ev.observed_at
        if not is_dead_verdict(ev.verdict):
            continue
        prior = first_death.get(ev.url)
        if prior is None or ev.observed_at < prior.observed_at:
            first_death[ev.url] = ev

    cells: dict[tuple[str, date], int] = defaultdict(int)
    path_totals: Counter[str] = Counter()
    for ev in first_death.values():
        bucket = _bucket_path(ev.file_path, depth=path_depth)
        week = _week_start(ev.observed_at)
        cells[(bucket, week)] += 1
        path_totals[bucket] += 1
        if ev.host:
            deaths_per_host[ev.host] += 1

    # Build a contiguous list of weeks across the window.
    start_week = _week_start(since_ts)
    end_week = _week_start(until_ts)
    weeks: list[date] = []
    cur = start_week
    while cur <= end_week:
        weeks.append(cur)
        cur += timedelta(days=7)

    # Sort paths by death count desc, then alphabetically for stability.
    paths = sorted(path_totals, key=lambda p: (-path_totals[p], p))

    # Mean time between failures per host, in days, based on observed
    # window for that host and how many of its URLs died.
    mtbf: list[tuple[str, float]] = []
    for host, deaths in deaths_per_host.items():
        if deaths <= 0:
            continue
        span_s = max(1, last_seen_per_host.get(host, until_ts) - first_seen_per_host.get(host, since_ts))
        mtbf.append((host, round((span_s / deaths) / 86400.0, 2)))
    mtbf.sort(key=lambda item: item[1])

    stats = HeatmapStats(
        total_deaths=len(first_death),
        top_paths=path_totals.most_common(top_paths_n),
        mtbf_per_host=mtbf[:top_paths_n],
        deaths_per_host=dict(deaths_per_host),
    )
    return HeatmapGrid(
        weeks=weeks,
        paths=paths,
        cells=dict(cells),
        stats=stats,
        since_ts=since_ts,
        until_ts=until_ts,
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

_SHADES = [" ", "·", "░", "▒", "▓", "█"]  # 0..5+
_ANSI_COLORS = ["", "\x1b[38;5;229m", "\x1b[38;5;221m", "\x1b[38;5;208m", "\x1b[38;5;196m", "\x1b[38;5;124m"]
_ANSI_RESET = "\x1b[0m"


def _shade_index(value: int, ceiling: int) -> int:
    if value <= 0 or ceiling <= 0:
        return 0
    ratio = value / ceiling
    if ratio >= 1.0:
        return len(_SHADES) - 1
    return max(1, min(len(_SHADES) - 2, int(ratio * (len(_SHADES) - 1)) + 1))


def render_ansi(grid: HeatmapGrid, *, color: bool = True) -> str:
    """Render a terminal heatmap. Mono-spaced, ANSI-256 when ``color``."""
    if not grid.paths or not grid.weeks:
        return _render_empty_text(grid)

    ceiling = grid.max_intensity()
    label_w = max((len(p) for p in grid.paths), default=4)
    label_w = max(label_w, len("path"))

    header_dates = [w.strftime("%m-%d") for w in grid.weeks]
    out: list[str] = []
    out.append(f"link-rot heatmap  ({_fmt_ts(grid.since_ts)} → {_fmt_ts(grid.until_ts)})")
    out.append("")
    header = "path".ljust(label_w) + "  " + " ".join(d for d in header_dates)
    out.append(header)
    out.append("-" * len(header))
    for path in grid.paths:
        row_cells: list[str] = []
        for week in grid.weeks:
            value = grid.cell(path, week)
            idx = _shade_index(value, ceiling)
            glyph = _SHADES[idx]
            cell = glyph * 5  # match "MM-DD" width
            if color and idx > 0:
                cell = f"{_ANSI_COLORS[idx]}{cell}{_ANSI_RESET}"
            row_cells.append(cell)
        out.append(path.ljust(label_w) + "  " + " ".join(row_cells))

    out.append("")
    out.append(_legend_ansi(ceiling, color=color))
    out.append("")
    out.extend(_stats_lines(grid.stats))
    return "\n".join(out) + "\n"


def _legend_ansi(ceiling: int, *, color: bool) -> str:
    if ceiling <= 0:
        return "legend: (no deaths recorded)"
    chunks = []
    for idx, shade in enumerate(_SHADES):
        cell = shade * 3
        if color and idx > 0:
            cell = f"{_ANSI_COLORS[idx]}{cell}{_ANSI_RESET}"
        chunks.append(cell)
    return "legend: " + " ".join(chunks) + f"   (0 → ≥{ceiling} deaths/week)"


def _stats_lines(stats: HeatmapStats) -> list[str]:
    lines = [f"total deaths: {stats.total_deaths}"]
    if stats.top_paths:
        lines.append("top rotting paths:")
        for path, count in stats.top_paths:
            lines.append(f"  {count:>4}  {path}")
    if stats.mtbf_per_host:
        lines.append("worst hosts (MTBF, days/death):")
        for host, days in stats.mtbf_per_host:
            lines.append(f"  {days:>6}d  {host}")
    return lines


def _render_empty_text(grid: HeatmapGrid) -> str:
    return (
        f"link-rot heatmap  ({_fmt_ts(grid.since_ts)} → {_fmt_ts(grid.until_ts)})\n\n"
        "No probe history in this window — run `link-coroner autopsy --cache` first.\n"
    )


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=UTC).strftime("%Y-%m-%d")


# -- SVG --------------------------------------------------------------------

_SVG_COLORS = ["#0f1117", "#fff4b0", "#ffd166", "#f08a3e", "#e2433a", "#7a1313"]


def render_svg(grid: HeatmapGrid, *, cell_size: int = 14, gap: int = 2) -> str:
    if not grid.paths or not grid.weeks:
        return _empty_svg(grid)
    ceiling = grid.max_intensity()
    label_w = 220
    header_h = 30
    width = label_w + len(grid.weeks) * (cell_size + gap) + 10
    height = header_h + len(grid.paths) * (cell_size + gap) + 80

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'font-family="ui-monospace, Menlo, monospace" font-size="11" fill="#cdd6f4">'
    )
    parts.append('<rect width="100%" height="100%" fill="#0f1117"/>')
    parts.append(
        f'<text x="10" y="18" font-size="13" fill="#f8f8f2">link-rot heatmap '
        f'({_fmt_ts(grid.since_ts)} → {_fmt_ts(grid.until_ts)})</text>'
    )

    for col, week in enumerate(grid.weeks):
        x = label_w + col * (cell_size + gap)
        parts.append(
            f'<text x="{x}" y="{header_h - 4}" font-size="9" fill="#a6adc8" '
            f'transform="rotate(-60 {x} {header_h - 4})">{week.strftime("%m-%d")}</text>'
        )

    for row, path in enumerate(grid.paths):
        y = header_h + row * (cell_size + gap)
        parts.append(
            f'<text x="{label_w - 6}" y="{y + cell_size - 3}" text-anchor="end">'
            f"{html.escape(path)}</text>"
        )
        for col, week in enumerate(grid.weeks):
            x = label_w + col * (cell_size + gap)
            value = grid.cell(path, week)
            idx = _shade_index(value, ceiling)
            fill = _SVG_COLORS[idx] if idx > 0 else "#1b1d27"
            title = f"{path} · {week.isoformat()} · {value} death(s)"
            parts.append(
                f'<rect x="{x}" y="{y}" width="{cell_size}" height="{cell_size}" '
                f'rx="2" ry="2" fill="{fill}"><title>{html.escape(title)}</title></rect>'
            )

    footer_y = header_h + len(grid.paths) * (cell_size + gap) + 24
    parts.append(
        f'<text x="10" y="{footer_y}" fill="#a6adc8">'
        f"total deaths: {grid.stats.total_deaths}</text>"
    )
    if grid.stats.top_paths:
        line = ", ".join(f"{count}× {p}" for p, count in grid.stats.top_paths)
        parts.append(
            f'<text x="10" y="{footer_y + 16}" fill="#a6adc8">top: '
            f"{html.escape(line)}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _empty_svg(grid: HeatmapGrid) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="80" '
        f'font-family="ui-monospace, monospace" font-size="12" fill="#cdd6f4">'
        f'<rect width="100%" height="100%" fill="#0f1117"/>'
        f'<text x="10" y="30" fill="#f8f8f2">link-rot heatmap '
        f'({_fmt_ts(grid.since_ts)} → {_fmt_ts(grid.until_ts)})</text>'
        f'<text x="10" y="56" fill="#a6adc8">No probe history in this window.</text>'
        f"</svg>"
    )


# -- HTML -------------------------------------------------------------------


def render_html(grid: HeatmapGrid) -> str:
    svg = render_svg(grid)
    stats_rows = "".join(
        f"<tr><td>{html.escape(p)}</td><td style='text-align:right'>{c}</td></tr>"
        for p, c in grid.stats.top_paths
    )
    hosts_rows = "".join(
        f"<tr><td>{html.escape(h)}</td><td style='text-align:right'>{d}</td></tr>"
        for h, d in grid.stats.mtbf_per_host
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>link-coroner — link-rot heatmap</title>
<style>
  body {{ background:#0f1117; color:#cdd6f4; font-family: ui-monospace, Menlo, monospace; padding:24px; }}
  h1 {{ font-size: 18px; margin: 0 0 12px; }}
  table {{ border-collapse: collapse; margin-top: 16px; }}
  td, th {{ padding: 4px 12px; border-bottom: 1px solid #2a2d3a; }}
  .meta {{ color:#a6adc8; font-size:12px; }}
</style></head>
<body>
  <h1>🪦 link-rot heatmap</h1>
  <div class="meta">{_fmt_ts(grid.since_ts)} → {_fmt_ts(grid.until_ts)} · {grid.stats.total_deaths} total deaths</div>
  {svg}
  <h2 style="font-size:14px;margin-top:24px">Top rotting paths</h2>
  <table>{stats_rows or '<tr><td colspan=2 class=meta>none</td></tr>'}</table>
  <h2 style="font-size:14px;margin-top:24px">Worst MTBF hosts (days/death)</h2>
  <table>{hosts_rows or '<tr><td colspan=2 class=meta>none</td></tr>'}</table>
</body></html>
"""


__all__ = [
    "HeatmapGrid",
    "HeatmapStats",
    "build_grid",
    "render_ansi",
    "render_html",
    "render_svg",
]
