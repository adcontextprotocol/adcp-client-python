# Migrating from fragmented webhook senders

If your operator stack has multiple legacy webhook-sending paths — `requests.post`-and-handroll-the-headers in one service, a homemade HMAC helper in another, ad-hoc retries in a third — `WebhookSender` consolidates them. Same one-call delivery API across every authentication mode supported by AdCP and by common buyer ecosystems (Svix/Resend/operator-bearer).

This guide is a translation table. Find your legacy pattern on the left; the right column is the equivalent on `WebhookSender`.

The killer property: in every row, the bytes the sender signs are byte-for-byte the bytes that hit the wire. The classic "I signed `json.dumps(payload, separators=(',',':'))` but `requests` re-serialized with whitespace before POST" bug is impossible by construction.

---

## Auth modes

`WebhookSender` exposes one classmethod per auth mode. Pick one per sender; if you need two modes (bearer-at-gateway plus end-to-end signature), construct two senders.

### RFC 9421 JWK signing — the AdCP-conformant default

```python
from adcp.webhooks import WebhookSender

sender = WebhookSender.from_jwk(webhook_signing_jwk_with_private_d)
async with sender:
    result = await sender.send_mcp(
        url="https://buyer.example.com/webhooks/adcp/create_media_buy/op_abc",
        task_id="task_456",
        task_type="create_media_buy",
        operation_id="op_abc",
        status="completed",
        result={"media_buy_id": "mb_1"},
    )
```

Use this for every AdCP-conformant buyer. JWK signing is the spec baseline.

### `Authorization: Bearer <token>` — for buyers who authenticate at the gateway

```python
sender = WebhookSender.from_bearer_token("super-secret-token")
async with sender:
    result = await sender.send_mcp(
        url=..., task_id=..., task_type=..., operation_id=..., status=...
    )
```

The body still goes through the same byte-exact marshaling, and `idempotency_key` still ends up inside the JSON for receiver dedup. There is no body signature; a buyer treating the bearer as the sole authenticity signal must enforce TLS pinning or mTLS at the transport layer to make a stolen token unusable.

### AdCP-legacy HMAC-SHA256 — back-compat for AdCP 3.x receivers

```python
sender = WebhookSender.from_adcp_legacy_hmac(
    secret=b"shared-secret-bytes",
    key_id="kid_buyer_42",
)
async with sender:
    result = await sender.send_mcp(
        url=..., task_id=..., task_type=..., operation_id=..., status=...
    )
```

