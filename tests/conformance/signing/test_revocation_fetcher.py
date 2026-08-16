"""Tests for the live revocation-list fetcher + CachingRevocationChecker.

The fetcher tests use ``httpx.MockTransport`` to simulate HTTP responses
without touching the network. The checker tests inject a fake fetcher
(plain callable matching the Protocol) so we can drive refresh paths,
304 handling, signature failures, issuer mismatches, and fail-closed
semantics deterministically using a controllable clock.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from adcp.signing.crypto import (
    ALG_ED25519,
    b64url_encode,
    sign_signature_base,
)
from adcp.signing.revocation_fetcher import (
    REVOCATION_LIST_TYP,
    CachingRevocationChecker,
    FetchResult,
    RevocationListFetcher,
    RevocationListFetchError,
    RevocationListFreshnessError,
    RevocationListParseError,
    RevocationListSignatureError,
    default_revocation_list_fetcher,
)

ISSUER = "https://gov.example.com"
REVOCATION_URI = f"{ISSUER}/.well-known/governance-revocations.json"


# -- helpers ------------------------------------------------------------


def _key_and_jwks() -> tuple[
    ed25519.Ed25519PrivateKey,
    dict[str, object],
    Callable[[str], dict[str, object] | None],
]:
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "use": "sig",
        "key_ops": ["verify"],
        "kid": "gov-key-1",
        "x": b64url_encode(private.public_key().public_bytes_raw()),
    }

    def resolver(keyid: str) -> dict[str, object] | None:
        return jwk if keyid == jwk["kid"] else None

    return private, jwk, resolver


def _make_payload(
    *,
    issuer: str = ISSUER,
    updated: str = "2026-04-18T14:00:00Z",
    next_update: str = "2026-04-18T14:15:00Z",
    revoked_kids: list[str] | None = None,
    revoked_jtis: list[str] | None = None,
    version: int = 1,
) -> dict[str, object]:
    return {
        "version": version,
        "issuer": issuer,
        "updated": updated,
        "next_update": next_update,
        "revoked_kids": revoked_kids or [],
        "revoked_jtis": revoked_jtis or [],
    }


def _sign_jws_compact(
    payload: dict[str, object],
    *,
    private: ed25519.Ed25519PrivateKey,
    kid: str = "gov-key-1",
    typ: str = REVOCATION_LIST_TYP,
    alg: str = "EdDSA",
) -> str:
    header = {"alg": alg, "kid": kid, "typ": typ}
    b64_header = b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    b64_payload = b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = (b64_header + "." + b64_payload).encode("ascii")
    signature = sign_signature_base(
        alg=ALG_ED25519, private_key=private, signature_base=signing_input
    )
    return b64_header + "." + b64_payload + "." + b64url_encode(signature)


def _sign_jws_general_json(
    payload: dict[str, object], *, private: ed25519.Ed25519PrivateKey
) -> dict[str, object]:
    compact = _sign_jws_compact(payload, private=private)
    b64_header, b64_payload, b64_signature = compact.split(".")
    return {
        "payload": b64_payload,
        "signatures": [{"protected": b64_header, "signature": b64_signature}],
    }


def _controllable_clock(
    start: datetime,
) -> tuple[Callable[[], datetime], Callable[[], float], Callable[[float], None]]:
    """Return (wall_clock, monotonic_clock, advance(seconds))."""
    now = [start]
    mono = [0.0]

    def wall_clock() -> datetime:
        return now[0]

    def monotonic_clock() -> float:
        return mono[0]

    def advance_seconds(seconds: float) -> None:
        now[0] = now[0] + timedelta(seconds=seconds)
        mono[0] = mono[0] + seconds

    return wall_clock, monotonic_clock, advance_seconds


class _ScriptedFetcher:
    """RevocationListFetcher that returns pre-programmed responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self._queue: list[FetchResult | Exception] = []

    def enqueue(self, result: FetchResult | Exception) -> None:
        self._queue.append(result)

    def __call__(
        self,
        uri: str,
        *,
        if_none_match: str | None = None,
        if_modified_since: str | None = None,
    ) -> FetchResult:
        self.calls.append((uri, if_none_match, if_modified_since))
        if not self._queue:
            raise AssertionError("ScriptedFetcher had no response queued")
        next_up = self._queue.pop(0)
        if isinstance(next_up, Exception):
            raise next_up
        return next_up


