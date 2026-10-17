"""A2A task ownership follows the verified origin; a delisted origin gets no exception."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from a2a import types as pb
from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from asgi_lifespan import LifespanManager
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from adcp.server import ADCPHandler, RequestSignatureVerification, a2a_server
from adcp.server.auth import BearerTokenAuth, Principal, _A2AAuthenticatedUser
from adcp.server.serve import _build_mcp_and_a2a_app
from adcp.server.signed_requests import current_verified_signer, current_wba_authorization_error
from adcp.signing import (
    OriginBindings,
    StaticSignerKeys,
    TrustRecordEvaluator,
    TrustResponse,
    WbaUnavailableError,
    public_directory_key,
    sign_wba_request,
)
from adcp.signing.canonical import canonical_origin

ORIGIN = "https://buyer.platform.com"
OTHER = "https://other.platform.com"


class OriginUser(User):
    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return ORIGIN


@pytest.fixture
async def task_env(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    monkeypatch.setenv("ADCP_SANDBOX", "1")
    private = {origin: Ed25519PrivateKey.generate() for origin in (ORIGIN, OTHER)}
    now, mode = [1000.0], ["listed"]
    started, bearer_lookups, controlled = [], [], []
    release = asyncio.Event()
    producers: set[asyncio.Task[Any]] = set()

    async def fetch(url: str, *, allow_private: bool = False) -> TrustResponse:
        if mode[0] == "offline":
            raise OSError("operator unavailable")
        origins = (ORIGIN, OTHER) if mode[0] == "listed" else (OTHER,)
        return TrustResponse(
            json.dumps(
                {"agents": [{"url": origin, "roles": ["adcp:buying"]} for origin in origins]}
            ).encode()
        )

    async def not_revoked(origin: str, key_id: str) -> bool:
        if store.signer_unavailable:
            raise WbaUnavailableError("revocation status unavailable")
        return False

    async def validate_token(token: str):
        bearer_lookups.append(token)
        return Principal(caller_identity="bearer-principal")

    class Store(InMemoryTaskStore):
        signer_unavailable = False
        hide_binding = False
        max_binding_reads: int | None = None
        binding_reads: list[str] = []

        async def get(self, task_id: str, context: ServerCallContext) -> pb.Task | None:
            controlled.append((task_id, context.user.user_name))
            return await super().get(task_id, context)

    store = Store()
    bindings = OriginBindings()
    for origin in private:
        bindings.bind(origin, "principal:" + origin)

    async def resolve_binding(origin: str) -> str | None:
        store.binding_reads.append(origin)
        if (
            store.max_binding_reads is not None
            and len(store.binding_reads) > store.max_binding_reads
        ):
            raise TimeoutError("an unnecessary second binding read must not occur")
        return None if store.hide_binding else await bindings(origin)

    class Handler(ADCPHandler[Any]):
        advertised_tools = {"get_products", "get_principal"}

        async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
            started.append(context.metadata["adcp.auth_info"])
            await asyncio.wait_for(release.wait(), timeout=10)
            return {"products": []}

        async def get_principal(self, params: Any, context: Any = None) -> dict[str, Any]:
            return {"principal": context.metadata["adcp.auth_info"].extra["principal_id"]}

    # Long-running executors emit a working event before producing their result.
    # Keep the real SDK ActiveTask lifecycle, persistence, and HTTP dispatch.
    execute = a2a_server.ADCPAgentExecutor.execute

    async def working(self: Any, context: Any, queue: Any) -> None:
        task = asyncio.current_task()
        assert task is not None
        producers.add(task)
        signer = current_verified_signer.get()
        long_running = (
            self._parse_request(context)[0] == "get_products"
            and signer is not None
            and signer.principal_id is not None
            and current_wba_authorization_error.get() is None
        )
        if long_running:
            await queue.enqueue_event(
                pb.Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=pb.TaskStatus(state=pb.TaskState.TASK_STATE_WORKING),
                )
            )

        class Updates:
            async def enqueue_event(self, event: Any) -> None:
                if isinstance(event, pb.Task):
                    for artifact in event.artifacts:
                        await queue.enqueue_event(
                            pb.TaskArtifactUpdateEvent(
                                task_id=event.id, context_id=event.context_id, artifact=artifact
                            )
                        )
                    await queue.enqueue_event(
                        pb.TaskStatusUpdateEvent(
                            task_id=event.id, context_id=event.context_id, status=event.status
                        )
                    )
                else:
                    await queue.enqueue_event(event)

        try:
            await execute(self, context, Updates() if long_running else queue)
        finally:
            producers.discard(task)

    monkeypatch.setattr(a2a_server.ADCPAgentExecutor, "execute", working)
    build_card = a2a_server._build_agent_card

    def streaming_card(*args: Any, **kwargs: Any) -> pb.AgentCard:
        card = build_card(*args, **kwargs)
        card.capabilities.streaming = True
        return card

    monkeypatch.setattr(a2a_server, "_build_agent_card", streaming_card)
    app = _build_mcp_and_a2a_app(
        Handler(),
        name="prior-task-controls",
        port=0,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        task_store=store,
        validation=None,
        stateless_http=True,
        allowed_hosts=["localhost"],
        auth=BearerTokenAuth(validate_token=validate_token),
        request_signature_verification=RequestSignatureVerification(
            signer_keys=StaticSignerKeys(
                {origin: {"keys": [public_directory_key(key)]} for origin, key in private.items()}
            ),
            enable_web_bot_auth=True,
            origin_bindings=resolve_binding,
            trust_records=TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0]),
            wba_revocation_checker=not_revoked,
            unbound_for=frozenset({"get_adcp_capabilities", "get_principal"})
            | (frozenset({"GetTask"}) if getattr(request, "param", False) else frozenset()),
        ),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as client:

            async def post(
                method: str,
                params: dict[str, Any],
                *,
                origin: str = ORIGIN,
                path: str = "/",
                bearer: bool = False,
                disconnect_after_first_event: bool = False,
                preauthenticated_origin: str | None = None,
            ) -> httpx.Response:
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": uuid4().hex, "method": method, "params": params}
                ).encode()
                headers = {
                    "host": "localhost",
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                }
                if not method.startswith(("message/", "tasks/", "tools/")):
                    headers["A2A-Version"] = "1.0"
                if bearer:
                    headers["authorization"] = "Bearer independent"
                headers.update(
                    sign_wba_request(
                        method="POST",
                        url="http://localhost" + path,
                        headers=headers,
                        body=body,
                        private_key=private[canonical_origin(origin)],
                        agent_origin=origin,
                    )
                )
                if disconnect_after_first_event or preauthenticated_origin is not None:
                    delivered = False
                    disconnect = asyncio.Event()
                    sent: list[dict[str, Any]] = []

                    async def receive() -> dict[str, Any]:
                        nonlocal delivered
                        if not delivered:
                            delivered = True
                            return {"type": "http.request", "body": body, "more_body": False}
                        await asyncio.wait_for(disconnect.wait(), 2)
                        return {"type": "http.disconnect"}

                    async def send(message: dict[str, Any]) -> None:
                        sent.append(message)
                        if b"data:" in message.get("body", b"") or (
                            message["type"] == "http.response.body"
                            and not message.get("more_body", False)
                        ):
                            disconnect.set()

                    scope = {
                        "type": "http",
                        "asgi": {"version": "3.0", "spec_version": "2.3"},
                        "http_version": "1.1",
                        "method": "POST",
                        "scheme": "http",
                        "path": path,
                        "raw_path": path.encode(),
                        "query_string": b"",
                        "root_path": "",
                        "server": ("localhost", 80),
                        "client": ("127.0.0.1", 50000),
                        "headers": [
                            (key.lower().encode(), value.encode()) for key, value in headers.items()
                        ],
                    }
                    if preauthenticated_origin is not None:
                        scope["user"] = _A2AAuthenticatedUser(display_name=preauthenticated_origin)
                        scope["auth"] = {"caller_identity": preauthenticated_origin}
                    await asyncio.wait_for(app(scope, receive, send), 5)
                    assert disconnect.is_set(), sent
                    start_message = next(
                        item for item in sent if item["type"] == "http.response.start"
                    )
                    return httpx.Response(
                        start_message["status"],
                        headers=start_message["headers"],
                        content=b"".join(item.get("body", b"") for item in sent),
                    )
                return await asyncio.wait_for(client.post(path, content=body, headers=headers), 5)

            async def start(origin: str = ORIGIN) -> str:
                response = await post(
                    "message/send",
                    {
                        "message": {
                            "messageId": uuid4().hex,
                            "role": "user",
                            "parts": [
                                {
                                    "kind": "data",
                                    "data": {"skill": "get_products", "parameters": {}},
                                }
                            ],
                        },
                        "configuration": {"blocking": False},
                    },
                    origin=origin,
                )
                assert response.status_code == 200, response.text
                task = response.json()["result"]
                assert task["status"]["state"] == "working", response.text
                return task["id"]

            try:
                yield post, start, store, now, mode, started, bearer_lookups, controlled
            finally:
                release.set()
                if producers:
                    await asyncio.wait_for(asyncio.gather(*producers, return_exceptions=True), 2)
                await asyncio.sleep(0)


def assert_not_found(response: httpx.Response) -> None:
    assert response.status_code == 200, response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        events = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        assert len(events) == 1, response.text
        wire = events[0]
    else:
        wire = response.json()
    # The installed legacy adapter maps native TaskNotFound to its generic
    # JSON-RPC error; its message and missing/foreign behavior remain identical.
    assert wire["error"]["code"] in {-32001, -32603}, response.text
    assert wire["error"]["message"] == "Task not found", response.text
    assert "accept-signature" not in response.headers


@pytest.mark.parametrize(
    "method", ["tasks/get", "tasks/cancel", "GetTask", "CancelTask", "SubscribeToTask"]
)
async def test_delisted_origin_gets_no_task_control_exception(task_env, method):
    post, start, store, now, mode, started, bearer, controlled = task_env
    task_id = await start()
    mode[0], now[0] = "delisted", 1061
    controlled.clear()
    # Identity-established for every request, polling and cancelling the
    # tasks it created while listed included: what an anonymous caller gets.
    anonymous = await post(method, {"id": task_id})
    assert anonymous.status_code == 401, anonymous.text
    assert "accept-signature" not in anonymous.headers
    # An independent bearer credential is its own owner, never the origin's.
    with_bearer = await post(method, {"id": task_id}, bearer=True)
    assert_not_found(with_bearer)
    assert (task_id, ORIGIN) not in controlled
    assert len(started) == 1


@pytest.mark.parametrize(
    "method", ["CancelTask", "SubscribeToTask", "tasks/cancel", "tasks/resubscribe"]
)
async def test_active_tasks_cannot_be_controlled_by_another_origin(task_env, method):
    post, start, store, now, mode, started, bearer, controlled = task_env
    task_id = await start(OTHER)
    # Even duplicate IDs in two owner-scoped partitions must not let the
    # globally indexed ActiveTask of OTHER be canceled/subscribed by ORIGIN.
    await store.save(
        pb.Task(
            id=task_id,
            context_id="duplicate",
            status=pb.TaskStatus(state=pb.TaskState.TASK_STATE_WORKING),
        ),
        ServerCallContext(user=OriginUser()),
    )
    assert_not_found(await post(method, {"id": task_id}))
    own = await post("GetTask", {"id": task_id}, origin=OTHER)
    assert own.json()["result"]["status"]["state"] == "TASK_STATE_WORKING", own.text


async def test_delisted_task_listing_is_not_admitted(task_env):
    post, start, store, now, mode, started, bearer, controlled = task_env
    await start()
    mode[0], now[0] = "delisted", 1061
    response = await post("ListTasks", {})
    assert response.status_code == 401


@pytest.mark.parametrize("method", ["CancelTask", "SubscribeToTask"])
async def test_registry_replacement_is_checked_on_the_exact_task_used(
    task_env, method, monkeypatch: pytest.MonkeyPatch
):
    from a2a.server.agent_execution.active_task_registry import ActiveTaskRegistry

    post, start, store, now, mode, started, bearer, controlled = task_env
    own_id, foreign_id = await start(), await start(OTHER)
    get_or_create = ActiveTaskRegistry.get_or_create

    async def replaced(self: Any, task_id: str, *args: Any, **kwargs: Any):
        # Simulate cleanup/replacement after the persisted owner check. A
        # second lookup could otherwise select a foreign ActiveTask.
        return await get_or_create(
            self, foreign_id if task_id == own_id else task_id, *args, **kwargs
        )

    monkeypatch.setattr(ActiveTaskRegistry, "get_or_create", replaced)
    assert_not_found(await post(method, {"id": own_id}))
    foreign = await post("GetTask", {"id": foreign_id}, origin=OTHER)
    assert foreign.json()["result"]["status"]["state"] == "TASK_STATE_WORKING"


async def test_delisted_principal_introspection_stays_demoted_during_later_outage(task_env):
    post, start, store, now, mode, started, bearer, controlled = task_env
    await start()
    mode[0], now[0] = "delisted", 1061
    assert (await post("GetTask", {"id": "any"})).status_code == 401
    mode[0], now[0] = "offline", 1122
    response = await post(
        "message/send",
        {
            "message": {
                "messageId": "principal",
                "role": "user",
                "parts": [{"kind": "data", "data": {"skill": "get_principal", "parameters": {}}}],
            }
        },
    )
    assert response.json()["result"]["artifacts"][0]["parts"][0]["data"]["principal"] is None


async def test_new_message_cannot_retarget_a_foreign_active_task(task_env):
    post, start, store, now, mode, started, bearer, controlled = task_env
    task_id = await start(OTHER)
    await store.save(
        pb.Task(
            id=task_id,
            context_id="duplicate",
            status=pb.TaskStatus(state=pb.TaskState.TASK_STATE_WORKING),
        ),
        ServerCallContext(user=OriginUser()),
    )
    response = await post(
        "message/send",
        {
            "message": {
                "messageId": "continuation",
                "taskId": task_id,
                "role": "user",
                "parts": [{"kind": "data", "data": {"skill": "get_products", "parameters": {}}}],
            },
            "configuration": {"blocking": False},
        },
    )
    assert_not_found(response)
    assert len(started) == 1
    foreign = await post("GetTask", {"id": task_id}, origin=OTHER)
    assert foreign.json()["result"]["status"]["state"] == "TASK_STATE_WORKING"


@pytest.mark.parametrize("method", ["SubscribeToTask", "tasks/resubscribe"])
async def test_real_client_disconnect_ends_only_the_owned_subscription(task_env, method):
    post, start, store, now, mode, started, bearer, controlled = task_env
    task_id = await start()
    response = await post(method, {"id": task_id}, disconnect_after_first_event=True)
    assert response.status_code == 200, response.text
    assert task_id in response.text
    assert "working" in response.text.lower()
    still_working = await post("GetTask", {"id": task_id})
    assert still_working.json()["result"]["status"]["state"] == "TASK_STATE_WORKING"


@pytest.mark.parametrize("task_env", [True], indirect=True)
async def test_explicit_anonymous_task_policy_does_not_recover_an_unbound_origin_owner(task_env):
    post, start, store, now, mode, started, bearer, controlled = task_env
    task_id = await start()
    store.hide_binding = True
    assert_not_found(await post("GetTask", {"id": task_id}))
    assert controlled[-1] == (task_id, "")
