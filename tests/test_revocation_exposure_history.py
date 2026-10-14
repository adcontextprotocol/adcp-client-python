"""Authenticated refreshes cannot narrow previously observed compromise exposure."""

from datetime import datetime, timedelta, timezone

import pytest

from adcp.signing import AsyncCachingRevocationChecker, FetchResult
from tests.conformance.signing.test_async_revocation import (
    ISSUER,
    _operator_key_and_resolver,
    _ScriptedAsyncFetcher,
    _sign_compact,
)


@pytest.mark.parametrize(
    "first_detail,second_detail,exposes_earlier_receipt",
    [
        (
            {"reason": "keyCompromise"},
            {"reason": "keyCompromise", "invalidity_date": "2026-04-18T00:00:00Z"},
            True,
        ),
        (
            {"reason": "keyCompromise", "invalidity_date": "2026-04-18T00:00:00Z"},
            {"reason": "keyCompromise"},
            True,
        ),
        (
            {"reason": "keyCompromise"},
            {"reason": "superseded", "invalidity_date": "2026-04-18T00:00:00Z"},
            True,
        ),
        (
            {"reason": "superseded", "invalidity_date": "2026-04-18T00:00:00Z"},
            {"reason": "keyCompromise"},
            True,
        ),
        (
            {"reason": "superseded"},
            {"reason": "keyCompromise", "invalidity_date": "2026-04-18T00:00:00Z"},
            False,
        ),
    ],
)
async def test_signed_refresh_retains_undated_compromise_exposure(
    first_detail, second_detail, exposes_earlier_receipt
):
    private, resolver = _operator_key_and_resolver()
    now = [datetime(2026, 4, 18, 14, tzinfo=timezone.utc)]
    elapsed = [0.0]
    fetcher = _ScriptedAsyncFetcher()

    def enqueue(detail):
        payload = {
            "version": 1,
            "issuer": ISSUER,
            "updated": now[0].isoformat(),
            "next_update": (now[0] + timedelta(seconds=60)).isoformat(),
            "revoked_kids": ["compromised"],
            "revoked_jtis": [],
            "revocation_details": {"compromised": detail},
        }
        fetcher.enqueue(FetchResult(_sign_compact(payload, private=private), etag=None))

    enqueue(first_detail)
    checker = AsyncCachingRevocationChecker.from_issuer_origin(
        ISSUER,
        jwks_resolver=resolver,
        fetcher=fetcher,
        clock=lambda: elapsed[0],
        wall_clock=lambda: now[0],
    )
    assert await checker("compromised")
    now[0] += timedelta(seconds=61)
    elapsed[0] += 61
    enqueue(second_detail)
    assert await checker("compromised")
    assert len(fetcher.calls) == 2
    detail = checker._current_list.revocation_details["compromised"]
    assert detail.reason == "keyCompromise"
    assert detail.exposes(datetime(2026, 4, 17, tzinfo=timezone.utc)) is exposes_earlier_receipt
    assert detail.exposes(datetime(2026, 4, 19, tzinfo=timezone.utc))
