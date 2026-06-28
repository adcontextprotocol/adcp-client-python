# Migrating from v3.x to v4.0

4.0 realigns the SDK surface with the current AdCP spec. The schema redesign
removed several types and renamed fields; this guide lists each change with
before/after code.

## Automated migration

Run the bundled codemod against your source tree first — it rewrites the 9
mechanical `<Type>Asset` → `<Type>Content` renames and prints a structured
report of every removed-type usage that still needs a manual replacement:

```bash
# Dry run — see the plan before anything moves.
python -m adcp.migrate v3-to-v4 ./src

# Rewrite in place. Commit first so `git diff` is your review.
python -m adcp.migrate v3-to-v4 ./src --apply

# Also auto-fix safe import rewrites (generated_poc paths + numbered Assets aliases).
# flag_removed findings still require human review.
python -m adcp.migrate v3-to-v4 ./src --auto-apply

# Structured JSON for CI / editor integrations.
python -m adcp.migrate v3-to-v4 ./src --json
```

`--auto-apply` implies `--apply` and additionally rewrites the ~78% of findings
that are mechanically safe: `from adcp.types.generated_poc.X import Symbol` lines
where every symbol has a known public alias on `adcp.types`, and `Assets<N>` numbered
classes that have a documented semantic alias (`Assets81 → VideoFormatAsset`, etc.).
`flag_removed` findings always require human attention.

The CLI exits 0 when all remaining findings are mechanical (or none); exits 1 when
`flag_removed` findings remain for human review — wire it into CI to gate merges
until every flagged usage is addressed.

## Audit your exposure first

If you'd rather stick with grep:

```bash
grep -rnE "BrandManifest|FormatCategory|DeliverTo|PromotedProducts|PromotedOfferings|PackageStatus|from adcp import Pricing|\.brand_manifest|adcp\.types\.generated_poc|(Audio|Css|Html|Image|Javascript|Text|Url|Video|Webhook)Asset\b" src/
```

Each match is either an import that will now raise `ImportError`, an attribute
access that will raise `AttributeError`, or a coupling to a private module.

Update your dependency pin:

```toml
# pyproject.toml
[project]
dependencies = [
    "adcp>=4.0.0b1,<5",
]
```

## Fix removed-type imports first — they cascade

Real-adopter feedback (salesagent v3→v4 experiment at v4.0 release): 270 files
scanned, 161 test-collection failures. These figures reflect tooling at initial
release, before subsequent alias coverage and codemod improvements; run the codemod against
your own tree to see your actual count. Consumers tend to centralize SDK imports
in one schema module, so a single broken import there crashes test collection
across the whole codebase. salesagent re-exported through
`src/core/schemas/_base.py` — **one missing `FormatCategory` import there
cascaded into ~140 test failures during pytest collect-only**, and stubbing it
revealed the next ~140-test cascade from `BrandManifest`, then the next from
the `generated_poc` reach-ins.

The codemod's static finding count understates this by 100x+. To minimize the
felt blast radius, work in this order:

1. **Removed types in central re-export modules first.** Find every
   `BrandManifest`, `FormatCategory`, `DeliverTo`, `PromotedProducts`,
   `PromotedOfferings`, `Pricing`, `PackageStatus` import in modules that
   re-export to the rest of your codebase (e.g. a `_base.py` schema barrel).
   Fix those before anything else — most of your test-collection failures
   disappear.
2. **`adcp.types.generated_poc` reach-ins.** The codemod's per-symbol mapping
   tells you the public alias for each (`ContextObject` →
   `adcp.types.ContextObject`, etc.). Mechanical lookup once you know the
   pattern.
