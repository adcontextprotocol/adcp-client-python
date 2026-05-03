"""Tests for ``resolve_validation_modes`` env-var resolution.

JS parity: the TS SDK supports ``ADCP_VALIDATION_MODE=strict|warn|off`` to
override defaults for both sides at call time. ``ADCP_ENV=prod|production``
narrowly flips only the response default to ``warn``.

Resolution order (locked in here so a regression breaks loudly):

1. Explicit ``requests=`` / ``responses=`` on ``ValidationHookConfig``.
2. ``ADCP_VALIDATION_MODE`` env var (applies to both sides unless
   overridden).
3. ``ADCP_ENV=prod|production`` flip on the response side.
4. Hard defaults: ``requests="warn"``, ``responses="strict"``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adcp.validation.client_hooks import (
    ValidationHookConfig,
    resolve_validation_modes,
)


def test_defaults_when_no_env_or_config() -> None:
    """``warn``/``strict`` is the documented default surface."""
    with patch.dict("os.environ", {}, clear=True):
        req, resp = resolve_validation_modes()

    assert req == "warn"
    assert resp == "strict"


def test_adcp_env_production_flips_only_response() -> None:
    """Lock in the existing narrow behavior — request side stays at the
    type default. Tests must mirror the spec, not the implementation."""
    with patch.dict("os.environ", {"ADCP_ENV": "production"}, clear=True):
        req, resp = resolve_validation_modes()

    assert req == "warn"
    assert resp == "warn"


def test_adcp_env_prod_alias_flips_response() -> None:
    with patch.dict("os.environ", {"ADCP_ENV": "prod"}, clear=True):
        _, resp = resolve_validation_modes()

    assert resp == "warn"


@pytest.mark.parametrize("mode", ["strict", "warn", "off"])
def test_adcp_validation_mode_applies_to_both_sides(mode: str) -> None:
    """Single env var sets both sides — matches the TS port."""
    with patch.dict("os.environ", {"ADCP_VALIDATION_MODE": mode}, clear=True):
        req, resp = resolve_validation_modes()

    assert req == mode
    assert resp == mode


def test_adcp_validation_mode_uppercase_is_normalized() -> None:
    """Operators tend to TYPE_LIKE_THIS in shell exports; accept it."""
    with patch.dict("os.environ", {"ADCP_VALIDATION_MODE": "STRICT"}, clear=True):
        req, resp = resolve_validation_modes()

    assert req == "strict"
    assert resp == "strict"


def test_adcp_validation_mode_with_whitespace_is_normalized() -> None:
    with patch.dict("os.environ", {"ADCP_VALIDATION_MODE": "  warn "}, clear=True):
        req, resp = resolve_validation_modes()

    assert req == "warn"
    assert resp == "warn"


def test_invalid_validation_mode_falls_back_to_defaults() -> None:
    """Typos in deploy env (``WARNING`` for ``warn``) fall back to defaults
    rather than breaking the SDK on the next request."""
    with patch.dict("os.environ", {"ADCP_VALIDATION_MODE": "WARNING"}, clear=True):
        req, resp = resolve_validation_modes()

    assert req == "warn"
    assert resp == "strict"


def test_empty_validation_mode_is_treated_as_unset() -> None:
    with patch.dict("os.environ", {"ADCP_VALIDATION_MODE": ""}, clear=True):
        req, resp = resolve_validation_modes()

    assert req == "warn"
    assert resp == "strict"


def test_validation_mode_takes_precedence_over_adcp_env() -> None:
    """When both env vars are set, ``ADCP_VALIDATION_MODE`` wins. The
    TS port treats ``ADCP_VALIDATION_MODE`` as the explicit override
    and ``ADCP_ENV`` as the deploy-environment flip — explicit beats
    implicit."""
    env = {"ADCP_VALIDATION_MODE": "strict", "ADCP_ENV": "production"}
    with patch.dict("os.environ", env, clear=True):
        req, resp = resolve_validation_modes()

    assert req == "strict"
    assert resp == "strict"


def test_validation_mode_off_overrides_strict_default() -> None:
    """High-throughput callers can disable validation entirely via env."""
    with patch.dict("os.environ", {"ADCP_VALIDATION_MODE": "off"}, clear=True):
        req, resp = resolve_validation_modes()

    assert req == "off"
    assert resp == "off"


def test_explicit_config_overrides_validation_mode_env() -> None:
    """Explicit ``ValidationHookConfig`` is the highest precedence —
    storyboards and compliance runners that pass strict on both sides
    must not be silently downgraded by an env var."""
    config = ValidationHookConfig(requests="strict", responses="strict")
    with patch.dict("os.environ", {"ADCP_VALIDATION_MODE": "off"}, clear=True):
        req, resp = resolve_validation_modes(config)

    assert req == "strict"
    assert resp == "strict"


def test_explicit_config_per_side_falls_through_to_env() -> None:
    """Setting only one side explicitly leaves the other to the env-var
    chain. Proves the config fields are independent."""
    config = ValidationHookConfig(requests="off")
    with patch.dict("os.environ", {"ADCP_VALIDATION_MODE": "strict"}, clear=True):
        req, resp = resolve_validation_modes(config)

    assert req == "off"  # explicit wins
    assert resp == "strict"  # env-var chain


def test_resolution_is_evaluated_at_call_time_not_import_time() -> None:
    """Tests that mutate env vars must see the new value on the next
    call. Module-level caching of the resolved modes would break
    ``patch.dict`` and force a reset hook."""
    with patch.dict("os.environ", {"ADCP_VALIDATION_MODE": "strict"}, clear=True):
        first = resolve_validation_modes()

    with patch.dict("os.environ", {"ADCP_VALIDATION_MODE": "off"}, clear=True):
        second = resolve_validation_modes()

    assert first == ("strict", "strict")
    assert second == ("off", "off")
