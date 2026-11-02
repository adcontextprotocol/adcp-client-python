"""Self-reported ``adcp.implementation.sdks[]`` for ``get_adcp_capabilities``.

AdCP 3.3 lets an agent report which AdCP SDKs it runs and which server-side
components each SDK supplies (``adcp.implementation``). :func:`adcp.server.serve`
(and :func:`adcp.decisioning.serve`, which delegates to it) fills the block in
automatically: one entry ``{"name": "adcp", "version": <installed adcp
distribution version>, "components": [...]}``, where ``components`` lists only
what this process can prove the SDK supplies across every task the agent
advertises. The spec is normative here: a component whose coverage varies
across task paths is omitted, and an empty ``components`` list is legal (SDK
use with no agent-wide claim).

Derivation rules (each component is claimed only when all of its applicable
responsibilities are SDK-owned):

``transport``
    Always, under :func:`adcp.server.serve`: the SDK's MCP / A2A stack frames,
    dispatches and serializes every task.
``types``
    The handler is a Decisioning :class:`~adcp.decisioning.handler.PlatformHandler`:
    the framework parses every request into the SDK's generated models and
    projects capabilities from them. A plain ``ADCPHandler`` may return raw
    dicts, so it is not claimed there.
``schema_validation``
    Request *and* response validation are both ``"strict"`` and the server
    does not serve a legacy-adapter wire version (``2.5``), whose translated
    responses are not re-validated. Warn-only or request-only validation never
    qualifies.
``authentication``
    ``serve(auth=BearerTokenAuth(...))`` on an HTTP transport, with no
    ``allow_unauthenticated`` and no unauthenticated tools beyond the spec's
    discovery set.
``idempotency``
    The served capabilities declare ``adcp.idempotency.supported: true``,
    deduplication is not declared external (``_adcp_idempotency_external``),
    and every advertised task whose request schema requires
    ``idempotency_key`` resolves to a method wrapped by
    :meth:`adcp.server.idempotency.IdempotencyStore.wrap`.
``webhooks``
    Decisioning only: task webhooks are delivered by the SDK's atomic task
    outbox (RFC 9421-signed, durable retries) and no advertised task accepts a
    webhook the SDK does not deliver (``reporting_webhook``,
    ``artifact_webhook``, ``revocation_webhook``, ``webhook_url``,
    ``sync_agent_notification_configs``).
``signing``
    Request-signature verification is wired in ``serve()``, the served
    capabilities declare ``request_signing.supported: true``, and outbound
    webhooks are either SDK-signed (``webhooks`` claimed) or not offered at all.
``lifecycle``
    Never derived: domain state-transition enforcement
    (``assert_media_buy_transition`` and friends) is called by adopter code, so
    the SDK cannot prove it agent-wide. Adopters who enforce it everywhere add
    it with ``ImplementationReport(extra_components=["lifecycle"])``.

The block is only added to responses served at AdCP 3.3 or later (the field
was introduced in 3.3; earlier releases treated ``adcp.implementation`` as an
open extension point). A capabilities response that already carries
``adcp.implementation`` -- declared on ``DecisioningCapabilities.adcp``, passed
to :func:`~adcp.server.responses.capabilities_response`, or returned by a
custom handler -- is never touched.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from pydantic import BaseModel

    from adcp.server.auth import BearerTokenAuth
    from adcp.server.base import ToolContext
    from adcp.server.helpers import ResponseEnhancer
    from adcp.server.signed_requests import RequestSignatureVerification
    from adcp.types.domains.protocol.get_adcp_capabilities_response import Component
    from adcp.validation.client_hooks import ValidationHookConfig

logger = logging.getLogger(__name__)

#: PyPI distribution name of this SDK, as reported in ``sdks[].name``.
SDK_PACKAGE_NAME: Final[str] = "adcp"

#: First AdCP release whose ``get_adcp_capabilities`` schema defines
#: ``adcp.implementation``.
IMPLEMENTATION_MIN_ADCP_VERSION: Final[str] = "3.3-beta.0"

_MAX_SDKS: Final[int] = 16
_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"\S{1,32}")
# Spec order of ``get-adcp-capabilities-response.json`` ``Component`` (pinned by
# a test against the generated enum so the import stays lazy).
_COMPONENT_ORDER: Final[tuple[str, ...]] = (
    "types",
    "schema_validation",
    "transport",
    "authentication",
    "signing",
    "lifecycle",
    "idempotency",
    "webhooks",
)

# Request fields carrying a webhook the SDK does not deliver itself.
_ADOPTER_WEBHOOK_FIELDS: Final[frozenset[str]] = frozenset(
    {"reporting_webhook", "artifact_webhook", "revocation_webhook", "webhook_url"}
)
# Tasks whose existence implies adopter-delivered notifications.
_ADOPTER_NOTIFICATION_TASKS: Final[frozenset[str]] = frozenset({"sync_agent_notification_configs"})


def sdk_package_version() -> str | None:
    """Return the installed ``adcp`` distribution version, or ``None``.

    ``None`` when the distribution metadata is unavailable or the version does
    not fit the schema (no whitespace, at most 32 characters) -- the SDK then
    reports nothing rather than an invalid or approximate version.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        resolved = version(SDK_PACKAGE_NAME)
    except PackageNotFoundError:
        return None
    if not _VERSION_PATTERN.fullmatch(resolved):
        return None
    return resolved


