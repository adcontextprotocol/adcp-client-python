"""Adopter pattern: construct every public ``AuthorizedAgents`` variant.

``adcp.types._generated`` rebinds the whole ``AuthorizedAgents*`` window at
import time (``AuthorizedAgents = AuthorizedAgents1``, ``AuthorizedAgents1 =
AuthorizedAgents2``, ...) so the historical adagents variant numbering keeps
working. mypy keeps the *pre-rebind* declaration for each rebound name, so
re-exporting the public surface from there handed adopters a runtime object of
one variant behind the static type of its neighbour: bare ``AuthorizedAgents``
resolved to the obsolete aggregate ``RootModel`` and rejected every documented
constructor keyword, and each semantic alias was typed one variant off.

The public surface now sources the six variants from the generated module
directly, so this file pins both halves of the contract: the documented
constructor keywords type-check, and ``authorization_type`` — the
discriminator that tells the variants apart — infers the right ``Literal`` for
each symbol. Under the old shifted binding every ``assert_type`` below fails.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AnyUrl, TypeAdapter
from typing_extensions import assert_type

from adcp.types import (
    AuthorizedAgent,
    AuthorizedAgents,
    AuthorizedAgentsByInlineProperties,
    AuthorizedAgentsByPropertyId,
    AuthorizedAgentsByPropertyTag,
    AuthorizedAgentsByPublisherProperties,
    AuthorizedAgentsBySignalId,
    AuthorizedAgentsBySignalTag,
    Property,
    PropertyId,
    PropertyTag,
)

AGENT_URL = AnyUrl("https://agent.example.com")

# --- Bare ``AuthorizedAgents`` is the ``property_ids`` variant ---

bare = AuthorizedAgents(
    authorization_type="property_ids",
    authorized_for="Premium display inventory",
    property_ids=[PropertyId("homepage"), PropertyId("sports")],
    url=AGENT_URL,
)
assert_type(bare.authorization_type, Literal["property_ids"])
assert_type(bare.property_ids, list[PropertyId])

# --- The six semantic aliases ---

by_property_id = AuthorizedAgentsByPropertyId(
    authorization_type="property_ids",
    authorized_for="Premium display inventory",
    property_ids=[PropertyId("homepage")],
    url=AGENT_URL,
)
assert_type(by_property_id.authorization_type, Literal["property_ids"])
assert_type(by_property_id.property_ids, list[PropertyId])

by_property_tag = AuthorizedAgentsByPropertyTag(
    authorization_type="property_tags",
    authorized_for="Video inventory",
    property_tags=[PropertyTag("video"), PropertyTag("premium")],
    url=AGENT_URL,
)
assert_type(by_property_tag.authorization_type, Literal["property_tags"])
assert_type(by_property_tag.property_tags, list[PropertyTag])

by_inline_properties = AuthorizedAgentsByInlineProperties(
    authorization_type="inline_properties",
    authorized_for="Custom inventory bundle",
    properties=[
        Property.model_validate(
            {
                "property_type": "website",
                "name": "Example homepage",
                "identifiers": [{"type": "domain", "value": "example.com"}],
            }
        )
    ],
    url=AGENT_URL,
)
assert_type(by_inline_properties.authorization_type, Literal["inline_properties"])
assert_type(by_inline_properties.properties, list[Property])

# ``publisher_properties`` / ``signal_ids`` / ``signal_tags`` are lists of root
# models (``PublisherPropertySelector``, ``SignalId``, ``SignalTag``) that the
# public surface does not export, so an adopter reaches these three variants by
# validating the adagents.json fragment rather than by keyword construction.
by_publisher_properties = TypeAdapter(AuthorizedAgentsByPublisherProperties).validate_python(
    {
        "authorization_type": "publisher_properties",
        "authorized_for": "Network inventory across publishers",
        "publisher_properties": [
            {"publisher_domain": "publisher1.com", "selection_type": "all"},
        ],
        "url": "https://agent.example.com",
    }
)
assert_type(by_publisher_properties.authorization_type, Literal["publisher_properties"])

by_signal_id = TypeAdapter(AuthorizedAgentsBySignalId).validate_python(
    {
        "authorization_type": "signal_ids",
        "authorized_for": "Resold intent signals",
        "signal_ids": ["high-intent"],
        "url": "https://signals.example.com",
    }
)
assert_type(by_signal_id.authorization_type, Literal["signal_ids"])

by_signal_tag = TypeAdapter(AuthorizedAgentsBySignalTag).validate_python(
    {
        "authorization_type": "signal_tags",
        "authorized_for": "Resold tagged signals",
        "signal_tags": ["auto-intender"],
        "url": "https://signals.example.com",
    }
)
assert_type(by_signal_tag.authorization_type, Literal["signal_tags"])

# --- Every variant satisfies the public union ---

agents: list[AuthorizedAgent] = [
    bare,
    by_property_id,
    by_property_tag,
    by_inline_properties,
    by_publisher_properties,
    by_signal_id,
    by_signal_tag,
]
