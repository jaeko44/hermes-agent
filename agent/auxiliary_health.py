"""Endpoint identity for auxiliary custom-provider health checks."""
import contextlib
from typing import Any, Optional

from hermes_cli.route_identity import normalize_route_base_url

def _unhealthy_cache_key(provider: str, base_url: Optional[str] = None) -> Any:
    """Provider-wide key, or endpoint-specific key for an explicit custom endpoint — prefixed with the
    active profile home: a 402 on profile A's account must not hide the provider from profile B's
    (differently funded) account in the same multiplexed process."""
    from agent.auxiliary_client import _normalize_chain_label
    from hermes_constants import hermes_home_key
    label = _normalize_chain_label(provider)
    endpoint = normalize_route_base_url(_custom_health_base_url(provider, base_url))
    home_key = hermes_home_key()
    if endpoint:
        return home_key, "custom-endpoint", endpoint
    return home_key, label


def _custom_health_base_url(provider: str, explicit_base_url: Optional[str] = None) -> str:
    """Return the concrete custom endpoint used to scope health and failed-route checks."""
    from agent.auxiliary_client import _current_custom_base_url
    explicit = str(explicit_base_url or "").strip()
    from agent.auxiliary_client import _normalize_chain_label
    label = _normalize_chain_label(provider)
    if label == "local/custom":
        return explicit or _current_custom_base_url()
    if label.startswith("custom:") and explicit:
        return explicit
    with contextlib.suppress(ImportError):
        from hermes_cli.runtime_provider import _get_named_custom_provider, _resolves_to_custom
        if _resolves_to_custom(label):
            return explicit or _current_custom_base_url()
        entry = _get_named_custom_provider(provider)
        if entry:
            return explicit or str(entry.get("base_url") or "").strip()
    return ""




def fallback_candidate_unavailable_reason(exc: Exception) -> Optional[str]:
    """Why a fallback candidate cannot serve this walk (``_FALLBACK_REASONS`` label), or None.

    The same capacity classes that admitted the primary failure into the chain (payment/quota,
    rate limit, connection, route-incompatible model, malformed response) mean "this lane is out
    for now, try the next configured one"; anything else (a 400 request-shape error, a ValueError)
    is the caller's bug and must still propagate. Auth errors are excluded on purpose: they have
    their own refresh-then-quarantine path in the candidate helpers (#106367)."""
    from agent.auxiliary_client import _FALLBACK_REASONS
    return next(
        (label for predicate, label in _FALLBACK_REASONS if label != "auth error" and predicate(exc)),
        None,
    )


# Quarantine hold per unavailable-reason label. Payment/quota depletion and a dead credential
# last hours, so those keep the long default TTL (None); a per-minute 429, a dropped connection or
# a garbled body clears in seconds — holding the lane for 10 minutes process-wide would hide a
# healthy fallback from every aux task over one transient blip.
_TRANSIENT_CANDIDATE_QUARANTINE_SECONDS = 60.0
_CANDIDATE_QUARANTINE_TTL: dict[str, Optional[float]] = {
    "rate limit": _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS,
    "connection error": _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS,
    "invalid provider response": _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS,
}

# A declared reset shorter than this is indistinguishable from the 60s blip hold, so the table
# answers for it; longer than the cap, the hold saturates (see declared_reset_quarantine_ttl).
_MIN_DECLARED_RESET_QUARANTINE_SECONDS = 300.0
_MAX_DECLARED_RESET_QUARANTINE_SECONDS = 24 * 3600.0


def declared_reset_quarantine_ttl(exc: Optional[Exception]) -> Optional[float]:
    """Seconds to hide a lane the provider itself said is out for a while, or None.

    The per-reason table below assumes a 429 is a per-minute blip. It is not always one: a
    quota EXHAUSTION is also a 429, and providers declare the real window (Retry-After,
    ``resets_at``/``resets_in_seconds``, x-ratelimit-reset, or a duration in the message). The
    main path has always sized its primary cooldown from that declaration
    (``agent/fallback_cooldown.py`` -> ``extract_api_error_context``); the aux quarantine did
    not, so an exhausted tier was re-probed once a minute for the entire window — 881 wasted
    calls + log lines against opencode-go's monthly quota over 2026-09-19..27 while the main
    agent correctly waited ~21 days. Reuse the same extractor so both layers read one clock.

    Capped at 24h: a long window still deserves a periodic real probe (the key may be topped
    up, or the window may be mis-declared), and a cap bounds how stale the decision can get.
    """
    if exc is None:
        return None
    try:
        from agent.agent_runtime_helpers import extract_api_error_context
        from agent.fallback_cooldown import _provider_reset_delay
        delay = _provider_reset_delay(extract_api_error_context(exc).get("reset_at"))
    except Exception:  # noqa: BLE001 - a missing extractor must not change quarantine behaviour
        return None
    if delay is None or delay < _MIN_DECLARED_RESET_QUARANTINE_SECONDS:
        return None
    return min(delay, _MAX_DECLARED_RESET_QUARANTINE_SECONDS)


def fallback_candidate_quarantine_ttl(
    reason: Optional[str], exc: Optional[Exception] = None,
) -> Optional[float]:
    """Seconds to hide a fallback candidate for ``reason`` (a ``_FALLBACK_REASONS`` label, or None
    for a stale credential); None means the long default TTL.

    A provider-declared reset outranks the table: "rate limit" means 60s only when the provider
    said nothing about how long. ``exc`` is the failure that produced ``reason``; callers that
    have it must pass it, or an exhausted lane is re-probed every minute until it recovers.
    """
    declared = declared_reset_quarantine_ttl(exc)
    if declared is not None:
        return declared
    return _CANDIDATE_QUARANTINE_TTL.get(reason or "")
