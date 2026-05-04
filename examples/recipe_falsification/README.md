# Phase 1B — recipe shape falsification (Q2)

The harness for **Q2** of the [salesagent side-car experiment](../../docs/proposals/salesagent-sidecar-experiment.md):

> Does the recipe shape carry GAM's `implementation_config` without escape hatches?

## What's here

- **`gam_recipe.py`** — typed `GAMRecipe` Pydantic model, constructed from
  every field salesagent's
  [`gam_product_config_service.py`](https://github.com/prebid/salesagent/blob/main/src/services/gam_product_config_service.py)
  generates, validates, or parses. `extra="forbid"` on every model.

- **`fixtures/gam_impl_config_examples.json`** — five recorded shapes
  derived from `GAMProductConfigService.generate_default_config()` and
  `parse_form_config()`:
  - `guaranteed_default` — auto-generated guaranteed-mode defaults
  - `non_guaranteed_default` — auto-generated non-guaranteed defaults
  - `video_with_targeting` — full video config with inventory targeting,
    frequency caps, custom targeting, video duration limits
  - `native_with_discount` — native ad with percentage discount + style id
  - `minimal` — bare-minimum config (only `validate_config()`-required fields)

- **`test_recipe_round_trip.py`** — pytest harness running the
  pre-registered Q2 falsifiers from PR #506:
  - `(c)` lossy round-trip: `dict → GAMRecipe → dict` not equal
  - `(a)` any `extra: dict[str, Any]` field on the recipe
  - `(b)` any `# type: ignore` needed to construct
  - extra-forbid: smuggled fields fail validation

## Run

```bash
python3 -m pytest examples/recipe_falsification/ -v
```

## Result (current commit)

```
8 passed in 0.18s
```

**All Q2 falsifiers refused to fire.**

- Round-trip is lossless across all five fixture shapes
- Zero `Any`-typed fields on the recipe (the only dict-typed field —
  `custom_targeting_keys: dict[str, str | list[str]]` — is strict
  typing matching GAM's documented API, not an escape hatch)
- Direct construction with sub-models needs no `# type: ignore`
- Unknown fields are rejected (`extra="forbid"`)

## What this confirms

The Q2 prior held: **a typed Pydantic recipe can carry the full GAM
`implementation_config` shape without escape hatches**, given the
field set salesagent's service code generates and parses.

Combined with Phase 1A's Q1.5 finding (recipe is adopter-owned, not
framework-managed — see [PR #507](https://github.com/adcontextprotocol/adcp-client-python/pull/507)),
the architecture story is now:

* **Recipe is typed at the framework boundary** (Q2 confirmed) —
  `recipe_type: ClassVar[type[Recipe]]` on `DecisioningPlatform`
  validates shape at dispatch.
* **Recipe storage is adopter-owned** (Q1.5 confirmed) — the framework
  doesn't manage session caches, persistence on `finalize`, or
  hydration at `create_media_buy`.

The framework's job is small and focused: type the contract, route
transitions, dispatch typed recipes. Storage lives where it always
lived in salesagent's case.

## Caveats

* **Fixtures are derived from `gam_product_config_service.py`'s code
  paths**, not pulled from a production salesagent database. Real
  deployments might have field combinations the service code
  doesn't currently produce. To fully validate, dump actual
  `Product.implementation_config` JSON from a salesagent dev DB and
  rerun the harness; if any payload fails to round-trip, that's a
  finding that revises `GAMRecipe`.
* **`custom_targeting_keys: dict[str, str | list[str]]`** is the
  borderline case. Strict typing per GAM's documented API. If
  salesagent's own data has deeper-nested values (e.g., admin UI
  edge cases) they'd reject — that's "right" against GAM's API
  but might bite migrations from existing data. Document the
  contract clearly when promoting to a real `GAMPlatform`.
* **The set of fields could grow.** If GAM adds a new line-item type,
  cost type, or environment, the `Literal[...]` enums need updating.
  Strict typing means versioning the recipe explicitly.

## Where this fits

This harness lives in `examples/` because the recipe model is
adopter-shaped, not SDK-shipped. When the SDK formalizes a `Recipe`
base class (likely `recipe_kind` discriminated union per #502 Path B),
adopters subclass it like this. The example here demonstrates the
pattern with GAM as the worked case.