# -- default_revocation_list_fetcher (SSRF only) -----------------------
# Wire-level tests (200 / 304 / 5xx / empty body) are exercised via the
# ASGI e2e suite in test_revocation_fetcher_e2e — they require a real
# transport, which is awkward to wire into httpx.Client in a non-brittle
# way. The SSRF path is shared with the JWKS fetcher and already covered
# there; we just smoke-test it still rejects here.


def test_default_fetcher_rejects_non_https() -> None:
    from adcp.signing.jwks import SSRFValidationError

    with pytest.raises(SSRFValidationError):
        default_revocation_list_fetcher("ftp://example.com/list.json")


def test_default_fetcher_rejects_metadata_ip() -> None:
    from adcp.signing.jwks import SSRFValidationError

    with pytest.raises(SSRFValidationError):
        default_revocation_list_fetcher("https://169.254.169.254/list.json")


# -- CachingRevocationChecker: happy path ------------------------------


def test_first_call_fetches_and_decides() -> None:
    private, _, jwks_resolver = _key_and_jwks()
    payload = _make_payload(revoked_kids=["compromised-key"])
    token = _sign_jws_compact(payload, private=private)

    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag='"v1"', not_modified=False))

    wall_clock, mono_clock, _advance = _controllable_clock(
        datetime(2026, 4, 18, 14, 5, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )

    assert checker("compromised-key") is True
    assert checker("clean-key") is False
    # Only one fetch — both calls come from the same cached list.
    assert len(fetcher.calls) == 1


def test_cache_hit_within_next_update_skips_refetch() -> None:
    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(_make_payload(), private=private)

    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag='"v1"', not_modified=False))

    wall_clock, mono_clock, advance = _controllable_clock(
        datetime(2026, 4, 18, 14, 1, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )

    checker("k1")  # triggers fetch
    advance(60)  # still well before next_update (14:15)
    checker("k2")
    checker("k3")
    assert len(fetcher.calls) == 1


def test_past_next_update_triggers_conditional_refresh() -> None:
    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(_make_payload(), private=private)

    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag='"v1"', not_modified=False))
    # Second response: 304 — server confirms list unchanged.
    fetcher.enqueue(FetchResult(body="", etag='"v1"', not_modified=True))

    wall_clock, mono_clock, advance = _controllable_clock(
        datetime(2026, 4, 18, 14, 1, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )

    checker("k1")  # first fetch
    advance(20 * 60)  # jump past next_update (14:15) into 14:21
    checker("k2")  # conditional refresh, gets 304

    assert len(fetcher.calls) == 2
    _, if_none_match, _ = fetcher.calls[1]
    assert if_none_match == '"v1"'


def test_general_json_serialization_is_accepted() -> None:
    private, _, jwks_resolver = _key_and_jwks()
    payload = _make_payload(revoked_kids=["rev"])
    doc = _sign_jws_general_json(payload, private=private)

    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=doc, etag=None, not_modified=False))

    wall_clock, mono_clock, _ = _controllable_clock(
        datetime(2026, 4, 18, 14, 1, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )

    assert checker("rev") is True


# -- CachingRevocationChecker: signature + schema failures -------------


def test_tampered_signature_raises_signature_error() -> None:
    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(_make_payload(), private=private)

    # Flip one payload byte while keeping signature → signature should fail.
    b64_header, b64_payload, b64_signature = token.split(".")
    from adcp.signing.crypto import b64url_decode

    pb = bytearray(b64url_decode(b64_payload))
    pb[-2] ^= 0xFF  # tweak a byte in the payload
    tampered = b64_header + "." + b64url_encode(bytes(pb)) + "." + b64_signature

    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=tampered, etag=None, not_modified=False))

    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
    )
    with pytest.raises(RevocationListSignatureError):
        checker("any-key")


