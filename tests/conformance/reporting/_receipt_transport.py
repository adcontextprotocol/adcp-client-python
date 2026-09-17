"""Real unified production mounts, with deterministic trusted auth adapters."""

import json
from contextlib import asynccontextmanager

import httpx
from asgi_lifespan import LifespanManager

from adcp.decisioning import Account, AuthInfo, RequestContext
from adcp.decisioning.registry import (
    ApiKeyCredential,
    BuyerAgent,
    HttpSigCredential,
    OAuthCredential,
)
from adcp.reporting.receipts import ReportingReceiptError, ReportingReceiptHandler
from adcp.server.auth import BearerTokenAuth, Principal, auth_context_factory
from adcp.server.idempotency import IdempotencyStore, MemoryBackend
from adcp.server.serve import _build_mcp_and_a2a_app


class Registry:
    def __init__(self):
        self.agents = {}
        self.calls = []

    async def resolve_by_agent_url(self, agent_url):
        self.calls.append(("signed", agent_url))
        return self.agents.get(agent_url)

    async def resolve_by_credential(self, credential):
        key = (
            credential.key_id if isinstance(credential, ApiKeyCredential) else credential.client_id
        )
        self.calls.append((credential.kind, key))
        return self.agents.get(key)


class ForbiddenGenericCache(MemoryBackend):
    async def get(self, *args, **kwargs):
        raise AssertionError("receipt ingress reached the generic idempotency cache")


