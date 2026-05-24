"""SDK-source ``errors[]`` advisory construction.

The canonical-formats projection emits non-fatal advisories on the
``errors[]`` array of ``get_products`` / ``list_creative_formats``
responses. Advisories carry ``source="sdk"`` (vs. seller-emitted
``producer`` entries) and ``sdk_id="adcontextprotocol-adcp-python@<version>"``
so multi-hop consumers can attribute the entry to this SDK and
deduplicate on ``(code, field)`` per the multi-hop propagation contract
in ``core/error.json``.

The advisory functions live separately from :mod:`adcp.canonical_formats.projection`
so other SDK paths (e.g., the v1→v2 reverse projection in a future PR,
the closed-set validator's own dispatch path) can emit the same shape
without circular imports.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any, TypeAlias

from adcp.types import Error
from adcp.types.generated_poc.core.error import Recovery, Source

# ``Error`` is the wire model carried on ``errors[]``; ``SdkAdvisory`` is
# the same shape used for naming intent at API boundaries — alias rather
# than subclass to avoid pydantic schema-rebuild side effects.
SdkAdvisory: TypeAlias = Error


def _resolve_sdk_id() -> str:
    """Return the wire-format ``sdk_id`` for this SDK build.

    Format per ``core/error.json``: ``<sdk_package_name>@<version>``.
    Falls back to a development marker when the package isn't installed
    (e.g., running directly out of a checkout without ``pip install -e``).
    """
    try:
        v = _pkg_version("adcp")
    except PackageNotFoundError:
        v = "0.0.0-dev"
    return f"adcontextprotocol-adcp-python@{v}"


SDK_ID: str = _resolve_sdk_id()


def make_sdk_advisory(
    *,
    code: str,
    message: str,
    field: str | None = None,
    details: dict[str, Any] | None = None,
    recovery: Recovery = Recovery.correctable,
    suggestion: str | None = None,
) -> Error:
    """Build an SDK-source advisory entry for ``errors[]`` augmentation.

    Sets ``source=sdk`` and ``sdk_id=<package>@<version>`` per the
    multi-hop propagation contract in ``core/error.json``. Consumers
    receiving this entry MUST treat it as advisory — the response stays
    success on the v1 path; only the v2 projection is degraded.

    Args:
        code: AdCP error code (e.g., ``FORMAT_DECLARATION_V1_AMBIGUOUS``).
            Must be ≤64 chars per the wire schema.
        message: Human-readable description.
        field: JSONPath-lite pointer to the offending field
            (e.g., ``products[0].format_options[2]``).
        details: Code-specific structured payload.
        recovery: Recovery classification — defaults to ``correctable``
            because canonical-projection advisories tell the seller what
            to fix (add ``v1_format_ref``, file a registry PR, etc.).
        suggestion: Optional one-line fix hint surfaced to operators.
    """
    return Error(
        code=code,
        message=message,
        field=field,
        details=details,
        recovery=recovery,
        source=Source.sdk,
        sdk_id=SDK_ID,
        suggestion=suggestion,
    )


__all__ = ["SDK_ID", "SdkAdvisory", "make_sdk_advisory"]