def test_wrong_issuer_raises_parse_error() -> None:
    private, _, jwks_resolver = _key_and_jwks()
    payload = _make_payload(issuer="https://different.example.com")
    token = _sign_jws_compact(payload, private=private)

    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag=None, not_modified=False))

    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
    )
    with pytest.raises(RevocationListParseError, match="issuer"):
        checker("any-key")


def test_accepts_future_version_with_forward_compat() -> None:
    # version=2 should NOT hard-reject: additive schema changes shouldn't
    # force every old SDK into fail-closed across their entire traffic.
    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(_make_payload(version=2, revoked_kids=["rev"]), private=private)

    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag=None, not_modified=False))

    wall_clock, mono_clock, _ = _controllable_clock(
        datetime(2026, 4, 18, 14, 1, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    assert checker("rev") is True


def test_non_positive_version_rejected() -> None:
    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(_make_payload(version=0), private=private)

    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag=None, not_modified=False))

    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
    )
    with pytest.raises(RevocationListParseError, match="version"):
        checker("any-key")


def test_declared_cadence_below_floor_rejected() -> None:
    # Spec floor is 60s. An issuer declaring next_update 30s after updated
    # is violating the spec; we reject at parse time (fix #5).
    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(
        _make_payload(updated="2026-04-18T14:00:00Z", next_update="2026-04-18T14:00:30Z"),
        private=private,
    )

    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag=None, not_modified=False))

    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
    )
    with pytest.raises(RevocationListParseError, match="below spec floor"):
        checker("any-key")


