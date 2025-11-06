from __future__ import annotations

"""Operation ID generation utilities."""

from uuid import uuid4


def create_operation_id() -> str:
    """
    Generate a unique operation ID.

    Returns:
        A unique operation ID in the format 'op_{hex}'
    """
    return f"op_{uuid4().hex[:12]}"
