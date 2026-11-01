"""Publisher-domain attribution, the permissive domain gate and cross-domain vouching (#1697).

Every test that involves another publisher's domain is written from the
attacker's side: a document served at ``attacker.com`` (or a network
file) naming a domain it does not control must not authorize the agent for
it unless that domain vouches.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from adcp import (
    AdagentsTransportFactory,
    CrossDomainVerification,
    UnverifiedProperty,
    fetch_adagents,
    properties_for_publisher_domain,
    resolve_properties_for_agent,
    verify_cross_domain_properties,
)
from adcp.exceptions import AdagentsNotFoundError, AdagentsValidationError

AGENT = "https://sales.agent.com/mcp"


def site(pid: str, *values: str, **extra: Any) -> dict[str, Any]:
    return {
        "property_id": pid,
        "property_type": "website",
        "name": pid,
        "identifiers": [{"type": "domain", "value": v} for v in values],
        **extra,
    }


def app(pid: str, bundle: str, **extra: Any) -> dict[str, Any]:
    return {
        "property_id": pid,
        "property_type": "mobile_app",
        "name": pid,
        "identifiers": [{"type": "bundle_id", "value": bundle}],
        **extra,
    }


def venue(pid: str, venue_id: str) -> dict[str, Any]:
    return {
        "property_id": pid,
        "property_type": "dooh",
        "name": pid,
        "identifiers": [{"type": "venue_id", "value": venue_id}],
    }


def inline_file(*props: dict[str, Any], agent: str = AGENT) -> dict[str, Any]:
    return {
        "authorized_agents": [
            {
                "url": agent,
                "authorized_for": "Display",
                "authorization_type": "inline_properties",
                "properties": list(props),
            }
        ]
    }


def bare_file(*props: dict[str, Any]) -> dict[str, Any]:
    return {
        "authorized_agents": [{"url": AGENT, "authorized_for": "Everything"}],
        "properties": list(props),
    }


# ---------------------------------------------------------------------------
# 1. properties_for_publisher_domain
# ---------------------------------------------------------------------------


def test_shared_file_subdomains_belong_to_the_publisher() -> None:
    """The reported case: property_ids authorize shop./ctv. under example.org's file."""
    props = [
        site("main", "example.org"),
        site("shop", "shop.example.org"),
        site("ctv", "ctv.example.org"),
        site("wild", "*.example.org"),
        site("mobile", "m.example.org"),
        site("sister", "sister-paper.com"),
    ]
    attributed = properties_for_publisher_domain(props, "example.org")
    assert [p["property_id"] for p in attributed] == ["main", "shop", "ctv", "wild", "mobile"]
    assert [
        p["property_id"] for p in properties_for_publisher_domain(props, "sister-paper.com")
    ] == ["sister"]


@pytest.mark.parametrize("entered", ["www.example.org", "WWW.Example.ORG.", " example.org "])
def test_publisher_entered_as_www_is_the_apex(entered: str) -> None:
    assert properties_for_publisher_domain([site("main", "example.org")], entered)


@pytest.mark.parametrize(
    "value",
    [
        "notexample.org",
        "example.org.attacker.com",
        "*.org",
        "org",
        "example.organic",
        "https://example.org/",
        "example.org:443",
        "user@example.org",
        "93.184.216.34",
        "",
    ],
)
def test_lookalike_and_malformed_identifiers_do_not_attribute(value: str) -> None:
    assert properties_for_publisher_domain([site("x", value)], "example.org") == []


def test_www_dot_tld_is_its_own_domain() -> None:
    assert properties_for_publisher_domain([site("x", "www.com")], "www.com")
    assert properties_for_publisher_domain([site("x", "com")], "www.com") == []


def test_declared_publisher_domain_wins_over_identifiers() -> None:
    """A network file attributing a property to its publisher is read as written."""
    props = [
        site("theirs", "example.org", publisher_domain="other.com"),
        site("ours", "cdn.other.com", publisher_domain="www.example.org"),
        site("garbage", "example.org", publisher_domain=None),
    ]
    assert [p["property_id"] for p in properties_for_publisher_domain(props, "example.org")] == [
        "ours"
    ]


def test_domainless_properties_are_excluded_unless_requested() -> None:
    props = [site("main", "example.org"), app("app", "com.example.app"), venue("v", "123")]
    assert properties_for_publisher_domain(props, "example.org") == [props[0]]
    assert properties_for_publisher_domain(props, "example.org", include_domainless=True) == props
    # An app that declares its publisher_domain is attributable.
    declared = app("app", "com.example.app", publisher_domain="example.org")
    assert properties_for_publisher_domain([declared], "example.org") == [declared]


def test_idn_hosts_compare_in_a_label_form() -> None:
    assert properties_for_publisher_domain([site("x", "shop.bücher.com")], "xn--bcher-kva.com")
    assert properties_for_publisher_domain([site("x", "xn--bcher-kva.com")], "Bücher.com")