3. **Numbered `Assets<N>` imports.** Switch to the semantic alias from
   `adcp.types`. See [Numbered discriminated-union classes
   shifted](#numbered-discriminated-union-classes-shifted) below.
4. **Per-call-site shape changes** (e.g. `BrandManifest(name=..., logo_url=...)`
   → `BrandReference(domain=...)`). The codemod can't auto-rewrite these —
   the data is different.

## a2a-sdk transitive bump (only matters if you have direct `a2a` imports)

v4 of this SDK requires `a2a-sdk>=1.0.1,<1.0.2` (pinned tight due to a
1.0.2 upstream regression; see the comment in `pyproject.toml` for context).
If your codebase imports from the `a2a` package directly — typically
hand-rolled a2a-sdk 0.3 server code or direct type imports — those are a
separate migration this SDK forces on you transitively.

Symptoms during pytest collection after upgrading:

- `cannot import name 'ServerError' from 'a2a.utils.errors'`
- `cannot import name 'DataPart' from 'a2a.types'`

If you don't import from `a2a` directly (only via `adcp.server` /
`adcp.protocols.a2a`), this section doesn't apply — the SDK's wrapper layer
absorbs the change.

### Hand-rolled a2a-sdk 0.3 servers — no mechanical migration path

If you have a hand-rolled a2a-sdk 0.3 server (a module that imports from
`a2a.server.apps`, constructs `DataPart`, `TextPart`, or `Part(root=...)`
directly, or references `a2a.utils.errors.ServerError`), there is no
symbol-by-symbol migration path to a2a-sdk 1.0.x. The 0.3 Pydantic types
(`DataPart`, `TextPart`, `Part(root=...)`) are replaced by a protobuf `Part`
message with a `content` oneof; `a2a.server.apps` is replaced by
`a2a.server.routes` + `a2a.server.agent_execution`; `ServerError` is replaced
by `a2a.utils.errors.A2AError` and its subclasses (`InternalError`,
`InvalidParamsError`).

**The recommended path:** delete the hand-rolled server and use
`adcp.server.serve(transport="a2a")` instead:

```python
from adcp.server import ADCPHandler, serve

class MyHandler(ADCPHandler):
    async def get_products(self, req, ctx=None):
        return {"products": [...]}

serve(MyHandler(), name="my-agent", transport="a2a")
```

The SDK's A2A layer handles `DataPart`/`Part` construction, task lifecycle,
agent card, and dual 0.3/1.0 wire compatibility internally.

### Direct `a2a` type imports (non-server code)

If your non-server code imports `a2a` types directly, the 0.3 → 1.0 changes
include:

