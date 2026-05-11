"""Tests for ``adcp.validation.version.resolve_bundle_key``."""

from __future__ import annotations

import pytest

from adcp.validation.version import resolve_bundle_key


def test_resolve_bundle_key_collapses_patch_to_minor() -> None:
    assert resolve_bundle_key("3.0.0") == "3.0"
    assert resolve_bundle_key("3.0.7") == "3.0"
    assert resolve_bundle_key("3.0.42") == "3.0"


def test_resolve_bundle_key_distinguishes_minors() -> None:
    assert resolve_bundle_key("3.0.0") == "3.0"
    assert resolve_bundle_key("3.1.0") == "3.1"
    assert resolve_bundle_key("4.0.0") == "4.0"


def test_resolve_bundle_key_keeps_prereleases_exact() -> None:
    assert resolve_bundle_key("3.1.0-beta.1") == "3.1.0-beta.1"
    assert resolve_bundle_key("3.1.0-rc.2") == "3.1.0-rc.2"
    assert resolve_bundle_key("4.0.0-alpha") == "4.0.0-alpha"


def test_resolve_bundle_key_strips_whitespace() -> None:
    assert resolve_bundle_key("  3.0.7  ") == "3.0"


def test_resolve_bundle_key_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="not a valid semver"):
        resolve_bundle_key("latest")
    with pytest.raises(ValueError, match="not a valid semver"):
        resolve_bundle_key("3.0")
    with pytest.raises(ValueError, match="not a valid semver"):
        resolve_bundle_key("v3.0.7")
    with pytest.raises(ValueError, match="not a valid semver"):
        resolve_bundle_key("")
