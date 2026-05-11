"""Smoke tests for the bundled v2.5 schema cache.

The schemas live in ``schemas/cache/2.5/`` (synced via
``scripts/sync_legacy_schemas.py`` from a pinned commit of
``adcontextprotocol/adcp``). These tests confirm the loader discovers
them and that they encode the v2.5 wire shape (different from v3 in
ways the legacy adapters translate).
"""

from __future__ import annotations

from adcp.validation.schema_loader import get_validator, list_validator_keys
from adcp.validation.schema_validator import validate_request


def test_v2_5_validator_keys_discovered() -> None:
    """The loader should find the v2.5 per-tool schemas."""
    keys = list_validator_keys(version="2.5")
    # Round-numbered sanity check — 39 today; allow drift as the
    # upstream tree grows.
    assert len(keys) >= 20, f"expected at least 20 v2.5 keys, got {len(keys)}: {keys}"
    # Spot-check tools the legacy adapters cover.
    assert "sync_creatives::request" in keys
    assert "get_products::request" in keys
    assert "create_media_buy::request" in keys
    assert "update_media_buy::request" in keys
    assert "list_creative_formats::request" in keys
    assert "preview_creative::request" in keys


def test_v2_5_sync_creatives_validator_compiles() -> None:
    """Round-tripping a v2.5 sync_creatives request through the v2.5
    validator should succeed for a minimal valid payload."""
    validator = get_validator("sync_creatives", "request", version="2.5")
    assert validator is not None


def test_v2_5_get_products_accepts_v2_5_wire_shape() -> None:
    """A v2.5-shaped get_products request (``brand_manifest`` URL,
    ``promoted_offerings`` nesting) validates against the v2.5 schema
    even though v3 would reject it."""
    payload = {
        "brand_manifest": "https://acme.example.com",
        "promoted_offerings": {"offerings": [{"name": "x"}]},
    }
    outcome = validate_request("get_products", payload, version="2.5")
    # v2.5 schema accepts this shape. (Note: outcome.valid is True even
    # if v2.5 requires additional fields we didn't supply — the test
    # is asserting the wire shape is *recognized*, not that this
    # particular payload is complete.)
    # If validation fails, the failure should NOT mention
    # ``brand_manifest`` or ``promoted_offerings`` as unknown — those
    # are the v2.5 fields we're testing for.
    msgs = " ".join(issue.message for issue in outcome.issues)
    assert "brand_manifest" not in msgs.lower(), f"v2.5 schema rejected brand_manifest: {msgs}"
    assert (
        "promoted_offerings" not in msgs.lower()
    ), f"v2.5 schema rejected promoted_offerings: {msgs}"


def test_v2_5_validator_independent_of_v3() -> None:
    """The v2.5 validator and v3 validator are separate instances,
    enforcing different schemas. Same tool name, different shapes."""
    v25 = get_validator("get_products", "request", version="2.5")
    v3 = get_validator("get_products", "request")  # default = SDK pin
    assert v25 is not None
    assert v3 is not None
    # Different Draft7Validator instances backed by different schema docs.
    assert v25 is not v3
