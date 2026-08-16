"""Pin and consume the TypeScript 13.0.0-rc.3 canonical transition corpus."""

# ruff: noqa: E501 -- immutable source paths plus SHA-256 digests are intentionally long.

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from adcp import Format, Product

CORPUS_ROOT = Path(__file__).parent / "fixtures" / "canonical" / "typescript-13.0.0-rc.3"

_CORPUS_SHA256 = {
    "test/canonical-creatives-a2a-e2e.test.js": "8e72f466b6616489682c6f093b9feec33d9029ea62d71e754afcafb0bd1cc3f5",
    "test/canonical-creatives-mcp-server-e2e.test.js": "c82e6e09186cf31decf602c73567f8ada1a8449c7c366930717a91fd2b714cae",
    "test/lib/catalog-unique-id.test.js": "f3d7edbe7b444cf7879f07271a1ecedf83cdaa0ee177d949c9e7db8b1b63c477",
    "test/lib/canonical-creative-async-boundary.test.js": "287fddd4feda3ab6e5bee75ebde4bc96c0606f7d251382dffb381080f25cbe5d",
    "test/lib/canonical-format-builders.test.js": "02fc8c85a7671cf6b9edb47fc073c2026fce00f9dd636f802841bc5e49581148",
    "test/lib/canonical-legacy-route-cache.test.js": "5ce52946c4f64f60f124b846e21c7a41ccd85f20c23909e4bc4c506faeacfad4",
    "test/lib/creative-format-projection.test.js": "1dfd72871af9b172db3f2f87f1e3531c1a4283a250723e92081e5a467a45a947",
    "test/lib/projection-catalog-adapters.test.js": "5330d4ce948c22aded434f0df25bb8c0e7d2fdbd7e3ac1a99f040d8427b987a3",
    "test/lib/storyboard-canonical-format-satisfaction.test.js": "f9645a507d7345aaacc50ed2eba883cc2c2926c8429ec41a7f8e2b79cd0d62ea",
    "test/lib/v1-to-v2-projection.test.js": "965a38ac32d727cb8e494e4e30a875891742b57907c73f88733a5714818863aa",
    "test/lib/v1-v2-roundtrip-matrix.test.js": "79f89200c5db90a0eec2e700e6249793a0dcdb1372278cd94a6b3c7eb335d327",
    "test/lib/v2-canonical-only-projection.test.js": "6618c801c9db6b285715028b60cf016923095dcd472f3b536544d42bd32a342a",
    "test/lib/v2-cross-version-smoke.test.js": "bbdadad19c971df21a4da65718bb3e1df9871e9a53d69cb6c6486fcfcd4ffdc7",
    "test/lib/v2-getproducts-autowire.test.js": "c9e5bfb86da9b061188a45ba322b5f59f63c1877642f88ec1cfe185e7b927dcc",
    "test/lib/v2-projection-fixtures/aao-reference-formats.json": "2d1bed294fcc86aa233bb3ac3181420176f2c1ccae39fd11986edea808a2649f",
    "test/lib/v2-projection-fixtures/amazon_sponsored_products.json": "919a3f87ac29d374c13eec7c4fad815d19e9c535c66756d8caad399d4197cf40",
    "test/lib/v2-projection-fixtures/chatgpt_brand_mention.json": "130866d0ecf8e7de728bb5ecec379b935264329206b97368aa7971be74c6d706",
    "test/lib/v2-projection-fixtures/community/meta.json": "3201abe0d284a765a94ab04616677507e214501b7abf2b2be7510e8f7cf4738a",
    "test/lib/v2-projection-fixtures/gam_3p_display_tag.json": "9ac2858a246bca6b3136485fb913e2486ff5f17d01a9452190895ef60c56d76f",
    "test/lib/v2-projection-fixtures/google_performance_max.json": "71fc6b70695f031c1ed8a14b2e7e21fb00728cf9cb808ff32421aa58a673ff24",
    "test/lib/v2-projection-fixtures/meta_carousel.json": "c0ca7448263e2f37a4bd0490c3d4cfc05cd4b66e06c6a8c53e4d357dcd6471da",
    "test/lib/v2-projection-fixtures/meta_reels_us.json": "c2103f2631b4f83a2d9bcfc11cab07075331764a68c4be4be49557323a6fbf22",
    "test/lib/v2-projection-fixtures/nytimes_homepage_html5.json": "7ec718664c3ac5802c736470571ff92bac8597c1c51e82a4c6977d12539f154e",
    "test/lib/v2-projection-fixtures/nytimes_homepage_mrec.json": "ef74b1369f9c7764548959f05115742e1ff069152c34680e81fb6c0f5e2a5392",
    "test/lib/v2-projection-fixtures/nytimes_homepage_takeover_custom.json": "d7db7ad38783b6800e5dc85ba2fdc2f05e6cc4cf442dbdcacc078758f1e6b483",
    "test/lib/v2-projection-fixtures/the_daily_30s_host_read.json": "80c381e6fb734cec655c4784b606851994fdfe611e8709ca0a9831c718566773",
    "test/lib/v2-projection-fixtures/triton_daast_audio_30s.json": "e063bd213d456b9b329003788b7e0cb492e2cdc019c8f65e3b3ea236a3a6913c",
    "test/lib/v2-projection-fixtures/veo_generative_video_15s.json": "2475ae8561fbd9c32e146aa647e49302d2c8fde56e225178512f6f0e793061d3",
    "test/lib/v2-projection-fixtures/youtube_vast_preroll.json": "1b2fb23ad5d96a7220c46d2bd16b21fe3d49c4ff2ca549d9eb58296b495a8978",
    "test/lib/v2-to-v1-projection.test.js": "ea1da172890307e2c211c04e9e129536aecfd2c45ada70ae2bfa5e3b7d23ed5f",
    "test/lib/v2-write-side.test.js": "a0212f6bc1fe865b054511198ac09ed93bf4b9adfd9e8785871c38caf53de7b2",
}


