"""Publisher attribution and verification without live network dependencies."""

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from adcp import (
    fetch_adagents,
    get_properties_by_agent,
    properties_for_publisher_domain,
    resolve_properties_for_agent,
    verify_cross_domain_properties,
)
from adcp.exceptions import (
    AdagentsNotFoundError,
    AdagentsTimeoutError,
    AdagentsValidationError,
)

AGENT = "https://sales.example.com/mcp"


def site(domain: str, property_id: str = "site", **extra: Any) -> dict[str, Any]:
    return {
        "property_id": property_id,
        "property_type": "website",
        "name": domain,
        "identifiers": [{"type": "domain", "value": domain}],
        **extra,
    }


def manifest(properties: list[dict[str, Any]], *, bare: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {"url": AGENT, "authorized_for": "Sales"}
    if not bare:
        entry.update(
            authorization_type="property_ids", property_ids=[p["property_id"] for p in properties]
        )
    return {"authorized_agents": [entry], "properties": properties}


@pytest.mark.parametrize("publisher", ["example.org", "www.example.org", " WWW.EXAMPLE.ORG. "])
def test_attribution_keeps_authorized_subdomains(publisher: str) -> None:
    props = [
        site(d, d)
        for d in [
            "example.org",
            "www.example.org",
            "m.example.org",
            "mobile.example.org",
            "shop.example.org",
            "ctv.example.org",
            "deep.shop.example.org",
            "other.org",
            "notexample.org",
            "example.org.attacker.org",
        ]
    ]
    document = manifest(props)
    before = deepcopy(document)
    resolved = get_properties_by_agent(document, AGENT)
    selected = properties_for_publisher_domain(resolved, publisher)
    assert selected == props[:7]
    assert all(a is b for a, b in zip(selected, props))
    assert document == before


def test_attribution_explicit_publisher_takes_precedence() -> None:
    selected = site("elsewhere.org", publisher_domain="WWW.EXAMPLE.ORG.")
    wrong = site("example.org", publisher_domain="other.org")
    child_publisher = site("example.org", publisher_domain="shop.example.org")
    app = {
        "property_type": "mobile_app",
        "identifiers": [{"type": "bundle_id", "value": "org.app"}],
    }
    props = [wrong, selected, child_publisher, app]
    assert properties_for_publisher_domain(props, "example.org") == [selected, app]
    assert properties_for_publisher_domain(props, "example.org", include_unattributed=False) == [
        selected
    ]


@pytest.mark.parametrize("bad", [None, "", 1, [], "https://example.org", "bad..org"])
def test_invalid_claims_are_not_unattributed(bad: Any) -> None:
    assert (
        properties_for_publisher_domain([site("example.org", publisher_domain=bad)], "example.org")
        == []
    )
    assert properties_for_publisher_domain([site(bad)], "example.org") == []


@pytest.mark.parametrize(
    "bad", ["", "https://example.org", "*.example.org", "example.org:443", "bad..org"]
)
def test_invalid_publisher_arguments_raise(bad: str) -> None:
    with pytest.raises(AdagentsValidationError):
        properties_for_publisher_domain([], bad)
    with pytest.raises(AdagentsValidationError):
        resolve_properties_for_agent(
            manifest([], bare=True), AGENT, mode="permissive", publisher_domain=bad
        )


def test_attribution_handles_multi_label_suffixes_and_idna() -> None:
    uk = site("shop.publisher.co.uk")
    idn = site("shop.xn--bcher-kva.example")
    assert properties_for_publisher_domain([uk], "www.publisher.co.uk") == [uk]
    assert properties_for_publisher_domain([idn], "bücher.example") == [idn]


def test_permissive_gate_uses_only_domain_identifiers() -> None:
    good = site("ctv.example.org", publisher_domain="other.org")
    forged = site("victim.org", publisher_domain="example.org")
    app = {
        "property_id": "app",
        "publisher_domain": "example.org",
        "identifiers": [{"type": "bundle_id", "value": "org.app"}],
    }
    venue = {"property_id": "venue", "identifiers": [{"type": "venue_id", "value": "example.org"}]}
    props = [
        good,
        forged,
        app,
        venue,
        site("example.org.attacker.org"),
        site("https://shop.example.org"),
    ]
    document = manifest(props, bare=True)
    before = deepcopy(document)
    assert resolve_properties_for_agent(document, AGENT, mode="permissive") == props
    assert resolve_properties_for_agent(
        document, AGENT, mode="permissive", publisher_domain="www.example.org"
    ) == [good]
    assert document == before


@pytest.mark.parametrize("mode", ["strict", "permissive"])
def test_domain_gate_does_not_change_typed_selectors(mode: str) -> None:
    props = [site("other.org")]
    assert (
        resolve_properties_for_agent(
            manifest(props), AGENT, mode=mode, publisher_domain="example.org"
        )
        == props
    )


def test_gate_does_not_expand_unlisted_or_ambiguous_authorizations() -> None:
    document = manifest([site("example.org")], bare=True)
    assert (
        resolve_properties_for_agent(
            document, AGENT + "/other", mode="permissive", publisher_domain="example.org"
        )
        == []
    )
    document["authorized_agents"] *= 2
    assert (
        resolve_properties_for_agent(
            document, AGENT, mode="permissive", publisher_domain="example.org"
        )
        == []
    )
    document["authorized_agents"] = [
        {"url": AGENT, "authorized_for": "Sales", "authorization_type": "unknown"}
    ]
    assert (
        resolve_properties_for_agent(
            document, AGENT, mode="permissive", publisher_domain="example.org"
        )
        == []
    )


def test_gate_honors_existing_revocations() -> None:
    prop = site("ctv.example.org", publisher_domain="example.org")
    document = manifest([prop], bare=True)
    document["revoked_publisher_domains"] = [{"publisher_domain": "example.org"}]
    assert (
        resolve_properties_for_agent(
            document, AGENT, mode="permissive", publisher_domain="example.org"
        )
        == []
    )


async def test_verification_keeps_local_and_unanchored_inventory_without_fetching() -> None:
    props = [
        site("example.org"),
        site("ctv.example.org"),
        {"property_id": "app", "identifiers": [{"type": "bundle_id", "value": "org.app"}]},
    ]
    fetcher = AsyncMock()
    assert (
        await verify_cross_domain_properties(props, "WWW.EXAMPLE.ORG.", AGENT, fetcher=fetcher)
        == props
    )
    fetcher.assert_not_awaited()


async def test_verification_fetches_once_per_domain_and_retains_input_order() -> None:
    first = site("remote.example", "first")
    second = site("remote.example", "second")
    local = site("publisher.example", "local")
    props = [first, local, second]
    before = deepcopy(props)
    fetcher = AsyncMock(return_value=manifest([first, second]))
    result = await verify_cross_domain_properties(
        props, "publisher.example", AGENT, fetcher=fetcher
    )
    assert result == props
    assert all(a is b for a, b in zip(result, props))
    fetcher.assert_awaited_once_with("remote.example")
    assert props == before


@pytest.mark.parametrize(
    "authorization_type",
    ["property_ids", "property_tags", "inline_properties", "publisher_properties"],
)
async def test_remote_selectors_resolve_inventory_independently_of_file_local_ids(
    authorization_type: str,
) -> None:
    claim = site("remote.example", "parent-id")
    remote_property = site(
        "remote.example", "child-id", publisher_domain="remote.example", tags=["sales"]
    )
    remote = manifest([remote_property])
    entry = remote["authorized_agents"][0]
    entry.pop("property_ids")
    entry["authorization_type"] = authorization_type
    entry.update(
        {
            "property_ids": {"property_ids": ["child-id"]},
            "property_tags": {"property_tags": ["sales"]},
            "inline_properties": {"properties": [remote_property]},
            "publisher_properties": {
                "publisher_properties": [
                    {"publisher_domain": "remote.example", "selection_type": "all"}
                ]
            },
        }[authorization_type]
    )
    assert await verify_cross_domain_properties(
        [claim], "publisher.example", AGENT, fetcher=AsyncMock(return_value=remote)
    ) == [claim]


async def test_verification_refreshes_between_calls_to_observe_revocation() -> None:
    prop = site("remote.example")
    remote = manifest([prop])
    revoked = {**remote, "revoked_publisher_domains": [{"publisher_domain": "remote.example"}]}
    fetcher = AsyncMock(side_effect=[remote, revoked])
    assert await verify_cross_domain_properties(
        [prop], "publisher.example", AGENT, fetcher=fetcher
    ) == [prop]
    assert (
        await verify_cross_domain_properties([prop], "publisher.example", AGENT, fetcher=fetcher)
        == []
    )
    assert fetcher.await_count == 2


@pytest.mark.parametrize(
    "remote",
    [
        {"authorized_agents": []},
        manifest([site("remote.example")], bare=True),
        manifest([site("elsewhere.example")]),
        manifest([site("remote.example", property_type="mobile_app")]),
        {
            "authorized_agents": [
                {
                    "url": AGENT,
                    "authorized_for": "Sales",
                    "authorization_type": "property_ids",
                    "property_ids": [],
                }
            ]
        },
        {
            "authorized_agents": [
                {
                    "url": "https://other.example",
                    "authorization_type": "inline_properties",
                    "properties": [site("remote.example")],
                }
            ]
        },
    ],
)
async def test_listing_agent_or_reusing_property_id_is_insufficient(remote: dict[str, Any]) -> None:
    fetcher = AsyncMock(return_value=remote)
    assert (
        await verify_cross_domain_properties(
            [site("remote.example")], "publisher.example", AGENT, fetcher=fetcher
        )
        == []
    )


async def test_every_claimed_identifier_must_be_authorized() -> None:
    claim = site("remote.example")
    claim["identifiers"].append({"type": "bundle_id", "value": "org.forged"})
    assert (
        await verify_cross_domain_properties(
            [claim],
            "publisher.example",
            AGENT,
            fetcher=AsyncMock(return_value=manifest([site("remote.example")])),
        )
        == []
    )


async def test_forged_local_publisher_anchor_does_not_bypass_external_domain_check() -> None:
    prop = site("victim.example", publisher_domain="publisher.example")
    fetcher = AsyncMock(return_value=manifest([], bare=True))
    assert (
        await verify_cross_domain_properties([prop], "publisher.example", AGENT, fetcher=fetcher)
        == []
    )
    fetcher.assert_awaited_once_with("victim.example")


async def test_every_external_domain_must_vouch() -> None:
    prop = site("first.example")
    prop["identifiers"].append({"type": "domain", "value": "second.example"})
    fetcher = AsyncMock(side_effect=[manifest([prop]), {"authorized_agents": []}])
    assert (
        await verify_cross_domain_properties([prop], "publisher.example", AGENT, fetcher=fetcher)
        == []
    )
    assert [c.args[0] for c in fetcher.await_args_list] == ["first.example", "second.example"]


async def test_bundle_only_inventory_uses_explicit_publisher_anchor() -> None:
    prop = {
        "property_id": "app",
        "property_type": "mobile_app",
        "publisher_domain": "remote.example",
        "identifiers": [{"type": "bundle_id", "value": "org.app"}],
    }
    fetcher = AsyncMock(return_value=manifest([prop]))
    assert await verify_cross_domain_properties(
        [prop], "publisher.example", AGENT, fetcher=fetcher
    ) == [prop]
    fetcher.assert_awaited_once_with("remote.example")
    # Matching a file-local ID cannot override a different bundle identifier.
    remote = {**prop, "identifiers": [{"type": "bundle_id", "value": "org.other"}]}
    assert (
        await verify_cross_domain_properties(
            [prop], "publisher.example", AGENT, fetcher=AsyncMock(return_value=manifest([remote]))
        )
        == []
    )

    # An app with no publisher anchor in a shared manager file cannot vouch for B.
    unanchored = {k: v for k, v in prop.items() if k != "publisher_domain"}
    assert (
        await verify_cross_domain_properties(
            [prop],
            "publisher.example",
            AGENT,
            fetcher=AsyncMock(return_value=manifest([unanchored])),
        )
        == []
    )


async def test_publisher_scoped_property_id_fallback() -> None:
    prop = {"property_id": "venue", "publisher_domain": "remote.example"}
    assert await verify_cross_domain_properties(
        [prop], "publisher.example", AGENT, fetcher=AsyncMock(return_value=manifest([prop]))
    ) == [prop]
    other = {**prop, "publisher_domain": "elsewhere.example"}
    assert (
        await verify_cross_domain_properties(
            [prop], "publisher.example", AGENT, fetcher=AsyncMock(return_value=manifest([other]))
        )
        == []
    )


@pytest.mark.parametrize("inline", [False, True])
async def test_remote_revocation_overrides_authorization(inline: bool) -> None:
    prop = site("remote.example", publisher_domain="remote.example")
    remote = manifest([prop])
    if inline:
        remote["authorized_agents"][0] = {
            "url": AGENT,
            "authorization_type": "inline_properties",
            "properties": [prop],
        }
    remote["revoked_publisher_domains"] = [{"publisher_domain": "WWW.REMOTE.EXAMPLE."}]
    assert (
        await verify_cross_domain_properties(
            [prop], "publisher.example", AGENT, fetcher=AsyncMock(return_value=remote)
        )
        == []
    )


@pytest.mark.parametrize(
    "error",
    [
        AdagentsNotFoundError("remote.example"),
        AdagentsTimeoutError("remote.example", 10),
        AdagentsValidationError("bad manifest"),
        httpx.ConnectError("unavailable"),
        OSError("DNS unavailable"),
    ],
)
async def test_fetch_failure_is_closed_and_cached(error: Exception) -> None:
    props = [
        site("remote.example", "one"),
        site("remote.example", "two"),
        site("publisher.example", "local"),
    ]
    fetcher = AsyncMock(side_effect=error)
    assert (
        await verify_cross_domain_properties(props, "publisher.example", AGENT, fetcher=fetcher)
        == props[-1:]
    )
    fetcher.assert_awaited_once()


@pytest.mark.parametrize("selector", ["property_ids", "property_tags"])
async def test_malformed_remote_inventory_excludes_only_that_origin(selector: str) -> None:
    from adcp import validate_adagents_structure

    remote = manifest([site("remote.example")])
    if selector == "property_ids":
        remote["properties"][0]["property_id"] = []
    else:
        remote["authorized_agents"][0].pop("property_ids")
        remote["authorized_agents"][0].update(
            authorization_type="property_tags", property_tags=["sales"]
        )
        remote["properties"][0]["tags"] = None
    # Fetch-time structural validation checks entries, not the inventory catalog.
    assert validate_adagents_structure(remote).schema_valid
    props = [site("remote.example"), site("publisher.example", "local")]
    assert (
        await verify_cross_domain_properties(
            props, "publisher.example", AGENT, fetcher=AsyncMock(return_value=remote)
        )
        == props[-1:]
    )


async def test_fetcher_programming_errors_are_not_hidden() -> None:
    with pytest.raises(TypeError, match="fetcher bug"):
        await verify_cross_domain_properties(
            [site("remote.example")],
            "publisher.example",
            AGENT,
            fetcher=AsyncMock(side_effect=TypeError("fetcher bug")),
        )


async def test_cancellation_is_propagated() -> None:
    with pytest.raises(asyncio.CancelledError):
        await verify_cross_domain_properties(
            [site("remote.example")],
            "publisher.example",
            AGENT,
            fetcher=AsyncMock(side_effect=asyncio.CancelledError),
        )


@pytest.mark.parametrize("bad", [None, "https://remote.example", "", []])
async def test_invalid_cross_domain_claims_are_excluded_without_fetch(bad: Any) -> None:
    fetcher = AsyncMock()
    assert (
        await verify_cross_domain_properties(
            [site(bad)], "publisher.example", AGENT, fetcher=fetcher
        )
        == []
    )
    assert (
        await verify_cross_domain_properties(
            [site("publisher.example", publisher_domain=bad)],
            "publisher.example",
            AGENT,
            fetcher=fetcher,
        )
        == []
    )
    fetcher.assert_not_awaited()


@pytest.mark.parametrize("module", ["adcp", "adcp.adagents"])
def test_helpers_import_in_a_fresh_interpreter(module: str) -> None:
    code = (
        f"from {module} import fetch_adagents, properties_for_publisher_domain, "
        "verify_cross_domain_properties; "
        "assert properties_for_publisher_domain([], 'publisher.example') == []"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("publisher", "victim"),
    [
        ("www.com", "victim.com"),
        ("www.co.uk", "victim.co.uk"),
        ("www.github.io", "victim.github.io"),
    ],
)
async def test_www_alias_never_broadens_authority_to_public_or_private_suffix(
    publisher: str, victim: str
) -> None:
    local = site(publisher, "local")
    remote = site(victim, "victim")
    assert properties_for_publisher_domain([local, remote], publisher) == [local]
    assert resolve_properties_for_agent(
        manifest([local, remote], bare=True), AGENT, mode="permissive", publisher_domain=publisher
    ) == [local]
    fetcher = AsyncMock(return_value={"authorized_agents": []})
    assert await verify_cross_domain_properties(
        [local, remote], publisher, AGENT, fetcher=fetcher
    ) == [local]
    fetcher.assert_awaited_once_with(victim)


async def test_verified_publisher_anchor_covers_its_own_subdomain_inventory() -> None:
    prop = site("ctv.publisher.example", publisher_domain="publisher.example")
    fetcher = AsyncMock(return_value=manifest([prop]))
    assert await verify_cross_domain_properties(
        [prop], "network.example", AGENT, fetcher=fetcher
    ) == [prop]
    fetcher.assert_awaited_once_with("publisher.example")
    assert (
        await verify_cross_domain_properties(
            [prop],
            "network.example",
            AGENT,
            fetcher=AsyncMock(return_value={"authorized_agents": []}),
        )
        == []
    )


async def test_child_publisher_cannot_vouch_for_parent_domain() -> None:
    prop = site("publisher.example", publisher_domain="ctv.publisher.example")
    fetcher = AsyncMock(side_effect=[manifest([prop]), {"authorized_agents": []}])
    assert (
        await verify_cross_domain_properties([prop], "network.example", AGENT, fetcher=fetcher)
        == []
    )
    assert [c.args[0] for c in fetcher.await_args_list] == [
        "ctv.publisher.example",
        "publisher.example",
    ]


async def test_subdomain_identifier_requires_external_authorization() -> None:
    prop = site("ctv.victim.example")
    prop["identifiers"][0]["type"] = "subdomain"
    fetcher = AsyncMock(return_value={"authorized_agents": []})
    assert (
        await verify_cross_domain_properties([prop], "attacker.example", AGENT, fetcher=fetcher)
        == []
    )
    fetcher.assert_awaited_once_with("ctv.victim.example")
    assert properties_for_publisher_domain([prop], "victim.example") == [prop]
    assert properties_for_publisher_domain([prop], "attacker.example") == []
    assert await verify_cross_domain_properties(
        [prop], "network.example", AGENT, fetcher=AsyncMock(return_value=manifest([prop]))
    ) == [prop]
    # The bare-entry gate continues to require the documented domain type.
    assert (
        resolve_properties_for_agent(
            manifest([prop], bare=True), AGENT, mode="permissive", publisher_domain="victim.example"
        )
        == []
    )


@pytest.mark.parametrize("kind", ["domain", "subdomain"])
async def test_unicode_and_idna_inventory_identities_match(kind: str) -> None:
    prop = site("bücher.example")
    prop["identifiers"][0]["type"] = kind
    remote = site("xn--bcher-kva.example")
    remote["identifiers"][0]["type"] = kind
    fetcher = AsyncMock(return_value=manifest([remote]))
    assert await verify_cross_domain_properties(
        [prop], "network.example", AGENT, fetcher=fetcher
    ) == [prop]
    fetcher.assert_awaited_once_with("xn--bcher-kva.example")


async def test_wildcard_inventory_is_attributed_gated_and_verified_at_its_base() -> None:
    prop = site("*.publisher.example")
    assert properties_for_publisher_domain([prop], "publisher.example") == [prop]
    assert resolve_properties_for_agent(
        manifest([prop], bare=True), AGENT, mode="permissive", publisher_domain="publisher.example"
    ) == [prop]
    fetcher = AsyncMock(return_value=manifest([prop]))
    assert await verify_cross_domain_properties(
        [prop], "network.example", AGENT, fetcher=fetcher
    ) == [prop]
    fetcher.assert_awaited_once_with("publisher.example")
    # A single authorized subdomain cannot vouch for every subdomain in a wildcard.
    assert (
        await verify_cross_domain_properties(
            [prop],
            "network.example",
            AGENT,
            fetcher=AsyncMock(return_value=manifest([site("ctv.publisher.example")])),
        )
        == []
    )
    with pytest.raises(AdagentsValidationError):
        properties_for_publisher_domain([prop], "*.publisher.example")


async def test_default_fetcher_is_sdk_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    prop = site("remote.example")
    fetcher = AsyncMock(return_value=manifest([prop]))
    monkeypatch.setattr("adcp.adagents.fetch_adagents", fetcher)
    assert await verify_cross_domain_properties([prop], "publisher.example", AGENT) == [prop]
    fetcher.assert_awaited_once_with("remote.example")


@pytest.mark.parametrize(
    "delegation", ["direct", "authoritative_location", "ads_txt_managerdomain"]
)
@pytest.mark.parametrize("authorized", [True, False])
async def test_real_discovery_requires_publisher_vouching(
    delegation: str, authorized: bool
) -> None:
    prop = site("remote.example", publisher_domain="remote.example")
    parent = manifest([prop])
    no_vouch = manifest([site("unrelated.example")])
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url == "https://remote.example/.well-known/adagents.json":
            if delegation == "authoritative_location":
                return httpx.Response(
                    200,
                    json={
                        "authoritative_location": "https://manager.example/.well-known/adagents.json"
                    },
                )
            if delegation == "ads_txt_managerdomain":
                return httpx.Response(404)
            return httpx.Response(200, json=parent if authorized else no_vouch)
        if url == "https://remote.example/ads.txt":
            return httpx.Response(200, text="MANAGERDOMAIN=manager.example\n")
        if url == "https://manager.example/.well-known/adagents.json":
            return httpx.Response(200, json=parent if authorized else no_vouch)
        raise AssertionError(f"Unexpected fetch: {url}")

    @asynccontextmanager
    async def transport(url: str, timeout: float) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            yield client

    async def fetcher(domain: str) -> dict[str, Any]:
        return await fetch_adagents(domain, transport_factory=transport)

    resolved = get_properties_by_agent(parent, AGENT)
    assert await verify_cross_domain_properties(
        resolved, "manager.example", AGENT, fetcher=fetcher
    ) == ([prop] if authorized else [])
    assert requested[0] == "https://remote.example/.well-known/adagents.json"
    if delegation == "ads_txt_managerdomain":
        assert "https://remote.example/ads.txt" in requested
    if delegation != "direct":
        assert "https://manager.example/.well-known/adagents.json" in requested
