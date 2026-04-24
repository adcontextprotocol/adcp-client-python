# Migrating from v4.0 to v4.1

4.1 migrates the A2A stack from `a2a-sdk>=0.3,<1.0` to `a2a-sdk>=1.0.1,<2.0`.
The 1.0 Python SDK is a protobuf-typed rewrite at a new A2A protocol wire
version; the server keeps a 0.3 compat route (`enable_v0_3_compat=True`)
so external A2A peers on the 0.3 wire continue to interoperate without
any buyer-side change.

**Most users need to do nothing.** The migration is wire-preserving: a
0.3-era peer hitting our 4.1 server sees `"state": "completed"` and
`"kind": "task"` as before. The items below apply only if you:

* subclass `ADCPHandler` with `from a2a.types import …` annotations, or
* catch `a2a.utils.errors.ServerError`, or
* read `adcp_version` / `protocols_supported` off the return of
  `ADCPClient.get_agent_info()`.

Update your dependency pin:

```toml
# pyproject.toml
[project]
dependencies = [
    "adcp>=4.1.0,<5",
]
```

## Breaking changes for subclassers

### `a2a.utils.errors.ServerError` → `A2AError`

The 1.0 SDK removes the `ServerError` wrapper class. Errors now inherit
directly from `A2AError` subclasses (`InternalError`, `InvalidParamsError`,
etc.) and are returned as proto messages rather than raised through the
`ServerError` envelope.

**Before (v4.0):**

```python
from a2a.utils.errors import ServerError

try:
    result = await handler.handle(request)
except ServerError as exc:
    log.error("handler failed", exc_info=exc)
```

**After (v4.1):**

```python
from a2a.utils.errors import A2AError

try:
    result = await handler.handle(request)
except A2AError as exc:
    log.error("handler failed", exc_info=exc)
```

If you need a transitional shim while rolling out the upgrade across a
multi-package deploy:

```python
try:
    from a2a.utils.errors import A2AError as _A2AError_or_ServerError
except ImportError:  # a2a-sdk < 1.0
    from a2a.utils.errors import ServerError as _A2AError_or_ServerError
```

### `get_agent_info()` no longer returns `adcp_version` / `protocols_supported`

The 1.0 proto `AgentCard` has no duck-typeable `extensions` map. The
0.3-era code path that surfaced `extensions.adcp.adcp_version` and
`extensions.adcp.protocols_supported` on
`ADCPClient.get_agent_info()` now returns a card-shape dict without
those two keys.

**Before (v4.0):**

```python
info = await client.get_agent_info()
adcp_version = info["adcp_version"]             # raises KeyError in 4.1
protocols    = info["protocols_supported"]      # raises KeyError in 4.1
```

**After (v4.1):** dispatch the `get_adcp_capabilities` skill to get the
live declaration from the peer:

```python
capabilities = await client.fetch_capabilities()
adcp_version = capabilities.adcp.major_versions
protocols    = capabilities.supported_protocols
```

`get_agent_info()` still returns `name`, `description`, `version`,
`protocol`, `tools`, and a new `a2a_protocol_versions` list (e.g.
`["0.3", "1.0"]`) that lets callers confirm which A2A wire versions the
peer advertises before pinning.

### `a2a-sdk` type import paths shifted

If you import a2a types directly for annotations or construction, the
module layout changed from Pydantic models under `a2a.types` to proto
messages under the same namespace. Most names are the same (`Task`,
`Message`, `Part`, `Artifact`, `TaskStatus`, `Role`, `TaskState`) but
their construction forms differ:

* `Part(root=DataPart(data={...}))` → `Part(data=<protobuf Value>)`
* `TaskState.completed` (Pydantic string enum) →
  `TaskState.TASK_STATE_COMPLETED` (protobuf int enum)
* `Role.user` → `Role.ROLE_USER`

**If you build A2A messages in application code,** use
`adcp.webhooks.create_a2a_webhook_payload` (which handles the proto
construction internally) or consult `src/adcp/protocols/a2a.py`'s
`_make_data_part` / `_make_text_part` helpers as reference.

**If you only read `.data` off a Part in handler code,** the receiver
side has not changed — `ADCPHandler` still hands you a plain dict.

## Why 4.1 and not 5.0

See PR #261 for the full discussion. Short version: A2A adopter
population is near-zero at the time of 4.1 (adoption is deferred behind
pluggable TaskStore / push-notif / middleware work that isn't merged),
and 4.0 shipped less than 48 hours earlier. Semver-strict would be 5.0,
but the pragmatic blast radius is small enough that a second major
release 48h later was judged worse DX than disclosing the
subclasser-facing breakages here.

If you subclass `ADCPHandler` or catch `ServerError` and hit a surprise
on upgrade, please file a GitHub issue so the 5.0 release notes carry
the real-world impact inventory.