def _legacy_identity_paths(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key in {"format_id", "format_ids", "v1_format_ref"}:
                found.append(child)
            if key == "agent_url":
                found.append(child)
            found.extend(_legacy_identity_paths(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_legacy_identity_paths(item, f"{path}[{index}]"))
    return found


def test_vendored_rc3_corpus_is_byte_exact() -> None:
    actual = {
        path.relative_to(CORPUS_ROOT).as_posix()
        for path in CORPUS_ROOT.rglob("*")
        if path.is_file() and path.name != "README.md"
    }
    assert actual == set(_CORPUS_SHA256)
    assert sum(path.endswith(".js") for path in actual) == 16
    assert sum(path.endswith(".json") for path in actual) == 15
    for relative, expected in _CORPUS_SHA256.items():
        digest = hashlib.sha256((CORPUS_ROOT / relative).read_bytes()).hexdigest()
        assert digest == expected, relative


def test_every_rc3_canonical_product_crosses_the_primary_boundary() -> None:
    fixture_dir = CORPUS_ROOT / "test" / "lib" / "v2-projection-fixtures"
    product_paths = sorted(
        path for path in fixture_dir.glob("*.json") if path.name != "aao-reference-formats.json"
    )
    assert len(product_paths) == 13

    for path in product_paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["format_options"], path.name
        # Consume every exact RC3 declaration, including its v1_format_ref.
        # Format stores that route in private compatibility state; passing the
        # resulting models into Product proves the unmodified fixture can cross
        # the canonical boundary without exposing the route on the wire.
        declarations = [Format.model_validate(option) for option in raw["format_options"]]
        product = Product.model_validate({**raw, "format_options": declarations})
        for source, declaration in zip(raw["format_options"], declarations, strict=True):
            assert [
                ref.model_dump(mode="json", exclude_none=True)
                for ref in declaration.legacy_format_refs
            ] == source.get("v1_format_ref", [])
        dumped = product.model_dump(mode="json")
        assert not _legacy_identity_paths(dumped), path.name


def test_rc3_catalog_and_community_snapshots_are_complete() -> None:
    fixture_dir = CORPUS_ROOT / "test" / "lib" / "v2-projection-fixtures"
    aao = json.loads((fixture_dir / "aao-reference-formats.json").read_text())
    meta = json.loads((fixture_dir / "community" / "meta.json").read_text())
    assert len(aao) == 55
    assert len(meta["formats"]) == 4
