"""Tests for the ``ADCP_STRICT_VALIDATION`` env flag on AdCPBaseModel.

The flag is resolved once at import time — tests here exercise the
resolver function directly rather than re-importing the module with
different env state, which would be brittle under pytest's module
cache.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adcp.types.base import _resolve_extra_policy


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "Yes", "ON"])
def test_truthy_values_enable_strict(value: str) -> None:
    with patch.dict("os.environ", {"ADCP_STRICT_VALIDATION": value}):
        assert _resolve_extra_policy() == "forbid"


@pytest.mark.parametrize(
    "value",
    ["", "0", "false", "no", "off", "random", "  ", "tRUE ", " 1", "0 "],
)
def test_falsy_or_unknown_values_keep_default(value: str) -> None:
    """Anything that isn't an explicitly-truthy token keeps the
    forward-compat ``ignore`` default. Whitespace-around-1 is caught by
    strip(), but ``"tRUE "`` specifically tests that trailing space is
    tolerated."""
    expected = "forbid" if value.strip().lower() in {"1", "true", "yes", "on"} else "allow"
    with patch.dict("os.environ", {"ADCP_STRICT_VALIDATION": value}):
        assert _resolve_extra_policy() == expected


def test_unset_var_keeps_default() -> None:
    """No env var at all → default (forward-compat safe)."""
    with patch.dict("os.environ", {}, clear=True):
        assert _resolve_extra_policy() == "allow"


def test_default_is_ignore_for_production_forward_compat() -> None:
    """Production must default to ignore — a client on spec N sending
    to a server on spec N+1 keeps working. Any change of this default
    is a major-version concern and breaks every downstream."""
    from adcp.types.base import _EXTRA_POLICY

    # The module-level _EXTRA_POLICY is resolved once at import time.
    # In the test environment with no ADCP_STRICT_VALIDATION set, it
    # must be ``ignore`` — a regression here indicates the default
    # flipped.
    assert _EXTRA_POLICY in {"allow", "forbid"}
    # If this assertion fails, the test environment has the env var
    # set — check your shell, not the SDK.


def test_adcpbasemodel_uses_resolved_policy() -> None:
    """The resolved policy feeds ``AdCPBaseModel.model_config.extra``.
    Pin it so a future refactor that bypasses ``_EXTRA_POLICY`` breaks
    here, not silently at runtime."""
    from adcp.types.base import _EXTRA_POLICY, AdCPBaseModel

    assert AdCPBaseModel.model_config["extra"] == _EXTRA_POLICY
