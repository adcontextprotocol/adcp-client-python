"""``platform_id`` / ``seller_id`` dual emission through the real server path (#1639)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from adcp.compat.versions import CONVERSION_METADATA_KEY
from adcp.decisioning import (
    Account,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.dispatch import _build_request_context, _project_handoff
from adcp.decisioning.serve import create_adcp_server_from_platform
from adcp.decisioning.types import TaskHandoff
from adcp.exceptions import ADCPTaskError
from adcp.server import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller

BUYER_DUAL = "3.3-rc.3"
BUYER_LEGACY = "3.2"

AUDIENCE_REQUEST: dict[str, Any] = {
    "idempotency_key": "audience-key-0123456789",
    "account": {"account_id": "acct_1"},
    "audiences": [{"audience_id": "a1", "name": "A"}],
}
EVENT_SOURCE_REQUEST: dict[str, Any] = {
    "idempotency_key": "event-key-0123456789",
    "account": {"account_id": "acct_1"},
    "event_sources": [{"event_source_id": "e1", "name": "E"}],
}


class _EventSourceSeller(ADCPHandler):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def sync_event_sources(self, params: Any, context: ToolContext | None = None) -> Any:
        return {"event_sources": self.rows}


def _audience_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"audience_id": "a1", "action": "created", **row}]


def _event_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"event_source_id": "e1", "action": "created", "status": "active", **row}]


class _Accounts(SingletonAccounts):
    def resolve(self, ref: dict[str, Any] | None, *, auth_info: Any = None) -> Account[Any]:
        return Account(id="acct_1")


class _AudienceSeller(DecisioningPlatform):
    capabilities = DecisioningCapabilities(specialisms=["audience-sync"])
    accounts = _Accounts(account_id="acct_1")

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def sync_audiences(self, audiences: Any, ctx: Any) -> Any:
        return {"audiences": self.rows}


@pytest.fixture
def audience_caller():
    made: list[Any] = []

    def _make(rows: list[dict[str, Any]]) -> Any:
        handler, executor, _ = create_adcp_server_from_platform(
            _AudienceSeller(rows), registry=InMemoryTaskRegistry(), validate_at_init=False
        )
        made.append(executor)
        return create_tool_caller(handler, "sync_audiences")

    yield _make
    for executor in made:
        executor.shutdown(wait=True)


def _event_caller(rows: list[dict[str, Any]]) -> Any:
    return create_tool_caller(_EventSourceSeller(rows), "sync_event_sources")


def _assert_no_platform_id_loss(context: ToolContext) -> None:
    report = context.metadata[CONVERSION_METADATA_KEY]
    assert not [loss for loss in report["losses"] if "platform_id" in loss["pointer"]]


@pytest.mark.parametrize(
    "returned", [{"platform_id": "p1"}, {"seller_id": "p1"}], ids=["pid", "sid"]
)
async def test_event_sources_buyer_at_3_3_gets_both_names(returned: dict[str, Any]) -> None:
    caller = _event_caller(_event_rows(returned))
    response = await caller({**EVENT_SOURCE_REQUEST, "adcp_version": BUYER_DUAL})
    row = response["event_sources"][0]
    assert row["platform_id"] == row["seller_id"] == "p1"


@pytest.mark.parametrize(
    "returned", [{"platform_id": "p1"}, {"seller_id": "p1"}], ids=["pid", "sid"]
)
async def test_event_sources_buyer_at_3_2_gets_only_seller_id(returned: dict[str, Any]) -> None:
    caller = _event_caller(_event_rows(returned))
    context = ToolContext()
    response = await caller({**EVENT_SOURCE_REQUEST, "adcp_version": BUYER_LEGACY}, context)
    row = response["event_sources"][0]
    assert row["seller_id"] == "p1"
    assert "platform_id" not in row
    _assert_no_platform_id_loss(context)


@pytest.mark.parametrize(
    "returned", [{"platform_id": "p1"}, {"seller_id": "p1"}], ids=["pid", "sid"]
)
async def test_audiences_buyer_at_3_3_gets_both_names(
    audience_caller: Any, returned: dict[str, Any]
) -> None:
    caller = audience_caller(_audience_rows(returned))
    response = await caller({**AUDIENCE_REQUEST, "adcp_version": BUYER_DUAL})
    row = response["audiences"][0]
    assert row["platform_id"] == row["seller_id"] == "p1"


@pytest.mark.parametrize(
    "returned", [{"platform_id": "p1"}, {"seller_id": "p1"}], ids=["pid", "sid"]
)
async def test_audiences_buyer_at_3_2_gets_only_seller_id(
    audience_caller: Any, returned: dict[str, Any]
) -> None:
    caller = audience_caller(_audience_rows(returned))
    context = ToolContext()
    response = await caller({**AUDIENCE_REQUEST, "adcp_version": BUYER_LEGACY}, context)
    row = response["audiences"][0]
    assert row["seller_id"] == "p1"
    assert "platform_id" not in row
    _assert_no_platform_id_loss(context)


async def test_rows_without_a_platform_id_are_untouched() -> None:
    caller = _event_caller(_event_rows({}))
    response = await caller({**EVENT_SOURCE_REQUEST, "adcp_version": BUYER_DUAL})
    assert "platform_id" not in response["event_sources"][0]
    assert "seller_id" not in response["event_sources"][0]


@pytest.mark.parametrize("version", [BUYER_DUAL, BUYER_LEGACY])
async def test_event_sources_mismatch_is_internal_error(version: str) -> None:
    caller = _event_caller(_event_rows({"platform_id": "a", "seller_id": "b"}))
    with pytest.raises(ADCPTaskError) as caught:
        await caller({**EVENT_SOURCE_REQUEST, "adcp_version": version})
    assert caught.value.errors[0].code == "INTERNAL_ERROR"
    assert "/event_sources/0" in caught.value.errors[0].message


@pytest.mark.parametrize("version", [BUYER_DUAL, BUYER_LEGACY])
async def test_audiences_mismatch_is_internal_error(audience_caller: Any, version: str) -> None:
    caller = audience_caller(_audience_rows({"platform_id": "a", "seller_id": "b"}))
    with pytest.raises(ADCPTaskError) as caught:
        await caller({**AUDIENCE_REQUEST, "adcp_version": version})
    assert caught.value.errors[0].code == "INTERNAL_ERROR"
    assert "/audiences/0" in caught.value.errors[0].message


# ---- async handoff ----


class _HandoffAudienceSeller(_AudienceSeller):
    def sync_audiences(self, audiences: Any, ctx: Any) -> Any:
        rows = self.rows

        async def work(task_ctx: Any) -> dict[str, Any]:
            return {"audiences": rows}

        return ctx.handoff_to_task(work)


@pytest.fixture
def handoff_server():
    made: list[Any] = []

    def _make(rows: list[dict[str, Any]]) -> tuple[Any, Any, Any]:
        registry = InMemoryTaskRegistry()
        handler, executor, _ = create_adcp_server_from_platform(
            _HandoffAudienceSeller(rows), registry=registry, validate_at_init=False
        )
        made.append(executor)
        return (
            create_tool_caller(handler, "sync_audiences"),
            create_tool_caller(handler, "get_task_status"),
            registry,
        )

    yield _make
    for executor in made:
        executor.shutdown(wait=True)


async def _terminal_status(
    status_caller: Any, registry: Any, task_id: str, version: str
) -> dict[str, Any]:
    for _ in range(200):
        record = await registry.get(task_id)
        if record["state"] in {"completed", "failed"}:
            break
        await asyncio.sleep(0.01)
    return await status_caller(
        {"adcp_version": version, "task_id": task_id, "include_result": True}
    )


DUAL_PROJECTION = {"platform_id": "p1", "seller_id": "p1"}


def _ids(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in ("platform_id", "seller_id") if k in row}


@pytest.mark.parametrize(
    "returned", [{"platform_id": "p1"}, {"seller_id": "p1"}], ids=["pid", "sid"]
)
async def test_handoff_terminal_result_carries_both_names_for_3_3(
    handoff_server: Any, returned: dict[str, Any]
) -> None:
    caller, status_caller, registry = handoff_server(_audience_rows(returned))
    submitted = await caller({**AUDIENCE_REQUEST, "adcp_version": BUYER_DUAL})
    assert submitted["status"] == "submitted"
    terminal = await _terminal_status(status_caller, registry, submitted["task_id"], BUYER_DUAL)
    assert terminal["status"] == "completed"
    assert _ids(terminal["result"]["audiences"][0]) == DUAL_PROJECTION
    stored = (await registry.get(submitted["task_id"]))["result"]["audiences"][0]
    assert _ids(stored) == DUAL_PROJECTION


async def test_handoff_mismatch_fails_the_task(handoff_server: Any) -> None:
    caller, status_caller, registry = handoff_server(
        _audience_rows({"platform_id": "a", "seller_id": "b"})
    )
    submitted = await caller({**AUDIENCE_REQUEST, "adcp_version": BUYER_DUAL})
    terminal = await _terminal_status(status_caller, registry, submitted["task_id"], BUYER_DUAL)
    assert terminal["status"] == "failed"
    assert terminal["error"]["code"] == "INTERNAL_ERROR"
    assert "/audiences/0" in terminal["error"]["message"]


# The caller cannot submit a sync_audiences handoff to a 3.2 buyer (the bundle
# has no submitted-envelope schema for it at 3.2), so the legacy projection is
# driven through ``_project_handoff`` with the release the dispatcher resolved.


async def _run_handoff(rows: list[dict[str, Any]], *, resolved: str | None, applied: str | None):
    registry = InMemoryTaskRegistry()
    tool_ctx = ToolContext(resolved_adcp_version=resolved, application_adcp_version=applied)
    ctx = _build_request_context(tool_ctx, Account(id="acct_1"), None)

    async def work(task_ctx: Any) -> dict[str, Any]:
        return {"audiences": rows}

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as executor:
        envelope = await _project_handoff(
            TaskHandoff(work),
            ctx,
            method_name="sync_audiences",
            registry=registry,
            executor=executor,
        )
        for _ in range(200):
            record = await registry.get(envelope["task_id"], expected_account_id="acct_1")
            if record["state"] in {"completed", "failed"}:
                return record
            await asyncio.sleep(0.01)
    raise AssertionError("handoff did not finish")


@pytest.mark.parametrize(
    "returned", [{"platform_id": "p1"}, {"seller_id": "p1"}], ids=["pid", "sid"]
)
@pytest.mark.parametrize(
    ("resolved", "applied"),
    [
        (BUYER_LEGACY, None),
        ("3.0", None),
        (BUYER_DUAL, None),
        (None, BUYER_LEGACY),
        (None, None),
    ],
)
async def test_project_handoff_stores_both_names_whatever_the_callers_release(
    returned: dict[str, Any], resolved: str | None, applied: str | None
) -> None:
    # The stored artifact serves every later reader, so it is not narrowed to
    # the release the submitting caller negotiated.
    expected = DUAL_PROJECTION
    record = await _run_handoff(_audience_rows(returned), resolved=resolved, applied=applied)
    assert record["state"] == "completed"
    assert _ids(record["result"]["audiences"][0]) == expected


@pytest.mark.parametrize("resolved", [BUYER_LEGACY, BUYER_DUAL])
async def test_project_handoff_mismatch_fails_with_internal_error(resolved: str) -> None:
    record = await _run_handoff(
        _audience_rows({"platform_id": "a", "seller_id": "b"}), resolved=resolved, applied=None
    )
    assert record["state"] == "failed"
    assert record["error"]["code"] == "INTERNAL_ERROR"
    assert "/audiences/0" in record["error"]["message"]
    assert not record.get("result")


# ---- WorkflowHandoff: externally completed through the registry ----


class _WorkflowAudienceSeller(_AudienceSeller):
    def sync_audiences(self, audiences: Any, ctx: Any) -> Any:
        return ctx.handoff_to_workflow(lambda task_ctx: None)


@pytest.fixture
def workflow_server():
    made: list[Any] = []

    def _make() -> tuple[Any, Any, Any]:
        registry = InMemoryTaskRegistry()
        handler, executor, _ = create_adcp_server_from_platform(
            _WorkflowAudienceSeller([]), registry=registry, validate_at_init=False
        )
        made.append(executor)
        return (
            create_tool_caller(handler, "sync_audiences"),
            create_tool_caller(handler, "get_task_status"),
            registry,
        )

    yield _make
    for executor in made:
        executor.shutdown(wait=True)


@pytest.mark.parametrize(
    "returned", [{"seller_id": "p1"}, {"platform_id": "p1"}], ids=["sid", "pid"]
)
async def test_workflow_handoff_external_completion_carries_both_names(
    workflow_server: Any, returned: dict[str, Any]
) -> None:
    caller, status_caller, registry = workflow_server()
    submitted = await caller({**AUDIENCE_REQUEST, "adcp_version": BUYER_DUAL})
    assert submitted["status"] == "submitted"
    task_id = submitted["task_id"]

    await registry.complete(task_id, {"audiences": _audience_rows(returned)})

    status = await status_caller(
        {"adcp_version": BUYER_DUAL, "task_id": task_id, "include_result": True}
    )
    assert status["status"] == "completed"
    assert _ids(status["result"]["audiences"][0]) == DUAL_PROJECTION


async def test_workflow_handoff_conflicting_completion_raises(workflow_server: Any) -> None:
    caller, status_caller, registry = workflow_server()
    submitted = await caller({**AUDIENCE_REQUEST, "adcp_version": BUYER_DUAL})
    task_id = submitted["task_id"]

    with pytest.raises(ValueError, match="/audiences/0"):
        await registry.complete(
            task_id, {"audiences": _audience_rows({"platform_id": "a", "seller_id": "b"})}
        )

    status = await status_caller({"adcp_version": BUYER_DUAL, "task_id": task_id})
    assert status["status"] == "submitted"


# ---- InMemoryTaskRegistry.complete: the persistence boundary ----


async def _issued(registry: InMemoryTaskRegistry, task_type: str) -> str:
    return await registry.issue(task_type=task_type, account_id="acct_1")


@pytest.mark.parametrize(
    "returned", [{"seller_id": "p1"}, {"platform_id": "p1"}], ids=["sid", "pid"]
)
async def test_registry_complete_stores_both_names(returned: dict[str, Any]) -> None:
    registry = InMemoryTaskRegistry()
    task_id = await _issued(registry, "sync_audiences")
    await registry.complete(task_id, {"audiences": _audience_rows(returned)})
    record = await registry.get(task_id)
    assert record["state"] == "completed"
    assert _ids(record["result"]["audiences"][0]) == DUAL_PROJECTION


async def test_registry_complete_stores_both_names_for_event_sources() -> None:
    registry = InMemoryTaskRegistry()
    task_id = await _issued(registry, "sync_event_sources")
    await registry.complete(task_id, {"event_sources": _event_rows({"seller_id": "p1"})})
    record = await registry.get(task_id)
    assert _ids(record["result"]["event_sources"][0]) == DUAL_PROJECTION


async def test_registry_complete_does_not_mutate_the_callers_result() -> None:
    registry = InMemoryTaskRegistry()
    task_id = await _issued(registry, "sync_audiences")
    result = {"audiences": _audience_rows({"seller_id": "p1"})}
    await registry.complete(task_id, result)
    assert "platform_id" not in result["audiences"][0]


async def test_registry_complete_mismatch_raises_and_stays_non_terminal() -> None:
    registry = InMemoryTaskRegistry()
    task_id = await _issued(registry, "sync_audiences")
    with pytest.raises(ValueError, match="/audiences/0"):
        await registry.complete(
            task_id, {"audiences": _audience_rows({"platform_id": "a", "seller_id": "b"})}
        )
    record = await registry.get(task_id)
    assert record["state"] not in {"completed", "failed"}
    assert not record.get("result")


async def test_registry_complete_recompletion_is_idempotent() -> None:
    registry = InMemoryTaskRegistry()
    task_id = await _issued(registry, "sync_audiences")
    result = {"audiences": _audience_rows({"seller_id": "p1"})}
    await registry.complete(task_id, result)
    await registry.complete(task_id, result)
    await registry.complete(task_id, {"audiences": _audience_rows(DUAL_PROJECTION)})
    with pytest.raises(ValueError, match="different result"):
        await registry.complete(task_id, {"audiences": _audience_rows({"seller_id": "other"})})


async def test_registry_complete_leaves_other_task_types_untouched() -> None:
    registry = InMemoryTaskRegistry()
    task_id = await _issued(registry, "get_products")
    result = {"audiences": [{"audience_id": "a1", "seller_id": "p1"}]}
    await registry.complete(task_id, result)
    record = await registry.get(task_id)
    assert record["result"] == result
