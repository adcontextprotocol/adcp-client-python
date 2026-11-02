"""Response-envelope contracts shared by transport and schema validation.

Through AdCP 3.2, ``list_products`` forbids ``status`` outright and the
synchronous ``decline_proposals`` arm permits it only on its submitted arm, so
grafting ``completed`` onto either makes the result fail the pinned response
schema. The 3.3 prereleases before beta.3 add ``list_proposals`` and
``list_collections`` with closed roots that forbid it too. From 3.3.0-beta.3
every task response and async status payload composes
``core/protocol-envelope.json`` (adcontextprotocol/adcp#8547), whose
``status`` is required, so the exception ends.

The served release's own response schema decides, not its version string, so a
3.2.x release that later composes the envelope needs no code change (as in the
TypeScript SDK, adcontextprotocol/adcp-client#3493). The version comparison is
only the answer for a release that ships no schema for the tool. Keep the
behavioral tests in ``test_compact_lifecycle_matrix.py`` and
``test_response_status_gating.py`` aligned when adding another exception.
"""

from __future__ import annotations

_NATIVE_COMPACT_ENVELOPE_TOOLS: frozenset[str] = frozenset(
    {
        "list_products",
        "decline_proposals",
        "list_proposals",
        "list_collections",
    }
)

#: The first release whose every task response composes the protocol envelope.
ENVELOPE_ON_EVERY_RESPONSE_FROM = "3.3.0-beta.3"


def uses_task_status_envelope(tool_name: str, version: str | None = None) -> bool:
    """Return whether a tool response uses the task ``status`` envelope at ``version``.

    ``version`` is the release the response is shaped for; ``None`` is the
    SDK's pin. Only the compact tools in ``_NATIVE_COMPACT_ENVELOPE_TOOLS`` ever
    answer ``False``, and only for a release whose response schema for them does
    not compose ``core/protocol-envelope.json``. A release with no schema for the
    tool, or an unparseable version, keeps the compact exception unless the
    version is at or past :data:`ENVELOPE_ON_EVERY_RESPONSE_FROM`.
    """
    if tool_name not in _NATIVE_COMPACT_ENVELOPE_TOOLS:
        return True
    from adcp._version import (
        _read_packaged_version,
        is_adcp_version_at_least,
        resolve_adcp_version_alias,
    )
    from adcp.validation.schema_loader import response_composes_protocol_envelope

    try:
        composed = response_composes_protocol_envelope(tool_name, version=version)
    except ValueError:
        composed = None
    if composed is not None:
        return composed
    release = _read_packaged_version() if version is None else version
    try:
        return is_adcp_version_at_least(
            resolve_adcp_version_alias(release.strip()), ENVELOPE_ON_EVERY_RESPONSE_FROM
        )
    except ValueError:
        return False
