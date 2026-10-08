"""AdCP 3.1.27 macro URLs validate offline without rewriting ad-server tokens."""

from __future__ import annotations

from typing import Any

import pytest

from adcp.types import v31
from adcp.types.domains.core.assets.image_asset import ImageAsset
from adcp.validation.schema_loader import (
    _build_format_checker,
    _draft7_validator_type,
    get_bundle_adcp_version,
    get_named_validator,
)
from adcp.validation.schema_validator import validate_request

ASSETS = [
    ("vast", {"asset_type": "vast", "delivery_type": "url"}),
    ("daast", {"asset_type": "daast", "delivery_type": "url"}),
    ("url", {"asset_type": "url", "url_type": "clickthrough"}),
    ("vast-tracker", {"asset_type": "vast_tracker", "vast_event": "start"}),
    ("daast-tracker", {"asset_type": "daast_tracker", "daast_event": "start"}),
    ("pixel-tracker", {"asset_type": "pixel_tracker", "event": "impression"}),
]
VALID_URLS = [
    "https://ads.acme.example/tag?cb=%%CACHEBUSTER%%",
    "https://ads.acme.example/tag?u=%%PATTERN:url%%",
    "https://ads.acme.example/tag?click=%%CLICK_URL_UNESC%%",
    "https://ads.acme.example/tag?omid=[OMIDPARTNER]&gdpr=${GDPR}&cb=%%CACHEBUSTER%%",
    "https://ads.acme.example/tag?cb={CACHEBUSTER}",
    "https://ads.acme.example/tag?url=https%3A%2F%2Fexample.test&cb=%%CACHEBUSTER%%",
    "https://[::1]:8080/tag?cb=%%CACHEBUSTER%%",
    "https://ads.acme.example/o'clock",
    "myapp://creative/launch",
    "//cdn.acme.example/creative.js",
    "https://{PUB}.ads.acme.example/tag",
]
INVALID_URLS = [
    "not a valid uri template",
    "https://ads.acme.example/tag?cb=%%CACHEBUSTER%%&bad=%zz",
    "https://ads.acme.example/tag?cb=%%CACHEBUSTER%%&bad=%2",
    "https://ads.acme.example/tag?cb=%%CACHE BUSTER%%",
    "https://ads.acme.example/tag?cb=%%CACHEBUSTER%",
    "https://ads.acme.example/tag?cb=%%%%",
    "https://ads.acme.example/tag?cb=%%CACHEBUSTER%%\n",
    "https://ads.acme.example/tag?cb=%%CACHEBUSTER%%&bad=\\path",
    "https:///tag?cb=%%CACHEBUSTER%%",
    "https://?cb=%%CACHEBUSTER%%",
    "ftp://ads.acme.example/tag?cb=%%CACHEBUSTER%%",
]


def test_retained_source_bundle_and_latest_minor_are_isolated() -> None:
    assert get_bundle_adcp_version(version="3.1") == "3.1.27"
    assert get_bundle_adcp_version(version="3.1.27") == "3.1.27"
    assert get_bundle_adcp_version(version="3.1.15") == "3.1.15"

    asset = {"asset_type": "vast", "delivery_type": "url", "url": VALID_URLS[0]}
    old = get_named_validator("core/assets/vast-asset.json", version="3.1.15")
    latest = get_named_validator("core/assets/vast-asset.json", version="3.1")
    assert old is not None and latest is not None
    assert not old.is_valid(asset)
    latest.validate(asset)


@pytest.mark.parametrize(
    ("url", "valid"),
    [
        ("https://ads.acme.example/o'clock", True),
        ("https://[::1]:8080/tag?url=https%3A%2F%2Fexample.test", True),
        ("urn:example:creative", True),
        ("https://ads.acme.example/tag?cb=%2F", True),
        ("https://\u4f8b\u3048.\u30c6\u30b9\u30c8/path", True),
        ("https://ads.acme.example/cr\u00e9ative?nom=\u00e9t\u00e9", True),
        ("https://\u4f8b\u3048.\u30c6\u30b9\u30c8/path?bad=%zz", False),
        ("https://\u4f8b\u3048.\u30c6\u30b9\u30c8/\\path", False),
        ("https://ads.acme.example/path\u0080", False),
        ("https://ads.acme.example/path\ud800", False),
        ("https://ads.acme.example/tag?cb=%%CACHEBUSTER%%", False),
        ("https://ads.acme.example/tag?cb=%zz", False),
        ("https://ads.acme.example/tag with spaces", False),
        ("https://ads.acme.example/tag\n", False),
        ("https://ads.acme.example/\\tag", False),
    ],
)
def test_uri_format_checks_original_syntax(url: str, valid: bool) -> None:
    checker = _build_format_checker()
    assert "uri-template" in checker.checkers
    assert checker.conforms(url, "uri") is valid


@pytest.mark.parametrize(
    "url",
    [
        "https://\u4f8b\u3048.\u30c6\u30b9\u30c8/path",
        "https://ads.acme.example/cr\u00e9ative?nom=\u00e9t\u00e9",
    ],
)
def test_primary_pin_retains_internationalized_url_model_compatibility(url: str) -> None:
    asset = {"asset_type": "image", "url": url, "width": 1, "height": 1}
    assert get_bundle_adcp_version() == "3.2.1"
    validator = get_named_validator("core/assets/image-asset.json")
    assert validator is not None
    validator.validate(asset)
    assert ImageAsset.model_validate(asset).url is not None


