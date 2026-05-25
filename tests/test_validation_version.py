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


def test_resolve_bundle_key_accepts_major_minor_pass_through() -> None:
    """Bare ``MAJOR.MINOR`` is already a bundle key — passed through.

    The wire envelope's ``adcp_version`` field (3.1+) is emitted at this
    precision, so the dispatcher can hand it straight to the loader.
    """
    assert resolve_bundle_key("3.0") == "3.0"
    assert resolve_bundle_key("3.1") == "3.1"
    assert resolve_bundle_key("2.5") == "2.5"


def test_resolve_bundle_key_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="not a valid version"):
        resolve_bundle_key("latest")
    with pytest.raises(ValueError, match="not a valid version"):
        resolve_bundle_key("v3.0.7")
    with pytest.raises(ValueError, match="not a valid version"):
        resolve_bundle_key("3")
    with pytest.raises(ValueError, match="not a valid version"):
        resolve_bundle_key("")


def test_resolve_bundle_key_accepts_wire_format_prerelease() -> None:
    """Wire ``adcp_version`` is release-precision-with-prerelease — patchless.

    Per ``core/version-envelope.json``, a v3.1 seller emits e.g.
    ``adcp_version: "3.1-beta.1"`` (NOT ``"3.1.0-beta.1"`` — the full
    semver form is meta-only). The bundle key resolver MUST recognize
    this shape and map it to the cache directory
    (``schemas/cache/3.1.0-beta.1/``) so validator routing finds the
    bundle. Without this normalization, every v3.1 response from a
    spec-conformant seller raises ``ValueError`` at validator selection.
    """
    assert resolve_bundle_key("3.1-beta.1") == "3.1.0-beta.1"
    assert resolve_bundle_key("3.1-rc.1") == "3.1.0-rc.1"
    # Bare prerelease tag without dotted suffix
    assert resolve_bundle_key("3.1-beta") == "3.1.0-beta"
    # Whitespace tolerated
    assert resolve_bundle_key("  3.1-beta.1  ") == "3.1.0-beta.1"


def test_resolve_bundle_key_wire_and_meta_forms_agree() -> None:
    """The wire form (``3.1-beta.1``) and the full-semver form
    (``3.1.0-beta.1``) MUST resolve to the same cache key. Otherwise a
    SDK that pinned via the bundle's ``published_version`` (meta) and a
    seller emitting the wire form would land on different validators
    for the same release.
    """
    assert resolve_bundle_key("3.1-beta.1") == resolve_bundle_key("3.1.0-beta.1")
    assert resolve_bundle_key("3.1-rc.2") == resolve_bundle_key("3.1.0-rc.2")
