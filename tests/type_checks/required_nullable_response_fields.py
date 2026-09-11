"""Adopter pattern: read and set a required-and-nullable response field.

Regression for #1137. ``media-buy/create-media-buy-response.json`` lists
``confirmed_at`` in the success branch's ``required`` while typing it
``["string", "null"]`` — the key must be present and may be null. The custom
response emitter in ``scripts/post_generate_fixes.py`` used to read
``required`` as "not Optional", so the generated annotation was a bare
``AwareDatetime`` and adopters had to widen it on their own subclass with a
``# type: ignore[assignment]`` Liskov suppression.

Both surfaces an adopter can reach are pinned here:

* ``adcp.types.CreateMediaBuySuccessResponse`` — the public canonical alias.
  It is built at runtime by ``_canonical_clone``, so type checkers read it
  from the hand-maintained ``canonical_creative.pyi`` stub; the stub has to
  declare the field for the contract to be visible statically at all.
* ``adcp.types.legacy.LegacyCreateMediaBuyResponse1`` — the raw generated wire
  model the stub mirrors, reached through the legacy facade.
"""

from __future__ import annotations

from datetime import datetime

from typing_extensions import assert_type

from adcp.types import CreateMediaBuySuccessResponse
from adcp.types.legacy import LegacyCreateMediaBuyResponse1

# --- Public canonical alias ---


def public_commitment_instant(resp: CreateMediaBuySuccessResponse) -> datetime | None:
    """A provisional buy reports no commitment instant — ``None`` is legal."""
    assert_type(resp.confirmed_at, datetime | None)
    return resp.confirmed_at


def build_provisional_buy() -> CreateMediaBuySuccessResponse:
    """``confirmed_at=None`` is a valid constructor argument, not an error."""
    return CreateMediaBuySuccessResponse(
        media_buy_id="mb_1",
        status="completed",
        confirmed_at=None,
        revision=1,
        packages=[],
    )


def build_committed_buy(confirmed_at: datetime) -> CreateMediaBuySuccessResponse:
    """A real commitment timestamp still satisfies the same parameter."""
    return CreateMediaBuySuccessResponse(
        media_buy_id="mb_1",
        status="completed",
        confirmed_at=confirmed_at,
        revision=1,
        packages=[],
    )


def public_has_committed(resp: CreateMediaBuySuccessResponse) -> bool:
    """Narrowing still works, so committed buys keep a non-optional datetime."""
    confirmed_at = resp.confirmed_at
    if confirmed_at is None:
        return False
    assert_type(confirmed_at, datetime)
    return confirmed_at <= datetime.now(tz=confirmed_at.tzinfo)


# --- Generated wire model behind the canonical alias ---


def wire_commitment_instant(resp: LegacyCreateMediaBuyResponse1) -> datetime | None:
    """The generated model carries the same required-and-nullable contract."""
    assert_type(resp.confirmed_at, datetime | None)
    return resp.confirmed_at


def wire_required_sibling_stays_non_optional(resp: LegacyCreateMediaBuyResponse1) -> str:
    """Required-and-non-nullable siblings are unaffected by the widening."""
    assert_type(resp.media_buy_id, str)
    return resp.media_buy_id