@pytest.mark.parametrize(
    ("template", "valid"),
    [
        ("https://{PUB}.acme.example/{path}{?query,list*}", True),
        ("{+path}{#fragment}{.labels*}{/segments*}{;params*}{&more}", True),
        ("{name:4}{name:9999}{profile.name}{%41}", True),
        ("//cdn.acme.example/creative.js", True),
        ("https://acme.example/%2F", True),
        ("https://acme.example/creative-\u00e9", True),
        ("{\u212aey}", False),
        ("{\u0130d}", False),
        ("https://acme.example/%%CACHEBUSTER%%", False),
        ("https://acme.example/%zz", False),
        ("https://acme.example/space here", False),
        ("https://acme.example/newline\n", False),
        ("https://acme.example/\\path", False),
        ("{name:0}", False),
        ("{name:10000}", False),
        ("{name..part}", False),
        ("{unclosed", False),
    ],
)
def test_uri_template_format_checks_literals_and_expressions(template: str, valid: bool) -> None:
    assert _build_format_checker().conforms(template, "uri-template") is valid


@pytest.mark.parametrize(
    ("pattern", "value", "valid"),
    [
        (r"^value$", "value", True),
        (r"^value$", "value\n", False),
        (r"value\$", "value$\n", True),
        (r"value\$$", "value$", True),
        (r"value\$$", "value$\n", False),
        (r"^value\\$", "value\\", True),
        (r"^value\\$", "value\\\n", False),
        (r"[a-z$]", "$\n", True),
    ],
)
def test_validation_anchors_match_ecma262(pattern: str, value: str, valid: bool) -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "string",
        "pattern": pattern,
    }
    validator = _draft7_validator_type()(schema)
    assert validator.is_valid(value) is valid
    assert validator.schema == schema
    assert validator.schema["pattern"] == pattern


@pytest.mark.parametrize(("schema_name", "asset"), ASSETS)
@pytest.mark.parametrize("url", VALID_URLS)
def test_named_assets_accept_macro_urls(schema_name: str, asset: dict[str, Any], url: str) -> None:
    validator = get_named_validator(f"core/assets/{schema_name}-asset.json", version="3.1")
    assert validator is not None
    validator.validate({**asset, "url": url})


@pytest.mark.parametrize(("schema_name", "asset"), ASSETS)
@pytest.mark.parametrize("url", INVALID_URLS)
def test_named_assets_reject_malformed_macro_urls(
    schema_name: str, asset: dict[str, Any], url: str
) -> None:
    validator = get_named_validator(f"core/assets/{schema_name}-asset.json", version="3.1")
    assert validator is not None
    assert not validator.is_valid({**asset, "url": url})


def _request(tool: str, asset_type: str, url: str) -> dict[str, Any]:
    creative = {
        "creative_id": "creative_macro_test",
        "name": "Acme macro tag",
        "format_kind": "video_vast" if asset_type == "vast" else "audio_daast",
        "assets": {"tag": {"asset_type": asset_type, "delivery_type": "url", "url": url}},
    }
    common = {
        "idempotency_key": "macro-backport-test-7993",
        "account": {"account_id": "account_acme"},
    }
    if tool == "sync_creatives":
        return {**common, "creatives": [creative]}
    return {
        **common,
        "brand": {"domain": "acme.example"},
        "start_time": "2026-10-10T00:00:00Z",
        "end_time": "2026-10-11T00:00:00Z",
        "packages": [
            {
                "product_id": "product_acme",
                "pricing_option_id": "cpm_acme",
                "budget": 100,
                "creatives": [creative],
            }
        ],
    }


@pytest.mark.parametrize("tool", ["sync_creatives", "create_media_buy"])
@pytest.mark.parametrize("asset_type", ["vast", "daast"])
@pytest.mark.parametrize("url", VALID_URLS[:3])
def test_versioned_requests_preserve_macro_bytes(tool: str, asset_type: str, url: str) -> None:
    payload = _request(tool, asset_type, url)
    outcome = validate_request(tool, payload, version="3.1")
    assert outcome.valid, outcome.issues

    model_class = (
        v31.SyncCreativesRequest if tool == "sync_creatives" else v31.CreateMediaBuyRequest
    )
    model = model_class.model_validate(payload)
    wire = model.model_dump(mode="json", exclude_none=True)
    creatives = wire["creatives"] if tool == "sync_creatives" else wire["packages"][0]["creatives"]
    assert creatives[0]["assets"]["tag"]["url"].encode() == url.encode()


@pytest.mark.parametrize("tool", ["sync_creatives", "create_media_buy"])
@pytest.mark.parametrize("asset_type", ["vast", "daast"])
def test_requests_reject_malformed_macro_urls(tool: str, asset_type: str) -> None:
    outcome = validate_request(tool, _request(tool, asset_type, INVALID_URLS[3]), version="3.1")
    assert not outcome.valid
