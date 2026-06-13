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
🚧 Pre-alpha. See [PLAN.md](./PLAN.md).

## Why
Existing link checkers print a status code and exit. `link-coroner` tells you _what killed it_, _when_, and _where the body is buried_ — with personality.

## License
MIT (TBD).