def _normalize_components(components: Iterable[Component | str]) -> list[str]:
    values = {str(getattr(item, "value", item)) for item in components}
    unknown = sorted(values.difference(_COMPONENT_ORDER))
    if unknown:
        raise ValueError(
            f"unknown adcp.implementation component(s) {unknown}; "
            f"choose from {list(_COMPONENT_ORDER)}"
        )
    return [value for value in _COMPONENT_ORDER if value in values]


def adcp_sdk_entry(components: Iterable[Component | str] = ()) -> dict[str, Any] | None:
    """Build this SDK's ``sdks[]`` entry for a hand-written ``adcp.implementation``.

    Use it when you declare ``adcp.implementation`` yourself (which turns the
    automatic report off for that response) but still want this SDK listed
    with its exact installed version::

        from adcp.server.implementation import adcp_sdk_entry

        implementation = {"sdks": [adcp_sdk_entry(["transport", "types"]), my_other_sdk]}

    Returns ``None`` when the installed version cannot be resolved.

    :raises ValueError: a component is not a spec ``Component`` value.
    """
    resolved = sdk_package_version()
    if resolved is None:
        return None
    return {
        "name": SDK_PACKAGE_NAME,
        "version": resolved,
        "components": _normalize_components(components),
    }


@dataclass(frozen=True)
class ImplementationReport:
    """Tune the automatic ``adcp.implementation`` report of :func:`adcp.server.serve`.

    Pass as ``serve(report_implementation=...)``. ``True`` (the default) is
    ``ImplementationReport()``; ``False`` omits the block entirely, which the
    spec reads as "SDK use unknown".

    :param extra_components: Components you assert for this SDK that the
        framework cannot detect -- typically ``"lifecycle"`` when your code
        enforces domain state transitions on every task with the SDK's
        ``assert_*_transition`` helpers. Claim a component only when it holds
        across every advertised task.
    :param extra_sdks: Further ``sdks[]`` entries (dicts or
        :class:`~adcp.types.domains.protocol.get_adcp_capabilities_response.Sdk`)
        appended after this SDK's entry, for deployments that mix SDKs. An
        entry named ``"adcp"`` is refused: the SDK reports itself.

    To replace the report entirely, declare ``adcp.implementation`` in your
    capabilities instead; the SDK never overwrites a declared block.
    """

    extra_components: Collection[Component | str] = ()
    extra_sdks: Sequence[Mapping[str, Any] | BaseModel] = field(default=())

    def __post_init__(self) -> None:
        if isinstance(self.extra_components, str):
            raise TypeError("extra_components must be a collection of component names")
        object.__setattr__(
            self, "extra_components", tuple(_normalize_components(self.extra_components))
        )
        from adcp.types.domains.protocol.get_adcp_capabilities_response import Sdk

        entries: list[dict[str, Any]] = []
        for entry in self.extra_sdks:
            raw = (
                entry.model_dump(mode="json", exclude_none=True)
                if hasattr(entry, "model_dump")
                else entry
            )
            dumped = Sdk.model_validate(raw).model_dump(mode="json", exclude_none=True)
            if dumped["name"] == SDK_PACKAGE_NAME:
                raise ValueError(
                    f"extra_sdks may not list {SDK_PACKAGE_NAME!r}; the SDK reports its own "
                    "entry. Use extra_components to add components to it."
                )
            entries.append(dumped)
        if len(entries) + 1 > _MAX_SDKS:
            raise ValueError(f"adcp.implementation.sdks allows at most {_MAX_SDKS} entries")
        object.__setattr__(self, "extra_sdks", tuple(entries))


def resolve_implementation_report(
    value: bool | ImplementationReport | None,
) -> ImplementationReport | None:
    """Normalize ``serve(report_implementation=...)``; ``None`` means off."""
    if value is None or value is False:
        return None
    if value is True:
        return ImplementationReport()
    if isinstance(value, ImplementationReport):
        return value
    raise TypeError(
        "report_implementation must be True, False or an ImplementationReport, "
        f"got {type(value).__name__}"
    )


