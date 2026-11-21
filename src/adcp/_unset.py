"""Sentinel default for arguments whose omission selects a deprecated call shape.

Unlike ``None``, the sentinel tells "not passed" apart from "passed as None", so
an explicit ``None`` keeps failing the way the current API does instead of
quietly selecting a deprecated path. It lives in its own module so that
``adcp.webhooks`` and ``adcp.webhook_sender`` share one object without an
import cycle.
"""

from __future__ import annotations

from typing import Any

UNSET: Any = object()
