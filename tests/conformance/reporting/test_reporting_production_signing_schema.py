"""3.2 normative declarations, distinct from intentionally optional JSON fields."""

import hashlib
import json
from copy import deepcopy

import pytest

from adcp.server.responses import capabilities_response
from adcp.validation import schema_loader

from ._production_support import production_harness
from ._production_transport import MountedProduction

PIN = "3.2.0-rc.3"
HORIZON = "/properties/webhook_signing/properties/delivery_retry_horizon_seconds"
IDENTITY = "/properties/identity/description"
CACHED = (
    (
        "protocol/get-adcp-capabilities-response.json",
        213991,
        "b8bb9cbd19491f352a277b0a5e7a39d88ab1290481e0f48cc025e61be6e52be1",
    ),
    (
        "bundled/protocol/get-adcp-capabilities-response.json",
        802990,
        "bb852633ddf0873d935ab296b284b8cdf84f1857126b6ee01d4af1338750e1c8",
    ),
)


@pytest.mark.parametrize("relative,size,digest", CACHED)
def test_exact_cached_schema_accepts_omission_despite_normative_32_requirement(
    relative, size, digest
):
    root = schema_loader._resolve_schema_root(PIN)
    assert root is not None
    path = root.root / relative
    original = path.read_bytes()
    assert len(original) == size and hashlib.sha256(original).hexdigest() == digest
    schema = json.loads(original)
    props = schema["properties"]
    horizon = props["webhook_signing"]["properties"]["delivery_retry_horizon_seconds"]
    identity = props["identity"]["description"]
    assert "A webhook-emitting AdCP 3.2 agent MUST populate" in horizon["description"]
    assert "Retries do not extend the horizon" in horizon["description"]
    assert "brand_json_url` MUST be present" in identity
    validator = schema_loader.get_named_validator(relative, version=PIN)
    assert validator is not None and validator.schema == schema
    omitted = capabilities_response(["media_buy"], sandbox=False, idempotency={"supported": False})
    omitted["webhook_signing"] = {
        "supported": True,
        "profile": "adcp/webhook-signing/v1",
        "algorithms": ["ed25519"],
        "legacy_hmac_fallback": False,
    }
    # This is an executed ACCEPTANCE, never the #1179 cached rejection.
    validator.validate(omitted)
    complete = deepcopy(omitted)
    complete["webhook_signing"]["delivery_retry_horizon_seconds"] = 86400
    complete["identity"] = {"brand_json_url": "https://seller.example.test/brand.json"}
    validator.validate(complete)
    for field, value in (
        ("delivery_retry_horizon_seconds", 86399),
        ("delivery_retry_horizon_seconds", 604801),
        ("delivery_retry_horizon_seconds", "86400"),
        ("algorithms", ["hs256"]),
        ("profile", "invented-signing-profile"),
    ):
        invalid = deepcopy(complete)
        invalid["webhook_signing"][field] = value
        assert not validator.is_valid(invalid)
    invalid = deepcopy(complete)
    invalid["identity"]["brand_json_url"] = "http://seller.example.test/brand.json"
    assert not validator.is_valid(invalid)
    assert path.read_bytes() == original
    print(
        json.dumps(
            {
                "cached_optional_signing_declarations": {
                    "version": PIN,
                    "file": str(path),
                    "uri": f"https://adcontextprotocol.org/schemas/{PIN}/{relative}",
                    "bytes": size,
                    "sha256": digest,
                    "dialect": schema["$schema"],
                    "normative_pointers": {HORIZON: horizon, IDENTITY: identity},
                    "omitted_payload": omitted,
                    "unmodified_schema_result": "accepted",
                    "semantic_32_result": "missing required horizon and operator declaration",
                }
            },
            sort_keys=True,
        )
    )


async def test_actual_public_declarations_and_schema_optional_omissions_on_all_mounts(tmp_path):
    async with production_harness(
        "postgres", tmp_path / "destination.sqlite", notifications=True, notification_delivery=True
    ) as h:
        mount = MountedProduction(h)
        mount.authorize(h.item)
        async with mount.client() as client:
            for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                _, raw = await mount.call(client, "get_adcp_capabilities", {}, transport=transport)
                assert raw["webhook_signing"]["delivery_retry_horizon_seconds"] == 86400
                assert raw["identity"]["brand_json_url"] == (
                    "https://seller.example.test/brand.json"
                )
                for relative, _, _ in CACHED:
                    validator = schema_loader.get_named_validator(relative, version=PIN)
                    assert validator is not None
                    validator.validate(raw)
                    omitted = deepcopy(raw)
                    del omitted["webhook_signing"]["delivery_retry_horizon_seconds"]
                    del omitted["identity"]
                    validator.validate(omitted)
