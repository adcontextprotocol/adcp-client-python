# Canonical-formats reference fixtures

Vendored from upstream `adcontextprotocol/adcp` for SDK conformance
tests in `tests/test_canonical_formats_roundtrip.py`. Fixtures are
content-pinned to the upstream commit below; do not edit them
directly — re-vendor from upstream when refreshing.

## v2 Product fixtures (14 files)

Source path: `static/examples/products/canonical/*.json`

* `amazon_sponsored_products.json`
* `chatgpt_brand_mention.json`
* `gam_3p_display_tag.json`
* `google_performance_max.json`
* `meta_carousel.json`
* `meta_reels_us.json`
* `nytimes_homepage_html5.json`
* `nytimes_homepage_mrec.json`
* `nytimes_homepage_takeover_custom.json`
* `taboola_content_recommendation.json`
* `the_daily_30s_host_read.json`
* `triton_daast_audio_30s.json`
* `veo_generative_video_15s.json`
* `youtube_vast_preroll.json`

## v1 reference catalog (1 file, 50 entries)

Source path: `server/src/creative-agent/reference-formats.json` →
vendored as `v1-reference-formats.json`.

## Refresh procedure

```bash
# 1. Get the current upstream tip SHA on @main:
gh api repos/adcontextprotocol/adcp/branches/main --jq '.commit.sha'

# 2. Pull each file:
for f in <names>; do
  gh api repos/adcontextprotocol/adcp/contents/static/examples/products/canonical/$f \
    --jq '.content' | base64 -d > tests/fixtures/canonical/$f
done
gh api repos/adcontextprotocol/adcp/contents/server/src/creative-agent/reference-formats.json \
  --jq '.content' | base64 -d > tests/fixtures/canonical/v1-reference-formats.json

# 3. Run the round-trip suite and update this VENDOR.md with the new SHA:
.venv/bin/pytest tests/test_canonical_formats_roundtrip.py -v
```

## Upstream pin (last refresh)

Re-vendored on the initial vendor pass for #741 part 2. No SHA
pinning is enforced in CI — these are content tests; a diff in the
fixtures triggers a tests-fail signal that's the intended
conformance-drift detector.
