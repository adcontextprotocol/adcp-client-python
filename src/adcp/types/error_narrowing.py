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

from typing import Any


# Heuristic: a "variant" location segment is a class name.
# Pydantic emits ``("assets", "hero", "ImageAsset", "width")`` for
# union-validation errors. The variant name is the
# second-to-last position when the error is on a field, OR the
# last position when the error is at the variant itself
# (e.g. ``union_tag_not_found``).
def _looks_like_variant_name(segment: Any) -> bool:
    """Heuristic: a Python class name (CamelCase, starts with capital).

    Used to detect variant segments in ``ValidationError.errors()[i].loc``.
    Pydantic interleaves variant class names into the loc tuple for
    discriminated-union failures; we strip those segments to identify
    "this error belongs to variant X."
    """
    if not isinstance(segment, str):
        return False
    if not segment:
        return False
    # Class names start with an uppercase letter and contain only
    # alphanumerics. Reject ``snake_case`` field names.
    return segment[0].isupper() and segment.replace("_", "").isalnum() and "_" not in segment


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


#: Cap on input list size. Beyond this we pass through unchanged —
#: narrowing is a UX feature, not correctness, and an attacker
#: submitting a request that triggers thousands of validation errors
#: shouldn't get to amplify CPU through O(N) bucketing logic. The cap
#: is generous enough that genuine union dumps (~30 errors for a 13-
#: variant asset union) never hit it.
_MAX_NARROW_INPUT_SIZE = 500


def narrow_union_errors(
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
    # DoS guard: don't process pathologically-large inputs. Below the
    # cap, narrowing helps. Above it, we're either in a hostile
    # request or a legitimately massive schema; either way, the
    # narrowing UX win doesn't justify the CPU.
    if len(errors_list) > _MAX_NARROW_INPUT_SIZE:
        return errors_list

    # Bucket errors by (prefix_before_variant) — every error sharing
    # the same prefix is contending for the same logical slot, and
    # different errors in the same bucket are different variants of
    # the same union.
    buckets: dict[tuple[Any, ...], list[tuple[str, Any]]] = {}
    passthrough: list[Any] = []

    for err in errors_list:
        loc = tuple(err.get("loc", ()))
        split = _split_at_variant(loc)
        if split is None:
            passthrough.append(err)
            continue
        prefix, variant, _suffix = split
        buckets.setdefault(prefix, []).append((variant, err))

    if not buckets:
        return errors_list

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
_DISCRIMINATOR_MISMATCH_TYPES = frozenset(
    {"literal_error", "union_tag_not_found", "union_tag_invalid"}
)


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
        # Multiple matched (rare — would mean two variants share the
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
    "narrow_union_errors",
]
