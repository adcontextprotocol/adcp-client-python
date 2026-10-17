from __future__ import annotations

import pytest

from adcp.identifiers import normalize_identifier
from adcp.property_lists import ProductPropertyPolicy, ResolvedPropertyList


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "allowed_domain,inventory_domain,expected_status",
    [
        ("strasse.de", "straße.de", "mismatch"),
        ("straße.de", "strasse.de", "mismatch"),
        ("straße.de", "xn--strae-oqa.de", "match"),
        ("xn--strae-oqa.de", "straße.de", "match"),
    ],
)
async def test_buyer_domain_matching_preserves_idna_2008_identity(
    allowed_domain, inventory_domain, expected_status
):
    allowed = ResolvedPropertyList((normalize_identifier("domain", allowed_domain),))

    async def expand_identifiers(seller_url, selectors):
        return {
            "identifiers": [
                {
                    "type": "domain",
                    "value": inventory_domain,
                    "publisher_domain": "publisher.example",
                    "property_id": "site",
                }
            ]
        }

    policy = ProductPropertyPolicy(expand_identifiers=expand_identifiers)
    products = [
        {
            "product_id": "inventory",
            "publisher_properties": [
                {
                    "selection_type": "by_id",
                    "publisher_domain": "publisher.example",
                    "property_ids": ["site"],
                }
            ],
        }
    ]

    assessments = await policy.assess(products, allowed, seller_url="https://seller.example/mcp")

    assert assessments[0].status == expected_status
