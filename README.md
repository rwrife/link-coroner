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
🚧 Pre-alpha (M2 — basic autopsy). See [PLAN.md](./PLAN.md).

## Install (dev)
```bash
uv venv && uv pip install -e ".[dev]"
```

## Usage
```bash
link-coroner --version
link-coroner scan path/to/repo            # list URLs only
link-coroner autopsy path/to/repo          # probe + verdict each URL
link-coroner autopsy . --format json       # machine-readable
link-coroner autopsy . --concurrency 32 --per-host 8 --timeout 5
```

`autopsy` exits non-zero if any links come back **DEAD** (disable with `--no-fail-on-dead`).
M2 verdicts are intentionally coarse — `ALIVE | DEAD | UNREACHABLE`. Full cause-of-death
taxonomy + death certificate rendering land in M3.

## Why
Existing link checkers print a status code and exit. `link-coroner` tells you _what killed it_, _when_, and _where the body is buried_ — with personality.

## License
MIT (TBD).