class MountedReceipts:
    def __init__(self, h, *, hydrated=False, registry_kind=None, version=None):
        self.h = h
        self.hydrated = hydrated
        self.registry_kind = registry_kind
        self.tokens = {}
        self.grants = set()
        self.accounts = {}
        self.auth_calls = []
        self.contexts = []
        self.registry = Registry() if registry_kind is not None else None
        self.sessions = {}
        self.counter = 0
        self.idempotency = IdempotencyStore(backend=ForbiddenGenericCache())
        self.handler = ReportingReceiptHandler(
            h.store, resolve_account=self.resolve_account, buyer_agents=self.registry
        )
        # Exercise both the direct method decorator and the common generic
        # middleware whose wrapped function is named execute, not the task.
        self.handler.sync_reporting_receipts = self.idempotency.wrap(
            self.handler.sync_reporting_receipts
        )
        if version is not None:
            self.handler.adcp_version = version

    def authorize(self, s, *, token="token-one"):
        account, consumer = s.obligation.account_id, s.binding.consumer_id
        self.tokens[token] = Principal(
            caller_identity=consumer,
            tenant_id="one-shared-tenant",
            metadata={"account_id": account, "credential_id": token},
        )
        self.grants.add((account, consumer))
        self.accounts[account] = account
        if self.registry is not None:
            agent = BuyerAgent(consumer, "Fixture buyer", "active")
            self.registry.agents[token] = self.registry.agents[consumer] = agent

    async def resolve_account(self, reference, context, consumer):
        self.auth_calls.append((dict(reference), consumer))
        account = self.accounts.get(reference.get("account_id"))
        if (account, consumer) not in self.grants:
            raise ReportingReceiptError("UNAUTHORIZED")
        return account

    def context(self, meta):
        raw = auth_context_factory(meta)
        auth = raw.metadata.get("adcp.auth_info")
        agent = None
        if self.registry_kind is not None and raw.caller_identity is not None:
            key = raw.metadata["credential_id"]
            if self.registry_kind == "api_key":
                credential = ApiKeyCredential("api_key", key)
            elif self.registry_kind == "oauth":
                credential = OAuthCredential("oauth", key, ("reporting:write",))
            else:
                credential = HttpSigCredential("http_sig", key, raw.caller_identity, 1.0)
            # This is the adapter output of trusted verification, never an
            # inbound body field. API/OAuth consumer resolution is registry-owned.
            auth = AuthInfo(kind=self.registry_kind, credential=credential)
            raw.metadata["adcp.auth_info"] = auth
            agent = BuyerAgent(raw.caller_identity, "Fixture buyer", "active")
        if self.hydrated:
            context = RequestContext(
                account=Account(id=raw.metadata.get("account_id", "unresolved")),
                caller_identity="opaque:AccountStore:cache:key:identical-for-both-consumers",
                tenant_id=raw.tenant_id,
                metadata=raw.metadata,
                auth_info=auth,
                buyer_agent=agent,
                auth_principal=raw.caller_identity,
            )
        else:
            context = raw
        self.contexts.append(context)
        return context

    async def middleware(self, name, params, context, call_next):
        async def execute(params, context):
            return await call_next()

        return await self.idempotency.wrap(execute)(params, context)

    @asynccontextmanager
    async def client(self, **options):
        app = _build_mcp_and_a2a_app(
            self.handler,
            name="receipt-ingress",
            port=3001,
            host="127.0.0.1",
            instructions=None,
            test_controller=None,
            context_factory=self.context,
            middleware=[self.middleware],
            allowed_hosts=["localhost"],
            auth=BearerTokenAuth(validate_token=lambda token: self.tokens.get(token)),
            **options,
        )
        async with LifespanManager(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                yield client

    async def mcp(
        self, client, request=None, *, token="token-one", inventory=False, mutate_wire=None
    ):
        headers = {
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {token}",
        }
        if token not in self.sessions:
            initial = await client.post(
                "/mcp/",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "receipt-test", "version": "1"},
                    },
                },
            )
            assert initial.status_code == 200, initial.text
            self.sessions[token] = initial.headers.get("mcp-session-id")
        if self.sessions[token] is not None:
            headers["mcp-session-id"] = self.sessions[token]
        self.counter += 1
        wire = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self.counter,
                "method": "tools/list" if inventory else "tools/call",
                "params": (
                    {} if inventory else {"name": "sync_reporting_receipts", "arguments": request}
                ),
            }
        )
        response = await client.post(
            "/mcp/",
            headers={**headers, "content-type": "application/json"},
            content=mutate_wire(wire) if mutate_wire is not None else wire,
        )
        if response.status_code != 200:
            try:
                payload = response.json()
            except ValueError:
                payload = {"unstructured_transport_error": True}
            return response.status_code, payload
        payload = next(
            (
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ),
            None,
        )
        if payload is None:
            payload = response.json()
        if inventory:
            return 200, payload["result"]
        result = payload.get("result", payload)
        if "structuredContent" in result:
            return 200, result["structuredContent"]
        for part in result.get("content", []):
            if part.get("type") == "text":
                return 200, json.loads(part["text"])
        return 200, result

    async def a2a(self, client, request, *, token="token-one", mutate_wire=None, v1=False):
        self.counter += 1
        envelope = {
            "jsonrpc": "2.0",
            "id": str(self.counter),
            "method": "SendMessage" if v1 else "message/send",
            "params": {
                "message": {
                    "messageId": f"message-{self.counter}",
                    "role": "user",
                    "parts": [
                        {
                            "kind": "data",
                            "data": {"skill": "sync_reporting_receipts", "parameters": request},
                        }
                    ],
                }
            },
        }
        if v1:
            del envelope["params"]["message"]["parts"][0]["kind"]
            envelope["params"]["message"]["role"] = "ROLE_USER"
        wire = json.dumps(envelope)
        response = await client.post(
            "/",
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
                "A2A-Version": "1.0" if v1 else "0.3",
            },
            content=mutate_wire(wire) if mutate_wire is not None else wire,
        )
        if response.status_code != 200:
            try:
                payload = response.json()
            except ValueError:
                payload = {"unstructured_transport_error": True}
            return response.status_code, payload
        payload = response.json()
        result = payload.get("result", payload)
        if "task" in result:
            result = result["task"]
        for artifact in result.get("artifacts", []):
            for part in artifact.get("parts", []):
                if "data" in part:
                    return 200, part["data"]
        return 200, result


def error_code(payload):
    if "adcp_error" in payload:
        return payload["adcp_error"]["code"]
    if "errors" in payload:
        return payload["errors"][0]["code"]
    raise AssertionError(payload)
