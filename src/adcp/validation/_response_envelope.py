"""Response-envelope contracts shared by transport and schema validation.

Through AdCP 3.2, ``list_products`` forbids ``status`` outright and the
synchronous ``decline_proposals`` arm permits it only on its submitted arm, so
grafting ``completed`` onto either makes the result fail the pinned response
schema. From 3.3.0-beta.3 every task response and async status payload
composes ``core/protocol-envelope.json`` (adcontextprotocol/adcp#8547), whose
``status`` is required, so the exception ends. Keep the behavioral tests in
``test_compact_lifecycle_matrix.py`` aligned when adding another exception.
"""

from __future__ import annotations

_NATIVE_COMPACT_ENVELOPE_TOOLS: frozenset[str] = frozenset(
    {
        "list_products",
        "decline_proposals",
    }
)

#: The first release whose every task response composes the protocol envelope.
ENVELOPE_ON_EVERY_RESPONSE_FROM = "3.3.0-beta.3"


def uses_task_status_envelope(tool_name: str, version: str | None = None) -> bool:
    """Return whether a tool response uses the task ``status`` envelope at ``version``.

    ``version`` is the release the response is shaped for; ``None`` is the
    SDK's pin. An unparseable version keeps the compact exception.
    """
    if tool_name not in _NATIVE_COMPACT_ENVELOPE_TOOLS:
        return True
    from adcp._version import (
        _read_packaged_version,
        is_adcp_version_at_least,
        resolve_adcp_version_alias,
    )

    release = _read_packaged_version() if version is None else version
    try:
        return is_adcp_version_at_least(
            resolve_adcp_version_alias(release.strip()), ENVELOPE_ON_EVERY_RESPONSE_FROM
        )
    except ValueError:
        return False
