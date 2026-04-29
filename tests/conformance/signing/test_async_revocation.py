"""Unit + e2e tests for the async revocation-list fetcher and checker.

Mirrors the sync coverage in ``test_revocation_fetcher.py`` and
``test_revocation_e2e.py`` against the async counterparts. Uses a
scripted async fetcher for unit tests and a real Starlette app via
``httpx.ASGITransport`` + ``httpx.AsyncClient`` for e2e — no
``asyncio.run`` bridge, which is the whole point of the async path.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from adcp.signing import (
    REVOCATION_LIST_TYP,
    AsyncCachingJwksResolver,
    AsyncCachingRevocationChecker,
    AsyncJwksResolver,
    FetchResult,
    RevocationListFetchError,
    RevocationListFreshnessError,
    as_async_resolver,
    async_default_revocation_list_fetcher,
    averify_jws_document,
)
from adcp.signing.crypto import ALG_ED25519, b64url_encode, sign_signature_base
from adcp.signing.jws import JwsMalformedError

ISSUER = "https://gov.example.com"
REVOCATION_URI = f"{ISSUER}/.well-known/governance-revocations.json"


# -- shared fixtures ----------------------------------------------------


def _operator_key_and_resolver() -> tuple[ed25519.Ed25519PrivateKey, AsyncJwksResolver]:
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "use": "sig",
        "key_ops": ["verify"],
        "kid": "operator-2026",
        "x": b64url_encode(private.public_key().public_bytes_raw()),
    }

    async def resolver(keyid: str) -> dict[str, Any] | None:
        return jwk if keyid == "operator-2026" else None

    return private, resolver


def _make_payload(
    *,
    issuer: str = ISSUER,
    updated: str = "2026-04-18T14:00:00Z",
    next_update: str = "2026-04-18T14:15:00Z",
    revoked_kids: list[str] | None = None,
    revoked_jtis: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "issuer": issuer,
        "updated": updated,
        "next_update": next_update,
        "revoked_kids": revoked_kids or [],
        "revoked_jtis": revoked_jtis or [],
    }


def _sign_compact(payload: dict[str, Any], *, private: ed25519.Ed25519PrivateKey) -> str:
    header = {"alg": "EdDSA", "kid": "operator-2026", "typ": REVOCATION_LIST_TYP}
    b64_header = b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    b64_payload = b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = (b64_header + "." + b64_payload).encode("ascii")
    signature = sign_signature_base(
        alg=ALG_ED25519, private_key=private, signature_base=signing_input
    )
    return b64_header + "." + b64_payload + "." + b64url_encode(signature)


class _ScriptedAsyncFetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self._queue: list[FetchResult | Exception] = []

    def enqueue(self, result: FetchResult | Exception) -> None:
        self._queue.append(result)

    async def __call__(
        self,
        uri: str,
        *,
        if_none_match: str | None = None,
        if_modified_since: str | None = None,
    ) -> FetchResult:
        self.calls.append((uri, if_none_match, if_modified_since))
        if not self._queue:
            raise AssertionError("ScriptedAsyncFetcher had no response queued")
        next_up = self._queue.pop(0)
        if isinstance(next_up, Exception):
            raise next_up
        return next_up


def _controllable_clock(
    start: datetime,
) -> tuple[Callable[[], datetime], Callable[[], float], Callable[[float], None]]:
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


# -- averify_jws_document ----------------------------------------------


async def test_averify_jws_round_trip() -> None:
    private, resolver = _operator_key_and_resolver()
    token = _sign_compact(_make_payload(), private=private)

    verified = await averify_jws_document(
        token, jwks_resolver=resolver, expected_typ=REVOCATION_LIST_TYP
    )
    assert verified["issuer"] == ISSUER


async def test_averify_jws_rejects_unknown_kid() -> None:
    private, _ = _operator_key_and_resolver()
    token = _sign_compact(_make_payload(), private=private)

    async def empty_resolver(_keyid: str) -> dict[str, Any] | None:
        return None

    with pytest.raises(Exception) as exc_info:
        await averify_jws_document(
            token, jwks_resolver=empty_resolver, expected_typ=REVOCATION_LIST_TYP
        )
    assert "no JWK" in str(exc_info.value)


async def test_averify_jws_rejects_wrong_typ() -> None:
    private, resolver = _operator_key_and_resolver()
    # Build a token with the wrong typ header.
    header = {"alg": "EdDSA", "kid": "operator-2026", "typ": "different+jws"}
    payload = _make_payload()
    b64_header = b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    b64_payload = b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signature = sign_signature_base(
        alg=ALG_ED25519,
        private_key=private,
        signature_base=(b64_header + "." + b64_payload).encode("ascii"),
    )
    token = b64_header + "." + b64_payload + "." + b64url_encode(signature)

    with pytest.raises(JwsMalformedError, match="typ"):
        await averify_jws_document(token, jwks_resolver=resolver, expected_typ=REVOCATION_LIST_TYP)


# -- as_async_resolver --------------------------------------------------


async def test_as_async_resolver_wraps_sync_resolver() -> None:
    def sync_resolver(keyid: str) -> dict[str, Any] | None:
        return {"kid": keyid} if keyid == "x" else None

    async_resolver: AsyncJwksResolver = as_async_resolver(sync_resolver)
    assert await async_resolver("x") == {"kid": "x"}
    assert await async_resolver("y") is None


# -- AsyncCachingRevocationChecker: happy path + cache ------------------


async def test_async_first_call_fetches_and_decides() -> None:
    private, resolver = _operator_key_and_resolver()
    token = _sign_compact(_make_payload(revoked_kids=["rev"]), private=private)

    fetcher = _ScriptedAsyncFetcher()
    fetcher.enqueue(FetchResult(body=token, etag='"v1"', not_modified=False))

    wall_clock, mono_clock, _ = _controllable_clock(
        datetime(2026, 4, 18, 14, 5, tzinfo=timezone.utc)
    )
    checker = AsyncCachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )

    assert await checker("rev") is True
    assert await checker("clean") is False
    assert len(fetcher.calls) == 1


async def test_async_cache_hit_within_next_update_skips_refetch() -> None:
    private, resolver = _operator_key_and_resolver()
    token = _sign_compact(_make_payload(), private=private)
    fetcher = _ScriptedAsyncFetcher()
    fetcher.enqueue(FetchResult(body=token, etag=None, not_modified=False))

    wall_clock, mono_clock, advance = _controllable_clock(
        datetime(2026, 4, 18, 14, 1, tzinfo=timezone.utc)
    )
    checker = AsyncCachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    await checker("k")
    advance(60)
    await checker("k")
    assert len(fetcher.calls) == 1


async def test_async_past_next_update_triggers_conditional_refresh() -> None:
    private, resolver = _operator_key_and_resolver()
    token = _sign_compact(_make_payload(), private=private)
    fetcher = _ScriptedAsyncFetcher()
    fetcher.enqueue(FetchResult(body=token, etag='"v1"', not_modified=False))
    fetcher.enqueue(FetchResult(body="", etag='"v1"', not_modified=True))

    wall_clock, mono_clock, advance = _controllable_clock(
        datetime(2026, 4, 18, 14, 1, tzinfo=timezone.utc)
    )
    checker = AsyncCachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    await checker("k")
    advance(20 * 60)
    await checker("k")
    assert len(fetcher.calls) == 2
    _, if_none_match, _ = fetcher.calls[1]
    assert if_none_match == '"v1"'


async def test_async_replay_older_list_rejected() -> None:
    private, resolver = _operator_key_and_resolver()
    newer = _make_payload(
        updated="2026-04-18T14:10:00Z",
        next_update="2026-04-18T14:25:00Z",
        revoked_kids=["compromised"],
    )
    older = _make_payload(
        updated="2026-04-18T14:00:00Z",
        next_update="2026-04-18T14:15:00Z",
        revoked_kids=[],
    )
    fetcher = _ScriptedAsyncFetcher()
    fetcher.enqueue(
        FetchResult(body=_sign_compact(newer, private=private), etag='"v2"', not_modified=False)
    )
    fetcher.enqueue(
        FetchResult(body=_sign_compact(older, private=private), etag='"v1"', not_modified=False)
    )

    wall_clock, mono_clock, advance = _controllable_clock(
        datetime(2026, 4, 18, 14, 15, tzinfo=timezone.utc)
    )
    checker = AsyncCachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    assert await checker("compromised") is True
    advance(15 * 60)
    assert await checker("compromised") is True
    assert checker._current_list is not None
    assert checker._current_list.updated == "2026-04-18T14:10:00Z"


async def test_async_refresh_failure_past_grace_raises_freshness_error() -> None:
    private, resolver = _operator_key_and_resolver()
    token = _sign_compact(_make_payload(revoked_kids=["rev"]), private=private)
    fetcher = _ScriptedAsyncFetcher()
    fetcher.enqueue(FetchResult(body=token, etag='"v1"', not_modified=False))
    for _ in range(5):
        fetcher.enqueue(RevocationListFetchError("server unavailable"))

    wall_clock, mono_clock, advance = _controllable_clock(
        datetime(2026, 4, 18, 14, 1, tzinfo=timezone.utc)
    )
    checker = AsyncCachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    await checker("rev")
    advance(45 * 60 + 1)
    with pytest.raises(RevocationListFreshnessError):
        await checker("rev")


async def test_async_aprime_fails_fast() -> None:
    _, resolver = _operator_key_and_resolver()
    fetcher = _ScriptedAsyncFetcher()
    fetcher.enqueue(RevocationListFetchError("operator unreachable"))

    checker = AsyncCachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=resolver,
        fetcher=fetcher,
    )
    with pytest.raises(RevocationListFetchError, match="operator unreachable"):
        await checker.aprime()


async def test_async_is_jti_revoked() -> None:
    private, resolver = _operator_key_and_resolver()
    token = _sign_compact(_make_payload(revoked_jtis=["jti-abc"]), private=private)
    fetcher = _ScriptedAsyncFetcher()
    fetcher.enqueue(FetchResult(body=token, etag=None, not_modified=False))

    wall_clock, mono_clock, _ = _controllable_clock(
        datetime(2026, 4, 18, 14, 5, tzinfo=timezone.utc)
    )
    checker = AsyncCachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=resolver,
        fetcher=fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    assert await checker.is_jti_revoked("jti-abc") is True
    assert await checker.is_jti_revoked("jti-other") is False


async def test_async_from_issuer_origin_builds_spec_path() -> None:
    _, resolver = _operator_key_and_resolver()
    checker = AsyncCachingRevocationChecker.from_issuer_origin(
        "https://Gov.Example.COM/",
        jwks_resolver=resolver,
    )
    assert checker._revocation_uri == (
        "https://gov.example.com/.well-known/governance-revocations.json"
    )
    assert checker._issuer == "https://gov.example.com"


# -- concurrency: lock serializes refreshes ----------------------------


async def test_async_lock_serializes_first_fetch_under_concurrency() -> None:
    """N concurrent tasks hitting the first miss should fire exactly one fetch."""
    private, resolver = _operator_key_and_resolver()
    token = _sign_compact(_make_payload(revoked_kids=["rev"]), private=private)

    # Fetcher with a small await inside to make the race observable.
    fetch_count = [0]

    async def slow_fetcher(
        _uri: str,
        *,
        if_none_match: str | None = None,
        if_modified_since: str | None = None,
    ) -> FetchResult:
        fetch_count[0] += 1
        await asyncio.sleep(0)  # let other tasks interleave
        return FetchResult(body=token, etag='"v1"', not_modified=False)

    wall_clock, mono_clock, _ = _controllable_clock(
        datetime(2026, 4, 18, 14, 5, tzinfo=timezone.utc)
    )
    checker = AsyncCachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=resolver,
        fetcher=slow_fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    results = await asyncio.gather(checker("rev"), checker("rev"), checker("rev"), checker("rev"))
    assert all(r is True for r in results)
    assert fetch_count[0] == 1


# -- async JWKS resolver -----------------------------------------------


async def test_async_caching_jwks_resolver_caches_first_fetch() -> None:
    jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "use": "sig",
        "key_ops": ["verify"],
        "kid": "my-kid",
        "x": "a" * 43,
    }
    fetch_count = [0]

    async def fake_fetcher(_uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        fetch_count[0] += 1
        return {"keys": [jwk]}

    resolver = AsyncCachingJwksResolver(
        "https://gov.example.com/.well-known/jwks.json",
        fetcher=fake_fetcher,
    )
    assert await resolver("my-kid") == jwk
    assert await resolver("my-kid") == jwk  # cached
    assert fetch_count[0] == 1


async def test_async_caching_jwks_resolver_handles_concurrent_misses() -> None:
    jwk = {"kid": "concurrent-kid", "kty": "OKP", "crv": "Ed25519", "x": "a" * 43}
    fetch_count = [0]

    async def slow_fetcher(_uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        fetch_count[0] += 1
        await asyncio.sleep(0)
        return {"keys": [jwk]}

    resolver = AsyncCachingJwksResolver(
        "https://gov.example.com/.well-known/jwks.json",
        fetcher=slow_fetcher,
    )
    results = await asyncio.gather(
        resolver("concurrent-kid"),
        resolver("concurrent-kid"),
        resolver("concurrent-kid"),
    )
    assert all(r == jwk for r in results)
    assert fetch_count[0] == 1


# -- e2e via Starlette + ASGITransport (native async, no asyncio.run) ---


def _build_revocation_app(*, body: str, etag: str) -> Starlette:
    async def handler(request: Any) -> Response:
        if_none_match = request.headers.get("if-none-match")
        if if_none_match == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return PlainTextResponse(
            content=body,
            media_type="application/jose",
            headers={"ETag": etag},
        )

    return Starlette(
        routes=[Route("/.well-known/governance-revocations.json", handler, methods=["GET"])]
    )


def _asgi_async_fetcher(app: Starlette) -> Any:
    transport = httpx.ASGITransport(app=app)

    async def fetch(
        uri: str,
        *,
        if_none_match: str | None = None,
        if_modified_since: str | None = None,
    ) -> FetchResult:
        headers: dict[str, str] = {"Accept": "application/jose"}
        if if_none_match is not None:
            headers["If-None-Match"] = if_none_match
        if if_modified_since is not None:
            headers["If-Modified-Since"] = if_modified_since

        async with httpx.AsyncClient(transport=transport, base_url=ISSUER) as client:
            response = await client.get("/.well-known/governance-revocations.json", headers=headers)
        if response.status_code == 304:
            return FetchResult(
                body="",
                etag=if_none_match,
                last_modified=if_modified_since,
                not_modified=True,
            )
        response.raise_for_status()
        return FetchResult(
            body=response.text,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            not_modified=False,
        )

    return fetch


async def test_async_e2e_asgi_round_trip() -> None:
    """Native async path — no asyncio.run bridge in the test."""
    private, resolver = _operator_key_and_resolver()
    payload = _make_payload(revoked_kids=["compromised"])
    token = _sign_compact(payload, private=private)
    app = _build_revocation_app(body=token, etag='"rev-1"')

    checker = AsyncCachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=resolver,
        fetcher=_asgi_async_fetcher(app),
        wall_clock=lambda: datetime(2026, 4, 18, 14, 5, tzinfo=timezone.utc),
    )
    assert await checker("compromised") is True
    assert await checker("clean") is False


# -- default fetcher (SSRF smoke only, like sync version) --------------


async def test_async_default_fetcher_rejects_non_https() -> None:
    from adcp.signing.jwks import SSRFValidationError

    with pytest.raises(SSRFValidationError):
        await async_default_revocation_list_fetcher("ftp://example.com/list.json")


async def test_async_default_fetcher_rejects_metadata_ip() -> None:
    from adcp.signing.jwks import SSRFValidationError

    with pytest.raises(SSRFValidationError):
        await async_default_revocation_list_fetcher("https://169.254.169.254/list.json")


async def test_concurrent_first_calls_share_one_refresh() -> None:
    """Concurrent first-miss calls on a brand-new checker use the eager
    lock to serialize refreshes.

    Regression test for the lazy-lock-init race — previously two tasks
    both seeing ``self._lock is None`` would construct separate Locks
    and skip serialization.
    """
    private, resolver = _operator_key_and_resolver()
    token = _sign_compact(_make_payload(revoked_kids=["k"]), private=private)

    refresh_count = [0]

    async def counting_fetcher(
        _uri: str,
        *,
        if_none_match: str | None = None,
        if_modified_since: str | None = None,
    ) -> FetchResult:
        refresh_count[0] += 1
        # Yield to force interleaving between tasks.
        await asyncio.sleep(0)
        return FetchResult(body=token, etag='"v1"', not_modified=False)

    wall_clock, mono_clock, _ = _controllable_clock(
        datetime(2026, 4, 18, 14, 5, tzinfo=timezone.utc)
    )
    checker = AsyncCachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=resolver,
        fetcher=counting_fetcher,
        wall_clock=wall_clock,
        clock=mono_clock,
    )
    # 20 concurrent tasks all hitting the first miss.
    results = await asyncio.gather(*(checker("k") for _ in range(20)))
    assert all(r is True for r in results)
    assert refresh_count[0] == 1  # one shared refresh for all 20 tasks


# -- cancellation safety ------------------------------------------------


async def test_cancellation_rolls_back_cooldown_attempt() -> None:
    """A cancelled refresh doesn't burn the 60s cooldown for the next caller.

    Covers the security-reviewer's cancellation-safety concern: setting
    ``_last_refresh_attempt`` before the await meant a cancelled task
    could block legitimate retries for up to ``MIN_POLLING_INTERVAL_SECONDS``.
    The fix rolls the timestamp back on CancelledError.
    """
    _, resolver = _operator_key_and_resolver()

    cancel_event = asyncio.Event()

    async def cancellable_fetcher(
        _uri: str,
        *,
        if_none_match: str | None = None,
        if_modified_since: str | None = None,
    ) -> FetchResult:
        # Block forever until the task is cancelled.
        await cancel_event.wait()
        raise AssertionError("unreachable — task must be cancelled before this")

    checker = AsyncCachingRevocationChecker(
        revocation_uri=REVOCATION_URI,
        issuer=ISSUER,
        jwks_resolver=resolver,
        fetcher=cancellable_fetcher,
    )
    prior_attempt = checker._last_refresh_attempt
    assert prior_attempt is None

    task = asyncio.create_task(checker("rev"))
    await asyncio.sleep(0)  # let the task block on the fetcher
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The cancelled refresh must NOT have left `_last_refresh_attempt`
    # stamped — otherwise the next legitimate caller would be blocked
    # by the cooldown.
    assert checker._last_refresh_attempt == prior_attempt