- `a2a.types.DataPart` and `a2a.types.TextPart` → `a2a.types.Part` (the
  standalone `DataPart`/`TextPart` classes are gone; 1.0 uses a single
  protobuf `Part` with a `content` oneof — see the
  [a2a-sdk 1.0 release notes](https://github.com/a2aproject/a2a-python/releases)
  for the full protobuf API)
- `a2a.types.Part(root=<DataPart|TextPart>)` → `a2a.types.Part` directly
  (the 0.3 wrapper is gone; `Part` is the message itself)
- `a2a.utils.errors.ServerError` → `a2a.utils.errors.A2AError` (base class)
  or `InternalError` / `InvalidParamsError` for specific cases

Note: `a2a.types.A2AError` and `a2a.types.MessagePart` do **not** exist in
a2a-sdk 1.0.1. If a guide or resource points you at these symbols, it is out
of date. The error base class lives at `a2a.utils.errors.A2AError`.

## Removed types

The following types were removed from the AdCP spec and have no replacement
stubs in the SDK. Imports will fail at runtime with `ImportError`.

| Removed | Replacement |
| --- | --- |
| `BrandManifest` | `BrandReference(domain=...)` when constructing requests; inline dict when reading registry `brand` payloads |
| `FormatCategory` | Removed without replacement (previously an enum, now inferred from format metadata) |
| `DeliverTo` | Removed — use `publisher_properties` on the request |
| `PromotedProducts` / `PromotedOfferings` | Removed — use the `offerings` field shape from the current spec |
| `Pricing` | Use the discriminated `*PricingOption` types (e.g. `CpmFixedRatePricingOption`) |
| `PackageStatus` | Package status is now carried by `MediaBuyStatus`; per-package status was removed |

### `BrandManifest` → `BrandReference`

**Before (v3.x):**
```python
from adcp import CreateMediaBuyRequest, BrandManifest

request = CreateMediaBuyRequest(
    brand_manifest=BrandManifest(
        name="Coffee Co",
        brand_url="https://coffeeco.com",
        logo_url="https://coffeeco.com/logo.png",
    ),
    packages=[...],
    publisher_properties=...,
)
```

**After (v4.0):**
```python
from adcp import CreateMediaBuyRequest, BrandReference

request = CreateMediaBuyRequest(
    brand=BrandReference(domain="coffeeco.com"),
    packages=[...],
    publisher_properties=...,
)
```

The spec now resolves brand identity from `/.well-known/brand.json` at the
supplied domain, or from the AdCP registry. The SDK no longer models the
brand manifest as a typed class.

### `FormatCategory` → removed

The spec no longer models category as a separate enum. Read category
information from `Format` metadata instead.

```python
# Before
from adcp import FormatCategory
if fmt.category == FormatCategory.video: ...

# After — category info lives on Format itself
if fmt.type == "video": ...  # or fmt.channel, depending on what you were matching
```

### `DeliverTo` → `publisher_properties`

```python
# Before
request = CreateMediaBuyRequest(deliver_to=DeliverTo(...), ...)

# After
request = CreateMediaBuyRequest(
    publisher_properties=PublisherPropertiesAll(selection_type="all"),
    ...,
)
```

### `PromotedProducts` / `PromotedOfferings` → `offerings`

```python
# Before
request.promoted_offerings = PromotedOfferings(...)

# After — pass the spec-current offerings shape as a dict/model
request.offerings = [...]
```

### `Pricing` → discriminated `*PricingOption`

```python
# Before
from adcp import Pricing
pricing = Pricing(model="cpm", rate=5.0, currency="USD")

# After — each pricing model has its own class
from adcp import CpmFixedRatePricingOption
pricing = CpmFixedRatePricingOption(
    pricing_option_id="cpm_usd",
    pricing_model="cpm",
    is_fixed=True,
    currency="USD",
    rate=5.0,
)
```

### `PackageStatus` → `MediaBuyStatus`

Per-package status was removed. Status now lives on the media buy.

```python
# Before
if package.status == PackageStatus.active: ...

# After
if media_buy.status == MediaBuyStatus.active: ...
```

### `MediaBuyStatus.pending_activation` → split

The single `pending_activation` enum value was split into two distinct
states based on cause. The codemod flags every reference; the correct
replacement is per-call-site.

| Cause | Replacement |
| --- | --- |
| Buy is scheduled and waiting for its start time | `MediaBuyStatus.pending_start` |
| Buy is waiting on creative approval / asset processing | `MediaBuyStatus.pending_creatives` |

**Before (v3.x):**
```python
if media_buy.status == MediaBuyStatus.pending_activation:
    notify_trafficker(media_buy)
```

**After (v4.0):**
```python
if media_buy.status in (
    MediaBuyStatus.pending_start,
    MediaBuyStatus.pending_creatives,
):
    notify_trafficker(media_buy)
```

When the original branch only fired for one cause, narrow to the right
state (e.g. only `pending_creatives` for creative-review notifications).
A blanket replacement to either single value is almost always wrong —
the spec split was driven by adopters needing distinct behaviour for
the two cases. The wire enum still accepts `pending` as a legacy alias
for `pending_start`, so existing buyer clients reading older payloads
keep working without code changes.

### `ResolvedBrand.brand_manifest` field removed

`RegistryClient.lookup_brand()` returns a `ResolvedBrand` whose
`brand_manifest` field and cross-populate validator are gone.

**Before (v3.x):**
```python
result = await registry.lookup_brand("nike.com")
manifest = result.brand_manifest  # Either `brand` or `brand_manifest` worked
```

**After (v4.0):**
```python
result = await registry.lookup_brand("nike.com")
manifest = result.brand  # Only `.brand`
```

## Numbered discriminated-union classes shifted

`datamodel-code-generator` numbers variant classes in the order they appear in
the upstream `oneOf`. When the spec reorders variants, the numbers shift.
Example: `Assets5`…`Assets14` in 3.x now correspond to higher-numbered
variants (`Assets57`…`Assets149`) across different response modules.

**Don't** import numbered classes directly:

```python
# Fragile — will break on the next spec revision:
from adcp.types.generated_poc.bundled.creative.build_creative_response import Assets9
```

**Do** import the semantic alias from `adcp.types`:

```python
from adcp.types import CreateMediaBuySuccessResponse, BuildCreativeSuccessResponse
```

Aliases for all discriminated-union success/error variants live in
`adcp/types/aliases.py`. If a variant you need isn't aliased, file an issue —
aliasing is the supported path; direct `Assets*` imports aren't.

### Creative format asset slots: `<Type>FormatAsset` aliases

Format definitions enumerate asset slots with a discriminated union on
`asset_type`. These are the classes salesagent hit when `Assets5`/`Assets14`
renumbered to `Assets57`/`Assets149`. The stable names are:

| Generated class    | Semantic alias                | `asset_type`       |
|--------------------|-------------------------------|--------------------|
| `Assets` (base)    | `ImageFormatAsset`            | `image`            |
| `Assets81`         | `VideoFormatAsset`            | `video`            |
| `Assets82`         | `AudioFormatAsset`            | `audio`            |
| `Assets83`         | `TextFormatAsset`             | `text`             |
| `Assets84`         | `MarkdownFormatAsset`         | `markdown`         |
| `Assets85`         | `HtmlFormatAsset`             | `html`             |
| `Assets86`         | `CssFormatAsset`              | `css`              |
| `Assets87`         | `JavascriptFormatAsset`       | `javascript`       |
| `Assets88`         | `VastFormatAsset`             | `vast`             |
| `Assets89`         | `DaastFormatAsset`            | `daast`            |
| `Assets90`         | `UrlFormatAsset`              | `url`              |
| `Assets91`         | `WebhookFormatAsset`          | `webhook`          |
| `Assets92`         | `BriefFormatAsset`            | `brief`            |
| `Assets93`         | `CatalogFormatAsset`          | `catalog`          |
| `Assets94`         | `RepeatableAssetGroup`        | `repeatable_group` |
| `Assets95…Assets106` | `ImageFormatGroupAsset` etc.| (same type inside a group) |

The `Format` prefix disambiguates these *format-slot* types from the
separate *asset-content* types (`VideoContent`, `HtmlContent`, `ImageContent`,
etc. in `adcp.types` — renamed from `<Type>Asset` in 4.0, see below),
which describe the actual asset payload (codec, duration, file URL)
delivered by creative sync — a distinct concept.

`tests/test_asset_aliases_stable.py` pins each alias to its expected
`asset_type` discriminator default. When upstream renumbers, that test
fails and points at the specific alias that drifted — fix the numbered
import in `src/adcp/types/aliases.py`, not your call sites.

### Asset-content types: `<Type>Asset` → `<Type>Content`

The payload-describing types — the classes you construct to attach an
actual image, video, or HTML payload to a `CreativeManifest` — are
renamed. The 3.x names collided with the `<Type>FormatAsset` slot types
described above; autocomplete showed two entries whose only difference
was a `Format` infix, and agent authors picked the wrong one.

| 3.x name            | 4.0 name              |
|---------------------|-----------------------|
| `AudioAsset`        | `AudioContent`        |
| `CssAsset`          | `CssContent`          |
| `HtmlAsset`         | `HtmlContent`         |
| `ImageAsset`        | `ImageContent`        |
| `JavascriptAsset`   | `JavascriptContent`   |
| `TextAsset`         | `TextContent`         |
| `UrlAsset`          | `UrlContent`          |
| `VideoAsset`        | `VideoContent`        |
| `WebhookAsset`      | `WebhookContent`      |

**Before (v3.x):**
```python
from adcp.types import ImageAsset, UrlAsset

assets = {
    "primary_asset": ImageAsset(url="https://example.com/img.jpg",
                                width=300, height=250),
    "clickthrough_url": UrlAsset(url="https://example.com"),
}
```

**After (v4.0):**
```python
from adcp.types import ImageContent, UrlContent

assets = {
    "primary_asset": ImageContent(url="https://example.com/img.jpg",
                                  width=300, height=250),
    "clickthrough_url": UrlContent(url="https://example.com"),
}
```

Mechanical search-and-replace, nine names:

```bash
perl -pi -e '
  s/\b(Audio|Css|Html|Image|Javascript|Text|Url|Video|Webhook)Asset\b/$1Content/g
' $(git ls-files "*.py")
```

The regex intentionally *omits* `VastAsset`, `DaastAsset`, `BriefAsset`,
`CatalogAsset`, and `MarkdownAsset` — none were on the public surface in
3.x, so no rename applies. If you have user-defined classes whose names
happen to end in one of the nine suffixes (e.g. `MyImageAsset`), the
word-boundary regex will match them too; review the diff before
committing.

Field shapes are unchanged — the class identity is the same Pydantic
model underneath. For VAST/DAAST, continue using the delivery-type
variants (`UrlVastAsset` / `InlineVastAsset` / `UrlDaastAsset` /
`InlineDaastAsset`), which are unchanged.

The `<Type>FormatAsset` slot types (unchanged) remain the way to inspect
a format *definition* — `VideoFormatAsset` is a slot declaration inside
a `Format`; `VideoContent` is the payload you deliver into that slot.

### Deep-submodule `format_category` shim

Some older import sites reach into the raw generated path:

```python
from adcp.types.generated_poc.enums.format_category import FormatCategory
```

4.0 registers a ``sys.modules`` shim for this path so the import raises
an ``ImportError`` with the same migration pointer as the top-level
``from adcp import FormatCategory``, instead of a bare
``ModuleNotFoundError``. If you're seeing the deep path in your code,
switch to the migration above — the shim is a safety net, not a
permanent export.

## Public vs. internal imports

`adcp.types.generated_poc.*` is internal. Generated module paths and class
names can change with every schema regeneration. Import from `adcp.types`
instead.

**Before:**
```python
from adcp.types.generated_poc.core.context import ContextObject
from adcp.types.generated_poc.core.targeting import TargetingOverlay
```

**After:**
```python
from adcp import ContextObject, TargetingOverlay
# or
from adcp.types import ContextObject, TargetingOverlay
```

4.0 adds top-level re-exports for `TargetingOverlay`, `AdvertiserIndustry`,
`KellerType`, and `BrandSource`. If you need a type that isn't on the
top-level surface, check `from adcp.types import X` first — most generated
types are re-exported there.

For a narrower import surface, six curated partial modules group types by
domain: `adcp.types.media_buy`, `adcp.types.creative`, `adcp.types.signals`,
`adcp.types.protocol`, `adcp.types.buyer`, and `adcp.types.seller`. Each is
lazy and re-exports only the types relevant to that area
(`from adcp.types.media_buy import CreateMediaBuyRequest`). These and
`adcp.types` are the supported surfaces — never import from
`adcp.types.generated_poc.*` or `adcp.types._generated`.

## `__version__` now reflects the installed distribution

`adcp.__version__` now reads from `importlib.metadata.version("adcp")`
instead of a hardcoded constant, so it always matches `pyproject.toml`. If
you're running from a source checkout without `pip install -e .`, you'll see
`"0.0.0+unknown"` — install the package (or run `pip install -e .` in CI) to
get the real version. If your test suite asserts on `__version__`, it will
need the same install step.

## Watch for silent Pydantic field drops

`CreateMediaBuyRequest` (and most other request models) accept extra fields
without erroring. Passing `brand_manifest=...` by keyword after upgrading
won't raise at construction — the field is silently dropped, and you'll see
the failure as a server-side rejection or missing `brand` at execution time.
The `grep` in the audit section above catches these.