Wire format matches `verify_webhook_hmac` in `adcp.signing.webhook_hmac`: `X-AdCP-Signature: sha256=<hex>` over `f"{timestamp}.{body}"`, with `X-AdCP-Timestamp` set fresh on every delivery (resends produce a new signature over the same body bytes — receivers enforcing a 300s skew window won't reject the retry).

Plan to migrate to JWK signing before AdCP 4.0; the legacy `authentication` field is removed in 4.0.

### Standard Webhooks v1 — Svix / Resend / standardwebhooks.com interop

```python
sender = WebhookSender.from_standard_webhooks_secret(
    "whsec_<base64-distributed-by-buyer>",
    key_id="kid_svix_42",
)
async with sender:
    result = await sender.send_mcp(
        url=..., task_id=..., task_type=..., operation_id=..., status=...
    )
```

The constructor takes the canonical `whsec_<base64>` form Svix and Resend distribute and base64-decodes it internally. **Do not pass the literal `whsec_…` string to `from_adcp_legacy_hmac`** — the AdCP-legacy scheme HMACs against raw bytes, so you'd silently produce signatures Svix would reject. The two constructors enforce different secret-encoding contracts at the type level so this swap can't happen.

Wire format per spec: `webhook-id` / `webhook-timestamp` / `webhook-signature: v1,<base64>` over `f"{webhook_id}.{webhook_timestamp}.{body}"`. Each delivery gets a fresh `webhook-id` so a Svix-style receiver caching ids for replay defense doesn't false-positive on a legitimate retry.

---

## Translation table

### Pattern: hand-built bearer POST

```python
# Legacy
import requests, json
requests.post(
    url,
    data=json.dumps(payload),
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    },
)
```

```python
# Equivalent
sender = WebhookSender.from_bearer_token(token)
async with sender:
    await sender.send_raw(
        url=url,
        idempotency_key=payload["idempotency_key"],
        payload=payload,
    )
```

`send_raw` requires `idempotency_key` as a kwarg — it's injected into the payload before serialization. Pass the same value you used to use; if your legacy code generated it ad hoc, call `generate_webhook_idempotency_key()` once and pass it in.

### Pattern: hand-built HMAC POST with the sign-vs-body bug

```python
# Legacy — note the lurking serialization mismatch
import hmac, hashlib, json, requests
body_str = json.dumps(payload, separators=(",", ":"))
sig = hmac.new(secret, body_str.encode(), hashlib.sha256).hexdigest()
requests.post(
    url,
    json=payload,  # ← reserializes with default separators; signature mismatches
    headers={"X-AdCP-Signature": f"sha256={sig}", ...},
)
```

```python
# Equivalent — sender signs and sends the same bytes
sender = WebhookSender.from_adcp_legacy_hmac(secret, key_id="kid_buyer_42")
async with sender:
    await sender.send_raw(
        url=url,
        idempotency_key=payload["idempotency_key"],
        payload=payload,
    )
```

The "sign one byte sequence, POST a different one via `json=`" bug is one of the most common HMAC-webhook integration failures in the field. `WebhookSender` serializes once with `json.dumps(...)` and POSTs the same bytes via `httpx.AsyncClient.post(content=body)` — the bug class is impossible.

### Pattern: hand-built Standard-Webhooks (Svix-style) POST

```python
# Legacy
import base64, hmac, hashlib, json, time, requests, uuid
secret_bytes = base64.b64decode(secret_str.removeprefix("whsec_") + "==")
msg_id = f"msg_{uuid.uuid4().hex}"
ts = str(int(time.time()))
body = json.dumps(payload, separators=(",", ":")).encode()
mac = hmac.new(secret_bytes, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
requests.post(
    url,
    data=body,
    headers={
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": f"v1,{base64.b64encode(mac).decode()}",
        "Content-Type": "application/json",
    },
)
```

```python
# Equivalent
sender = WebhookSender.from_standard_webhooks_secret(secret_str, key_id="kid")
async with sender:
    await sender.send_raw(
        url=url,
        idempotency_key=payload["idempotency_key"],
        payload=payload,
    )
```

### Pattern: custom Docker localhost rewrite

```python
# Legacy — homemade rewrite for "deliver from Docker container to host webhook"
url = url.replace("localhost", "host.docker.internal")
url = url.replace("127.0.0.1", "host.docker.internal")
```

```python
# Equivalent — TransportHook composes with everything else
from adcp.webhook_sender import WebhookSender
from adcp.webhook_transport_hooks import DockerLocalhostRewrite

sender = WebhookSender.from_jwk(
    jwk,
    transport_hooks=(DockerLocalhostRewrite(),),
    allow_private_destinations=True,  # required — see note below
)
```

`DockerLocalhostRewrite` requires `allow_private_destinations=True` at sender construction — the rewrite produces a private-IP destination, and SSRF would reject the rewritten URL otherwise. The flag is the operator's explicit opt-in; it raises at construction time if forgotten so the misconfiguration doesn't bite at first delivery.

For Linux containers without `--add-host=host.docker.internal:host-gateway`, pass `DockerLocalhostRewrite(rewrite_to="172.17.0.1")` (Docker's default bridge gateway).

### Pattern: per-call retry loop with backoff

```python
# Legacy
import time
for attempt in range(5):
    try:
        response = requests.post(url, ...)
        if response.status_code < 500:
            break
    except requests.RequestException:
        pass
    time.sleep(2 ** attempt)
```

```python
# Equivalent — WebhookDeliverySupervisor owns retry + circuit breaker + dedup
from adcp.webhooks import (
    InMemoryWebhookDeliverySupervisor,
    WebhookDeliveryRequest,
)

supervisor = InMemoryWebhookDeliverySupervisor(
    sender=sender,
    # tune retry policy / circuit-breaker thresholds at construction
)
await supervisor.deliver(WebhookDeliveryRequest(url=..., payload=..., ...))
```

The in-memory supervisor handles retry policy and circuit-breaker state per buyer,
but it is process-local and cannot support the AdCP 3.2 retry-horizon capability.
The current `PgWebhookDeliverySupervisor` persists pending attempts but removes
final rows, so it also does not satisfy the beta.5 retention or atomic terminal
state/outbox contract. Use these APIs for explicit best-effort delivery only. A
webhook-emitting production seller must own publication in an external durable
outbox, set `auto_emit_task_webhooks=False`, and advertise the horizon with
`webhook_signing_managed_externally=True`.

### Pattern: per-call SSRF check

```python
# Legacy
host = urlparse(url).hostname
ip = socket.gethostbyname(host)
if ipaddress.ip_address(ip).is_private:
    raise ValueError("private IP not allowed")
requests.post(url, ...)  # ← TOCTOU: DNS may rebind between check and connect
```

```python
# Equivalent — automatic. WebhookSender pins the validated IP into the
# transport so DNS rebinding cannot swap the connect target between
# validation and POST.
sender = WebhookSender.from_jwk(jwk)  # SSRF runs on every send
```

The owned-client path rebuilds an `AsyncIpPinnedTransport` per request, runs the full SSRF range check (loopback / RFC 1918 / link-local / CGNAT / IPv6 ULA / multicast / cloud metadata), enforces an optional port allowlist, and pins the connection to the validated IP. There is no "remembered to call the SSRF helper" failure mode.

If your infrastructure uses a vetted egress proxy with mTLS to a fixed buyer set, pass your own `client=httpx.AsyncClient(...)` and the sender will trust the operator's transport instead.

### Pattern: ad-hoc retry that re-signs with the original timestamp

```python
# Legacy — replays the original signature, which receivers reject after
# the 300s skew window.
saved = (signed_headers, body_bytes)
time.sleep(60)
requests.post(url, data=saved[1], headers=saved[0])
```

```python
# Equivalent — WebhookSender.resend re-signs the same bytes with a
# fresh signature on every retry, preserving idempotency_key for dedup.
result = await sender.send_mcp(
    url=..., task_id=..., task_type=..., operation_id=..., status=...
)
if not result.ok:
    retry = await sender.resend(result)
```

`resend` works in every auth mode. JWK regenerates the 9421 Signature/Signature-Input headers; HMAC modes generate a new timestamp + signature over the same body; bearer mode is a no-op (the auth header is timestamp-independent).

---

## Failure modes the new API closes

* **Sign-vs-body divergence** — the sender owns marshaling; signed bytes equal sent bytes.
* **Retry stales out the signature** — `resend` re-signs.
* **Forgotten `idempotency_key`** — required kwarg on `send_raw`; receivers dedupe on it.
* **DNS rebinding between SSRF check and POST** — automatic IP-pin on the owned-client path.
* **Standard Webhooks secret encoding mistake** — typed split between `from_adcp_legacy_hmac(bytes)` and `from_standard_webhooks_secret(str)`.
* **Docker rewrite without operator opt-in** — `DockerLocalhostRewrite` raises at construction unless `allow_private_destinations=True`.
* **Hooks bypassing SSRF** — hooks run before SSRF, but SSRF validates the post-rewrite URL; the boundary holds.

If your current sender doesn't have one of these properties, this is what you're trading up to.
