# 🪦 link-coroner

> A forensic pathologist for the dead links rotting inside your repo.

`link-coroner` scans your repo for URLs, performs an autopsy on each one, and issues a **death certificate** for every corpse — complete with cause of death, estimated time of death, and a Wayback Machine resurrection suggestion.

```
┌────────────────────────────────────────────┐
│         CERTIFICATE OF DEATH               │
├────────────────────────────────────────────┤
│ URL:    https://example.dev/old-post       │
│ Cause:  DNS_NXDOMAIN                       │
│ T.O.D:  ~ 2024-08-12 (Wayback bisection)   │
│ Resurrect: https://web.archive.org/...     │
└────────────────────────────────────────────┘
```

## Status
🚧 Pre-alpha (M5 — Wayback resurrection). See [PLAN.md](./PLAN.md).

## Install (dev)
```bash
uv venv && uv pip install -e ".[dev]"
```

## Usage
```bash
link-coroner --version
link-coroner scan path/to/repo                # list URLs only
link-coroner scan --site https://example.com   # discover URLs via sitemap.xml + robots.txt
link-coroner autopsy --site https://example.com  # autopsy a live deployment
link-coroner autopsy path/to/repo              # probe + render death certificates
link-coroner autopsy . --format table          # compact table view (M2-style)
link-coroner autopsy . --format json           # machine-readable, includes cause + blurb
link-coroner autopsy . --fail-on suspicious    # also exit non-zero on UNREACHABLE
link-coroner autopsy . --concurrency 32 --per-host 8 --timeout 5
link-coroner autopsy . --resurrect                # add Wayback snapshot links
link-coroner rewrite path/to/repo                 # dry-run patch dead URLs
link-coroner rewrite path/to/repo --apply          # actually rewrite (with .bak)
link-coroner mortician path/to/repo                # dry-run resurrection report
link-coroner mortician path/to/repo --apply --open-pr  # patch + open auto-PR
```

### Output formats
- `pretty` (default) — rich-rendered **death certificate** per deceased URL + summary footer.
- `certificates` — explicit alias of `pretty`.
- `table` — compact table of every result (good for >100 URLs).
- `json` — every result with the M3+ cause taxonomy (`ALIVE`, `NXDOMAIN`, `DNS_FAILURE`, `CONN_REFUSED`, `TLS_EXPIRED`, `TLS_ERROR`, `HTTP_4XX`, `HTTP_5XX`, `TIMEOUT`, `REDIRECT_LOOP`, `SOFT_404`, `PARKED`, `BAD_URL`, `UNKNOWN`).
- `junit` — JUnit XML; deceased URLs become `<failure>`, suspicious become `<error>`. Drop straight into Jenkins/GitHub/GitLab test reporters.
- `sarif` — SARIF 2.1.0 JSON for GitHub code scanning and other dashboards. Each cause is a `ruleId`.

Use `--output FILE` (`-o`) to write any format to a file instead of stdout — handy for CI artifacts:
```bash
link-coroner autopsy . --format sarif -o link-coroner.sarif
link-coroner autopsy docs --format junit -o reports/links.xml
```

## CI integrations (M6)

### GitHub Action
A composite action lives at the repo root. Drop it into a workflow:
```yaml
- uses: rwrife/link-coroner@main
  with:
    path: .
    format: sarif
    output: link-coroner.sarif
    fail-on: dead
- uses: github/codeql-action/upload-sarif@v3
  with:
    sarif_file: link-coroner.sarif
```

### Pre-commit hook
Add to `.pre-commit-config.yaml`:
```yaml
repos:
  - repo: https://github.com/rwrife/link-coroner
    rev: main
    hooks:
      - id: link-coroner
```
The hook runs `link-coroner autopsy .` with `--fail-on dead`.

### Suspicious 200s (M4)
A URL that returns 200 isn't automatically alive. `link-coroner` sniffs HTML
bodies for **soft-404** templates ("page not found", tiny 404 pages) and
**parked / for-sale domains** (Sedo, HugeDomains, Afternic, etc.) and downgrades
those to `UNREACHABLE` with a `SOFT_404` or `PARKED` cause. Disable with the
library-level `ProbeConfig(detect_soft_404=False)` if you need pure status-code
behaviour.