@pytest.mark.parametrize(
    "bad", ["co.uk", "github.io", "localhost", "10.0.0.1", "", "*.example.org"]
)
def test_publisher_domain_must_be_registrable(bad: str) -> None:
    """A public suffix would make every tenant beneath it a 'subdomain'."""
    with pytest.raises(AdagentsValidationError, match="registrable hostname"):
        properties_for_publisher_domain([site("x", "victim.co.uk")], bad)


# ---------------------------------------------------------------------------
# 2. Permissive mode's domain gate
# ---------------------------------------------------------------------------


def test_permissive_requires_a_publisher_domain_or_an_explicit_opt_out() -> None:
    doc = bare_file(site("own", "attacker.com"))
    with pytest.raises(ValueError, match="requires publisher_domain"):
        resolve_properties_for_agent(doc, AGENT, mode="permissive")
    with pytest.raises(ValueError, match="not both"):
        resolve_properties_for_agent(
            doc,
            AGENT,
            mode="permissive",
            publisher_domain="attacker.com",
            allow_any_domain=True,
        )
    with pytest.raises(ValueError, match="apply to mode='permissive'"):
        resolve_properties_for_agent(doc, AGENT, publisher_domain="attacker.com")
    with pytest.raises(ValueError, match="apply to mode='permissive'"):
        resolve_properties_for_agent(doc, AGENT, allow_any_domain=True)


def test_bare_entry_cannot_claim_another_publishers_inventory() -> None:
    """A file on attacker.com bare-lists the agent and claims a victim's
    site, an app bundle and a DOOH venue; only its own domain survives."""
    own = site("own", "attacker.com")
    own_sub = site("own-sub", "news.attacker.com", "*.attacker.com")
    doc = bare_file(
        own,
        own_sub,
        site("victim", "victim.com"),
        site("mixed", "attacker.com", "victim.com"),
        site("relabelled", "attacker.com", publisher_domain="victim.com"),
        site("lookalike", "attacker.com.victim.com"),
        app("bundle", "com.victim.app"),
        {
            **site("site-plus-app", "attacker.com"),
            "identifiers": [
                {"type": "domain", "value": "attacker.com"},
                {"type": "bundle_id", "value": "com.victim.app"},
            ],
        },
        venue("venue", "victim-venue-1"),
        {"property_id": "no-ids", "property_type": "website", "name": "n"},
    )
    gated = resolve_properties_for_agent(
        doc, AGENT, mode="permissive", publisher_domain="www.attacker.com"
    )
    assert gated == [own, own_sub]
    # The SDK 9 behavior stays available, explicitly.
    ungated = resolve_properties_for_agent(doc, AGENT, mode="permissive", allow_any_domain=True)
    assert ungated == doc["properties"]


def test_gate_applies_only_to_the_bare_fallback() -> None:
    """Typed selectors are the publisher's explicit binding; the gate leaves them alone."""
    victim = site("victim", "victim.com")
    doc = inline_file(victim)
    assert resolve_properties_for_agent(
        doc, AGENT, mode="permissive", publisher_domain="attacker.com"
    ) == [victim]


def test_gate_still_honors_revocation_and_agent_matching() -> None:
    doc = bare_file(site("own", "attacker.com", publisher_domain="attacker.com"))
    doc["revoked_publisher_domains"] = [
        {"publisher_domain": "attacker.com", "revoked_at": "2026-10-01T00:00:00Z"}
    ]
    assert (
        resolve_properties_for_agent(doc, AGENT, mode="permissive", publisher_domain="attacker.com")
        == []
    )
    other = bare_file(site("own", "attacker.com"))
    assert (
        resolve_properties_for_agent(
            other,
            "https://other.agent.com",
            mode="permissive",
            publisher_domain="attacker.com",
        )
        == []
    )


def test_gate_rejects_an_unregistrable_publisher_domain() -> None:
    with pytest.raises(AdagentsValidationError):
        resolve_properties_for_agent(
            bare_file(site("x", "victim.co.uk")), AGENT, mode="permissive", publisher_domain="co.uk"
        )


# ---------------------------------------------------------------------------
# 3. verify_cross_domain_properties
# ---------------------------------------------------------------------------


def fetcher_for(
    documents: dict[str, dict[str, Any] | Exception], calls: list[str] | None = None
) -> Callable[[str], Any]:
    async def fetch(domain: str) -> dict[str, Any]:
        if calls is not None:
            calls.append(domain)
        if domain not in documents:
            raise AdagentsNotFoundError(domain)
        document = documents[domain]
        if isinstance(document, Exception):
            raise document
        return document

    return fetch


