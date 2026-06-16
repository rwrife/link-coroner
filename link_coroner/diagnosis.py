"""Cause-of-death taxonomy + diagnosis helpers (M3).

The probe layer (``link_coroner.forensics.probe``) emits short raw reason
strings like ``HTTP_404`` or ``CONN_ERROR:...`` so it stays small and easy to
test. This module turns those into a stable, user-facing :class:`Cause`
enum that the reporting layer (death certificates, JSON output) consumes.

Keeping the taxonomy here — not in ``probe`` — lets us evolve it (TLS,
soft-404, drift…) without churning the probe internals.
"""

from __future__ import annotations

from enum import StrEnum

from .forensics.probe import ProbeResult, Verdict


class Cause(StrEnum):
    """Cause of death (or life) for an autopsied URL."""

    ALIVE = "ALIVE"
    NXDOMAIN = "NXDOMAIN"
    DNS_FAILURE = "DNS_FAILURE"
    CONN_REFUSED = "CONN_REFUSED"
    TLS_EXPIRED = "TLS_EXPIRED"
    TLS_ERROR = "TLS_ERROR"
    HTTP_4XX = "HTTP_4XX"
    HTTP_5XX = "HTTP_5XX"
    TIMEOUT = "TIMEOUT"
    REDIRECT_LOOP = "REDIRECT_LOOP"
    BAD_URL = "BAD_URL"
    UNKNOWN = "UNKNOWN"


_CAUSE_BLURBS: dict[Cause, str] = {
    Cause.ALIVE: "Patient is breathing.",
    Cause.NXDOMAIN: "Domain does not exist in DNS — long since buried.",
    Cause.DNS_FAILURE: "DNS resolver could not be reached or returned no answer.",
    Cause.CONN_REFUSED: "Host is up, but the door is bolted shut.",
    Cause.TLS_EXPIRED: "TLS certificate is expired — death by neglect.",
    Cause.TLS_ERROR: "TLS handshake failed.",
    Cause.HTTP_4XX: "Server replied 4xx — page is gone or forbidden.",
    Cause.HTTP_5XX: "Server is hemorrhaging 5xx — internal organ failure.",
    Cause.TIMEOUT: "No response within the autopsy window.",
    Cause.REDIRECT_LOOP: "Caught in an eternal redirect — vertigo unto death.",
    Cause.BAD_URL: "URL itself is malformed — dead on arrival.",
    Cause.UNKNOWN: "Cause undetermined; further forensics required.",
}


def cause_blurb(cause: Cause) -> str:
    """Return a short human-readable description for ``cause``."""
    return _CAUSE_BLURBS.get(cause, "Cause undetermined; further forensics required.")


def _classify_conn_error(detail: str) -> Cause:
    text = detail.lower()
    if "certificate" in text or "ssl" in text or "tls" in text:
        if "expired" in text or "has expired" in text:
            return Cause.TLS_EXPIRED
        return Cause.TLS_ERROR
    if "refused" in text:
        return Cause.CONN_REFUSED
    if "timed out" in text or "timeout" in text:
        return Cause.TIMEOUT
    return Cause.DNS_FAILURE if "name or service" in text or "resolve" in text else Cause.UNKNOWN


def diagnose(result: ProbeResult) -> Cause:
    """Map a :class:`ProbeResult` to a :class:`Cause`.

    The mapping is intentionally defensive: anything we don't recognise
    falls back to :attr:`Cause.UNKNOWN` rather than crashing.
    """
    reason = result.reason or ""
    status = result.status_code

    if result.verdict is Verdict.ALIVE:
        return Cause.ALIVE

    # Fast-path on probe's raw reason tags.
    if reason == "NXDOMAIN":
        return Cause.NXDOMAIN
    if reason in {"DNS_TIMEOUT", "DNS_NO_ANSWER"} or reason.startswith("DNS_ERROR"):
        return Cause.DNS_FAILURE
    if reason == "TIMEOUT":
        return Cause.TIMEOUT
    if reason == "REDIRECT_LOOP":
        return Cause.REDIRECT_LOOP
    if reason == "BAD_URL":
        return Cause.BAD_URL
    if reason.startswith("CONN_ERROR"):
        return _classify_conn_error(reason)
    if reason.startswith("HTTP_ERROR"):
        # httpx wrapped something we couldn't classify earlier.
        return Cause.UNKNOWN

    # HTTP_<n> reasons — split on status code.
    if status is not None:
        if 400 <= status < 500:
            return Cause.HTTP_4XX
        if 500 <= status < 600:
            return Cause.HTTP_5XX

    return Cause.UNKNOWN


# ---- severity / exit-code policy --------------------------------------------------

# Higher numbers = more serious. Used by the CLI severity threshold flag.
_VERDICT_SEVERITY: dict[Verdict, int] = {
    Verdict.ALIVE: 0,
    Verdict.UNREACHABLE: 1,
    Verdict.DEAD: 2,
}


def verdict_severity(verdict: Verdict) -> int:
    """Return the numeric severity of ``verdict`` (alive=0, unreachable=1, dead=2)."""
    return _VERDICT_SEVERITY.get(verdict, 0)


def exit_code_for(
    results: list[ProbeResult],
    *,
    threshold: Verdict = Verdict.DEAD,
) -> int:
    """Return the CLI exit code given results and a severity ``threshold``.

    Exits non-zero (1) if any result's severity is >= the threshold's severity.
    """
    min_sev = verdict_severity(threshold)
    for r in results:
        if verdict_severity(r.verdict) >= min_sev:
            return 1
    return 0
