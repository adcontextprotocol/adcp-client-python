"""The actual production handler on authenticated MCP and both A2A mounts."""

import json

from ._receipt_transport import MountedReceipts


class MountedProduction(MountedReceipts):
    def __init__(self, h):
        super().__init__(h)
        self.handler = h.production.handler
        self.version = self.handler.get_adcp_version()

    def authorize(self, s, *, token="token-one"):
        super().authorize(s, token=token)
        self.h.authorized_bindings.add((s.obligation.account_id, s.binding.consumer_id))

    async def middleware(self, name, params, context, call_next):
        return await call_next()

    async def call(self, client, task, request, *, transport="mcp", token="token-one"):
        request = {"adcp_version": self.version, **request}

        def route(wire):
            value = json.loads(wire)
            if transport == "mcp":
                value["params"]["name"] = task
            else:
                value["params"]["message"]["parts"][0]["data"]["skill"] = task
            return json.dumps(value)

        if transport == "mcp":
            return await self.mcp(client, request, token=token, mutate_wire=route)
        return await self.a2a(
            client, request, token=token, mutate_wire=route, v1=transport == "a2a-1.0"
        )