### Wayback resurrection (M5)
Pass `--resurrect` to `autopsy` to query the Wayback Machine for every
deceased URL. Each death certificate gets a `Resurrect at:` line pointing
at the closest archived snapshot, plus an estimated **time of death**
based on the most recent healthy CDX snapshot.

The `rewrite` command goes further: it probes URLs, asks Wayback for
snapshots, and patches the dead ones in place. Dry-run by default; pass
`--apply` to actually overwrite files (a `.bak` sibling is written for
each touched file unless you pass `--no-backup`).

### Mortician auto-PR (issue #8)
The `mortician` command is `rewrite` + an opinionated CI workflow. It
respects a `--policy` allowlist (per-URL or per-host) and, with
`--open-pr`, creates a branch, commits the resurrections, pushes, and
opens a pull request via `gh`. The PR body itemises every replacement
plus URLs that were skipped (policy) or had no Wayback snapshot.

```bash
link-coroner mortician . --policy .link-coroner-allowlist --apply --open-pr
```

Policy file format (one directive per line, `#` comments allowed):

```
# leave this exact URL alone
https://important.example/keep-me

# leave every URL on this host (and its subdomains) alone
host: facebook.com
```

### Link-rot heatmap (issue #22)
Track how your repo decays over time. Feed the SQLite cache from
scheduled `autopsy` runs, then render a calendar/heatmap of when links
died and which directories rot the fastest.

```bash
# 1. populate history (e.g. via nightly CI)
link-coroner autopsy . --cache .link-coroner-cache.sqlite --no-fail-on-dead

# 2. render a terminal heatmap of the last 90 days
link-coroner heatmap --cache .link-coroner-cache.sqlite

# 3. export an SVG/HTML report for your docs site or CI artifacts
link-coroner heatmap --format html --output rot.html --since 180d
```

Sample ANSI output:

```
link-rot heatmap  (2026-03-27 → 2026-06-25)

path        04-06 04-13 04-20 04-27 05-04 05-11 05-18 05-25 06-01 06-08 06-15 06-22
----------------------------------------------------------------------------------
docs/       ▒▒▒▒▒ ▒▒▒▒▒ ▓▓▓▓▓ ▓▓▓▓▓ █████ ▒▒▒▒▒ ▓▓▓▓▓ ▒▒▒▒▒ ····· ····· ▒▒▒▒▒ ▒▒▒▒▒
src/cli/    ····· ····· ▒▒▒▒▒ ····· ····· ▓▓▓▓▓ ····· ····· ····· ····· ····· ·····

legend: ' '   ···   ░░░   ▒▒▒   ▓▓▓   ███    (0 → ≥5 deaths/week)

total deaths: 27
top rotting paths:
    19  docs/
     6  src/cli/
     2  examples/
worst hosts (MTBF, days/death):
    7.5d  blog.dead-startup.com
   14.0d  old-cdn.example.net
```

Use `--since 24h|90d|12w` to change the window, `--path-depth N` to
bucket finer (default 2 segments), and `--no-color` for plain text. The
SVG/HTML exports embed inline tooltips so they drop straight into a
docs site or GitHub Pages artifact.

### Obituary digest webhook (issue #9)
Post a short "newly deceased URLs since last run" digest to a Slack or
Discord incoming webhook. State is persisted to a JSON file so each run
only reports *new* casualties (and any URLs that came back from the
dead).

```bash
link-coroner digest . \
  --webhook-url "$SLACK_OR_DISCORD_WEBHOOK" \
  --state-file .link-coroner-state.json
```

The provider is auto-detected from the webhook host (Slack vs. Discord);
override with `--provider slack|discord`. Use `--dry-run` to print the
JSON payload without sending it, or `--post-if-empty` to always send a
heartbeat — perfect for a daily cron job.

### Exit codes
- `--fail-on dead` (default) — exit 1 if any URL is `DEAD`.
- `--fail-on suspicious` — exit 1 on `DEAD` _or_ `UNREACHABLE`.
- `--fail-on never` (or `--no-fail-on-dead`) — always exit 0.