def _serves_implementation(version: str | None) -> bool:
    if version is None:
        return False
    from adcp._version import (
        is_adcp_version_at_least,
        normalize_to_release_precision,
        resolve_adcp_version_alias,
    )

    try:
        release = resolve_adcp_version_alias(normalize_to_release_precision(version))
        return is_adcp_version_at_least(release, IMPLEMENTATION_MIN_ADCP_VERSION)
    except (TypeError, ValueError):
        return False


@lru_cache(maxsize=512)
def _schema_shape(tool_name: str, version: str | None) -> tuple[frozenset[str], frozenset[str]]:
    """Return (required, properties) of a task's request schema, arms merged."""
    from adcp.validation.schema_loader import get_schema

    try:
        schema = get_schema(tool_name, "request", version=version)
    except Exception:  # noqa: BLE001 - a missing bundle only weakens the claim
        schema = None
    required: set[str] = set()
    properties: set[str] = set()

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        required.update(node.get("required") or ())
        properties.update((node.get("properties") or {}).keys())
        for key in ("allOf", "oneOf", "anyOf"):
            for arm in node.get(key) or ():
                walk(arm)

    walk(schema)
    return frozenset(required), frozenset(properties)


def _platform_handler_type() -> type | None:
    try:
        from adcp.decisioning.handler import PlatformHandler
    except ImportError:  # pragma: no cover - decisioning ships with the SDK
        return None
    return PlatformHandler


@dataclass(frozen=True)
class _ToolFacts:
    mutating_covered: bool
    has_adopter_webhooks: bool
    has_any_webhooks: bool


