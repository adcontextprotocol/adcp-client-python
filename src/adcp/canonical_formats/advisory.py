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

from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import metadata as _pkg_metadata
from importlib.metadata import version as _pkg_version
from typing import Any, TypeAlias

from adcp.types import Error, Recovery, Source  # all three are public surface

# Maximum length we'll echo into advisory ``details`` from
# seller-controlled identifiers (``product_id``, ``capability_id``).
# Sellers control these strings and they round-trip into multi-hop
# responses + the idempotency replay cache, so cap to prevent
# log-injection / response-spoofing via newlines or absurd lengths.
_MAX_ECHOED_IDENTIFIER_LEN = 128

# ``Error`` is the wire model carried on ``errors[]``; ``SdkAdvisory`` is
# the same shape used for naming intent at API boundaries — alias rather
# than subclass to avoid pydantic schema-rebuild side effects.
SdkAdvisory: TypeAlias = Error


@lru_cache(maxsize=1)
def _resolve_sdk_id() -> str:
    """Return the wire-format ``sdk_id`` for this SDK build.

    Format per ``core/error.json``: ``<sdk_package_name>@<version>``.
    Reads the installed distribution's ``Name`` from the package metadata
    so the prefix never drifts from the PyPI distribution name — the
    audit-trail dedup key depends on it.

    Cached because the resolution is process-stable: the package
    metadata doesn't change at runtime. Lazy (computed on first call)
    so setuptools-scm-style late version resolution still works.
    Falls back to a development marker when the package isn't
    installed (e.g., running directly out of a checkout without
    ``pip install -e``).
    """
    try:
        dist_name = _pkg_metadata("adcp")["Name"]
        v = _pkg_version("adcp")
    except PackageNotFoundError:
        dist_name = "adcontextprotocol-adcp-python"
        v = "0.0.0-dev"
    return f"{dist_name}@{v}"


def _echo_identifier(value: str | None) -> str | None:
    """Cap seller-controlled identifiers before echoing into advisory details."""
    if value is None:
        return None
    if len(value) <= _MAX_ECHOED_IDENTIFIER_LEN:
        return value
    return value[:_MAX_ECHOED_IDENTIFIER_LEN] + "…[truncated]"


def __getattr__(name: str) -> Any:
    """Defer ``SDK_ID`` evaluation until first access.

    Lets the module import without invoking importlib.metadata, and
    keeps the documented module-level name backwards-compatible.
    """
    if name == "SDK_ID":
        return _resolve_sdk_id()
    raise AttributeError(name)


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
        sdk_id=_resolve_sdk_id(),
        suggestion=suggestion,
    )


# ``SDK_ID`` is resolved lazily via module ``__getattr__`` (above) — listed
# here so the public surface is documented + introspectable via ``dir()``.
__all__ = [  # noqa: F822 — SDK_ID provided via module __getattr__
    "SDK_ID",
    "SdkAdvisory",
    "_echo_identifier",
    "make_sdk_advisory",
]