### Editor integration (LSP)
Underline dying links live while you write. `link-coroner lsp` speaks the
Language Server Protocol over stdio, so any LSP-capable editor can wire it up.

```bash
link-coroner lsp                              # plain mode
link-coroner lsp --cache .link-coroner.sqlite # share probe history with `heatmap`
```

Capabilities: push diagnostics (DEAD → Error, suspicious → Warning), hover
cards with cause-of-death + Wayback suggestion, and a "Replace with Wayback
snapshot" quick-fix code action.

**VSCode** (`settings.json` with the [`generic-lsp-client`](https://marketplace.visualstudio.com/items?itemName=llllvvuu.llllvvuu-generic-lsp-client) extension or any custom client):

```json
{
  "genericLspClient.servers": [
    {
      "id": "link-coroner",
      "command": ["link-coroner", "lsp"],
      "languages": ["markdown", "plaintext"]
    }
  ]
}
```

**Neovim** (via [`nvim-lspconfig`](https://github.com/neovim/nvim-lspconfig) custom config):

```lua
local configs = require("lspconfig.configs")
configs.link_coroner = {
  default_config = {
    cmd = { "link-coroner", "lsp" },
    filetypes = { "markdown", "text" },
    root_dir = require("lspconfig.util").root_pattern(".git", "."),
  },
}
require("lspconfig").link_coroner.setup({})
```

**Helix** (`languages.toml`):

```toml
[[language]]
name = "markdown"
language-servers = ["link-coroner"]

[language-server.link-coroner]
command = "link-coroner"
args = ["lsp"]
```

### MCP server (issue #10)

Expose link-coroner to AI agents over the Model Context Protocol so they
can ask "is this URL alive, and what should I cite instead?" inline
while generating text.

```bash
link-coroner mcp   # speaks line-delimited JSON-RPC 2.0 on stdio
```

Tools advertised:

- `autopsy_url` — verdict + cause of death for a single URL.
- `autopsy_urls` — batch autopsy with one verdict per URL.
- `find_replacement` — Wayback Machine snapshot suggestion for a dead URL.

Wire it into any MCP-capable client (Claude Desktop, Continue,
openclaw, etc.) by pointing at the `link-coroner mcp` command as a
stdio server.

### README badge (issue #27)

Generate a shields-style "🪦 link health" badge from your latest autopsy.
Works from either a JSON results file or the SQLite cache (latest verdict
per URL wins, so a fixed link stops counting as dead):

```bash
# 1. Scan and dump JSON (or use --cache .link-coroner-cache.sqlite).
link-coroner autopsy --format json -o results.json

# 2. Render a self-contained SVG for docs/links.svg.
link-coroner badge --from results.json --format svg -o docs/links.svg

# 3. Or emit shields.io endpoint JSON to host somewhere static.
link-coroner badge --from results.json --format shields-endpoint \
    -o docs/link-coroner.json

# 4. Or grab a ready-to-paste Markdown snippet.
link-coroner badge --from results.json --format markdown --label "link health"
# → ![link health: 🪦 0 dead / 12 alive](https://img.shields.io/badge/...)
```

Colours follow worst-severity rules: **brightgreen** when nothing is dead
or suspicious, **yellow** when only soft-404 / parked entries exist, and
**red** the moment any link is fully deceased.

### Personas
Swap the narrator voice on every death certificate with `--persona`:

```bash
link-coroner autopsy ./docs --persona noir-detective
link-coroner autopsy ./docs --persona victorian-doctor
link-coroner autopsy ./docs --persona crime-scene-photographer
link-coroner autopsy ./docs --persona deadpan-medical-examiner
link-coroner personas   # list all available voices
```

The default `coroner` persona keeps the original, formal pathologist
copy. JSON output adds a `persona`/`persona_blurb` field when a
non-default persona is selected so downstream tools can render the
flavored text alongside the canonical taxonomy.

## Why
Existing link checkers print a status code and exit. `link-coroner` tells you _what killed it_, _when_, and _where the body is buried_ — with personality.

## License
MIT (TBD).