async def verify(
    props: list[dict[str, Any]],
    documents: dict[str, dict[str, Any] | Exception],
    calls: list[str] | None = None,
    **kwargs: Any,
) -> CrossDomainVerification:
    return await verify_cross_domain_properties(
        props,
        source_domain="network.com",
        agent_url=AGENT,
        fetcher=fetcher_for(documents, calls),
        **kwargs,
    )


def reasons(result: CrossDomainVerification) -> dict[str, tuple[str, str]]:
    return {u.prop["property_id"]: (u.domain, u.reason) for u in result.unverified}


async def test_own_domain_claims_need_no_fetch() -> None:
    calls: list[str] = []
    props = [
        site("own", "network.com"),
        site("sub", "ctv.network.com"),
        site("declared", "network.com", publisher_domain="www.network.com"),
    ]
    result = await verify(props, {}, calls)
    assert result.verified == props
    assert result.unverified == [] and result.domainless == [] and calls == []


async def test_claim_vouched_by_the_named_publishers_own_file() -> None:
    claim = site("b", "b.com", publisher_domain="b.com")
    result = await verify([claim], {"b.com": inline_file(site("b-own", "b.com"))})
    assert result.verified == [claim]


async def test_claim_is_refused_when_the_named_publisher_does_not_vouch() -> None:
    claims = [
        site("unlisted", "b.com"),
        site("other-agent", "c.com"),
        site("other-site", "d.com"),
        site("bare", "e.com"),
    ]
    documents: dict[str, dict[str, Any] | Exception] = {
        "b.com": inline_file(site("x", "b.com"), agent="https://other.agent.com"),
        "c.com": {"authorized_agents": []},
        "d.com": inline_file(site("x", "unrelated.d.com")),
        # A bare entry in the vouching file binds nothing.
        "e.com": bare_file(site("x", "e.com")),
    }
    result = await verify(claims, documents)
    assert result.verified == []
    assert reasons(result) == {
        "unlisted": ("b.com", "not_vouched"),
        "other-agent": ("c.com", "not_vouched"),
        "other-site": ("d.com", "not_vouched"),
        "bare": ("e.com", "not_vouched"),
    }


async def test_voucher_must_cover_every_identifier_of_the_claim() -> None:
    """B authorizing its site does not vouch for a victim's bundle bolted onto the claim."""
    claim = site("b", "b.com")
    claim["identifiers"].append({"type": "bundle_id", "value": "com.victim.app"})
    result = await verify([claim], {"b.com": inline_file(site("b", "b.com"))})
    assert reasons(result) == {"b": ("b.com", "not_vouched")}

    covered = await verify(
        [claim],
        {"b.com": inline_file(site("b", "b.com"), app("app", "com.victim.app"))},
    )
    assert covered.verified == [claim]


async def test_wildcard_and_subdomain_claims_follow_domain_matching() -> None:
    sub = site("sub", "shop.b.com")
    wild = site("wild", "*.b.com")
    calls: list[str] = []
    narrow = await verify([sub, wild], {"b.com": inline_file(site("b", "b.com"))}, calls)
    # A bare authorization of b.com covers www./m. only, never shop. or *.
    assert reasons(narrow) == {
        "sub": ("b.com", "not_vouched"),
        "wild": ("b.com", "not_vouched"),
    }
    # Each claim's registrable domain is fetched, once.
    assert calls == ["b.com"]
    broad = await verify([sub, wild], {"b.com": inline_file(site("b", "*.b.com"))})
    assert broad.verified == [sub, wild]


async def test_a_relay_publisher_cannot_vouch_for_a_victims_domain() -> None:
    """attacker.com's file says the victim's site lives under accomplice.com;
    the victim's own domain must vouch too."""
    claim = site("victim", "victim.com", publisher_domain="accomplice.com")
    documents: dict[str, dict[str, Any] | Exception] = {
        "accomplice.com": inline_file(claim),
        "victim.com": inline_file(site("x", "victim.com"), agent="https://real.agent.com"),
    }
    result = await verify([claim], documents)
    assert reasons(result) == {"victim": ("victim.com", "not_vouched")}


async def test_every_named_publisher_must_vouch() -> None:
    claim = site("both", "b.com", "c.com")
    documents: dict[str, dict[str, Any] | Exception] = {"b.com": inline_file(claim)}
    result = await verify([claim], documents)
    assert reasons(result) == {"both": ("c.com", "fetch_failed")}
    documents["c.com"] = inline_file(claim)
    assert (await verify([claim], documents)).verified == [claim]


@pytest.mark.parametrize(
    "failure",
    [
        AdagentsNotFoundError("b.com"),
        AdagentsValidationError("too large"),
        httpx.ConnectError("refused"),
        RuntimeError("fetcher bug"),
    ],
)
async def test_any_fetch_failure_vouches_for_nothing(failure: Exception) -> None:
    result = await verify([site("b", "b.com")], {"b.com": failure})
    assert reasons(result) == {"b": ("b.com", "fetch_failed")}


