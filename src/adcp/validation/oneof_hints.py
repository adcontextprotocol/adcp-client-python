"""Heuristic ``hint`` strings for ``oneOf`` near-miss validation failures.

When a payload fails a discriminated-union (``oneOf``) shape because the
caller used the wrong key as the discriminator (the v3 ref seller
``pricing_options`` regression: ``{"type": "cpm", ...}`` instead of
``{"pricing_model": "cpm", ...}``), the standard jsonschema diagnostic is
``"<value> is not valid under any of the given schemas"`` — accurate but
unactionable for an LLM client.

This module computes an additive ``hint`` string that names the closest
matching variant and the wrong / expected discriminator keys:

    Looks like you may have meant the 'cpm' variant. Use 'pricing_model'
    instead of 'type' as the discriminator.

The hint is best-effort: if no clear winner exists across variants, no
hint is emitted (silent is better than misleading).
"""

from __future__ import annotations

from typing import Any


def _navigate(schema: Any, path_segments: list[Any]) -> Any | None:
    """Walk ``schema`` along ``path_segments`` (jsonschema absolute_schema_path).

    Returns the sub-schema at the path or ``None`` if any segment misses.
    """
    node: Any = schema
    for seg in path_segments:
        if isinstance(node, dict):
            if seg in node:
                node = node[seg]
                continue
            # jsonschema sometimes emits int-as-string segments
            if isinstance(seg, int) and str(seg) in node:
                node = node[str(seg)]
                continue
            return None
        if isinstance(node, list):
            try:
                node = node[int(seg)]
                continue
            except (ValueError, IndexError, TypeError):
                return None
        return None
    return node


def _navigate_input(payload: Any, path_segments: list[Any]) -> Any | None:
    """Walk the request/response payload along an instance path."""
    node: Any = payload
    for seg in path_segments:
        if isinstance(node, dict):
            if seg in node:
                node = node[seg]
                continue
            return None
        if isinstance(node, list):
            try:
                node = node[int(seg)]
                continue
            except (ValueError, IndexError, TypeError):
                return None
        return None
    return node


def _detect_discriminator(variants: list[dict[str, Any]]) -> str | None:
    """Identify the discriminator field across ``oneOf`` variants.

    A field qualifies when at least two variants pin it to a literal
    ``const``. Ties broken by the field with the most variants pinning
    it; further ties broken by lexical order so the result is stable.

    The ``count >= 2`` floor distinguishes a real discriminator (a key
    that genuinely partitions the union) from incidental ``const``
    pinning on a single variant. A union with only one variant pinning
    a field is not discriminated by that field — applying near-miss
    heuristics there would just guess.
    """
    counts: dict[str, int] = {}
    for variant in variants:
        props = variant.get("properties")
        if not isinstance(props, dict):
            continue
        for field_name, field_schema in props.items():
            if isinstance(field_schema, dict) and "const" in field_schema:
                counts[field_name] = counts.get(field_name, 0) + 1
    if not counts:
        return None
    # Pick the field pinned by the most variants (>=2 to be a real discriminator).
    best = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    field_name, count = best[0]
    if count < 2:
        return None
    return field_name


def _variant_const_value(variant: dict[str, Any], field: str) -> Any | None:
    props = variant.get("properties")
    if not isinstance(props, dict):
        return None
    field_schema = props.get(field)
    if not isinstance(field_schema, dict):
        return None
    return field_schema.get("const")


def _score_variant(
    variant: dict[str, Any],
    value: dict[str, Any],
    discriminator: str | None = None,
) -> tuple[int, int, int, str | None]:
    """Score how close ``value`` is to a ``oneOf`` variant.

    Returns ``(const_match, required_present, total_present, seen_key)`` where:

    * ``const_match`` — 1 when the variant's discriminator ``const``
      value appears as the value of some top-level key in the payload
      *other than* the expected discriminator. Strongest signal: the
      caller picked this variant by value but used the wrong key
      (the v3 ref-seller ``pricing_options`` regression).
    * ``required_present`` — count of the variant's ``required`` fields
      present in ``value``. The variant the caller most nearly hit by
      shape.
    * ``total_present`` — count of the variant's declared ``properties``
      present in ``value``. Tiebreaker.
    * ``seen_key`` — the top-level key that carried the matching
      ``const`` value, if any. Recorded so the hint can name the exact
      key the caller misused rather than guessing later.

    The exact-pairing requirement (``key != discriminator AND
    val == const_value``) replaces a membership scan against
    ``value.values()``. The looser scan would mark a const_match when
    an unrelated field happened to carry the same scalar (e.g., a
    variant pinning ``"type": "object"`` matching a payload's
    ``"label": "object"``), producing a misleading hint.
    """
    required = variant.get("required") or []
    if not isinstance(required, list):
        required = []
    required_present = sum(1 for r in required if isinstance(r, str) and r in value)

    properties = variant.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    total_present = sum(1 for p in properties if p in value)

    const_match = 0
    seen_key: str | None = None
    if discriminator is not None:
        const_value = _variant_const_value(variant, discriminator)
        if const_value is not None:
            for key, val in value.items():
                if key == discriminator:
                    continue
                if val == const_value:
                    const_match = 1
                    seen_key = key
                    break

    return const_match, required_present, total_present, seen_key


