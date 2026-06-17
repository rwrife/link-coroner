# link-coroner — PLAN.md

> _"Time of death: 14:03 UTC. Cause: DNS_NXDOMAIN. The body is cold."_

## 1. Pitch
`link-coroner` is a CLI forensic pathologist for the dead links rotting inside your repo. It crawls markdown, source, and docs for URLs, performs a thorough autopsy (DNS, TLS, HTTP, soft-404, parked-domain, content-drift), and issues a printable **death certificate** for every corpse — complete with cause of death, estimated time of death, and a Wayback Machine résurrection suggestion. Built to be funny, fast, and CI-friendly.

## 2. Trend inspiration
- The "TUI / CLI renaissance" coverage (Posting, Serpl, gum, etc.) — devs love opinionated, character-driven CLIs in 2026. ([Terminal Renaissance article](https://1337skills.com/blog/2026-03-09-terminal-renaissance-modern-tui-tools-reshaping-developer-workflows/))
- The endless "best CLI tools 2026" / "awesome-cli" lists popping up — proves there's still hunger for novel single-purpose tools. ([awesome-cli-tools-2026](https://github.com/spinov001-art/awesome-cli-tools-2026))
- MCP/agent ecosystem chatter on HN about supply-chain rot and link-decay in AI-generated docs — agents cite URLs that die within months. ([MCP security on HN](https://news.ycombinator.com/item?id=47356600))
- Documentation-drift complaints on r/commandline and r/dataengineering: "half my README links 404 and I never noticed."

## 3. Why it's different
Existing tools (`lychee`, `muffet`, `markdown-link-check`, `linkchecker`) just print a status code and exit. They're sterile.

`link-coroner` is different because:
- **Forensic depth, not just status codes.** It distinguishes hard 404 from soft-404, parked domain, DNS death, TLS expiry, and "alive but content swapped" (e.g., domain was sold).
- **Personality + actionable output.** A death certificate per URL with cause, time-of-death estimate (via Wayback first-miss bisection), and a suggested replacement.
- **Single binary / one-shot UX**, not a CI plugin you have to configure for 20 minutes.
- **Designed for AI-era docs**, where LLM-generated READMEs are full of hallucinated or stale links — a "verify before merge" hook.

Distinct from sibling repos in this lab: not a roaster (commit-roast), not a data tool (schema-seance), not a regex game (regex-rumble).

## 4. MVP scope (v0.1)
- `link-coroner scan <path>` — recursively scan a directory for URLs in `.md`, `.mdx`, `.txt`, `.rst`, `.html`, plus `# comments` of `.py/.js/.ts/.go/.rs`.
- For each unique URL, perform: DNS lookup → TCP → HTTPS handshake → HTTP HEAD (fallback GET) → response classification.
- Concurrent worker pool (configurable, default 16) with timeout + retry.
- Output modes: `--format pretty` (death certificates), `--format json`, `--format junit`.
- Exit non-zero if any "deceased" links found (configurable severity threshold).
- Caching layer so a re-run skips healthy URLs within TTL.

## 5. Tech stack
- **Language:** Python 3.11+ (boring, fast to ship, rich ecosystem for HTTP + parsing).
- **HTTP:** `httpx` (async, HTTP/2, sane timeouts).
- **CLI:** `typer` + `rich` for the certificate rendering.
- **DNS:** `dnspython`.
- **Markdown/HTML parsing:** `markdown-it-py` + `selectolax`.
- **Caching:** SQLite via `sqlite3` stdlib.
- **Packaging:** `uv` + `pyproject.toml`, single entry-point script. PyPI later.
- **Tests:** `pytest` + `respx` for mocking httpx.

Justification: Python keeps iteration fast and lets us ship a working v0.1 in hours; `httpx` + `rich` give us async perf and gorgeous output without much code.

## 6. Architecture
```
link_coroner/
  cli.py            # typer entry-points
  scanner/
    walker.py       # filesystem walker
    extractors.py   # md/html/source URL extraction
  forensics/
    dns_probe.py
    tls_probe.py
    http_probe.py
    soft404.py      # heuristics for parked / soft-404
    drift.py        # content-drift via title/hash compare
  diagnosis.py      # combines probe results -> CauseOfDeath
  wayback.py        # Wayback Machine lookups + bisection
  reporting/
    pretty.py       # death certificates (rich)
    json_out.py
    junit_out.py
  cache.py          # sqlite TTL cache
  config.py
```

Pipeline: walker → extractor → dedupe → async probe fan-out → diagnosis → reporter.

## 7. Milestones
1. **M1 — scaffold + hello-world** — Repo layout, `uv`-managed env, `link-coroner --version`, `link-coroner scan PATH` that prints discovered URLs only. CI runs lint + tests.
2. **M2 — basic autopsy** — DNS + HTTP HEAD/GET probes; classifies each URL as `ALIVE | DEAD | UNREACHABLE`. Pretty + JSON output.
3. **M3 — death certificates & causes** — Rich-rendered certificate cards; cause taxonomy (NXDOMAIN, CONN_REFUSED, TLS_EXPIRED, HTTP_4XX, HTTP_5XX, TIMEOUT, REDIRECT_LOOP). Exit codes. **✅ Shipped.**
4. **M4 — soft-404 + parked-domain + drift detection** — Heuristics module; title/hash baselines via cache; flag "alive but content suspicious." Soft-404 + parked detection shipped; content-drift waits on M5 cache layer.
5. **M5 — Wayback resurrection** — For deceased URLs, query Wayback Machine, suggest closest snapshot + estimated time-of-death via bisection. `--rewrite` flag to patch files in-place.
6. **M6 — GitHub Action + pre-commit hook + JUnit/SARIF output** — Shippable action, docs, badges. v0.1.0 release.

## 8. Backlog / future features
- Persona modes (`--persona noir-detective`, `--persona victorian-doctor`, `--persona crime-scene-photographer`).
- ASCII chalk-outline rendering for the most egregious corpses.
- "Mortician mode": auto-PR that replaces dead URLs with archived snapshots.
- Slack/Discord webhook with daily "obituary digest" of newly-deceased links.
- Heatmap of link rot over time per repo (graph).
- LSP / editor integration to underline dying links live.
- Domain-squatter reputation feed.
- LLM-assisted "find the new home for this content" using fuzzy title matching.
- Site-map crawl mode (`scan --site https://...`).
- Robots.txt + rate-limit respect plus per-host concurrency.
- Sigstore-signed release attestations.
- MCP server wrapper so AI agents can ask "is this URL alive and what should I cite instead?"

## 9. Out of scope
- Full web crawler / search-engine indexing.
- Fixing CSS/JS issues on target sites.
- Authenticated/private-network link checking in v0.x (later, via plugins).
- Headless browser rendering (we'll only inspect raw HTML in v0).
- Native desktop GUI.
