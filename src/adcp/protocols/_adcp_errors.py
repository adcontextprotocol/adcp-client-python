"""Shared ``adcp_error`` envelope validation for protocol adapters.

The shape and size caps are spec-defined (transport-errors.mdx §The
``adcp_error`` Object): ``code`` is a non-empty string ≤ 64 chars,
total serialized envelope ≤ 4 KB. Both MCP and A2A transports project
their wire-specific error envelopes onto the same shape, so the
validation lives here rather than in either transport.
"""

from __future__ import annotations

import json
from typing import Any

MAX_ERROR_CODE_LEN = 64
MAX_ERROR_SIZE_BYTES = 4096


def validate_adcp_error(err: Any) -> dict[str, Any] | None:
    """Return ``err`` if it's a spec-shaped ``adcp_error`` envelope, else ``None``.

    Spec rules: ``code`` is a non-empty string ≤ 64 chars; the whole
    serialized envelope is ≤ 4 KB. Non-dict input, missing code, oversize
    payloads, and non-serializable values all fail closed (returns ``None``).
    """
    if not isinstance(err, dict):
        return None
    code = err.get("code")
    if not isinstance(code, str) or not (0 < len(code) <= MAX_ERROR_CODE_LEN):
        return None
    try:
        if len(json.dumps(err)) > MAX_ERROR_SIZE_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    return err
