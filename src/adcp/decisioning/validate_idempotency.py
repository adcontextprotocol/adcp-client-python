"""Boot-time validator: declared idempotency capability vs. wired decorator.

Catches the silent-lie configuration: a platform that advertises
``capabilities.adcp.idempotency.supported=True`` on the AdCP
``get_adcp_capabilities`` response while never applying
:meth:`adcp.server.idempotency.IdempotencyStore.wrap` to any handler
method. Buyers reading the capabilities envelope assume retries are
deduped; without the decorator, every retry re-executes side effects.

The check is loose by design — "any wrapped method on the platform"
passes. A platform that wraps ``create_media_buy`` but forgets
``update_media_buy`` slips through here. Tightening to per-method
coverage requires the spec to expose a canonical mutating-tool enum,
which AdCP #2315 doesn't yet. The loose check still catches the
dominant failure mode (capability declared, decorator never applied).

**Escape hatch.** Adopters who terminate idempotency upstream of the
SDK — gateway-tier dedup (Kong, Envoy, ASGI middleware), or a
bring-your-own decorator that doesn't go through
:meth:`IdempotencyStore.wrap` — set ``_adcp_idempotency_external = True``
on the platform class to opt out of this check. They still publish
``IdempotencySupported`` to buyers; the validator just trusts that the
dedup is wired somewhere the SDK can't see.

Mirrors :func:`adcp.decisioning.property_list.validate_property_list_config`
and :func:`adcp.decisioning.webhook_emit.validate_webhook_sender_for_platform` —
boot-time fail-fast with a structured :class:`AdcpError`.
"""

from __future__ import annotations

from typing import Any

from adcp.server.idempotency import is_wrapped


def _idempotency_capability_supported(capabilities: Any) -> bool:
    """Return True if ``capabilities.adcp.idempotency.supported`` is True."""
    if capabilities is None:
        return False
    adcp = getattr(capabilities, "adcp", None)
    if adcp is None:
        return False
    idempotency = getattr(adcp, "idempotency", None)
    if idempotency is None:
        return False
    return getattr(idempotency, "supported", None) is True


def idempotency_capability_supported(platform: Any) -> bool:
    """Return True if ``platform.capabilities.adcp.idempotency.supported`` is True.

    Walks the three-level attribute chain defensively — adopters may set
    capabilities to ``None`` or omit nested fields entirely.
    """
    return _idempotency_capability_supported(getattr(platform, "capabilities", None))


def _candidate_method_names(platform: Any) -> list[str]:
    """Return public method names on the platform.

    Uses ``dir()`` + per-name ``getattr`` with try/except so a
    boot-side-effect property (DB lookup, config fetch) on the platform
    class doesn't blow up the validator. ``inspect.getmembers`` would
    fire every descriptor.
    """
    out: list[str] = []
    for name in dir(platform):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(platform, name)
        except Exception:  # noqa: BLE001 — descriptor side effects are out of our control
            continue
        if callable(attr):
            out.append(name)
    return out


def _platform_has_wrapped_method(platform: Any) -> bool:
    """True if any callable on the platform is registered as wrapped.

    Walks instance attributes so adopters who bind a wrapped function
    in ``__init__`` (``self.create_media_buy = wrapped_fn``) are
    recognized too — not just class-level ``@`` decoration.
    """
    for name in dir(platform):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(platform, name)
        except Exception:  # noqa: BLE001
            continue
        if not callable(attr):
            continue
        if is_wrapped(attr):
            return True
    return False


def validate_idempotency_wiring_for_capabilities(platform: Any, capabilities: Any) -> None:
    """Fail fast: these capabilities advertise idempotency without wiring.

    Honors the ``_adcp_idempotency_external = True`` opt-out for
    adopters with upstream gateway dedup or a BYO decorator the SDK
    can't introspect.

    :raises AdcpError: ``recovery='terminal'`` when the platform
        declares ``IdempotencySupported(supported=True)`` but no method
        on the platform is decorated with
        :meth:`adcp.server.idempotency.IdempotencyStore.wrap` AND the
        external opt-out is not set.
    """
    if not _idempotency_capability_supported(capabilities):
        return
    if getattr(platform, "_adcp_idempotency_external", False):
        return
    if _platform_has_wrapped_method(platform):
        return

    from adcp.decisioning.types import AdcpError

    raise AdcpError(
        "INVALID_REQUEST",
        message=(
            "capabilities.adcp.idempotency.supported=True is declared but "
            "no method on the platform is decorated with @IdempotencyStore.wrap. "
            "Buyers reading the capabilities envelope expect retries to be "
            "deduped; without the decorator every retry re-executes side "
            "effects. Wrap your mutating handlers — typically "
            "create_media_buy, update_media_buy, sync_creatives, "
            "activate_signal — with the decorator:\n\n"
            "    from adcp.server.idempotency import IdempotencyStore, MemoryBackend\n"
            "    idempotency = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)\n\n"
            "    class MySeller(DecisioningPlatform):\n"
            "        @idempotency.wrap\n"
            "        async def create_media_buy(self, params, context=None):\n"
            "            ...\n\n"
            "Alternatively: set IdempotencySupported(supported=False) to opt "
            "out, OR — if dedup is wired upstream of the SDK (gateway tier, "
            "BYO middleware) — set _adcp_idempotency_external = True on the "
            "platform class."
        ),
        recovery="terminal",
        details={
            "missing": "@IdempotencyStore.wrap",
            "decorator_import": "from adcp.server.idempotency import IdempotencyStore",
            "candidate_methods": _candidate_method_names(platform),
            "opt_out": "IdempotencySupported(supported=False)",
            "external_opt_out": "_adcp_idempotency_external = True",
        },
    )


def validate_idempotency_wiring(platform: Any) -> None:
    """Boot-time fail-fast: idempotency advertised but no method wrapped."""
    validate_idempotency_wiring_for_capabilities(
        platform,
        getattr(platform, "capabilities", None),
    )


__all__ = [
    "idempotency_capability_supported",
    "validate_idempotency_wiring",
    "validate_idempotency_wiring_for_capabilities",
]
