"""Tests for ``adcp.validation.envelope.detect_wire_version``."""

from __future__ import annotations

import pytest

from adcp.validation.envelope import UnsupportedVersionError, detect_wire_version

# A canned supported set keeps the test independent of COMPATIBLE_ADCP_VERSIONS
# drift over time. Pinning the set inside the test also documents what
# range each case is claiming against.
_SUPPORTED = ("3.0", "3.1")


def test_explicit_adcp_version_release_precision() -> None:
    assert detect_wire_version({"adcp_version": "3.0"}, supported=_SUPPORTED) == "3.0"
    assert detect_wire_version({"adcp_version": "3.1"}, supported=_SUPPORTED) == "3.1"


def test_explicit_adcp_version_patch_precision_normalized() -> None:
    """Patch-precision wire values collapse to release-precision."""
    assert detect_wire_version({"adcp_version": "3.0.7"}, supported=_SUPPORTED) == "3.0"
    assert detect_wire_version({"adcp_version": "3.1.0"}, supported=_SUPPORTED) == "3.1"


def test_explicit_adcp_version_wins_over_major_version() -> None:
    """If both fields are set, the precision wins (3.1+ contract)."""
    payload = {"adcp_version": "3.1.0", "adcp_major_version": 3}
    assert detect_wire_version(payload, supported=_SUPPORTED) == "3.1"


def test_adcp_major_version_picks_highest_supported_minor() -> None:
    """Pre-3.1 buyer sends only ``adcp_major_version`` — pick highest minor."""
    assert detect_wire_version({"adcp_major_version": 3}, supported=_SUPPORTED) == "3.1"


def test_adcp_major_version_unsupported_major_raises() -> None:
    with pytest.raises(UnsupportedVersionError) as exc_info:
        detect_wire_version({"adcp_major_version": 4}, supported=_SUPPORTED)
    assert exc_info.value.wire_value == 4
    assert exc_info.value.supported == _SUPPORTED


def test_adcp_version_unsupported_release_raises() -> None:
    with pytest.raises(UnsupportedVersionError) as exc_info:
        detect_wire_version({"adcp_version": "2.5"}, supported=_SUPPORTED)
    assert exc_info.value.wire_value == "2.5"


def test_adcp_version_malformed_raises() -> None:
    with pytest.raises(UnsupportedVersionError):
        detect_wire_version({"adcp_version": "not-a-version"}, supported=_SUPPORTED)


def test_neither_field_returns_none_fallback_to_sdk_pin() -> None:
    assert detect_wire_version({}, supported=_SUPPORTED) is None
    assert detect_wire_version({"other_field": "x"}, supported=_SUPPORTED) is None


def test_non_dict_payload_returns_none() -> None:
    """Non-dict payloads can't carry the envelope — caller skips."""
    assert detect_wire_version("not_a_dict", supported=_SUPPORTED) is None
    assert detect_wire_version(None, supported=_SUPPORTED) is None
    assert detect_wire_version([], supported=_SUPPORTED) is None


def test_adcp_version_empty_string_treated_as_missing() -> None:
    """An empty string falls through to ``adcp_major_version`` lookup."""
    assert (
        detect_wire_version({"adcp_version": "", "adcp_major_version": 3}, supported=_SUPPORTED)
        == "3.1"
    )


def test_adcp_major_version_bool_rejected() -> None:
    """``True``/``False`` are int subclasses; reject so they don't map to 1/0."""
    assert detect_wire_version({"adcp_major_version": True}, supported=_SUPPORTED) is None
    assert detect_wire_version({"adcp_major_version": False}, supported=_SUPPORTED) is None


def test_supports_prerelease_in_supported_set() -> None:
    """When ``supported`` includes a prerelease-keyed entry, exact match works."""
    payload = {"adcp_version": "3.1.0-beta.1"}
    # 3.1.0-beta.1 normalizes to 3.1-beta.1 — present in supported.
    assert detect_wire_version(payload, supported=("3.0", "3.1-beta.1")) == "3.1-beta.1"
