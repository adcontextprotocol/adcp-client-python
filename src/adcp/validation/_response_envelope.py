"""Response-envelope contracts shared by transport and schema validation.

``status`` is REQUIRED on every task response whose release schema composes
``core/protocol-envelope.json``. AdCP 3.3 composes it into every response root.
The closed 3.2.x compact schemas for ``list_products`` and ``decline_proposals``
predate that composition: ``list_products`` forbids ``status`` outright and
``decline_proposals`` permits it only on its submitted arm. The served release's
schema decides, never its version string, so a 3.2.x release that later composes
the envelope needs no code change. Keep the behavioral tests in
``test_compact_lifecycle_matrix.py`` aligned when adding another exception.
"""

from __future__ import annotations

# Compact synchronous responses that a pre-composition release schema leaves
# without a ``status``. Grafting ``completed`` onto them there makes the result
# fail that release's response schema.
_NATIVE_COMPACT_ENVELOPE_TOOLS: frozenset[str] = frozenset(
    {
        "list_products",
        "decline_proposals",
    }
)


def uses_task_status_envelope(tool_name: str, version: str | None = None) -> bool:
    """Return whether a tool response carries the task ``status`` envelope.

    ``version`` selects the release whose schema answers (default: the SDK pin).
    Only the compact tools in ``_NATIVE_COMPACT_ENVELOPE_TOOLS`` ever answer
    ``False``, and only for a release whose schema for them does not compose
    ``core/protocol-envelope.json``. When that release ships no schema for the
    tool, the pre-composition behavior holds.
    """
    if tool_name not in _NATIVE_COMPACT_ENVELOPE_TOOLS:
        return True
    from adcp.validation.schema_loader import response_composes_protocol_envelope

    try:
        composed = response_composes_protocol_envelope(tool_name, version=version)
    except ValueError:
        # An unparseable version has no release schema to consult.
        return False
    return bool(composed)
