# Migrating from v4.0 to v4.1

4.1 picks up [AdCP 3.0.1](https://adcontextprotocol.org/docs/reference/release-notes#version-301)
(a stable-surface no-op for handlers — no field renames, no new enum values
on stable schemas) and ships the [a2a-sdk 1.0 migration](https://a2a-protocol.org/changelog).
Most code keeps working unchanged. This guide lists the three places where
that's not true.

## 1. `ADCPClient.pending_task_id` → `ADCPClient.active_task_id`

The attribute name reflects what it actually holds — the *currently active*
A2A task id, including non-terminal states (working, input-required, auth-required).
"pending" suggested only one specific lifecycle phase.

```python
# Before (4.0)
if client.pending_task_id is not None:
    ...

# After (4.1)
if client.active_task_id is not None:
    ...
```

Same rename on `A2AAdapter.pending_task_id` → `active_task_id`.

The constructor's `context_id=` kwarg and `reset_context()` now raise
`TypeError` (was `ValueError`) when called on non-A2A protocols. The string
value remains acceptable; only the operation is invalid for MCP. Catch
`TypeError` if you were catching `ValueError` here.

## 2. `FormatId` class identity changed

AdCP 3.0.1 polished `core/format-id.json`'s schema title from `"Format ID"`
to `"Format Reference (Structured Object)"`. datamodel-code-generator
follows the title, so the canonical class on disk is now
`FormatReferenceStructuredObject` — the public `FormatId` name is preserved
as an alias in `adcp.types.aliases`.

For 99% of code, this is invisible:

```python
# Both work identically on 4.0 and 4.1
from adcp import FormatId, Format

fid = FormatId(agent_url="https://creative.example.com", id="display_300x250")
fmt = Format(format_id=fid, ...)

isinstance(fid, FormatId)  # True on both versions
```

Two niche cases break:

- **Pickled `FormatId` instances from 4.0** fail to unpickle on 4.1 because
  the qualname `FormatId` no longer exists at
  `adcp.types.generated_poc.core.format_id`. Re-create the instances under
  4.1 (or migrate to JSON serialization, which round-trips cleanly across
  both versions).
- **Reflection on `FormatId.__name__`** sees `"FormatReferenceStructuredObject"`
  rather than `"FormatId"`. If you were snapshotting type names in tests or
  log scrapers, update the expected values.

```python
# Before (4.0)
assert FormatId.__name__ == "FormatId"

# After (4.1)
assert FormatId.__name__ == "FormatReferenceStructuredObject"
```

## 3. `MEDIA_BUY_STATE_MACHINE` keys match the spec enum

The stale `pending_activation` key has been replaced with `pending_creatives`
and `pending_start` (the two distinct phases that 4.0 was conflating). On
4.0, `valid_actions_for_status("pending_activation")` returned a list; on
4.1 it returns `[]` and the spec-correct keys return the action lists.

```python
# Before (4.0) — was already broken in production agents
actions = valid_actions_for_status("pending_activation")  # returns a list

# After (4.1) — match the spec
actions = valid_actions_for_status("pending_creatives")   # creatives still pending
# or
actions = valid_actions_for_status("pending_start")       # creatives approved, awaiting start
```

If you stored `"pending_activation"` as a status string anywhere, map it to
`"pending_start"` on read.

## 4. Signing-prep hardening — three behavior changes

The 4.1 release includes the foundation-audit signing-prep work. Most of
it is purely additive (new opt-in kwargs, new exports), but three places
silently changed default behavior:

### 4a. `WebhookSender` and `webhooks.deliver()` now run SSRF guards

When the sender owns its httpx client (default path — no `client=` passed),
every webhook delivery now resolves the URL, validates against an SSRF
range list (loopback / RFC 1918 / link-local / CGNAT / IPv6 ULA / multicast
/ cloud-metadata), and pins the connection to the resolved IP. Plus
`follow_redirects=False` and `trust_env=False` close the rebinding-via-
redirect and `HTTPS_PROXY` env-var bypass.

For production deployments posting to real buyer URLs, this is a
no-op. For **dev/CI fixtures posting to internal endpoints** (loopback,
private, link-local), webhooks will start raising `SSRFValidationError`.
Two opt-outs:

```python
# Owned-client path — pass allow_private=True to disable the IP-range check
sender = WebhookSender.from_jwk(jwk, allow_private_destinations=True)
await deliver(config, payload, allow_private=True)

# Operator-supplied client path — the framework trusts the operator's
# transport completely, no SSRF guard runs (vetted egress proxy, ASGI
# test transport, etc.)
sender = WebhookSender.from_jwk(jwk, client=my_httpx_client)
```

Operators who want a hardened destination-port allowlist as defense
in depth opt INTO `DEFAULT_ALLOWED_PORTS = frozenset({443, 8443})`
(now exported from `adcp.signing`):

```python
from adcp.signing import DEFAULT_ALLOWED_PORTS

sender = WebhookSender.from_jwk(jwk, allowed_destination_ports=DEFAULT_ALLOWED_PORTS)
```

The default is permissive (`None` = no port filter) because AdCP doesn't
constrain `pushNotificationConfig.url` ports.

### 4b. `VerifierCapability.covers_content_digest` defaults to `"either"`

The default flipped from `"required"` to `"either"` to align with the
AdCP 3.0 schema (`get-adcp-capabilities-response.json` declares
`"either"` as the default explicitly). The schema rationale recommends
`"required"` for spend-committing operations in production, and AdCP
4.0 recommends `"required"` more broadly.

Adopters who relied on the implicit `"required"` default lose
body-integrity authentication on signed requests not enumerated in
`required_for`. **The webhook-signing profile (`adcp.signing.webhook_verifier`)
is unaffected — it hard-codes `"required"`** so every signed webhook
still carries a body-integrity-binding `Content-Digest`.

To preserve the prior strict behavior, opt INTO `"required"` explicitly
or use `required_for` to promote spend-committing operations:

```python
# Before (4.0): VerifierCapability() defaulted to covers_content_digest="required"
cap = VerifierCapability()

# After (4.1): explicit opt-in
cap = VerifierCapability(covers_content_digest="required")

# Or: scope the strictness to operations that move money
cap = VerifierCapability(
    required_for=frozenset({"create_media_buy", "update_media_buy"}),
)
```

## What to test after upgrading

- Run your full test suite — the `pending_task_id` rename is a noisy compile
  break that surfaces immediately; the other two are quieter.
- If you have any pickle-based fixtures or `__name__` assertions, search for
  `FormatId` references and update the expected values.
- If you operate a media-buy state machine, search for `pending_activation`
  in your codebase.
- If you have **dev/CI fixtures posting webhooks to private/internal IPs**,
  add `allow_private=True` (on `deliver()`) or
  `allow_private_destinations=True` (on `WebhookSender`).
- If you constructed `VerifierCapability()` with no kwargs and relied on
  the implicit `"required"` body-digest enforcement, set
  `covers_content_digest="required"` explicitly or scope via `required_for`.
