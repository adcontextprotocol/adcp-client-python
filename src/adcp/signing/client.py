"""Buyer-side ergonomic preset for adapters that don't use ``ADCPClient``.

When you build on :class:`ADCPClient` / :class:`ADCPMultiAgentClient`,
passing ``signing=SigningConfig(...)`` is enough — the client wires the
RFC 9421 signing event hook for you. This module is the equivalent for
adapters integrating against a seller via raw ``httpx`` (custom
orchestrators, edge proxies, anything the higher-level client doesn't
fit).

Two pieces:

* :func:`install_signing_event_hook` — installs a request event hook on
  an existing :class:`httpx.AsyncClient` that signs outbound requests
  per the seller's advertised ``request_signing`` policy.
* :func:`signing_operation` — context manager that sets the AdCP
  operation name on the shared :data:`current_operation` ContextVar so
  the hook knows which capability list to consult on each request.

Usage::

    import httpx
    from adcp.signing import (
        SigningConfig,
        VerifierCapability,  # if you mirror the seller's advertisement locally
        install_signing_event_hook,
        signing_operation,
    )

    signing = SigningConfig(private_key=..., key_id="my-agent-2026")

    seller_capability = await fetch_seller_request_signing_capability()

    client = httpx.AsyncClient()
    install_signing_event_hook(
        client,
        signing=signing,
        seller_capability=seller_capability,
    )

    async with client:
        with signing_operation("create_media_buy"):
            resp = await client.post("https://seller.example.com/mcp", json=payload)

The ``ADCPClient(signing=...)`` path remains the right shape when you
are using the SDK's high-level client — its built-in hook does the same
work plus capability prefetching, and you should not double-install.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from adcp.signing.autosign import (
    SigningConfig,
    current_operation,
    operation_needs_signing,
)
from adcp.signing.signer import sign_request

if TYPE_CHECKING:
    import httpx

    from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
        RequestSigning,
    )

logger = logging.getLogger(__name__)


CapabilityProvider = Callable[[], "RequestSigning | None | Awaitable[RequestSigning | None]"]
"""Returns the seller's ``request_signing`` capability block.

May be sync (``-> RequestSigning | None``) or async
(``-> Awaitable[RequestSigning | None]``). The hook awaits an
awaitable result so callers can plug in a capability fetcher backed by
a network call.
"""


def install_signing_event_hook(
    client: httpx.AsyncClient,
    *,
    signing: SigningConfig,
    seller_capability: RequestSigning | None = None,
    capability_provider: CapabilityProvider | None = None,
) -> None:
    """Install an RFC 9421 request-signing event hook on ``client``.

    The hook reads the AdCP operation name from
    :data:`adcp.signing.current_operation` (set by
    :func:`signing_operation`), consults the seller's advertised
    ``request_signing`` policy, and attaches ``Signature`` /
    ``Signature-Input`` / ``Content-Digest`` headers when the operation
    is in ``required_for`` / ``warn_for`` / ``supported_for``.

    Pass exactly one of ``seller_capability`` or ``capability_provider``.

    Parameters
    ----------
    client:
        An :class:`httpx.AsyncClient`. The hook is appended to its
        existing ``event_hooks["request"]`` list.
    signing:
        Buyer credentials. Same shape used by ``ADCPClient(signing=...)``.
    seller_capability:
        The seller's ``request_signing`` block. Use this when the value
        is known up front (you've already called ``get_adcp_capabilities``
        once at boot and you're caching it).
    capability_provider:
        Callable returning the capability per request. Use when the
        capability needs lazy / re-resolved lookup. Sync and async are
        both supported. Returning ``None`` means "seller doesn't sign;
        skip every operation."

    Notes
    -----
    Operations not yet bracketed in :func:`signing_operation` (e.g.
    health-check probes, metrics scrapes that share the client) pass
    through unsigned — same carve-out as ``ADCPClient``.
    """
    if (seller_capability is None) == (capability_provider is None):
        raise ValueError(
            "install_signing_event_hook requires exactly one of "
            "`seller_capability` or `capability_provider`."
        )

    async def _hook(request: httpx.Request) -> None:
        operation = current_operation.get()
        # Unset ContextVar → out-of-band call (health check, manual
        # probe). Skip without consulting capability.
        # ``get_adcp_capabilities`` is the bootstrap carve-out: signing
        # it would require capabilities we don't have yet. Mirrors
        # ADCPClient._sign_outgoing_request.
        if operation is None or operation == "get_adcp_capabilities":
            return

        if seller_capability is not None:
            capability = seller_capability
        else:
            assert capability_provider is not None
            result = capability_provider()
            if hasattr(result, "__await__"):
                capability = await result  # type: ignore[misc]
            else:
                capability = result  # type: ignore[assignment]

        decision = operation_needs_signing(capability, operation)
        if decision == "skip":
            return

        covers_policy: str | None = None
        if capability is not None and capability.covers_content_digest is not None:
            covers_policy = capability.covers_content_digest.value
        if covers_policy == "forbidden":
            cover_digest = False
        elif covers_policy == "required":
            cover_digest = True
        else:
            # "either" / absent — signer's choice; pick the stricter
            # body-bound option so the seller's content-digest verify
            # never rejects on a "missing optional component" path.
            cover_digest = True

        signed = sign_request(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            body=request.content,
            private_key=signing.private_key,
            key_id=signing.key_id,
            alg=signing.alg,
            cover_content_digest=cover_digest,
            tag=signing.tag,
        )
        # pop-then-set so our values are authoritative even if an
        # earlier layer set the same header in a different case.
        for header_name, header_value in signed.as_dict().items():
            request.headers.pop(header_name, None)
            request.headers[header_name] = header_value

    hooks = client.event_hooks
    request_hooks: list[Any] = list(hooks.get("request") or [])
    request_hooks.append(_hook)
    hooks["request"] = request_hooks
    client.event_hooks = hooks


@contextmanager
def signing_operation(name: str) -> Iterator[None]:
    """Set :data:`adcp.signing.current_operation` for the duration of the block.

    ``install_signing_event_hook`` reads this ContextVar to decide
    whether an outbound request should be signed and what the signing
    policy is for that operation.

    ::

        with signing_operation("create_media_buy"):
            resp = await client.post(url, json=payload)

    ContextVar values copy into ``asyncio.create_task`` children, so a
    background task spawned inside this block inherits the operation
    name. Don't spawn unrelated network calls inside a signing scope —
    they'd be classified under whichever operation is on the
    ContextVar at the moment they fire.
    """
    token = current_operation.set(name)
    try:
        yield
    finally:
        current_operation.reset(token)


__all__ = [
    "CapabilityProvider",
    "install_signing_event_hook",
    "signing_operation",
]
