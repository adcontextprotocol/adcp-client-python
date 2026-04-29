"""Defaults shipped on ``VerifyOptions``.

The verifier's ``replay_store`` defaults to a fresh
:class:`InMemoryReplayStore` so callers who omit it still get nonce-replay
protection — the alternative ("``None`` skips the check") was a silent
security regression for any caller who forgot to wire one.
"""

from __future__ import annotations

from adcp.signing.replay import InMemoryReplayStore
from adcp.signing.verifier import (
    VerifierCapability,
    VerifyOptions,
)


class _StubResolver:
    def __call__(self, _kid: str) -> None:
        return None


def _opts(**overrides) -> VerifyOptions:
    base = {
        "now": 0.0,
        "capability": VerifierCapability(),
        "operation": "create_media_buy",
        "jwks_resolver": _StubResolver(),
    }
    base.update(overrides)
    return VerifyOptions(**base)


def test_default_replay_store_is_in_memory() -> None:
    opts = _opts()
    assert isinstance(opts.replay_store, InMemoryReplayStore)


def test_explicit_none_replay_store_preserved() -> None:
    """Callers who genuinely want to bypass replay protection (e.g. integration
    tests) can still pass ``replay_store=None`` and get the previous skip-
    the-check behavior."""
    opts = _opts(replay_store=None)
    assert opts.replay_store is None


def test_each_verify_options_instance_gets_its_own_default_store() -> None:
    """``field(default_factory=...)`` constructs a fresh store per instance —
    so a replay seen on one verifier doesn't leak into another, the same
    isolation TS authenticator instances enforce."""
    a = _opts()
    b = _opts()
    assert a.replay_store is not b.replay_store


def test_default_replay_store_actually_dedup_replays() -> None:
    """End-to-end: the default store remembers a nonce across calls on the
    same VerifyOptions instance."""
    opts = _opts()
    store = opts.replay_store
    assert store is not None  # for mypy; preserved by the default
    assert not store.seen("kid", "nonce-1")
    store.remember("kid", "nonce-1", ttl_seconds=300)
    assert store.seen("kid", "nonce-1")


def test_revocation_stays_optional() -> None:
    """``revocation_checker`` and ``revocation_list`` are not defaulted —
    most agents don't track revocations at runtime, and the verifier
    correctly skips the check when both are absent."""
    opts = _opts()
    assert opts.revocation_checker is None
    assert opts.revocation_list is None


def test_default_covers_content_digest_is_required() -> None:
    """Body integrity must be authenticated end-to-end by default —
    ``"either"`` or ``"forbidden"`` lets a MITM inside TLS termination
    swap bodies on signed requests whose digest isn't covered.

    Operators who knowingly accept that tradeoff (e.g. a strict reverse-
    proxy boundary that owns body integrity at a different layer) opt
    out by constructing ``VerifierCapability(covers_content_digest=...)``
    explicitly. The default MUST be the secure choice."""
    cap = VerifierCapability()
    assert cap.covers_content_digest == "required"
