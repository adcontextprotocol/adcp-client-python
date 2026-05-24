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


# Canonical distribution name for the wire ``sdk_id``. Hardcoded (rather
# than read from ``pyproject.toml``'s ``[project].name``) so installed
# and dev builds emit the same audit-trail attribution. The
# ``core/error.json`` dedup contract keys on ``(code, field, sdk_id)``;
# drift here corrupts multi-hop deduplication. Installed wheels publish
# the PyPI distribution as ``adcp``; the fully-qualified
# ``adcontextprotocol-adcp-python`` form is what the spec example uses
# and what cross-SDK consumers expect.
_SDK_DIST_NAME: str = "adcontextprotocol-adcp-python"


@lru_cache(maxsize=1)
def _resolve_sdk_id() -> str:
    """Return the wire-format ``sdk_id`` for this SDK build.

    Format per ``core/error.json``: ``<sdk_package_name>@<version>``.
    The package-name prefix is fixed (``_SDK_DIST_NAME``) so installed
    and dev builds emit the same attribution; only the version
    component varies between them. Without this, dev installs would
    emit a different ``sdk_id`` from wheel installs and break the
    multi-hop dedup contract for the same SDK.

    Cached because the resolution is process-stable: the package
    metadata doesn't change at runtime. Lazy (computed on first call)
    so setuptools-scm-style late version resolution still works.
    Falls back to a ``0.0.0-dev`` version marker when the package
    isn't installed.
    """
    try:
        v = _pkg_version("adcp")
    except PackageNotFoundError:
        v = "0.0.0-dev"
    return f"{_SDK_DIST_NAME}@{v}"


def _echo_identifier(value: str | None) -> str | None:
    """Cap + scrub seller-controlled identifiers before echoing into advisory details.

    Two defenses applied in order:

    1. **Control-character scrub** — replaces every C0 control char
       (``\\x00``-``\\x1f``), the C1 range (``\\x7f``-``\\x9f``), and
       all Unicode line separators with a literal ``"\\u<hex>"``
       escape. A seller publishing a ``product_id`` containing ``\\n``
       or ``\\x1b[`` would otherwise round-trip into
       ``errors[].details.product_id``, forging log lines or
       triggering ANSI escape sequences in operator tooling that
       prints one advisory per line.

    2. **Length cap** — at 128 chars (after escaping), so a malformed
       seller identifier cannot grow the multi-hop ``errors[]`` array
       unbounded into the idempotency replay cache.

    Returns ``None`` for ``None`` input (the explicit absent-product case).
    """
    if value is None:
        return None
    scrubbed_chars: list[str] = []
    for ch in value:
        cp = ord(ch)
        # C0 (incl. \t, \n, \r) + DEL + C1 + LS/PS line separators.
        if cp < 0x20 or 0x7F <= cp <= 0x9F or ch in (" ", " "):
            scrubbed_chars.append(f"\\u{cp:04x}")
        else:
            scrubbed_chars.append(ch)
    scrubbed = "".join(scrubbed_chars)
    if len(scrubbed) <= _MAX_ECHOED_IDENTIFIER_LEN:
        return scrubbed
    return scrubbed[:_MAX_ECHOED_IDENTIFIER_LEN] + "…[truncated]"


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
        sdk_id=_resolve_sdk_id(),
        suggestion=suggestion,
    )


# ``_echo_identifier`` and ``_resolve_sdk_id`` are private helpers; not
# part of ``__all__``.
__all__ = [
    "SDK_ID",
    "SdkAdvisory",
    "make_sdk_advisory",
]
