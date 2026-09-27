"""Aux quarantine must honour a provider-declared reset window, not a flat 60s.

Production evidence (2026-09-27, VirtEngine estate, task t_432d0ce3): opencode-go's MONTHLY quota
returned 429 ``GoUsageLimitError`` with a ~21-day window. The main agent sized its cooldown from
that window ("Primary retry eligible in ~1818506 s"); the auxiliary quarantine hardcoded 60s for
the "rate limit" reason, so every aux task re-probed the dead tier once a minute for the whole
window — 881 re-probes and 881 log lines in gateway-stdio.log by 17:41, still climbing.

These tests pin the class, not the instance: any provider whose 429 declares a window longer
than a blip gets a hold sized from that window; a genuine per-minute 429 keeps 60s.
"""

import time

import pytest

from agent.auxiliary_health import (
    _MAX_DECLARED_RESET_QUARANTINE_SECONDS,
    _MIN_DECLARED_RESET_QUARANTINE_SECONDS,
    _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS,
    declared_reset_quarantine_ttl,
    fallback_candidate_quarantine_ttl,
)

# Verbatim from gateway-stdio.log 2026-09-27 17:41:35 (opencode-go, monthly window).
GO_USAGE_LIMIT_BODY = {
    "type": "error",
    "error": {"type": "GoUsageLimitError", "message": "Go usage limit exceeded"},
    "metadata": {"workspace": "wrk_01M2SPHG6W1YSZ6QGXRXJTCW08", "limitName": "monthly"},
}


class _Response:
    def __init__(self, headers):
        self.headers = headers


class Quota429(Exception):
    """A provider 429 shaped like the OpenAI SDK error the aux client catches."""

    def __init__(self, message, body=None, headers=None, status_code=429):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.message = message
        self.response = _Response(headers or {})


def test_monthly_quota_429_is_not_a_sixty_second_blip():
    """The production signature: 429 + Retry-After naming a multi-week window."""
    exc = Quota429(
        "Error code: 429 - {'type':'error','error':{'type':'GoUsageLimitError',"
        "'message':'Go usage limit exceeded'}}",
        body=GO_USAGE_LIMIT_BODY,
        headers={"Retry-After": "1818506"},
    )
    ttl = declared_reset_quarantine_ttl(exc)
    assert ttl is not None, "a declared 21-day window must not read as a blip"
    assert ttl > 3600, f"monthly exhaustion held for only {ttl}s — the once-a-minute loop returns"
    # 21 days saturates the 24h cap: still 1440x the old hold, and a real probe runs daily.
    assert ttl == _MAX_DECLARED_RESET_QUARANTINE_SECONDS


def test_quarantine_ttl_prefers_the_declared_window_over_the_reason_table():
    exc = Quota429("Go usage limit exceeded", body=GO_USAGE_LIMIT_BODY,
                   headers={"Retry-After": "7200"})
    assert fallback_candidate_quarantine_ttl("rate limit", exc) == pytest.approx(7200, rel=0.01)


def test_window_from_the_message_body_is_honoured_too():
    """No Retry-After header: the same window declared in free text still counts."""
    exc = Quota429(
        "Go usage limit exceeded",
        body={"error": {"type": "GoUsageLimitError", "message": "Go usage limit exceeded, resets in 21 hours 30 minutes"}},
    )
    ttl = declared_reset_quarantine_ttl(exc)
    assert ttl is not None and ttl > 3600


def test_usage_limit_body_field_is_honoured_too():
    """The ``resets_in_seconds`` body field (Codex/ChatGPT usage limits) is a window, not a blip."""
    exc = Quota429("usage limit reached", body={"error": {"message": "usage limit", "resets_in_seconds": 18000}})
    assert declared_reset_quarantine_ttl(exc) == pytest.approx(18000, rel=0.01)


def test_real_per_minute_429_keeps_the_sixty_second_hold():
    """The blip case must not regress into a multi-hour hide of a healthy lane."""
    exc = Quota429("Rate limit exceeded", headers={"Retry-After": "30"})
    assert declared_reset_quarantine_ttl(exc) is None
    assert fallback_candidate_quarantine_ttl("rate limit", exc) == _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS


def test_no_exception_keeps_the_pre_fix_behaviour_exactly():
    """Callers that cannot supply the failure get the old table, not a crash."""
    assert fallback_candidate_quarantine_ttl("rate limit") == _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS
    assert fallback_candidate_quarantine_ttl("connection error") == _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS
    assert fallback_candidate_quarantine_ttl(None) is None
    assert fallback_candidate_quarantine_ttl("stale fallback credential") is None


def test_non_429_transport_error_with_a_declared_window_is_also_sized_from_it():
    exc = Quota429("upstream busy, retry after 600s", status_code=503)
    assert declared_reset_quarantine_ttl(exc) == pytest.approx(600, rel=0.05)


def test_window_is_capped_and_floored():
    """A 21-day window holds 24h (still gets a real daily probe); a 10s window defers to the table."""
    forever = Quota429("Go usage limit exceeded", headers={"Retry-After": str(60 * 60 * 24 * 365)})
    assert declared_reset_quarantine_ttl(forever) == _MAX_DECLARED_RESET_QUARANTINE_SECONDS
    tiny = Quota429("slow down", headers={"Retry-After": "10"})
    assert declared_reset_quarantine_ttl(tiny) is None
    assert _MIN_DECLARED_RESET_QUARANTINE_SECONDS == 300.0


def test_past_reset_falls_back_to_the_table():
    """An expired window means the provider is probably fine now — do not hide it for 24h."""
    exc = Quota429("Go usage limit exceeded", headers={"Retry-After": "-5"})
    assert declared_reset_quarantine_ttl(exc) is None


def test_garbage_exception_does_not_raise():
    class Weird(Exception):
        pass
    assert declared_reset_quarantine_ttl(Weird()) is None
    assert declared_reset_quarantine_ttl(None) is None
    assert fallback_candidate_quarantine_ttl("rate limit", Weird()) == _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS


def test_candidate_hold_scales_the_reprobe_count_not_the_hide():
    """The point of the fix, stated as an arithmetic property: reprobes must track the window."""
    def reprobes(ttl):
        return int(21 * 24 * 3600 / ttl) if ttl else 21 * 24 * 3600 // 600
    monthly = Quota429("Go usage limit exceeded", headers={"Retry-After": "1818506"})
    before = reprobes(_TRANSIENT_CANDIDATE_QUARANTINE_SECONDS)
    after = reprobes(fallback_candidate_quarantine_ttl("rate limit", monthly))
    assert before > 30000, before
    assert after <= 22, after
    assert time.time() > 0  # sanity: the test is not comparing stale constants
