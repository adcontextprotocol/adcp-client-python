"""Managed delivery re-checks a reporting subscription's authority at each attempt.

AdCP 3.3: no new data is delivered after a loss of authority, checked at each
delivery attempt including retries. A loss holds the materialization: it stays
pending under the same identity and is never failed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from adcp.reporting.ledger import ReportingConfigurationGenerationKey
from adcp.reporting.materializer.contracts import ReportingWriterError
from adcp.reporting.materializer.service import ReportingMaterializerService
from tests.test_reporting_subscription_service import ACCOUNT, AGENCY, _buy, _Seller


def _service(authorize) -> ReportingMaterializerService:
    service = object.__new__(ReportingMaterializerService)
    object.__setattr__(service, "authorize_subscription_delivery", authorize)
    object.__setattr__(service, "authority_hold_seconds", 60)
    return service


def _target(offering_id: str | None):
    key = ReportingConfigurationGenerationKey(
        ACCOUNT, AGENCY.subject, "analytics-hourly", 1, offering_id
    )
    return SimpleNamespace(binding=SimpleNamespace(generation_key=key))


@pytest.mark.asyncio
async def test_account_generations_never_consult_the_subscription_authority() -> None:
    calls: list[tuple[str, str]] = []

    async def authorize(subject: str, account_id: str) -> bool:
        calls.append((subject, account_id))
        return False

    await _service(authorize)._require_subscription_authority(_target(None), effect="not_started")
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["not_started", "unknown"])
async def test_a_lost_grant_holds_the_attempt_under_the_same_identity(effect: str) -> None:
    async def authorize(subject: str, account_id: str) -> bool:
        assert (subject, account_id) == (AGENCY.subject, ACCOUNT)
        return False

    with pytest.raises(ReportingWriterError) as held:
        await _service(authorize)._require_subscription_authority(
            _target("GAM_SNAPSHOT_V1"), effect=effect  # type: ignore[arg-type]
        )
    failure = held.value.failure
    assert (failure.code, failure.retry, failure.effect, failure.retry_after_seconds) == (
        "AUTHORIZATION_DENIED",
        "same_identity",
        effect,
        60,
    )


@pytest.mark.asyncio
async def test_a_current_grant_delivers() -> None:
    async def authorize(subject: str, account_id: str) -> bool:
        return True

    await _service(authorize)._require_subscription_authority(
        _target("GAM_SNAPSHOT_V1"), effect="not_started"
    )


@pytest.mark.asyncio
async def test_the_engine_answers_from_the_grant() -> None:
    seller = _Seller(principals=(AGENCY,))
    await seller.subscribe(
        AGENCY,
        [_buy("media-buy-a1", "2026-11-01T01:00:00Z", "2026-11-01T03:00:00Z")],
        allowed_tasks=frozenset({"get_reporting_status"}),
    )
    assert await seller.engine.authorizes_delivery(AGENCY.subject, ACCOUNT) is True
    del seller.host.accounts[ACCOUNT]
    await seller.engine.reconcile(AGENCY)
    assert await seller.engine.authorizes_delivery(AGENCY.subject, ACCOUNT) is False
