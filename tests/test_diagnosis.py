"""Tests for the M3 cause-of-death taxonomy."""

from __future__ import annotations

from link_coroner.diagnosis import (
    Cause,
    cause_blurb,
    diagnose,
    exit_code_for,
    verdict_severity,
)
from link_coroner.forensics.probe import ProbeResult, Verdict


def _r(verdict: Verdict, reason: str = "", status: int | None = None) -> ProbeResult:
    return ProbeResult(url="https://x.example/", verdict=verdict, reason=reason, status_code=status)


def test_alive_diagnosis():
    assert diagnose(_r(Verdict.ALIVE, "HTTP_200", 200)) is Cause.ALIVE


def test_nxdomain_diagnosis():
    assert diagnose(_r(Verdict.UNREACHABLE, "NXDOMAIN")) is Cause.NXDOMAIN


def test_dns_failure_variants():
    assert diagnose(_r(Verdict.UNREACHABLE, "DNS_TIMEOUT")) is Cause.DNS_FAILURE
    assert diagnose(_r(Verdict.UNREACHABLE, "DNS_NO_ANSWER")) is Cause.DNS_FAILURE
    assert diagnose(_r(Verdict.UNREACHABLE, "DNS_ERROR:Boom")) is Cause.DNS_FAILURE


def test_http_status_split():
    assert diagnose(_r(Verdict.DEAD, "HTTP_404", 404)) is Cause.HTTP_4XX
    assert diagnose(_r(Verdict.DEAD, "HTTP_503", 503)) is Cause.HTTP_5XX


def test_timeout_and_redirect_loop():
    assert diagnose(_r(Verdict.UNREACHABLE, "TIMEOUT")) is Cause.TIMEOUT
    assert diagnose(_r(Verdict.DEAD, "REDIRECT_LOOP")) is Cause.REDIRECT_LOOP


def test_conn_error_subclassification():
    assert diagnose(_r(Verdict.UNREACHABLE, "CONN_ERROR:Connection refused")) is Cause.CONN_REFUSED
    assert (
        diagnose(_r(Verdict.UNREACHABLE, "CONN_ERROR:SSL certificate has expired"))
        is Cause.TLS_EXPIRED
    )
    assert (
        diagnose(_r(Verdict.UNREACHABLE, "CONN_ERROR:[SSL] bad handshake"))
        is Cause.TLS_ERROR
    )


def test_bad_url_and_unknown():
    assert diagnose(_r(Verdict.UNREACHABLE, "BAD_URL")) is Cause.BAD_URL
    assert diagnose(_r(Verdict.UNREACHABLE, "weird-thing-we-dont-know")) is Cause.UNKNOWN


def test_cause_blurbs_present_for_every_cause():
    for cause in Cause:
        assert cause_blurb(cause), f"missing blurb for {cause}"


def test_verdict_severity_ordering():
    assert verdict_severity(Verdict.ALIVE) < verdict_severity(Verdict.UNREACHABLE)
    assert verdict_severity(Verdict.UNREACHABLE) < verdict_severity(Verdict.DEAD)


def test_exit_code_threshold_dead():
    results = [_r(Verdict.ALIVE), _r(Verdict.UNREACHABLE, "TIMEOUT")]
    assert exit_code_for(results, threshold=Verdict.DEAD) == 0
    assert exit_code_for(results, threshold=Verdict.UNREACHABLE) == 1


def test_exit_code_includes_dead():
    results = [_r(Verdict.ALIVE), _r(Verdict.DEAD, "HTTP_404", 404)]
    assert exit_code_for(results, threshold=Verdict.DEAD) == 1
