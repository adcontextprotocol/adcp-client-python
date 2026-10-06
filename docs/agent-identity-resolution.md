# Resolve an agent's signing keys

`async_resolve_agent` calls `get_adcp_capabilities` at the agent's protocol
endpoint, reads `identity.brand_json_url`, resolves the matching entry in
`brand.json`, and fetches its JWKS. MCP Streamable HTTP is the default;
pass `protocol="a2a"` for an A2A agent.

```python
from adcp.signing import async_resolve_agent

resolution = await async_resolve_agent(
    "https://buyer.example.com/mcp",
    agent_type="buying",
)
```

`resolve_agent` provides the same lookup for synchronous scripts.
`verify_from_agent_url` also accepts `protocol="mcp"` or `protocol="a2a"`.
The CLI uses the same selection:

```sh
adcp --resolve https://buyer.example.com/a2a --agent-type buying --protocol a2a --json
```

The agent URL must expose the chosen protocol. Serving a capabilities JSON
document on a plain GET is insufficient; the resolver has no raw GET fallback.
MCP responses may use structured content or JSON text content, over JSON or
SSE. A2A discovery fetches the agent card and invokes the capability skill
through its JSON-RPC interface.

Discovery keeps IP pinning, HTTPS validation, disabled environment proxies,
and bounded HTTP responses (64 KiB by default). The capabilities timeout
covers the protocol handshake and capability call together. Redirects are
disabled by default; an explicit redirect allowance stays within the agent's
origin, as must the A2A interface advertised by its card.
