"""Narrow pydantic discriminated-union ValidationErrors to the
variant the user actually intended.

Background (Stability AI Emma backend test, verdict 5/10): when an
adopter constructs a ``CreativeManifest`` whose ``assets`` value
matches one variant of a discriminated union but is missing fields
required by THAT variant, pydantic 2 reports validation errors for
EVERY variant in the union (13+ for asset content types). The error
dump runs 60+ lines and obscures the actual problem (a single
missing field on the variant the user picked).

This module exposes :func:`narrow_union_errors` which post-processes
``ValidationError.errors()`` to keep only the errors from the
"closest fit" variant — the one whose discriminator matched OR the
one with the fewest non-discriminator errors. The result is a
focused error pointing at the user's actual mistake.

Used by:

* :func:`adcp.server.mcp_tools.create_tool_caller` — narrows
  wire-side ``INVALID_REQUEST`` errors automatically.
* Adopter code via :func:`narrow_validation_error` (manual) — for
  adopters who construct typed models in their platform method
  bodies and want the same friendlier error UX.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

#: A pydantic CORE-SCHEMA label, which is the other way pydantic spells a union
#: member in ``loc``. A member that is a plain model is spelled by its class
#: name; a member that is wrapped or composed is spelled by its core schema --
#: ``tagged-union[ImageAsset,VideoAsset]``,
#: ``function-after[_require_schema_required_group(), WebhookChallengeResponseToken]``,
#: ``nullable[...]``, ``definitions[...]``.
#:
#: Matched structurally rather than by a list of prefixes: every such label is
#: a lowercase-hyphenated schema kind followed by a bracketed body, and no field
#: name in this protocol's schemas has that shape.
_CORE_SCHEMA_LABEL = re.compile(r"^[a-z][a-z0-9-]*\[.*\]$")


# Heuristic: a "variant" location segment names a union MEMBER, not a document
# key. Pydantic emits ``("assets", "hero", "ImageAsset", "width")`` for
# union-validation errors. The variant name is the
# second-to-last position when the error is on a field, OR the
# last position when the error is at the variant itself
# (e.g. ``union_tag_not_found``).
def _looks_like_variant_name(segment: Any) -> bool:
    """Whether this ``loc`` segment names a union member rather than a key.

    Two spellings, because pydantic has two. A member that is a plain model is
    its class name: CamelCase, no underscore. A member that is wrapped or
    composed is a core-schema expression.

    Only the first was recognised, and the module's own docstring already noted
    the consequence for the second -- "an inline tagged union spells its branch
    in a form no variant-name heuristic recognizes". Branch classes made that
    hole load-bearing: a split root's members are
    ``function-after[_require_schema_required_group(), <Branch>]``, so every
    error under one carried a Python validator's name into the buyer-facing
    field path.
    """
    if not isinstance(segment, str):
        return False
    if not segment:
        return False
    if _CORE_SCHEMA_LABEL.match(segment):
        return True
    # Class names start with an uppercase letter and contain only
    # alphanumerics. Reject ``snake_case`` field names.
    return segment[0].isupper() and segment.replace("_", "").isalnum() and "_" not in segment


def without_schema_labels(loc: Iterable[Any]) -> tuple[Any, ...]:
    """``loc`` without pydantic's CORE-SCHEMA labels, keeping variant names.

    The two kinds of member label are not equally useful. A CamelCase class
    name identifies which variant an error came from, which is what the
    narrowing below groups by and what a caller reading
    ``details.validation_errors`` can act on. A core-schema expression --
    ``function-after[_require_schema_required_group(), <Branch>]`` -- is
    pydantic's internal spelling of a wrapper around that member and names a
    repo-private validator; it is never useful and must not reach a buyer.

    So this drops the second and keeps the first.

    A projection dropping BOTH lived here for a day, applied at the four places
    that join a ``loc`` into a buyer-facing field path. It was the wrong fix and
    is deleted: the element it was dropping came from the union a split root
    installs, and ``_conditions.name_the_combinator`` now reports ONE branch's
    errors with the label already gone, so there is nothing left downstream to
    recognise. A filter there could not have been right -- it decided membership
    by SPELLING, treating any CamelCase segment as a member label, so a
    legitimate CamelCase key in the buyer's document would have been deleted
    from the path, and a genuine union's variant name -- the one thing a label
    is good for -- was deleted from one.
    """
    return tuple(
        segment
        for segment in loc
        if not (isinstance(segment, str) and _CORE_SCHEMA_LABEL.match(segment))
    )


def _split_at_variant(
    loc: tuple[Any, ...],
) -> tuple[tuple[Any, ...], str, tuple[Any, ...]] | None:
    """Split a loc tuple at the LAST variant-name segment.

    Returns ``(prefix_before_variant, variant_name, suffix_after_variant)``
    or ``None`` if no variant segment is found. Used to group
    union-validation errors by their containing field path + variant.

    The LAST variant segment (innermost) is the one whose error
    mattered. For nested unions
    (``Union[Outer[Union[A, B]], C]``) pydantic emits
    ``("field", "Outer", "inner", "A", "subfield")`` — splitting at
    the FIRST variant segment ("Outer") would collapse "A" and "B"
    into one bucket; splitting at the LAST ("A") correctly groups by
    innermost variant.

    Example::

        loc = ("assets", "hero", "ImageAsset", "width")
        → (("assets", "hero"), "ImageAsset", ("width",))

        loc = ("field", "Outer", "inner", "A", "subfield")
        → (("field", "Outer", "inner"), "A", ("subfield",))
    """
    last_variant_idx: int | None = None
    for i, segment in enumerate(loc):
        if _looks_like_variant_name(segment):
            last_variant_idx = i
    if last_variant_idx is None:
        return None
    variant = loc[last_variant_idx]
    assert isinstance(variant, str)  # _looks_like_variant_name guarantees this
    return loc[:last_variant_idx], variant, loc[last_variant_idx + 1 :]


# There is deliberately NO cap on input size.
#
# One was here -- 500 errors, above which the input was returned UNCHANGED --
# on the reasoning that an attacker "shouldn't get to amplify CPU through O(N)
# bucketing logic". Two things were wrong with it.
#
# It made quality degrade exactly when error volume is highest, which is when a
# buyer can least afford a firehose: above the cap the wholesale-refusal error
# and every branch's duplicate reached the response untouched. And the cap was
# sized before branch classes multiplied every error by the branch count, so a
# legitimate 501-element array produced 1019 errors and silently crossed it.
#
# The premise does not survive measurement either. Narrowing is linear with a
# ~4-5us constant, flat from 300 to 150_000 errors (630ms at 150_000), so there
# is no amplification to guard against -- and pydantic has already done strictly
# more work to PRODUCE the errors than narrowing does to process them. A bound
# on total validation work belongs where the errors are generated, not as a
# correctness cliff here.


def _asset_variant_index(loc: tuple[Any, ...]) -> int | None:
    """Index of the asset-variant arm in ``loc``, or ``None``.

    Pydantic spells that arm by the wrapper class name when the union is
    wrapped (``"AssetVariant"``) and by its members when the union is inline
    (``"tagged-union[ImageAsset,VideoAsset,...]"``). Both name the same arm,
    and an error under it beats the sibling open-asset arm.
    """
    for index, segment in enumerate(loc):
        if segment == "AssetVariant" or (
            isinstance(segment, str) and segment.startswith("tagged-union[")
        ):
            return index
    return None


def _narrow_union_errors(
    errors: Any,
) -> list[Any]:
    """Return a focused subset of ``errors`` for discriminated-union
    failures.

    For each (parent_loc) where multiple variant errors exist, pick
    the "best fit" variant by:

    1. **Discriminator match**: variants with no ``literal_error``,
       ``union_tag_not_found``, or ``union_tag_invalid`` had their
       discriminator value match the user's input. Keep ONLY their
       errors.
    2. **Fewest non-discriminator errors**: if no clear discriminator
       winner, the variant with the smallest count of non-literal
       errors is the closest fit. Keep ONLY its errors.

    Errors that aren't part of a union failure (no variant in their
    ``loc``) pass through unchanged. The function never returns an
    empty list when the input is non-empty — the worst case falls
    back to the input.

    **Edge case** (residual): pydantic's ``literal_error`` type fires
    on ANY ``Literal[...]`` field mismatch, not just the discriminator.
    A user input that hits a non-discriminator literal mismatch on the
    matched variant (e.g., correct ``asset_type`` but wrong
    ``codec``) will eliminate the matched variant from step 1 and the
    fallback may pick a wrong variant. The narrowing reduces noise
    even in this case but may surface the wrong variant's errors.
    Resolving this requires knowing the discriminator field name,
    which the heuristic doesn't have access to. Schema-level fix
    (``Annotated[Union[...], Field(discriminator=...)]``) avoids the
    issue entirely; tracked as a follow-up.

    Mirrors the JS-side ``narrowUnionValidationErrors`` (when ported).
    """
    if not errors:
        return []

    errors_list = list(errors)
    # Bucket errors by (prefix_before_variant) — every error sharing
    # the same prefix is contending for the same logical slot, and
    # different errors in the same bucket are different variants of
    # the same union.
    buckets: dict[tuple[Any, ...], list[tuple[str, Any]]] = {}
    passthrough: list[Any] = []
    asset_variant_prefixes: set[tuple[Any, ...]] = set()

    for err in errors_list:
        loc = tuple(err.get("loc", ()))
        index = _asset_variant_index(loc)
        if index is not None:
            asset_variant_prefixes.add(loc[:index])

    for err in errors_list:
        loc = tuple(err.get("loc", ()))
        if "Assets" in loc and loc[: loc.index("Assets")] in asset_variant_prefixes:
            continue
        split = _split_at_variant(loc)
        if split is None:
            passthrough.append(err)
            continue
        prefix, variant, _suffix = split
        buckets.setdefault(prefix, []).append((variant, err))

    if not buckets:
        # Nothing to narrow by variant name. Return what survived the
        # asset-variant drop above, which is already the focused set; an
        # inline tagged union spells its arm in a form no variant-name
        # heuristic recognizes, and falling back to the input here would put
        # the dropped sibling arm back.
        return [dict(err) for err in passthrough] if passthrough else errors_list

    # Defensive copy of the dicts we're about to surface — the caller
    # might mutate the returned list and we don't want that to leak
    # back into the input. ``dict(err)`` is shallow which is fine:
    # ``loc`` is a tuple (immutable), and other values are scalars or
    # nested dicts pydantic doesn't share across errors.
    narrowed: list[Any] = [dict(err) for err in passthrough]
    for _prefix, variant_errors in buckets.items():
        # Group by variant name within this bucket.
        per_variant: dict[str, list[Any]] = {}
        for variant, err in variant_errors:
            per_variant.setdefault(variant, []).append(err)

        if len(per_variant) <= 1:
            # Only one variant in this bucket — no narrowing needed.
            for errs in per_variant.values():
                narrowed.extend(dict(e) for e in errs)
            continue

        winner = _pick_winning_variant(per_variant)
        if winner is None:
            # Couldn't disambiguate; fall back to all variants for
            # this bucket so the adopter doesn't lose information.
            for errs in per_variant.values():
                narrowed.extend(dict(e) for e in errs)
            continue
        narrowed.extend(dict(e) for e in per_variant[winner])

    return narrowed


def narrow_union_errors(errors: Any) -> list[Any]:
    """:func:`_narrow_union_errors`, with pydantic's core-schema labels removed.

    The projection is on the way OUT, not on the way in. Stripping at entry was
    tried and is wrong: the narrowing buckets errors BY their variant label, so
    removing a label first collapses three branches into one bucket with nothing
    to group by -- and a buyer then receives every branch's complaint instead of
    the one branch that got furthest. Measured on a ``create_media_buy`` request
    with a bad ``geo_countries`` entry: five errors where one is wanted.

    So the labels survive the narrowing and are dropped from what it returns:
    one place, after the one consumer of them has run.
    """
    return [
        dict(err, loc=without_schema_labels(err.get("loc", ()))) if isinstance(err, dict) else err
        for err in _narrow_union_errors(errors)
    ]


#: Pydantic-2 error types that signal "this variant's discriminator
#: didn't match the user's input". A variant whose error list contains
#: ANY of these is eliminated from step 1's candidate pool.
#:
#: * ``literal_error`` — a ``Literal[...]`` field rejected the value.
#:   Discriminators are typically Literal-typed (``asset_type:
#:   Literal["image"]``); a mismatch here means this variant isn't
#:   the user's intent.
#: * ``union_tag_not_found`` — a NESTED tagged union inside this
#:   variant couldn't be narrowed to any of ITS sub-variants. Means
#:   the user's input doesn't fit this variant's shape at all.
#: * ``union_tag_invalid`` — pydantic-2's "tag found but invalid for
#:   this union" code. Same semantic as ``union_tag_not_found`` for
#:   our purposes.
#: Error types that say "this variant wants a field the document does not
#: carry". A variant failing only these has not been reached at all; a sibling
#: with any other error type has been, and is the one worth reporting.
_REQUIREDNESS_TYPES = frozenset({"missing", "missing_argument", "missing_keyword_only_argument"})

_DISCRIMINATOR_MISMATCH_TYPES = frozenset(
    {"literal_error", "union_tag_not_found", "union_tag_invalid"}
)


def _document_depth(errs: list[Any]) -> int:
    """How far into the buyer's document this variant's errors reach.

    Depth is over :func:`without_schema_labels`, not raw ``loc``, so pydantic's
    core-schema expression for a wrapped member is not counted as a step.

    It was written over a ``document_path`` that dropped CamelCase variant names
    too. That projection is deleted -- see :func:`without_schema_labels`, which
    records why: it decided membership by SPELLING, so it would delete a
    legitimate CamelCase key from the buyer's path. The depth metric does not
    need it. A split root's branch errors arrive with the label already gone,
    and in a genuine union every variant carries one, so a surviving variant
    name adds the same single step to every candidate and cancels in the
    comparison this feeds.
    """
    return max((len(without_schema_labels(e.get("loc", ()))) for e in errs), default=0)


def _pick_winning_variant(
    per_variant: dict[str, list[Any]],
) -> str | None:
    """Return the variant name whose errors are the closest fit.

    Strategy (in order):

    1. **Discriminator match**: variants with ZERO discriminator-mismatch
       errors (see :data:`_DISCRIMINATOR_MISMATCH_TYPES`) had their
       discriminator value match the user's input. If exactly one
       such variant exists, it's the winner. This is the Stability AI
       / AudioStack 60-line-dump fix — when the user provides
       ``asset_type='image'`` and ImageAsset's other fields fail, we
       surface ImageAsset errors only.
    1b. **Past requiredness among those**: a variant whose every error is a
       missing required field loses to a sibling that got past its own
       required fields and failed on content.
    2. **Fewest errors among matched**: if multiple variants matched
       the discriminator, pick the one with fewest errors (closest
       fit to user's input shape).
    3. **Fallback to fewest errors overall**: if NO variant matched
       the discriminator (the user provided an invalid discriminator
       value, e.g. ``asset_type='image_asset'`` instead of ``'image'``),
       pick the variant with fewest non-literal errors as a closest-
       fit guess.
    4. **Tie**: return ``None`` so the caller passes through all
       errors — adopter can disambiguate manually.
    """
    if not per_variant:
        return None

    matched = {
        variant: errs
        for variant, errs in per_variant.items()
        if not any(e.get("type") in _DISCRIMINATOR_MISMATCH_TYPES for e in errs)
    }

    if matched:
        if len(matched) == 1:
            return next(iter(matched.keys()))
        # Requiredness, among the variants the discriminator admitted. A
        # variant that failed only because the document lacks a field IT
        # requires has not been reached; a sibling that got past its own
        # required fields and failed on CONTENT is the one a caller can act on.
        #
        # This is the rule the union's wrapping validator already applies to
        # the MESSAGE -- relabel to the combinator only when EVERY branch
        # reports `missing` -- stated here for the error LIST so the two agree.
        # Branch classes made it load-bearing: a split root's branches differ
        # by requiredness and nothing else, so the discriminator rules cannot
        # separate them and every branch's complaint reached the buyer.
        #
        # It runs AFTER the discriminator match, not before. Before, it
        # overrode a matched discriminator, which is stronger evidence --
        # measured as three regressions in this module's own tests.
        past_requiredness = {
            variant: errs
            for variant, errs in matched.items()
            if any(e.get("type") not in _REQUIREDNESS_TYPES for e in errs)
        }
        if past_requiredness and len(past_requiredness) < len(matched):
            if len(past_requiredness) == 1:
                return next(iter(past_requiredness))
            matched = past_requiredness
        # How far into the DOCUMENT each variant got, before counting errors.
        #
        # A variant that refuses the document wholesale reports at the root:
        # one `value_error` saying the shape is not its own. A variant that
        # accepted the shape and failed on a field reports at a deep path. So
        # depth measures how far a branch actually read, which is what "closest
        # fit" means -- whereas the error COUNT measures terseness, which a
        # wholesale refusal maximises precisely by not looking at the shape.
        #
        # Measured on create_media_buy with a bad geo_countries entry: the
        # branch refusing `packages` outright reports 1 error at depth 0, and
        # the two branches carrying the real string_type error report 8 and 10
        # at depth 5. Counting errors handed the buyer the root; depth does not.
        #
        # Depth is read through `without_schema_labels`, so pydantic's own
        # core-schema expression for a wrapped member never counts as a step,
        # and nothing here knows the name of any validator -- a name-keyed rule
        # would die on the next rename.
        deepest = max(_document_depth(errs) for errs in matched.values())
        reached = {
            variant: errs for variant, errs in matched.items() if _document_depth(errs) == deepest
        }
        if len(reached) == 1:
            return next(iter(reached))
        matched = reached
        # Multiple still matched (rare — would mean two variants share the
        # same discriminator literal). Pick the one with fewest
        # errors.
        sorted_matched = sorted(matched.items(), key=lambda kv: len(kv[1]))
        if len(sorted_matched) >= 2 and len(sorted_matched[0][1]) == len(sorted_matched[1][1]):
            return None  # tie
        return sorted_matched[0][0]

    # Step 3: no discriminator match — pick the variant with fewest
    # non-literal errors. That's the closest-fit guess for an adopter
    # who used an invalid discriminator.
    def _non_literal_score(item: tuple[str, list[Any]]) -> int:
        _variant, errs = item
        return sum(1 for e in errs if e.get("type") != "literal_error")

    sorted_all = sorted(per_variant.items(), key=_non_literal_score)
    if len(sorted_all) >= 2 and _non_literal_score(sorted_all[0]) == _non_literal_score(
        sorted_all[1]
    ):
        return None  # tie — don't guess
    return sorted_all[0][0]


__all__ = [
    "without_schema_labels",
    "narrow_union_errors",
]
