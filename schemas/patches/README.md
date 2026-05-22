# schemas/patches/

Tracked, reviewable patches applied to the regenerated schema cache after
`make regenerate-schemas` (i.e., after `scripts/sync_schemas.py` extracts
the upstream protocol bundle into `schemas/cache/{bundle_key}/`).

## Why this exists

The schema cache under `schemas/cache/` is **upstream-verbatim** by design.
`make regenerate-schemas` blows it away and re-extracts the protocol
bundle on every run. That's the right default — it guarantees the cache
matches the published protocol byte-for-byte and prevents silent drift.

But sometimes the SDK needs to carry a forward-looking surface (a field
the dict-layer helpers expose today, expected to land upstream soon) or
a workaround for an upstream regression. Before this directory existed,
the only option was to hand-edit `schemas/cache/` directly and hope
nobody ran `make regenerate-schemas` until upstream caught up.

That bet lost on PR #791: PR #753 hand-patched `revoked_publisher_domains[]`
and `publisher_domains[]` (compact form) onto the 3.0 cache anticipating
they'd land in 3.0.10+; upstream chose to put them in 3.1.0-beta.x
instead; the regen to 3.0.12 silently overwrote both patches; the
Pydantic-model layer lost the fields while the dict helpers kept
implementing them. Nobody caught it until the regen diff was reviewed
line-by-line.

This directory plus the post-process step in `sync_schemas.py` make
the pattern explicit: hand-rolled diffs are tracked, named, and applied
*after* a clean regen. CI fails loudly if a patch is dead (upstream
landed it) or broken (upstream restructured the file).

## File layout

```
schemas/patches/
├── README.md                           # this file
└── 01-publisher-domains-compact.patch  # numbered prefix → lex order
└── 02-revoked-publisher-domains.patch
```

Patches apply in **lex (alphanumeric) order**. Use numbered prefixes
(`01-`, `02-`, …) when ordering matters (e.g., a later patch depends
on a path the earlier patch added). Most patches are independent and
the prefix is just a sort key.

## Patch file format

Each `.patch` file is a unified diff (the format `git diff` and
`diff -u` emit) plus a comment header. `patch(1)` applies them with
`-p1` from the repo root.

```diff
# Patch: publisher_domains compact form on publisher-property-selector
# Reason: forward-looking add — the SDK's dict-layer helpers
#   (validate_publisher_properties_item, _fanout_publisher_properties)
#   implement the contract today; this patch restores the field on
#   the Pydantic-model layer so adopters get parity.
# Filed: PR #753
# Upstream status: landed in 3.1.0-beta.x (not 3.0.x); SDK is pinned to
#   3.0.x via ADCP_VERSION, so the field stays patched until the SDK
#   moves to a 3.1 floor.
# Drop when: SDK pins to 3.1.x AND upstream 3.1 ships the same shape.

--- a/schemas/cache/3.0/core/publisher-property-selector.json
+++ b/schemas/cache/3.0/core/publisher-property-selector.json
@@ -...
```

The header is plain comment lines starting with `#`. The unified-diff
body starts with `--- a/` / `+++ b/` paths relative to the repo root.

## Lifecycle of a patch

A patch is **alive** when it applies cleanly against the regenerated
upstream cache. The sync script applies it and continues.

A patch is **dead** when it reverse-applies cleanly — meaning the
upstream cache already contains the patch's target shape. Upstream
landed the field. The sync script fails loudly with the patch name
and the directive to delete the file with a documented rationale.
Silently no-op'ing here would let a stale `.patch` linger in the
directory forever.

A patch is **broken** when neither forward- nor reverse-application
works. Upstream restructured the file in a way the patch can't
follow. The sync script fails loudly with the patch name and the
operator must either update the patch hunks against the new shape or
delete the patch outright with a documented rationale (e.g. "upstream
removed this surface; SDK helpers also removed").

## Adding a new patch

1. Make the hand-edit on `schemas/cache/{bundle_key}/<file>.json`
   locally.
2. Run `git diff schemas/cache/{bundle_key}/<file>.json > schemas/patches/NN-name.patch`.
3. Edit the patch file to add a header (Patch / Reason / Filed /
   Upstream status / Drop when).
4. Stage both the patch file and the patched cache file.
5. CI's `check-schema-drift` will run `sync_schemas.py` (which now
   includes patch application) and confirm the patched cache matches
   the checked-in shape.

The cache file lives in the working tree alongside the patch because
adopters who don't run regen-with-patches still need a functional
cache. The patch is the audit-trail of how it got that way.

## Removing a dead patch

When the sync script reports "Patch X is dead — upstream landed this
change":

1. Delete the `.patch` file with a commit message referencing the
   upstream version that landed the feature.
2. Run `make regenerate-schemas` to confirm the cache now matches
   upstream-verbatim with no patch applied.
3. If consumer code (Pydantic models, dict helpers, tests) depended
   on the pre-upstream shape, fold those updates into the same
   commit or a follow-up.

## Anti-patterns

- **Don't edit `schemas/cache/` without a corresponding `.patch`
  file.** Next regen overwrites the edit. The infrastructure exists
  precisely to make this impossible to forget.
- **Don't bundle multiple unrelated diffs in one `.patch` file.**
  One file per logical change. Numbered prefixes order them.
- **Don't omit the `Drop when:` line.** A patch with no exit criterion
  becomes permanent technical debt. If you genuinely can't articulate
  when this would be removable, you probably shouldn't be patching
  upstream at all.
