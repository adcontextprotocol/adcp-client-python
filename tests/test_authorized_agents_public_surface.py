"""Runtime contract for the public ``AuthorizedAgents`` surface.

``adcp.types._generated`` rebinds the ``AuthorizedAgents*`` window at import
time (``AuthorizedAgents = AuthorizedAgents1``, ``AuthorizedAgents1 =
AuthorizedAgents2``, ...) so the historical adagents variant numbering keeps
working for adopters who import the numbered names. The public surface must
*not* be built on those rebound names: mypy reads the pre-rebind declaration,
so a public symbol sourced from ``_generated`` runs as one variant and
type-checks as its neighbour.

Both halves of the contract are pinned here. The seven public symbols must be
the raw ``generated_poc.adagents`` variant classes at runtime (this module),
and must infer as those same classes under ``mypy --strict``
(``tests/type_checks/authorized_agents_variants.py``).

The bindings are also checked in fresh subprocesses across several import
orders: ``adcp.types`` is a lazy PEP 562 facade over ``adcp.types._eager``, so
which module wins the first import is a real variable, not a theoretical one.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from adcp.types import generated_poc

# Public symbol -> the ``generated_poc.adagents`` class it must be.
EXPECTED_VARIANTS = {
    "AuthorizedAgents": "AuthorizedAgents1",
    "AuthorizedAgentsByPropertyId": "AuthorizedAgents1",
    "AuthorizedAgentsByPropertyTag": "AuthorizedAgents2",
    "AuthorizedAgentsByInlineProperties": "AuthorizedAgents3",
    "AuthorizedAgentsByPublisherProperties": "AuthorizedAgents4",
    "AuthorizedAgentsBySignalId": "AuthorizedAgents5",
    "AuthorizedAgentsBySignalTag": "AuthorizedAgents6",
}

# Public symbol -> the ``authorization_type`` discriminator its name promises.
EXPECTED_DISCRIMINATORS = {
    "AuthorizedAgents": "property_ids",
    "AuthorizedAgentsByPropertyId": "property_ids",
    "AuthorizedAgentsByPropertyTag": "property_tags",
    "AuthorizedAgentsByInlineProperties": "inline_properties",
    "AuthorizedAgentsByPublisherProperties": "publisher_properties",
    "AuthorizedAgentsBySignalId": "signal_ids",
    "AuthorizedAgentsBySignalTag": "signal_tags",
}

# The six semantic aliases, i.e. every public symbol except the bare one.
SEMANTIC_ALIASES = tuple(n for n in EXPECTED_VARIANTS if n != "AuthorizedAgents")


@pytest.mark.parametrize(("public_name", "variant_name"), sorted(EXPECTED_VARIANTS.items()))
def test_public_symbol_is_the_raw_generated_variant(public_name: str, variant_name: str) -> None:
    """Each public symbol is the raw variant class, not a shifted rebinding."""
    import adcp.types

    assert getattr(adcp.types, public_name) is getattr(generated_poc.adagents, variant_name)


@pytest.mark.parametrize(("public_name", "discriminator"), sorted(EXPECTED_DISCRIMINATORS.items()))
def test_public_symbol_carries_the_discriminator_its_name_promises(
    public_name: str, discriminator: str
) -> None:
    """``AuthorizedAgentsBySignalTag`` must be the ``signal_tags`` variant, etc."""
    import adcp.types

    model = getattr(adcp.types, public_name)
    field = model.model_fields["authorization_type"]
    assert field.default == discriminator


@pytest.mark.parametrize("public_name", SEMANTIC_ALIASES)
def test_aliases_module_and_package_facades_agree(public_name: str) -> None:
    """``adcp``, ``adcp.types`` and ``adcp.types.aliases`` hand out one class."""
    import adcp
    import adcp.types
    import adcp.types.aliases

    from_types = getattr(adcp.types, public_name)
    assert getattr(adcp.types.aliases, public_name) is from_types
    assert getattr(adcp, public_name) is from_types


def test_authorized_agent_union_covers_every_variant_in_order() -> None:
    """The public union is exactly the six raw variants, variant 1 through 6."""
    import adcp.types

    expected = tuple(getattr(generated_poc.adagents, f"AuthorizedAgents{n}") for n in range(1, 7))
    assert adcp.types.AuthorizedAgent.__args__ == expected


def test_property_ids_variant_validates_its_wire_fragment() -> None:
    """The bare public symbol accepts the ``property_ids`` adagents fragment."""
    import adcp.types

    agent = adcp.types.AuthorizedAgents.model_validate(
        {
            "url": "https://agent.example.com",
            "authorized_for": "Premium display inventory",
            "authorization_type": "property_ids",
            "property_ids": ["homepage"],
        }
    )
    assert agent.authorization_type == "property_ids"
    assert [p.root for p in agent.property_ids] == ["homepage"]


# --- Import-order coverage -------------------------------------------------

# Each entry is the import statement that runs *before* the public symbols are
# resolved, exercising a different first-toucher of the lazy type surface.
IMPORT_ORDERS = {
    "types_first": "import adcp.types",
    "aliases_first": "import adcp.types.aliases",
    "package_first": "import adcp",
    "eager_first": "import adcp.types._eager",
    "generated_first": "import adcp.types._generated",
    "generated_poc_first": "from adcp.types.generated_poc import adagents",
    "partial_module_first": "import adcp.types.seller",
}

_PROBE = """
{first_import}
import adcp
import adcp.types
import adcp.types.aliases
from adcp.types.generated_poc import adagents

names = {names!r}
for name in names:
    resolved = getattr(adcp.types, name)
    assert resolved is getattr(adcp.types.aliases, name, resolved), name
    assert resolved is getattr(adcp, name, resolved), name
    print(name, resolved.__module__ + "." + resolved.__qualname__)
"""


@pytest.mark.parametrize("order", sorted(IMPORT_ORDERS))
def test_bindings_are_stable_across_import_orders(order: str) -> None:
    """Whichever module builds the type graph first, the bindings are identical."""
    code = _PROBE.format(
        first_import=IMPORT_ORDERS[order],
        names=sorted(EXPECTED_VARIANTS),
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"subprocess failed:\n{result.stderr}"

    resolved = dict(line.split(" ", 1) for line in result.stdout.strip().splitlines())
    assert resolved == {
        name: f"adcp.types.generated_poc.adagents.{variant}"
        for name, variant in EXPECTED_VARIANTS.items()
    }
