"""Legacy purchase continuation when its historical schema is not installed."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

import adcp
from adcp.compat import (
    CompatibilityContinuationError,
    CompatibilityContinuationErrorCode,
    InMemoryCompatibilityContinuationStore,
)
from adcp.validation import (
    get_bundle_adcp_version,
    schema_loader,
    validate_request,
    validate_response,
)
from tests.test_purchase_continuation import (
    _cases,
    _coordinator,
    _issue,
    _success_result,
)


@pytest.fixture
def unavailable_v25_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    case = next(case for case in _cases() if case["source_version"] == "2.5.3")
    package_root = Path(adcp.__file__).resolve()
    checkout_root = Path(__file__).resolve().parents[1]
    if package_root.is_relative_to(checkout_root):
        # Editable/source tests have the historical cache. Exercise the same
        # unavailable-schema path that an installed distribution takes.
        original = schema_loader._ensure_state

        def without_v25(version: str | None = None) -> Any:
            if version is not None and schema_loader.resolve_bundle_key(version) == "2.5":
                return None
            return original(version)

        monkeypatch.setattr(schema_loader, "_ensure_state", without_v25)
    else:
        # The installed production gate executes this test from a wheel and
        # verifies that no source-layout fallback supplies the retired bundle.
        assert schema_loader._resolve_schema_root("2.5") is None

    assert get_bundle_adcp_version(version="2.5.3") is None
    assert (
        validate_request("get_products", case["legacy_request"], version="2.5.3").variant
        == "skipped"
    )
    assert (
        validate_response("get_products", case["legacy_response"], version="2.5.3").variant
        == "skipped"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("result_kind", ["completed", "failed"])
async def test_v25_continuation_without_bundled_schema(
    unavailable_v25_schema: None, result_kind: str
) -> None:
    case = copy.deepcopy(next(case for case in _cases() if case["source_version"] == "2.5.3"))

    calls = 0

    def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if result_kind == "failed":
            return {"errors": [{"code": "INVALID_REQUEST", "message": "Rejected"}]}
        return _success_result("2.5.3", "legacy-buy-25")

    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), execute)
    await _issue(coordinator, case)
    first = await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    replay = await coordinator.continue_legacy_purchase(
        copy.deepcopy(case["continuation_input"]),
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert first == replay
    assert calls == 1
    assert first == (
        {"errors": [{"code": "INVALID_REQUEST", "message": "Rejected"}]}
        if result_kind == "failed"
        else _success_result("2.5.3", "legacy-buy-25")
    )
    operation = await coordinator.get_legacy_purchase_operation_by_idempotency_key(
        case["continuation_input"]["idempotency_key"], principal_id="principal-acme"
    )
    assert operation.state.value == result_kind.replace("completed", "succeeded")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        {"status": "failed", "message": "Rejected"},
        {"status": "working", "task_id": "legacy-task-25"},
        {"status": "failed", "media_buy_id": "false-success", "buyer_ref": "b", "packages": []},
    ],
)
async def test_unbundled_v25_never_records_unvalidated_success(
    unavailable_v25_schema: None, invalid: dict[str, Any]
) -> None:
    case = copy.deepcopy(next(case for case in _cases() if case["source_version"] == "2.5.3"))
    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), lambda _ctx: invalid)
    await _issue(coordinator, case)
    with pytest.raises(CompatibilityContinuationError) as caught:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert caught.value.code == CompatibilityContinuationErrorCode.INVALID_LEGACY_RESPONSE
    operation = await coordinator.get_legacy_purchase_operation_by_idempotency_key(
        case["continuation_input"]["idempotency_key"], principal_id="principal-acme"
    )
    assert operation.state.value == "ambiguous"
