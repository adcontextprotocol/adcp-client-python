"""Adopter pattern: gate a creative format using a reference off a model field.

Generated model fields carry ``FormatReferenceStructuredObject``. An adopter
holding one — here ``FormatCard.format_id`` — passes it straight to the
canonical-format helpers under ``mypy --strict``, with no cast and no
hand-built legacy tuple.
"""

from __future__ import annotations

from adcp import format_is_supported
from adcp.canonical_formats import formats_are_equivalent, upgrade_legacy_format_id
from adcp.types import FormatCard


def card_is_supported(card: FormatCard, offered: str) -> bool:
    return format_is_supported(card.format_id, offered)


def card_matches_family(card: FormatCard, other: FormatCard) -> bool:
    return formats_are_equivalent(card.format_id, other.format_id)


def card_format_template(card: FormatCard) -> str:
    return upgrade_legacy_format_id(card.format_id).id


CARD = FormatCard.model_validate(
    {
        "format_id": {
            "agent_url": "https://creative.adcontextprotocol.org",
            "id": "display_image",
            "width": 300,
            "height": 250,
        },
        "manifest": {},
    }
)

assert card_is_supported(CARD, "display_300x250")
assert card_matches_family(CARD, CARD)
assert card_format_template(CARD) == "display_image"