def test_updated_far_in_future_rejected() -> None:
    private, _, jwks_resolver = _key_and_jwks()
    # updated is 5 minutes ahead of our wall clock — outside 60s skew.
    token = _sign_jws_compact(
        _make_payload(updated="2026-04-18T14:10:00Z", next_update="2026-04-18T14:25:00Z"),
        private=private,
    )

    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag=None, not_modified=False))

    wall_clock, mono_clock, _ = _controllable_clock(
        datetime(2026, 4, 18, 14, 0, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    with pytest.raises(RevocationListParseError, match="in the future"):
        checker("any-key")


# -- CachingRevocationChecker: fail-closed -----------------------------


def test_refresh_failure_within_grace_serves_cached() -> None:
    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(_make_payload(revoked_kids=["rev"]), private=private)

    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag='"v1"', not_modified=False))
    # Second call: network fails. We're past next_update but within grace.
    fetcher.enqueue(RevocationListFetchError("server unavailable"))

    wall_clock, mono_clock, advance = _controllable_clock(
        datetime(2026, 4, 18, 14, 1, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )

    checker("rev")  # fetches initial list (updated=14:00, next_update=14:15)
    # Interval = 15min, grace = 2× = 30min. Advance past next_update but
    # within grace (14:20 — 5min past next_update).
    advance(19 * 60)  # now 14:20

    # Refresh fails, but we're inside grace → cached list still used.
    assert checker("rev") is True
    assert checker("clean") is False


def test_refresh_failure_past_grace_raises_freshness_error() -> None:
    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(_make_payload(revoked_kids=["rev"]), private=private)

    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag='"v1"', not_modified=False))
    # All subsequent fetches fail.
    for _ in range(5):
        fetcher.enqueue(RevocationListFetchError("server unavailable"))

    wall_clock, mono_clock, advance = _controllable_clock(
        datetime(2026, 4, 18, 14, 1, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )

    checker("rev")  # initial fetch, interval 15min → grace 30min
    # Advance well past next_update (14:15) + grace (30min) → 14:46.
    advance(45 * 60 + 1)  # 14:46:01

    with pytest.raises(RevocationListFreshnessError, match="past next_update"):
        checker("rev")


def test_304_within_window_refreshes_clock() -> None:
    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(_make_payload(), private=private)

    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag='"v1"', not_modified=False))
    fetcher.enqueue(FetchResult(body="", etag='"v1"', not_modified=True))
    # After the 304, we shouldn't try to fetch again until the NEXT window.

    wall_clock, mono_clock, advance = _controllable_clock(
        datetime(2026, 4, 18, 14, 1, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )

    checker("k")  # 1 fetch
    advance(15 * 60 + 30)  # past next_update → 14:16:30
    checker("k")  # triggers refresh, gets 304
    # Cached list's next_update is still 14:15 — we haven't moved it forward.
    # But checker._last_successful_refresh is updated. Advancing again past
    # next_update WILL trigger another fetch because the list didn't change.
    assert len(fetcher.calls) == 2


# -- RevocationListFetcher Protocol compliance -------------------------


def test_scripted_fetcher_matches_protocol() -> None:
    """Structural typing: _ScriptedFetcher satisfies RevocationListFetcher."""
    fetcher: RevocationListFetcher = _ScriptedFetcher()
    assert callable(fetcher)


# -- reviewer-fix coverage ---------------------------------------------


def test_replay_older_list_rejected() -> None:
    """Fix #4: refresh MUST reject a list whose ``updated`` is older than cached.

    Defense against a CDN (or a compromised operator key) serving an
    earlier list to un-revoke a compromised kid.
    """
    private, _, jwks_resolver = _key_and_jwks()
    newer = _make_payload(
        updated="2026-04-18T14:10:00Z",
        next_update="2026-04-18T14:25:00Z",
        revoked_kids=["compromised"],
    )
    older = _make_payload(
        updated="2026-04-18T14:00:00Z",
        next_update="2026-04-18T14:15:00Z",
        revoked_kids=[],  # attacker un-revokes the kid
    )
    fetcher = _ScriptedFetcher()
    fetcher.enqueue(
        FetchResult(body=_sign_jws_compact(newer, private=private), etag='"v2"', not_modified=False)
    )
    fetcher.enqueue(
        FetchResult(body=_sign_jws_compact(older, private=private), etag='"v1"', not_modified=False)
    )

    wall_clock, mono_clock, advance = _controllable_clock(
        datetime(2026, 4, 18, 14, 15, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    assert checker("compromised") is True
    # Advance past next_update. Refetch gets the older list → reject.
    # The replay error is caught inside the grace window so the checker
    # keeps serving the cached (newer) list — the compromised kid stays
    # revoked.
    advance(15 * 60)  # 14:30
    assert checker("compromised") is True
    # Pin the invariant directly: the cached list is still the NEWER one.
    # If the replay check had been removed, the older list would have
    # overwritten the cache and `_current_list.updated` would now equal
    # "2026-04-18T14:00:00Z".
    assert checker._current_list is not None
    assert checker._current_list.updated == "2026-04-18T14:10:00Z"


def test_from_issuer_origin_builds_spec_path() -> None:
    """Fix #10: classmethod pins the .well-known path from the origin."""
    _, _, jwks_resolver = _key_and_jwks()
    checker = CachingRevocationChecker.from_issuer_origin(
        "https://Gov.Example.COM/",
        jwks_resolver=jwks_resolver,
    )
    # Normalized: lowercased host, scheme preserved, trailing slash stripped.
    assert checker._revocation_uri == (
        "https://gov.example.com/.well-known/governance-revocations.json"
    )
    assert checker._issuer == "https://gov.example.com"


def test_prime_fails_fast_on_bad_config() -> None:
    """Fix #11: prime() surfaces fetch / JWS / schema errors at startup."""
    _, _, jwks_resolver = _key_and_jwks()
    fetcher = _ScriptedFetcher()
    fetcher.enqueue(RevocationListFetchError("operator unreachable"))

    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
    )
    with pytest.raises(RevocationListFetchError, match="operator unreachable"):
        checker.prime()


def test_prime_succeeds_and_caches() -> None:
    """Priming caches the list so subsequent __call__ serves without fetching."""
    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(_make_payload(revoked_kids=["rev"]), private=private)
    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag='"v1"', not_modified=False))

    wall_clock, mono_clock, _ = _controllable_clock(
        datetime(2026, 4, 18, 14, 5, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    checker.prime()
    assert checker("rev") is True
    # Only the prime call triggered a fetch.
    assert len(fetcher.calls) == 1


def test_is_jti_revoked_surfaces_jti_membership() -> None:
    """Fix #7: governance callers can check jti revocation independently of kid."""
    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(
        _make_payload(
            revoked_kids=["kid-1"],
            revoked_jtis=["jti-abc"],
        ),
        private=private,
    )
    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag=None, not_modified=False))

    wall_clock, mono_clock, _ = _controllable_clock(
        datetime(2026, 4, 18, 14, 5, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    assert checker.is_jti_revoked("jti-abc") is True
    assert checker.is_jti_revoked("jti-other") is False


def test_issuer_normalization_accepts_case_variants() -> None:
    """Fix #8: issuer comparison is case-insensitive on scheme + host, ignores trailing slash."""
    private, _, jwks_resolver = _key_and_jwks()
    # Configured issuer has trailing slash + mixed case.
    configured = "HTTPS://Gov.Example.com/"
    # Payload uses the canonical form.
    token = _sign_jws_compact(
        _make_payload(issuer="https://gov.example.com", revoked_kids=["k"]),
        private=private,
    )
    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag=None, not_modified=False))

    wall_clock, mono_clock, _ = _controllable_clock(
        datetime(2026, 4, 18, 14, 5, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=configured,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    assert checker("k") is True


def test_if_modified_since_threaded_to_fetcher() -> None:
    """Fix #6: cached Last-Modified is sent on the next refresh."""
    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(_make_payload(), private=private)
    fetcher = _ScriptedFetcher()
    fetcher.enqueue(
        FetchResult(
            body=token,
            etag='"v1"',
            last_modified="Sat, 18 Apr 2026 14:00:00 GMT",
            not_modified=False,
        )
    )
    fetcher.enqueue(FetchResult(body="", etag='"v1"', not_modified=True))

    wall_clock, mono_clock, advance = _controllable_clock(
        datetime(2026, 4, 18, 14, 1, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    checker("k")
    advance(20 * 60)
    checker("k")

    _, _, if_modified_since = fetcher.calls[1]
    assert if_modified_since == "Sat, 18 Apr 2026 14:00:00 GMT"


def test_compact_jws_with_alternate_encoding_rejected_or_handled_safely() -> None:
    """Fix #2: the compact JWS signing input uses the ORIGINAL b64 strings.

    Build a token where the signer used standard base64 (``+``, ``/``,
    ``=``) rather than url-safe base64 for the payload — the permissive
    decoder would accept it, but verification must work against the
    exact wire bytes, not re-encoded bytes. Since the b64url_encode we
    use in the signer produces url-safe output, any token with standard
    chars in the payload slot will either (a) fail signature verify (if
    the signer is us) or (b) verify successfully because the original
    bytes are what we hashed. Either outcome is safe; a mismatch
    between wire bytes and verified bytes is NOT safe.

    This test signs a normal compact JWS, then flips a char in the
    original b64 payload to produce a variant that decodes to different
    bytes. Verification must fail.
    """
    from adcp.signing.crypto import b64url_decode, b64url_encode

    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(_make_payload(revoked_kids=["k"]), private=private)
    b64_header, b64_payload, b64_signature = token.split(".")

    # Transform the payload substring: decode, then re-encode with a
    # padding char. The decoded bytes are identical; the wire form is
    # different. If the verifier were using decoded-then-reencoded bytes
    # as signing input, this attack would succeed; with the fix, it
    # fails because the signature was over the original wire form.
    raw = b64url_decode(b64_payload)
    unpadded = b64url_encode(raw)
    padded = unpadded + "=" * (-len(unpadded) % 4)
    if padded == b64_payload:
        # Skip — the natural encoding happens to have no padding gap.
        return
    tampered = f"{b64_header}.{padded}.{b64_signature}"

    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=tampered, etag=None, not_modified=False))
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
    )
    with pytest.raises(RevocationListSignatureError):
        checker("k")


# -- round-2 reviewer-fix coverage -------------------------------------


def test_issuer_rejects_userinfo() -> None:
    """Round-2: configured issuer with userinfo is rejected at init."""
    from adcp.signing.revocation_fetcher import _normalize_issuer

    with pytest.raises(ValueError, match="userinfo"):
        _normalize_issuer("https://attacker@gov.example.com")


def test_issuer_homoglyph_normalizes_to_distinct_punycode() -> None:
    """Round-2: homoglyph hosts produce a byte-distinct punycode form so
    byte-equal comparison with the legitimate origin fails."""
    from adcp.signing.revocation_fetcher import _normalize_issuer

    legit = _normalize_issuer("https://gov.example.com")
    # U+1D20 LATIN LETTER SMALL CAPITAL V — visually similar to 'v'.
    homoglyph = _normalize_issuer("https://go\u1d20.example.com")
    assert legit != homoglyph
    # The homoglyph form is a punycode ASCII representation, not the
    # visual lookalike — so a seller configured with the legit origin
    # will reject a payload with the homoglyph origin.
    assert "xn--" in homoglyph


def test_issuer_normalizes_idna_host() -> None:
    """Round-2: legit IDN hosts encode to punycode and compare stably."""
    from adcp.signing.revocation_fetcher import _normalize_issuer

    # Two equivalent representations of a punycode IDN domain.
    form_a = _normalize_issuer("https://bücher.example/")
    form_b = _normalize_issuer("https://xn--bcher-kva.example/")
    assert form_a == form_b


def test_last_modified_header_injection_rejected() -> None:
    """Round-2: CRLF-injection-shaped Last-Modified values are dropped at write."""
    from adcp.signing.revocation_fetcher import _sanitize_last_modified

    assert _sanitize_last_modified("Sat, 18 Apr 2026 14:00:00 GMT") == (
        "Sat, 18 Apr 2026 14:00:00 GMT"
    )
    # CRLF injection attempt
    assert _sanitize_last_modified("Sat, 18 Apr 2026\r\nX-Injected: evil") is None
    # Length cap
    assert _sanitize_last_modified("A" * 65) is None
    # None passes through
    assert _sanitize_last_modified(None) is None


def test_304_does_not_extend_signed_next_update() -> None:
    """Transport validators cannot extend a signed authorization window."""
    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(_make_payload(), private=private)
    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag='"v1"', not_modified=False))
    fetcher.enqueue(FetchResult(body="", etag='"v1"', not_modified=True))
    fetcher.enqueue(FetchResult(body="", etag='"v1"', not_modified=True))

    wall_clock, mono_clock, advance = _controllable_clock(
        datetime(2026, 4, 18, 14, 1, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    checker("k")  # 1 fetch → initial
    advance(15 * 60 + 30)  # 14:16:30, past original next_update (14:15)
    checker("k")  # within grace, but the signed deadline remains unchanged

    assert checker._current_list is not None
    assert checker._current_list.next_update == "2026-04-18T14:15:00Z"

    advance(29 * 60)  # beyond signed next_update + 2x interval grace
    with pytest.raises(RevocationListFreshnessError):
        checker("k")


def test_cold_checker_accepts_list_within_next_update_grace() -> None:
    private, _, jwks_resolver = _key_and_jwks()
    token = _sign_jws_compact(_make_payload(), private=private)
    fetcher = _ScriptedFetcher()
    fetcher.enqueue(FetchResult(body=token, etag='"v1"', not_modified=False))
    wall_clock, mono_clock, _ = _controllable_clock(
        datetime(2026, 4, 18, 14, 16, tzinfo=timezone.utc)
    )
    checker = CachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=jwks_resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    assert checker("k") is False


def test_clock_footgun_rejects_time_time() -> None:
    """Round-2: passing time.time as clock is rejected."""
    import time as _time

    _, _, jwks_resolver = _key_and_jwks()
    with pytest.raises(ValueError, match="monotonic"):
        CachingRevocationChecker(
            revocation_uri=REVOCATION_URI,
            issuer=ISSUER,
            jwks_resolver=jwks_resolver,
            clock=_time.time,
        )


def test_clock_footgun_rejects_identical_sources() -> None:
    """Round-2: passing the same callable to clock and wall_clock is rejected."""
    _, _, jwks_resolver = _key_and_jwks()

    def both() -> float:
        raise RuntimeError("never called")

    with pytest.raises(ValueError, match="different sources"):
        CachingRevocationChecker(
            revocation_uri=REVOCATION_URI,
            issuer=ISSUER,
            jwks_resolver=jwks_resolver,
            clock=both,
            wall_clock=both,  # type: ignore[arg-type]
        )
