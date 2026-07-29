"""Pin and consume the complete TypeScript 13.0.0-rc.3 reference corpus."""

# ruff: noqa: E501 -- immutable source paths plus SHA-256 digests are intentionally long.

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from adcp import Product

CORPUS_ROOT = Path(__file__).parent / "fixtures" / "canonical" / "typescript-13.0.0-rc.3"

_CORPUS_SHA256 = {
    "test/canonical-creatives-mcp-server-e2e.test.js": "c82e6e09186cf31decf602c73567f8ada1a8449c7c366930717a91fd2b714cae",
    "test/lib/catalog-unique-id.test.js": "f3d7edbe7b444cf7879f07271a1ecedf83cdaa0ee177d949c9e7db8b1b63c477",
    "test/lib/creative-format-projection.test.js": "1dfd72871af9b172db3f2f87f1e3531c1a4283a250723e92081e5a467a45a947",
    "test/lib/projection-catalog-adapters.test.js": "5330d4ce948c22aded434f0df25bb8c0e7d2fdbd7e3ac1a99f040d8427b987a3",
    "test/lib/v1-to-v2-projection.test.js": "965a38ac32d727cb8e494e4e30a875891742b57907c73f88733a5714818863aa",
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
        # v1_format_ref is a wire-adapter route in the RC3 source fixture. The
        # primary Python boundary consumes the canonical declaration while the
        # route remains confined to explicit compatibility state.
        for option in raw["format_options"]:
            option.pop("v1_format_ref", None)
        product = Product.model_validate(raw)
        dumped = product.model_dump(mode="json")
        assert not _legacy_identity_paths(dumped), path.name


def test_rc3_catalog_and_community_snapshots_are_complete() -> None:
    fixture_dir = CORPUS_ROOT / "test" / "lib" / "v2-projection-fixtures"
    aao = json.loads((fixture_dir / "aao-reference-formats.json").read_text())
    meta = json.loads((fixture_dir / "community" / "meta.json").read_text())
    assert len(aao) == 55
    assert len(meta["formats"]) == 4
