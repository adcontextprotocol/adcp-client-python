"""Tests for ``ADCPClient.server_version`` (Stage 7-lite).

Stage 7-lite ships the API surface for adopters to declare which
AdCP wire shape their seller speaks. Today the pin is plumbing-only —
the SDK records it and warns when adopters declare a legacy pin
(since outbound translation isn't wired yet). Stage 7-full will use
this signal to drive request rewriting.
"""

from __future__ import annotations

import warnings

import pytest

from adcp.client import _resolve_server_version


def test_resolve_server_version_none_passes_through() -> None:
    """``None`` (default) records no pin — most adopters land here."""
    assert _resolve_server_version(None) is None


def test_resolve_server_version_current_major_pin() -> None:
    """A current-major pin is accepted silently — adopters using the
    knob for telemetry attribution don't need a warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning would fail
        assert _resolve_server_version("3.0") == "3.0"
        assert _resolve_server_version("3.0.7") == "3.0"
        assert _resolve_server_version("3.1.0-beta.1") == "3.1.0-beta.1"


def test_resolve_server_version_legacy_emits_deprecation_warning() -> None:
    """Legacy pins are acknowledged but adopters need to know that
    outbound translation isn't yet wired."""
    with pytest.warns(DeprecationWarning, match="legacy AdCP wire shape"):
        result = _resolve_server_version("2.5")
    assert result == "2.5"


def test_resolve_server_version_legacy_warning_mentions_stage_7_full() -> None:
    """Warning message should point adopters at the upgrade path so
    they know what to wait for."""
    with pytest.warns(DeprecationWarning) as record:
        _resolve_server_version("2.5")
    assert any("Stage 7-full" in str(w.message) for w in record)


def test_resolve_server_version_rejects_garbage() -> None:
    """Same contract as ``resolve_bundle_key`` — adopters get a loud
    ValueError on typos rather than silent acceptance."""
    with pytest.raises(ValueError):
        _resolve_server_version("latest")
    with pytest.raises(ValueError):
        _resolve_server_version("v3.0")


def test_resolve_server_version_normalizes_patch_to_bundle_key() -> None:
    """Patch-precision pins collapse to bundle-key precision, matching
    the loader's expectation."""
    assert _resolve_server_version("3.0.0") == "3.0"
    assert _resolve_server_version("3.0.99") == "3.0"
