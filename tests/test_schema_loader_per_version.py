"""Stage 2 tests: per-version validator loader.

Exercises the ``version=`` kwarg on :func:`get_validator`,
:func:`validate_request`, and :func:`validate_response`. Builds a
synthetic legacy bundle in ``tmp_path`` and monkeypatches the loader's
resolver to find it — keeps the test isolated from the repo's working
tree (which CI sometimes runs against an installed wheel where the
packaged path wins) and from concurrent fixture cleanup.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from adcp.validation import schema_loader as _loader_mod
from adcp.validation.schema_loader import (
    _reset_for_tests,
    _resolve_schema_root,
    _SchemaRoot,
    _sdk_pinned_bundle_key,
    get_validator,
    list_validator_keys,
)
from adcp.validation.schema_validator import validate_request, validate_response


@pytest.fixture
def synthetic_legacy_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    """Yield a ``(bundle_key, schemas_root)`` pair for a synthetic legacy
    bundle laid out under ``tmp_path``.

    The loader's resolver is monkeypatched to return our synthetic root
    when asked for the legacy bundle key. The SDK-pinned key falls back
    to the real resolver. This isolates the test from the repo's working
    tree and from the installed-wheel discovery path (which would
    otherwise win).
    """
    legacy_key = "2.5"
    legacy_root = tmp_path / "cache" / legacy_key
    bundled = legacy_root / "bundled"
    core = legacy_root / "core"
    bundled.mkdir(parents=True)
    core.mkdir()

    request_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Synthetic Tool Request",
        "type": "object",
        "required": ["legacy_field"],
        "properties": {
            "legacy_field": {"type": "string"},
            "extra_field": {"type": "integer"},
        },
        "additionalProperties": False,
    }
    response_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Synthetic Tool Response",
        "type": "object",
        "required": ["result"],
        "properties": {"result": {"type": "string"}},
    }

    (bundled / "synthetic-tool-request.json").write_text(
        json.dumps(request_schema), encoding="utf-8"
    )
    (bundled / "synthetic-tool-response.json").write_text(
        json.dumps(response_schema), encoding="utf-8"
    )

    real_resolve = _resolve_schema_root

    def _fake_resolve(bundle_key: str | None = None) -> _SchemaRoot | None:
        if bundle_key == legacy_key:
            return _SchemaRoot(legacy_root)
        return real_resolve(bundle_key)

    monkeypatch.setattr(_loader_mod, "_resolve_schema_root", _fake_resolve)

    _reset_for_tests()
    try:
        yield legacy_key, legacy_root
    finally:
        _reset_for_tests()
        # ``tmp_path`` is cleaned up by pytest, but a non-empty leftover
        # from a partial test run shouldn't break the next session.
        shutil.rmtree(legacy_root, ignore_errors=True)


def test_resolve_schema_root_returns_different_paths_per_bundle_key(
    synthetic_legacy_bundle: tuple[str, Path],
) -> None:
    legacy_key, legacy_path = synthetic_legacy_bundle
    sdk_key = _sdk_pinned_bundle_key()
    assert sdk_key != legacy_key, (
        "Test fixture assumes the SDK pin isn't 2.5 — pick a different "
        "synthetic key if the SDK ever pins to 2.5.x"
    )

    # Go through the module attribute so the fixture's monkeypatch fires.
    legacy_root = _loader_mod._resolve_schema_root(legacy_key)
    sdk_root = _loader_mod._resolve_schema_root(sdk_key)

    assert legacy_root is not None
    assert sdk_root is not None
    assert legacy_root.root == legacy_path
    assert legacy_root.root != sdk_root.root


def test_get_validator_per_version_finds_only_its_own_tools(
    synthetic_legacy_bundle: tuple[str, Path],
) -> None:
    legacy_key, _ = synthetic_legacy_bundle

    # Legacy bundle has only synthetic_tool.
    legacy_keys = list_validator_keys(version=legacy_key)
    assert "synthetic_tool::request" in legacy_keys
    assert "synthetic_tool::sync" in legacy_keys

    # SDK pin doesn't have synthetic_tool.
    sdk_keys = list_validator_keys()  # None → SDK pin
    assert "synthetic_tool::request" not in sdk_keys


def test_get_validator_returns_independent_validators_per_version(
    synthetic_legacy_bundle: tuple[str, Path],
) -> None:
    legacy_key, _ = synthetic_legacy_bundle

    # Legacy validator: synthetic_tool exists.
    legacy_validator = get_validator("synthetic_tool", "request", version=legacy_key)
    assert legacy_validator is not None

    # SDK pin: synthetic_tool does NOT exist; same call returns None.
    sdk_validator = get_validator("synthetic_tool", "request")
    assert sdk_validator is None


def test_get_validator_same_tool_different_versions_compiles_separately(
    synthetic_legacy_bundle: tuple[str, Path],
) -> None:
    """Pick a tool that exists in the SDK pin; assert a legacy-bundle call
    that *doesn't* ship that tool returns None — proving the loader keys
    on bundle, not just tool name.
    """
    legacy_key, _ = synthetic_legacy_bundle

    # ``get_products`` exists in the SDK pin (3.0+).
    sdk_validator = get_validator("get_products", "request")
    assert sdk_validator is not None

    # The synthetic legacy bundle doesn't ship get_products → None.
    legacy_validator = get_validator("get_products", "request", version=legacy_key)
    assert legacy_validator is None


def test_validate_request_threads_version_through(
    synthetic_legacy_bundle: tuple[str, Path],
) -> None:
    legacy_key, _ = synthetic_legacy_bundle

    # Legacy schema requires ``legacy_field`` and forbids unknown keys.
    valid = validate_request("synthetic_tool", {"legacy_field": "hello"}, version=legacy_key)
    assert valid.valid

    missing_required = validate_request("synthetic_tool", {"extra_field": 7}, version=legacy_key)
    assert not missing_required.valid
    assert any("legacy_field" in (issue.message or "") for issue in missing_required.issues)

    extra_field_rejected = validate_request(
        "synthetic_tool",
        {"legacy_field": "x", "not_in_schema": 1},
        version=legacy_key,
    )
    assert not extra_field_rejected.valid


def test_validate_request_unknown_version_skips_safely(
    synthetic_legacy_bundle: tuple[str, Path],
) -> None:
    """A version we don't have on disk degrades to ``skipped`` rather
    than crashing — same contract as a missing schema for the SDK pin."""
    outcome = validate_request("synthetic_tool", {"x": 1}, version="9.9.9")
    # ``skipped`` semantics: ``valid=True`` with no issues.
    assert outcome.valid
    assert outcome.issues == []


def test_validate_response_threads_version_through(
    synthetic_legacy_bundle: tuple[str, Path],
) -> None:
    legacy_key, _ = synthetic_legacy_bundle

    valid = validate_response("synthetic_tool", {"result": "ok"}, version=legacy_key)
    assert valid.valid

    bad = validate_response("synthetic_tool", {"result": 42}, version=legacy_key)
    assert not bad.valid


def test_default_version_unchanged_when_arg_omitted(
    synthetic_legacy_bundle: tuple[str, Path],
) -> None:
    """``version=None`` (or omitted) keeps the SDK-pin behaviour exactly.
    Regression guard: Stage 2 must not change validator selection for
    existing call sites that don't pass ``version=``."""
    # Pick a tool that ships in the SDK pin.
    explicit_default = get_validator("get_products", "request", version=None)
    omitted = get_validator("get_products", "request")
    assert explicit_default is omitted