class ImplementationReporter:
    """Response enhancer that stamps ``adcp.implementation`` on capabilities.

    Built by :func:`adcp.server.serve`; runs before the adopter's own
    ``response_enhancer`` (passed as ``then``) so that enhancer can still edit
    or remove the block. Only touches successful ``get_adcp_capabilities``
    responses served at AdCP 3.3 or later that do not already declare
    ``adcp.implementation``.
    """

    def __init__(
        self,
        handler: Any,
        *,
        report: ImplementationReport,
        transport: str,
        validation: ValidationHookConfig | None,
        supported_versions: Sequence[str] | None,
        auth: BearerTokenAuth | None,
        request_signature_verification: RequestSignatureVerification | None,
        advertise_all: bool,
        then: ResponseEnhancer | None = None,
    ) -> None:
        self._handler = handler
        self._report = report
        self._then = then
        self._advertise_all = advertise_all
        platform_handler = _platform_handler_type()
        self._is_platform = platform_handler is not None and isinstance(handler, platform_handler)
        self._static = self._static_components(
            transport=transport,
            validation=validation,
            supported_versions=supported_versions,
            auth=auth,
        )
        # ``serve`` drops the verifier on transports it cannot run on (stdio,
        # legacy SSE) before building the reporter.
        self._verifies_signatures = request_signature_verification is not None
        self._tool_facts: dict[str | None, _ToolFacts] = {}

    # ----- static (boot-time) facts -----

    def _static_components(
        self,
        *,
        transport: str,
        validation: ValidationHookConfig | None,
        supported_versions: Sequence[str] | None,
        auth: BearerTokenAuth | None,
    ) -> set[str]:
        components = {"transport"}
        if self._is_platform:
            components.add("types")
        if (
            validation is not None
            and validation.requests == "strict"
            and (validation.responses == "strict")
        ):
            from adcp.compat.legacy import LEGACY_ADAPTER_VERSIONS
            from adcp.validation.envelope import SUPPORTED_WIRE_VERSIONS

            served = SUPPORTED_WIRE_VERSIONS if supported_versions is None else supported_versions
            if not any(version in LEGACY_ADAPTER_VERSIONS for version in served):
                components.add("schema_validation")
        if auth is not None and transport != "stdio" and _auth_covers_every_task(auth):
            components.add("authentication")
        return components

    # ----- tool-surface facts (computed once per application version) -----

    def _facts(self, version: str | None) -> _ToolFacts:
        cached = self._tool_facts.get(version)
        if cached is not None:
            return cached
        from adcp.server.mcp_tools import get_tools_for_handler

        advertised = frozenset(
            tool["name"]
            for tool in get_tools_for_handler(
                self._handler, advertise_all=self._advertise_all, _include_schemas=False
            )
        )
        target = getattr(self._handler, "_platform", None) if self._is_platform else self._handler
        mutating_covered = target is not None and not getattr(
            target, "_adcp_idempotency_external", False
        )
        has_mutating = False
        has_adopter_webhooks = bool(advertised & _ADOPTER_NOTIFICATION_TASKS)
        has_any_webhooks = has_adopter_webhooks
        for name in advertised:
            required, properties = _schema_shape(name, version)
            if "idempotency_key" in required:
                has_mutating = True
                if mutating_covered and not _is_idempotency_wrapped(getattr(target, name, None)):
                    mutating_covered = False
            if properties & _ADOPTER_WEBHOOK_FIELDS:
                has_adopter_webhooks = True
                has_any_webhooks = True
            elif "push_notification_config" in properties:
                has_any_webhooks = True
        facts = _ToolFacts(
            mutating_covered=mutating_covered and has_mutating,
            has_adopter_webhooks=has_adopter_webhooks,
            has_any_webhooks=has_any_webhooks,
        )
        self._tool_facts[version] = facts
        return facts

    def _sdk_owns_task_webhooks(self) -> bool:
        if not self._is_platform:
            return False
        from adcp.decisioning.webhook_emit import (
            external_task_webhook_owner_ready,
            task_webhook_owner_ready,
        )

        handler = self._handler
        capabilities = handler._platform.capabilities
        kwargs: dict[str, Any] = {
            "capabilities": capabilities,
            "sender": handler._webhook_sender,
            "supervisor": handler._webhook_supervisor,
            "auto_emit_task_webhooks": handler._auto_emit_task_webhooks,
        }
        return task_webhook_owner_ready(
            **kwargs, registry=handler._registry
        ) and not external_task_webhook_owner_ready(**kwargs)

    # ----- per-response derivation -----

    def components(self, response: Mapping[str, Any], version: str | None) -> list[str]:
        """Return the components this SDK supplies for ``response``'s agent."""
        components = set(self._static)
        facts = self._facts(version)
        adcp = response.get("adcp")
        idempotency = adcp.get("idempotency") if isinstance(adcp, Mapping) else None
        if (
            isinstance(idempotency, Mapping)
            and idempotency.get("supported") is True
            and facts.mutating_covered
        ):
            components.add("idempotency")
        webhook_signing = response.get("webhook_signing")
        webhooks = (
            not facts.has_adopter_webhooks
            and isinstance(webhook_signing, Mapping)
            and webhook_signing.get("supported") is True
            and self._sdk_owns_task_webhooks()
        )
        if webhooks:
            components.add("webhooks")
        request_signing = response.get("request_signing")
        if (
            self._verifies_signatures
            and isinstance(request_signing, Mapping)
            and request_signing.get("supported") is True
            and (webhooks or not facts.has_any_webhooks)
        ):
            components.add("signing")
        components.update(self._report.extra_components)
        return _normalize_components(components)

    def __call__(
        self, method_name: str, result: dict[str, Any], context: ToolContext | None
    ) -> None:
        if method_name == "get_adcp_capabilities":
            try:
                self._stamp(result, context)
            except Exception:  # noqa: BLE001 - discovery must never fail on the report
                logger.warning(
                    "adcp.implementation self-report failed; serving capabilities without it",
                    exc_info=True,
                )
        if self._then is not None:
            from adcp.server.helpers import _apply_response_enhancer

            _apply_response_enhancer(self._then, method_name, result, context)

    def _stamp(self, result: dict[str, Any], context: ToolContext | None) -> None:
        if "adcp_error" in result or context is None:
            return
        adcp = result.get("adcp")
        if not isinstance(adcp, dict) or "implementation" in adcp:
            return
        application_version = context.application_adcp_version
        wire_version = context.resolved_adcp_version
        if not _serves_implementation(application_version):
            return
        if wire_version is not None and not _serves_implementation(wire_version):
            return
        entry = adcp_sdk_entry(self.components(result, application_version))
        if entry is None:
            return
        adcp["implementation"] = {"sdks": [entry, *self._report.extra_sdks]}


def _auth_covers_every_task(auth: BearerTokenAuth) -> bool:
    from adcp.server.mcp_tools import DISCOVERY_TOOLS

    if getattr(auth, "allow_unauthenticated", False):
        return False
    for widened in (
        getattr(auth, "mcp_discovery_tools", None),
        getattr(auth, "a2a_discovery_skills", None),
    ):
        if widened is not None and not set(widened) <= DISCOVERY_TOOLS:
            return False
    return True


def _is_idempotency_wrapped(method: Any) -> bool:
    if method is None or not callable(method):
        return False
    from adcp.server.idempotency import is_wrapped

    return is_wrapped(method)


__all__ = [
    "IMPLEMENTATION_MIN_ADCP_VERSION",
    "SDK_PACKAGE_NAME",
    "ImplementationReport",
    "ImplementationReporter",
    "adcp_sdk_entry",
    "resolve_implementation_report",
    "sdk_package_version",
]