def _fallback_seen_key(
    value: dict[str, Any],
    expected_discriminator: str,
    variants: list[dict[str, Any]],
) -> str | None:
    """Pick a likely "wrong discriminator" key when no const_match was found.

    Used only when the variant's score did not record a ``seen_key``
    via exact (key, val) pairing — i.e., the caller didn't carry the
    expected variant's ``const`` value at all. In that case we fall
    back to the first top-level key that isn't declared by any variant
    (an extraneous key, plausibly the caller's misnamed discriminator).
    """
    declared: set[str] = set()
    for variant in variants:
        props = variant.get("properties")
        if isinstance(props, dict):
            declared.update(props.keys())

    for key in value:
        if key == expected_discriminator:
            continue
        if key not in declared:
            return key

    return None


def compute_oneof_hint(
    schema: dict[str, Any],
    schema_path_segments: list[Any],
    instance_path_segments: list[Any],
    payload: Any,
) -> str | None:
    """Compute a near-miss hint for an ``oneOf`` failure.

    Args:
        schema: The compiled validator's root schema (refs already inlined).
        schema_path_segments: ``absolute_schema_path`` from the validation
            error — points at the ``oneOf`` keyword.
        instance_path_segments: ``absolute_path`` from the validation
            error — points at the offending value in the payload.
        payload: The full request/response payload that failed validation.

    Returns the hint string, or ``None`` if the heuristic can't pick a
    clear winner (no detectable discriminator, no clear best variant,
    or no obvious wrong-discriminator key).
    """
    if not schema_path_segments or schema_path_segments[-1] != "oneOf":
        return None

    parent = _navigate(schema, list(schema_path_segments[:-1]))
    if not isinstance(parent, dict):
        return None
    variants_raw = parent.get("oneOf")
    if not isinstance(variants_raw, list) or len(variants_raw) < 2:
        return None
    variants: list[dict[str, Any]] = [v for v in variants_raw if isinstance(v, dict)]
    if len(variants) < 2:
        return None

    value = _navigate_input(payload, list(instance_path_segments))
    if not isinstance(value, dict):
        return None

    discriminator = _detect_discriminator(variants)
    if discriminator is None:
        return None

    # Skip the hint when the caller already used the right discriminator
    # — they merely picked a value that doesn't match any variant. The
    # default "value not in allowed enum" message is more accurate there.
    if discriminator in value:
        return None

    scored = [(_score_variant(v, value, discriminator), idx, v) for idx, v in enumerate(variants)]
    # Sort by const_match (strongest), then required_present, then total_present.
    # seen_key (index 3 of the score tuple) is metadata, not a ranking signal.
    scored.sort(key=lambda s: (-s[0][0], -s[0][1], -s[0][2], s[1]))

    best_score, _, best_variant = scored[0]
    if len(scored) > 1:
        runner_up = scored[1][0]
        # Compare the ranking signals only — ignore seen_key.
        if best_score[:3] == runner_up[:3]:
            # No clear winner; silent rather than misleading.
            return None

    if best_score[:3] == (0, 0, 0):
        return None

    expected_const = _variant_const_value(best_variant, discriminator)
    if expected_const is None:
        return None

    # Prefer the seen_key recorded during scoring (exact key/value match
    # against the winning variant's const). Fall back to "extraneous
    # top-level key" only when no const-carrying key was found.
    seen_key = best_score[3] or _fallback_seen_key(value, discriminator, variants)
    if seen_key is None:
        return None

    return (
        f"Looks like you may have meant the {expected_const!r} variant. "
        f"Use {discriminator!r} instead of {seen_key!r} as the discriminator."
    )
