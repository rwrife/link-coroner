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
🚧 Pre-alpha (M4 — soft-404 + parked-domain detection). See [PLAN.md](./PLAN.md).

## Install (dev)
```bash
uv venv && uv pip install -e ".[dev]"
```

## Usage
```bash
link-coroner --version
link-coroner scan path/to/repo                # list URLs only
link-coroner autopsy path/to/repo              # probe + render death certificates
link-coroner autopsy . --format table          # compact table view (M2-style)
link-coroner autopsy . --format json           # machine-readable, includes cause + blurb
link-coroner autopsy . --fail-on suspicious    # also exit non-zero on UNREACHABLE
link-coroner autopsy . --concurrency 32 --per-host 8 --timeout 5
```

### Output formats
- `pretty` (default) — rich-rendered **death certificate** per deceased URL + summary footer.
- `certificates` — explicit alias of `pretty`.
- `table` — compact table of every result (good for >100 URLs).
- `json` — every result with the M3+ cause taxonomy (`ALIVE`, `NXDOMAIN`, `DNS_FAILURE`, `CONN_REFUSED`, `TLS_EXPIRED`, `TLS_ERROR`, `HTTP_4XX`, `HTTP_5XX`, `TIMEOUT`, `REDIRECT_LOOP`, `SOFT_404`, `PARKED`, `BAD_URL`, `UNKNOWN`).

### Suspicious 200s (M4)
A URL that returns 200 isn't automatically alive. `link-coroner` sniffs HTML
bodies for **soft-404** templates ("page not found", tiny 404 pages) and
**parked / for-sale domains** (Sedo, HugeDomains, Afternic, etc.) and downgrades
those to `UNREACHABLE` with a `SOFT_404` or `PARKED` cause. Disable with the
library-level `ProbeConfig(detect_soft_404=False)` if you need pure status-code
behaviour.

### Exit codes
- `--fail-on dead` (default) — exit 1 if any URL is `DEAD`.
- `--fail-on suspicious` — exit 1 on `DEAD` _or_ `UNREACHABLE`.
- `--fail-on never` (or `--no-fail-on-dead`) — always exit 0.

## Why
Existing link checkers print a status code and exit. `link-coroner` tells you _what killed it_, _when_, and _where the body is buried_ — with personality.

## License
MIT (TBD).