async def test_malformed_vouching_document_vouches_for_nothing() -> None:
    result = await verify([site("b", "b.com")], {"b.com": {"not": "adagents"}})
    assert reasons(result) == {"b": ("b.com", "fetch_failed")}


async def test_unusable_domain_values_fail_closed() -> None:
    props = [
        site("ip", "10.0.0.1"),
        site("url", "https://b.com/"),
        site("suffix", "co.uk"),
        site("declared", "network.com", publisher_domain="b.com/path"),
    ]
    calls: list[str] = []
    result = await verify(props, {}, calls)
    assert {k: v[1] for k, v in reasons(result).items()} == {
        "ip": "invalid_domain",
        "url": "invalid_domain",
        "suffix": "invalid_domain",
        "declared": "invalid_domain",
    }
    assert calls == []


async def test_private_suffix_tenants_vouch_for_themselves_only() -> None:
    """victim.github.io is vouched for by itself, not by github.io or a sibling tenant."""
    calls: list[str] = []
    await verify([site("v", "www.victim.github.io")], {}, calls)
    assert calls == ["victim.github.io"]


async def test_fetch_fanout_is_bounded() -> None:
    props = [site(f"p{i}", f"pub{i}.com") for i in range(5)]
    documents: dict[str, dict[str, Any] | Exception] = {
        f"pub{i}.com": inline_file(props[i]) for i in range(5)
    }
    calls: list[str] = []
    result = await verify(props, documents, calls, max_domains=2)
    assert calls == ["pub0.com", "pub1.com"]
    assert result.verified == props[:2]
    assert [u.reason for u in result.unverified] == ["domain_limit"] * 3
    with pytest.raises(ValueError):
        await verify(props, documents, max_domains=0)


async def test_domainless_properties_are_reported_separately_and_order_is_kept() -> None:
    own = site("own", "network.com")
    vouched = site("b", "b.com")
    bundle = app("app", "com.network.app")
    result = await verify([vouched, bundle, own], {"b.com": inline_file(vouched)})
    assert result.verified == [vouched, own]
    assert result.domainless == [bundle]
    assert isinstance(result.unverified, list) and not result.unverified
    assert UnverifiedProperty(own, "x", "not_vouched").prop is own


# Delegation runs through fetch_adagents, which follows the named publisher's
# authoritative_location and, when it has no file, its ads.txt MANAGERDOMAIN.


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))
        ],
    )


def http_fetcher(routes: dict[str, httpx.Response]) -> Callable[[str], Any]:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        return routes[url] if url in routes else httpx.Response(404)

    @asynccontextmanager
    async def factory(url: str, timeout: float) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout) as c:
            yield c

    transport_factory: AdagentsTransportFactory = factory

    async def fetch(domain: str) -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_adagents(domain, client=client, transport_factory=transport_factory)

    return fetch


NETWORK_FILE = "https://network.com/.well-known/adagents.json"


@pytest.mark.usefixtures("public_dns")
@pytest.mark.parametrize("delegation", ["authoritative_location", "managerdomain"])
async def test_publisher_delegating_to_the_network_vouches(delegation: str) -> None:
    claim = site("b", "b.com", publisher_domain="b.com")
    network = inline_file(claim)
    routes = {NETWORK_FILE: httpx.Response(200, json=network)}
    if delegation == "authoritative_location":
        routes["https://b.com/.well-known/adagents.json"] = httpx.Response(
            200, json={"authoritative_location": NETWORK_FILE}
        )
    else:
        routes["https://b.com/ads.txt"] = httpx.Response(200, text="MANAGERDOMAIN=network.com")
    result = await verify_cross_domain_properties(
        [claim], source_domain="network.com", agent_url=AGENT, fetcher=http_fetcher(routes)
    )
    assert result.verified == [claim]


@pytest.mark.usefixtures("public_dns")
async def test_publisher_with_its_own_file_is_not_overridden_by_its_ads_txt() -> None:
    """MANAGERDOMAIN is a fallback for a publisher with no file; a file that
    does not authorize the agent is the publisher's answer."""
    claim = site("b", "b.com", publisher_domain="b.com")
    routes = {
        NETWORK_FILE: httpx.Response(200, json=inline_file(claim)),
        "https://b.com/.well-known/adagents.json": httpx.Response(
            200, json=inline_file(site("x", "b.com"), agent="https://real.agent.com")
        ),
        "https://b.com/ads.txt": httpx.Response(200, text="MANAGERDOMAIN=network.com"),
    }
    result = await verify_cross_domain_properties(
        [claim], source_domain="network.com", agent_url=AGENT, fetcher=http_fetcher(routes)
    )
    assert reasons(result) == {"b": ("b.com", "not_vouched")}
