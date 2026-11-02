"""Cross-SDK delegation-chain vectors (L2 "Grants in trust records").

Vendored from adcontextprotocol/adcp#8473 (see
``tests/fixtures/delegation-chains/PINNED.json``). Each vector is an acts-for
claim, the parent organization the brand resolves to, and the trust records
each domain publishes; :class:`TrustGrantEvaluator` must reach the expected
outcome. Re-vendor from upstream rather than editing the fixture.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from adcp.signing import (
    TrustGrantEvaluator,
    TrustRecordEvaluator,
    TrustResponse,
    trust_record_url,
)
from adcp.signing.trust import BrandHouse

FIXTURES = Path(__file__).parent / "fixtures" / "delegation-chains"
FIXTURE_BYTES = (FIXTURES / "delegation-chains.json").read_bytes()
FIXTURE = json.loads(FIXTURE_BYTES)
PINNED = json.loads((FIXTURES / "PINNED.json").read_text())

# The vectors use ``.example`` names, which have no registrable domain under
# the PSL. Map each to a registrable placeholder; domains are opaque labels.
_EXAMPLE = re.compile(r"\.example\b")

OUTCOMES = {"implicit": "implicit", "authorized": "trust", "seller_discretion": "seller"}


def _mapped(value: Any) -> Any:
    return json.loads(_EXAMPLE.sub("-dcv.com", json.dumps(value)))


def _instant(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def test_fixture_matches_pinned_upstream_content():
    assert hashlib.sha256(FIXTURE_BYTES).hexdigest() == PINNED["sha256"]


def test_the_vendored_vectors_cover_every_outcome():
    outcomes = {vector["expected"]["outcome"] for vector in FIXTURE["vectors"]}
    assert outcomes == {"authorized", "grant_absent", "implicit", "seller_discretion"}


@pytest.mark.parametrize("vector", FIXTURE["vectors"], ids=lambda vector: vector["id"])
async def test_delegation_chain_vector(vector):
    claim = _mapped(vector["input"])
    now = _instant(claim["check_time"])
    signer = claim["signer"]
    documents = {domain: dict(record) for domain, record in claim["trust_records"].items()}
    # Every operator a chain reaches lists the signer, so binding is never the
    # reason a vector fails.
    documents[signer["operator"]] = {
        **documents.get(signer["operator"], {}),
        "agents": [{"url": signer["origin"], "roles": ["adcp:buying"]}],
    }
    by_url = {trust_record_url(domain): record for domain, record in documents.items()}

    async def fetch(url, *, allow_private=False):
        record = by_url.get(url)
        if record is None:
            return TrustResponse(b"", 404)
        return TrustResponse(json.dumps(record).encode())

    seller_calls = []

    async def seller_authorization(request):
        seller_calls.append(request)
        return True

    brand = claim["brand"]
    parent = claim["parent_organization"]

    async def resolve_house(domain, brand_id):
        assert domain == brand["domain"]
        return BrandHouse(parent, brand.get("brand_id")) if parent else None

    evaluator = TrustGrantEvaluator(
        TrustRecordEvaluator(fetcher=fetch, clock=lambda: now),
        clock=lambda: now,
        seller_authorization=seller_authorization,
        resolve_house=resolve_house,
    )
    result = await evaluator.check(
        agent_origin=signer["origin"],
        brand_domain=brand["domain"],
        account_operator=claim["account_operator"],
        scope=claim["scope"],
        brand_id=brand.get("brand_id"),
        countries=brand.get("countries", ()),
    )
    expected = vector["expected"]["outcome"]
    if expected == "grant_absent":
        assert not result.authorized, vector["description"]
        assert result.reason == "grant_absent", result
        assert result.error_code == "PERMISSION_DENIED"
        assert not seller_calls, "an exclusive record never defers to the seller"
    else:
        assert result.authorized, (vector["description"], result)
        assert result.source == OUTCOMES[expected], vector["description"]
        assert len(seller_calls) == (expected == "seller_discretion")
