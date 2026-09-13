"""Response-envelope contracts shared by transport and schema validation.

The compact tools named here exist only in the AdCP 3.2 schema family, so the
predicate does not need a separate version argument. Keep the behavioral tests
in ``test_compact_lifecycle_matrix.py`` aligned when adding another exception.
"""

# These compact synchronous responses contradict the general protocol-envelope
# default: list_products forbids status outright, while decline_proposals only
# permits status on its submitted arm. Grafting ``completed`` onto either makes
# the result fail the pinned response schema.
_NATIVE_COMPACT_ENVELOPE_TOOLS: frozenset[str] = frozenset(
    {
        "list_products",
        "decline_proposals",
    }
)


def uses_task_status_envelope(tool_name: str) -> bool:
    """Return whether a tool response uses the task ``status`` envelope."""
    return tool_name not in _NATIVE_COMPACT_ENVELOPE_TOOLS
