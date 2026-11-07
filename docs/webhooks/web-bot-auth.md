# Web Bot Auth webhooks (AdCP 3.3)

At AdCP 3.3 a webhook registration (`push_notification_config`,
`reporting_webhook`, `artifact_webhook`, `accounts[].notification_configs[]`,
or an agent-level notification configuration) may omit `authentication` and
name a `signing_profile`. The seller signs every delivery under that profile:

- `adcp/webhook-signing/v1`: the AdCP RFC 9421 webhook profile, also used
  when the registration names no profile. Sign with
  `WebhookSender.from_jwk` / `from_pem`.
- `web-bot-auth`: the Web Bot Auth profile, experimental in 3.3 and the only
  HTTP signing profile in AdCP 4.0. A webhook is an ordinary Web Bot Auth
  request from the seller's origin. Sign with `WebhookSender.from_wba`.

## Senders

A seller whose buyers all use Web Bot Auth:

```python
from adcp.signing import load_private_key_pem
from adcp.webhooks import WebhookSender

wba_key = load_private_key_pem(Path("wba-ed25519.pem").read_bytes())
sender = WebhookSender.from_wba(wba_key, agent_origin="https://seller.example")
```

During 3.3 a seller usually has buyers on both profiles. Extend the existing
RFC 9421 sender, which stays the default:

```python
sender = WebhookSender.from_jwk(webhook_jwk).with_web_bot_auth(
    wba_key, agent_origin="https://seller.example"
)
assert sender.signing_profiles == {"adcp/webhook-signing/v1", "web-bot-auth"}
```

The sender signs a delivery under the profile its registration selected,
which the prepared request carries as `required_signing_profile`
(`prepare_media_buy_delivery`, `prepare_artifact_webhook(registration=...)`,
and the task outbox below set it). A delivery whose profile the sender holds
no key for raises `WebhookSigningProfileUnavailableError` before any network
I/O. `sender.check_registration(registration)` runs the same check when the
buyer registers. Prove control of a notification destination under its
profile too: `challenge_webhook_destination(..., sender=sender,
signing_profile=config.signing_profile)` signs the challenge under it
(`adcp/webhook-signing/v1` when omitted).

Each `web-bot-auth` attempt is signed afresh by
`adcp.signing.wba.sign_wba_request`: a new 64-byte nonce and `created`,
`Signature-Agent` naming the origin, the key's RFC 7638 thumbprint as
`keyid`, and `@method`, `@target-uri`, `@authority`, `content-type`,
`content-digest` and `signature-agent` covered. Receivers record nonces,
including on an attempt they answered 503, so `resend`, `send_prepared` and
both outboxes never resend a stored signature. `resend` signs under the
profile the original was signed under. `Signature-Agent` is reserved like the
other signature fields: `extra_headers` cannot set it.

The key must be Ed25519, the profile's one algorithm. An ES256 key stays on
`adcp/webhook-signing/v1`.

## Publish the key directory first

Receivers verify a `web-bot-auth` webhook against the key directory at
`https://seller.example/.well-known/http-message-signatures-directory`. The
sender's key is verifiable only once that directory is live, so publish it
before the first delivery:

```python
from adcp.signing.wba import DIRECTORY_PATH

directory = sender.sign_web_bot_auth_directory(
    other_keys=[request_signing_key],  # every other Web Bot Auth key of the origin
)

@app.get(DIRECTORY_PATH)
def key_directory() -> Response:
    return Response(directory.body, status_code=200, headers=dict(directory.headers))
```

The directory belongs to the origin, not to the sender. List every Ed25519 key
the origin signs with under Web Bot Auth, including request-signing keys and
keys mid-rotation, in `other_keys`; a key left out stops verifying once
receivers refresh their cached copy (up to five minutes). The signatures
expire after `expires_in_seconds` (one day by default), so re-sign and
republish well before then. When rotating, publish the new key, wait for
caches to expire, switch the sender, and keep the old key in the directory
while redeliveries signed with it may still arrive.
`adcp.signing.wba.sign_key_directory` is the underlying helper; see
[agent identity](../agent-identity-3-3-migration.md) for the directory rules.

## Capabilities

Advertise the profiles you can sign under:

```python
WebhookSigning(
    supported=True,
    profiles=["adcp/webhook-signing/v1", "web-bot-auth"],
    algorithms=["ed25519"],
    delivery_retry_horizon_seconds=86400,
)
```

At boot, `validate_webhook_signing_for_capabilities` refuses a wired sender
that holds no key for an advertised profile (omitted `profiles` means
`adcp/webhook-signing/v1`, so a `from_wba`-only sender must list
`web-bot-auth`), or that signs with an algorithm outside `algorithms` (a dual
sender is checked for both of its keys).

## Outboxes

`PgTaskWebhookOutbox` reads `push_notification_config.signing_profile` when
the task is issued and stores it in the encrypted registration and, at the
terminal transition, in the encrypted delivery row. Every attempt is signed
under it, `adcp/webhook-signing/v1` when the registration named none and
carried no `authentication`. Before the task is issued, a registration is
refused with `INVALID_REQUEST` (field `push_notification_config.signing_profile`)
when the profile it requires is not in this request's advertised
`webhook_signing.profiles` (`adcp/webhook-signing/v1` when omitted), checked
against the effective capabilities, a tenant's
`get_adcp_capabilities_for_request` override included. That applies to every
publisher: a fixed-sender outbox, a `sender_resolver` outbox, or an externally
managed one. A fixed sender is also checked for a key under that profile. A
`sender_resolver` outbox's resolved sender is checked again at each attempt,
and a row it cannot sign is quarantined. A resolved sender's every algorithm
must be in the scope's `advertised_algorithms`, including its Web Bot Auth
key's `ed25519`. Registrations naming `adcp/webhook-signing/v1` are stored
exactly like ones naming no profile.

`PgNotificationOutbox` stores the prepared request's
`required_signing_profile` with the body and signs each attempt under it. For
notification registrations (`accounts[].notification_configs[]`, agent-level
configurations), set it from the stored registration before enqueueing:

```python
from dataclasses import replace
from adcp.webhooks import webhook_signing_profile_for

prepared = WebhookSender.prepare_account_status_changed(url=str(config.url), payload=event)
prepared = replace(prepared, required_signing_profile=webhook_signing_profile_for(config))
await outbox.enqueue_prepared(
    conn, prepared, notification_type="account.status_changed", caller_scope_id=scope
)
```

Rolling deploys: a delivery row stored with a profile by an upgraded pod is
quarantined, not sent under the wrong profile, by a pod running an earlier
SDK, in both outboxes. Rows without a profile are unchanged. One gap remains:
a task registered on an upgraded pod and completed on an earlier one is
enqueued and signed by the earlier pod under its sender's default profile,
because that pod ignores the registration's profile. Finish the rollout
before listing `web-bot-auth` in `webhook_signing.profiles`.

## Reporting notifications

`ReportingNotificationSubscription` takes the registration's
`signing_profile` (only with a `signing_scope_id`, never with legacy
`authentication`). A subscription without one is stored and fingerprinted
exactly as before, so existing subscriptions and queued deliveries are
unaffected. For a `web-bot-auth` subscription, the
`ReportingSigningMaterial` your `ReportingSigningResolver` returns must carry
the origin's key:

```python
from adcp.reporting.outbox import ReportingSigningMaterial, ReportingWebBotAuthKey

return ReportingSigningMaterial(
    webhook_key, webhook_kid, "ed25519", frozenset({"ed25519"}),
    web_bot_auth=ReportingWebBotAuthKey(wba_key, "https://seller.example"),
)
```

The worker builds a `from_wba` sender for each attempt of a `web-bot-auth`
subscription, and keeps the `adcp/webhook-signing/v1` key for every other
one. A `web-bot-auth` subscription whose material carries no Web Bot Auth key
is quarantined (`permanent_scope`). `ed25519` must be among
`advertised_algorithms` whenever `web_bot_auth` is set. Production reporting
opts in with `ReportingProductionSigning(..., web_bot_auth=True)`; see
[reporting production](../reporting-production.md).

Rolling deploys: a pod on an earlier SDK quarantines a delivery stored for a
subscription with `signing_profile` (`integrity_failure`) rather than sign it
under the wrong profile, and its resolver cannot return such a subscription.
Deploy every reporting worker before your subscription resolver starts
setting the field.
