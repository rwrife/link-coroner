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
link-coroner autopsy path/to/repo              # probe + render death certificates
link-coroner autopsy . --format table          # compact table view (M2-style)
link-coroner autopsy . --format json           # machine-readable, includes cause + blurb
link-coroner autopsy . --fail-on suspicious    # also exit non-zero on UNREACHABLE
link-coroner autopsy . --concurrency 32 --per-host 8 --timeout 5
link-coroner autopsy . --resurrect                # add Wayback snapshot links
link-coroner rewrite path/to/repo                 # dry-run patch dead URLs
link-coroner rewrite path/to/repo --apply          # actually rewrite (with .bak)
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

### Exit codes
- `--fail-on dead` (default) — exit 1 if any URL is `DEAD`.
- `--fail-on suspicious` — exit 1 on `DEAD` _or_ `UNREACHABLE`.
- `--fail-on never` (or `--no-fail-on-dead`) — always exit 0.

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
