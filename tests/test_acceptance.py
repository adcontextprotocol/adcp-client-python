from __future__ import annotations

import gzip
import hashlib
import json
import time
import zlib
from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from typing import Any

import httpx
import pytest
import rfc8785

from adcp import (
    AcceptanceContext,
    AcceptancePolicyAssessment,
    AcceptancePolicyCatalog,
    AcceptancePolicyDiagnosticCode,
    AcceptancePolicyDiscovery,
    AcceptancePolicyOutcome,
    AcceptancePolicyProfileIds,
    AcceptancePolicyResolver,
)
from adcp.types.core import Policy


def _jcs_digest(value: Mapping[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(rfc8785.dumps(dict(value))).hexdigest()}"


def _exact_digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _policy(
    policy_id: str = "policy.base",
    version: str = "1",
    *,
    acceptance_profile: dict[str, Any] | None = None,
) -> Policy:
    canonical = {"policy_id": policy_id, "version": version, "rule": "typed"}
    return Policy(
        policy_id=policy_id,
        version=version,
        name="Policy",
        category="standard",
        enforcement="required",
        policy="Display text is not executable.",
        content_digest=_jcs_digest(canonical),
        canonical_content=canonical,
        acceptance_profile=acceptance_profile,
    )


def _rule(
    rule_id: str,
    disposition: str,
    *,
    subject_category: str = "political",
    requirement: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "rule_id": rule_id,
        "subject_category": subject_category,
        "applies_to": ["media_buy"],
        "disposition": disposition,
        "policy_ids": ["policy.base"],
        **fields,
    }
    if requirement is not None:
        rule["requirements"] = [{"kind": requirement}]
    return rule


def _profile(
    profile_id: str,
    rules: list[dict[str, Any]],
    *,
    coverage: str = "complete",
    policy: Policy | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = policy or _policy()
    profile: dict[str, Any] = {
        "profile_id": profile_id,
        "version": "1",
        "policy_refs": [
            {
                "policy_id": policy.policy_id,
                "version": policy.version,
                "content_digest": policy.content_digest,
            }
        ],
        "coverage": coverage,
        "rules": rules,
    }
    if scope is not None:
        profile["scope"] = scope
    elif coverage == "complete":
        profile["scope"] = {
            "subject_categories": ["political"],
            "applies_to": ["media_buy"],
            "all_jurisdictions": True,
        }
    profile["content_digest"] = _jcs_digest(profile)
    return profile


def _catalog(*profiles: dict[str, Any]) -> bytes:
    return json.dumps(
        {"catalog_version": "1", "profiles": list(profiles)},
        separators=(",", ":"),
    ).encode()


def _discovery(body: bytes, *profile_ids: str) -> dict[str, Any]:
    return {
        "catalog_url": "https://seller.example/policies.json",
        "catalog_digest": _exact_digest(body),
        "default_profile_ids": list(profile_ids),
    }


def _context(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "subjects": [{"subject_category": "political"}],
        "delivery_jurisdictions": ["US"],
    }
    value.update(updates)
    return value


class _Registry:
    def __init__(self, *policies: Policy) -> None:
        self.policies = {(policy.policy_id, policy.version): policy for policy in policies}
        self.calls: list[tuple[str, str | None]] = []

    async def resolve_policy(self, policy_id: str, version: str | None = None) -> Policy | None:
        self.calls.append((policy_id, version))
        return self.policies.get((policy_id, version or ""))


class _ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    stream: httpx.AsyncByteStream | None = None,
    requests: list[httpx.Request] | None = None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        kwargs: dict[str, Any] = {"status_code": status, "headers": headers}
        if stream is None:
            kwargs["content"] = body
        else:
            kwargs["stream"] = stream
        return httpx.Response(request=request, **kwargs)

    monkeypatch.setattr(
        "adcp.acceptance.build_async_ip_pinned_transport",
        lambda _url: httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_complete_profile_allows_matching_context(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy()
    body = _catalog(_profile("seller-default", [_rule("allow", "allowed")], policy=policy))
    _install_transport(monkeypatch, body)
    resolver = AcceptancePolicyResolver(registry=_Registry(policy))

    result = await resolver.assess(
        _discovery(body, "seller-default"),
        _context(),
        applies_to="media_buy",
    )

    assert isinstance(result, AcceptancePolicyAssessment)
    assert result.outcome is AcceptancePolicyOutcome.allowed
    assert result.matching_rule_ids == ("allow",)
    assert not result.diagnostics


@pytest.mark.asyncio
async def test_assessment_fetches_uncached_catalog_once(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy()
    body = _catalog(_profile("p", [_rule("allow", "allowed")], policy=policy))
    requests: list[httpx.Request] = []
    _install_transport(monkeypatch, body, requests=requests)

    result = await AcceptancePolicyResolver(registry=_Registry(policy)).assess(
        _discovery(body, "p"), _context(), applies_to="media_buy"
    )

    assert result.outcome is AcceptancePolicyOutcome.allowed
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_most_restrictive_profile_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy()
    body = _catalog(
        _profile("default", [_rule("conditional", "conditional", requirement="disclosure")]),
        _profile("product", [_rule("blocked", "prohibited")]),
    )
    _install_transport(monkeypatch, body)

    result = await AcceptancePolicyResolver(registry=_Registry(policy)).assess(
        _discovery(body, "default"),
        _context(),
        applies_to="media_buy",
        product_profile_ids=["product"],
    )

    assert result.outcome is AcceptancePolicyOutcome.prohibited
    assert result.profile_ids == ("default", "product")
    assert set(result.matching_rule_ids) == {"conditional", "blocked"}


@pytest.mark.asyncio
async def test_accepts_generated_product_profile_ids_root_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    body = _catalog(
        _profile("default", [_rule("allow", "allowed")]),
        _profile("product", [_rule("blocked", "prohibited")]),
    )
    _install_transport(monkeypatch, body)

    result = await AcceptancePolicyResolver(registry=_Registry(policy)).assess(
        _discovery(body, "default"),
        _context(),
        applies_to="media_buy",
        product_profile_ids=AcceptancePolicyProfileIds.model_validate(["product"]),
    )

    assert result.outcome is AcceptancePolicyOutcome.prohibited


@pytest.mark.asyncio
async def test_rejects_bare_string_product_profile_ids_without_fetching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    body = _catalog(_profile("p", [_rule("allow", "allowed")]))
    requests: list[httpx.Request] = []
    _install_transport(monkeypatch, body, requests=requests)

    result = await AcceptancePolicyResolver(registry=_Registry(policy)).resolve(
        _discovery(body, "p"),
        product_profile_ids="product",  # type: ignore[arg-type]
    )

    assert result.diagnostics[0].code is AcceptancePolicyDiagnosticCode.invalid_profile_ids
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        ("disclosure", AcceptancePolicyOutcome.requires_disclosure),
        ("advertiser_verification", AcceptancePolicyOutcome.requires_setup),
        ("prior_authorization", AcceptancePolicyOutcome.requires_review),
    ],
)
async def test_conditional_outcome_buckets(
    monkeypatch: pytest.MonkeyPatch,
    requirement: str,
    expected: AcceptancePolicyOutcome,
) -> None:
    policy = _policy()
    body = _catalog(_profile("p", [_rule("conditional", "conditional", requirement=requirement)]))
    _install_transport(monkeypatch, body)

    result = await AcceptancePolicyResolver(registry=_Registry(policy)).assess(
        _discovery(body, "p"), _context(), applies_to="media_buy"
    )

    assert result.outcome is expected
    assert len(result.requirements) == 1


@pytest.mark.asyncio
async def test_missing_facts_and_partial_coverage_are_never_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    profile = _profile(
        "p",
        [_rule("role", "allowed", advertiser_roles=["political_actor"])],
        coverage="partial",
    )
    body = _catalog(profile)
    _install_transport(monkeypatch, body)

    result = await AcceptancePolicyResolver(registry=_Registry(policy)).assess(
        _discovery(body, "p"), _context(), applies_to="media_buy"
    )

    assert result.outcome is AcceptancePolicyOutcome.unknown
    assert {issue.code for issue in result.diagnostics} == {
        AcceptancePolicyDiagnosticCode.partial_coverage,
        AcceptancePolicyDiagnosticCode.incomplete_context,
    }


@pytest.mark.asyncio
async def test_prohibited_rule_wins_even_with_partial_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    body = _catalog(_profile("p", [_rule("blocked", "prohibited")], coverage="partial"))
    _install_transport(monkeypatch, body)

    result = await AcceptancePolicyResolver(registry=_Registry(policy)).assess(
        _discovery(body, "p"), _context(), applies_to="media_buy"
    )

    assert result.outcome is AcceptancePolicyOutcome.prohibited


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda body: _discovery(body, "missing"),
            AcceptancePolicyDiagnosticCode.profile_unresolved,
        ),
        (
            lambda body: {**_discovery(body, "p"), "catalog_digest": "sha256:" + "0" * 64},
            AcceptancePolicyDiagnosticCode.catalog_digest_mismatch,
        ),
    ],
)
async def test_resolution_failures_are_diagnostic_unknowns(
    monkeypatch: pytest.MonkeyPatch,
    mutation: Any,
    expected: AcceptancePolicyDiagnosticCode,
) -> None:
    policy = _policy()
    body = _catalog(_profile("p", [_rule("allow", "allowed")]))
    _install_transport(monkeypatch, body)

    result = await AcceptancePolicyResolver(registry=_Registry(policy)).assess(
        mutation(body), _context(), applies_to="media_buy"
    )

    assert result.outcome is AcceptancePolicyOutcome.unknown
    assert result.diagnostics[0].code is expected


@pytest.mark.asyncio
async def test_profile_and_policy_digest_pins_are_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    profile = _profile("p", [_rule("allow", "allowed")], policy=policy)
    profile["content_digest"] = "sha256:" + "0" * 64
    body = _catalog(profile)
    _install_transport(monkeypatch, body)

    result = await AcceptancePolicyResolver(registry=_Registry(policy)).resolve(
        _discovery(body, "p")
    )

    assert result.diagnostics[0].code is AcceptancePolicyDiagnosticCode.profile_digest_mismatch


@pytest.mark.asyncio
async def test_invalid_json_is_rejected_only_after_exact_digest_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b'{"catalog_version":'
    _install_transport(monkeypatch, body)

    result = await AcceptancePolicyResolver(registry=_Registry()).resolve(_discovery(body, "p"))

    assert result.diagnostics[0].code is AcceptancePolicyDiagnosticCode.catalog_invalid_json


@pytest.mark.asyncio
async def test_profile_ids_must_be_unique_across_local_and_registry_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    profile = _profile("same", [_rule("allow", "allowed")], policy=policy)
    catalog = {
        "catalog_version": "1",
        "profiles": [profile],
        "registry_profiles": [
            {
                "policy_id": "registry.profile",
                "policy_version": "1",
                "policy_digest": "sha256:" + "0" * 64,
                "profile_id": "same",
                "profile_version": "1",
                "profile_digest": profile["content_digest"],
            }
        ],
    }
    body = json.dumps(catalog, separators=(",", ":")).encode()
    _install_transport(monkeypatch, body)

    result = await AcceptancePolicyResolver(registry=_Registry(policy)).resolve(
        _discovery(body, "same")
    )

    assert result.diagnostics[0].code is AcceptancePolicyDiagnosticCode.duplicate_profile_id


@pytest.mark.asyncio
async def test_registry_policy_canonical_content_is_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    profile = _profile("p", [_rule("allow", "allowed")], policy=policy)
    body = _catalog(profile)
    _install_transport(monkeypatch, body)
    policy.content_digest = "sha256:" + "0" * 64

    result = await AcceptancePolicyResolver(registry=_Registry(policy)).resolve(
        _discovery(body, "p")
    )

    assert result.diagnostics[0].code is AcceptancePolicyDiagnosticCode.policy_digest_mismatch


@pytest.mark.asyncio
async def test_remote_identifiers_are_sanitized_from_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_id = "secret\n" + "x" * 500
    policy = _policy(remote_id)
    profile = _profile(
        "p",
        [
            {
                **_rule("allow", "allowed"),
                "policy_ids": [remote_id],
            }
        ],
        policy=policy,
    )
    body = _catalog(profile)
    _install_transport(monkeypatch, body)

    result = await AcceptancePolicyResolver(registry=_Registry()).resolve(_discovery(body, "p"))

    assert result.diagnostics[0].code is AcceptancePolicyDiagnosticCode.policy_unresolved
    assert result.diagnostics[0].policy_id is None
    assert "secret" not in repr(result.diagnostics)


@pytest.mark.asyncio
async def test_registry_profile_pins_outer_policy_and_embedded_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedded = _profile("shared", [_rule("allow", "allowed")])
    policy = _policy("registry.profile", acceptance_profile=embedded)
    catalog = {
        "catalog_version": "1",
        "registry_profiles": [
            {
                "policy_id": policy.policy_id,
                "policy_version": policy.version,
                "policy_digest": policy.content_digest,
                "profile_id": embedded["profile_id"],
                "profile_version": embedded["version"],
                "profile_digest": embedded["content_digest"],
            }
        ],
    }
    body = json.dumps(catalog, separators=(",", ":")).encode()
    _install_transport(monkeypatch, body)
    registry = _Registry(policy, _policy())

    result = await AcceptancePolicyResolver(registry=registry).assess(
        _discovery(body, "shared"), _context(), applies_to="media_buy"
    )

    assert result.outcome is AcceptancePolicyOutcome.allowed
    assert set(registry.calls) == {("registry.profile", "1"), ("policy.base", "1")}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "catalog",
    [
        {"catalog_version": "1", "profiles": []},
        {
            "catalog_version": "1",
            "profiles": [
                {
                    **_profile("p", [_rule("bad", "conditional", requirement="disclosure")]),
                    "rules": [_rule("bad", "conditional")],
                }
            ],
        },
        {
            "catalog_version": "1",
            "profiles": [
                {
                    key: value
                    for key, value in _profile("p", [_rule("ok", "allowed")]).items()
                    if key != "scope"
                }
            ],
        },
    ],
)
async def test_json_schema_conditionals_are_enforced(
    monkeypatch: pytest.MonkeyPatch,
    catalog: dict[str, Any],
) -> None:
    body = json.dumps(catalog, separators=(",", ":")).encode()
    _install_transport(monkeypatch, body)

    result = await AcceptancePolicyResolver(registry=_Registry(_policy())).resolve(
        _discovery(body, "p")
    )

    assert result.diagnostics[0].code is AcceptancePolicyDiagnosticCode.catalog_schema_invalid


@pytest.mark.asyncio
async def test_unknown_region_alias_invalidates_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile("p", [_rule("alias", "allowed", jurisdiction_groups=["EU"])])
    body = _catalog(profile)
    _install_transport(monkeypatch, body)

    result = await AcceptancePolicyResolver(registry=_Registry(_policy())).resolve(
        _discovery(body, "p")
    )

    assert result.diagnostics[0].code is AcceptancePolicyDiagnosticCode.invalid_region_alias


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://seller.example/catalog.json",
        "https://user:secret@seller.example/catalog.json",
        "https://seller.example/catalog.json#fragment",
    ],
)
async def test_unsafe_catalog_urls_fail_before_transport(
    url: str,
) -> None:
    result = await AcceptancePolicyResolver(registry=_Registry()).resolve(
        {
            "catalog_url": url,
            "catalog_digest": "sha256:" + "0" * 64,
            "default_profile_ids": ["p"],
        }
    )

    assert result.diagnostics[0].code is AcceptancePolicyDiagnosticCode.unsafe_catalog_url


@pytest.mark.asyncio
async def test_fetch_sends_no_credentials_and_rejects_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    _install_transport(
        monkeypatch,
        b"",
        status=302,
        headers={"Location": "https://other.example/catalog.json"},
        requests=requests,
    )

    result = await AcceptancePolicyResolver(registry=_Registry()).resolve(
        {
            "catalog_url": "https://seller.example/catalog.json",
            "catalog_digest": "sha256:" + "0" * 64,
            "default_profile_ids": ["p"],
        }
    )

    assert result.diagnostics[0].code is AcceptancePolicyDiagnosticCode.unsafe_catalog_url
    assert "authorization" not in requests[0].headers
    assert "cookie" not in requests[0].headers


@pytest.mark.asyncio
async def test_private_or_unresolvable_catalog_host_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(_url: str) -> httpx.AsyncBaseTransport:
        raise ValueError("private address details must not escape")

    monkeypatch.setattr("adcp.acceptance.build_async_ip_pinned_transport", reject)

    result = await AcceptancePolicyResolver(registry=_Registry()).resolve(
        {
            "catalog_url": "https://catalog.invalid/catalog.json",
            "catalog_digest": "sha256:" + "0" * 64,
            "default_profile_ids": ["p"],
        }
    )

    assert result.diagnostics[0].code is AcceptancePolicyDiagnosticCode.unsafe_catalog_url
    assert "private address" not in repr(result)


@pytest.mark.asyncio
async def test_dns_resolution_is_inside_catalog_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow_resolution(_url: str) -> httpx.AsyncBaseTransport:
        time.sleep(0.2)
        return httpx.MockTransport(lambda _request: httpx.Response(500))

    monkeypatch.setattr("adcp.acceptance.build_async_ip_pinned_transport", slow_resolution)
    started = time.monotonic()

    result = await AcceptancePolicyResolver(registry=_Registry(), timeout_seconds=0.005).resolve(
        {
            "catalog_url": "https://seller.example/catalog.json",
            "catalog_digest": "sha256:" + "0" * 64,
            "default_profile_ids": ["p"],
        }
    )

    assert result.diagnostics[0].code is AcceptancePolicyDiagnosticCode.catalog_timeout
    assert time.monotonic() - started < 0.15


@pytest.mark.asyncio
async def test_decoded_body_limit_stops_gzip_bomb_and_closes_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = gzip.compress(b"x" * 100_000)
    stream = _ChunkedStream([encoded[:5], encoded[5:]])
    _install_transport(
        monkeypatch,
        b"",
        headers={"Content-Encoding": "gzip"},
        stream=stream,
    )

    result = await AcceptancePolicyResolver(registry=_Registry(), max_body_bytes=128).resolve(
        {
            "catalog_url": "https://seller.example/catalog.json",
            "catalog_digest": "sha256:" + "0" * 64,
            "default_profile_ids": ["p"],
        }
    )

    assert result.diagnostics[0].code is AcceptancePolicyDiagnosticCode.catalog_too_large
    assert stream.closed


@pytest.mark.asyncio
async def test_fragmented_raw_deflate_catalog_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    body = _catalog(_profile("p", [_rule("allow", "allowed")]))
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    encoded = compressor.compress(body) + compressor.flush()
    stream = _ChunkedStream([bytes([value]) for value in encoded])
    _install_transport(
        monkeypatch,
        b"",
        headers={"Content-Encoding": "deflate"},
        stream=stream,
    )

    result = await AcceptancePolicyResolver(registry=_Registry(policy)).assess(
        _discovery(body, "p"), _context(), applies_to="media_buy"
    )

    assert result.outcome is AcceptancePolicyOutcome.allowed


@pytest.mark.asyncio
async def test_cache_uses_digest_and_capability_version_and_can_be_invalidated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    body = _catalog(_profile("p", [_rule("allow", "allowed")]))
    requests: list[httpx.Request] = []
    _install_transport(monkeypatch, body, requests=requests)
    resolver = AcceptancePolicyResolver(registry=_Registry(policy))
    discovery = _discovery(body, "p")

    first = await resolver.resolve(discovery, cache_ttl_seconds=60, capabilities_version="cap-1")
    second = await resolver.resolve(discovery, cache_ttl_seconds=60, capabilities_version="cap-1")
    third = await resolver.resolve(discovery, cache_ttl_seconds=60, capabilities_version="cap-2")
    await resolver.invalidate_capabilities()
    fourth = await resolver.resolve(discovery, cache_ttl_seconds=60, capabilities_version="cap-2")

    assert not first.from_cache
    assert second.from_cache
    assert not third.from_cache
    assert not fourth.from_cache
    assert len(requests) == 3


def test_acceptance_types_are_public_and_strict() -> None:
    assert AcceptancePolicyCatalog.model_validate
    assert AcceptancePolicyDiscovery.model_validate
    assert AcceptanceContext.model_validate
    with pytest.raises(Exception):
        AcceptancePolicyCatalog.model_validate({"catalog_version": "1", "unexpected": True})


def test_profile_digest_is_over_jcs_without_digest() -> None:
    profile = _profile("p", [_rule("allow", "allowed")])
    claimed = profile.pop("content_digest")
    assert claimed == _jcs_digest(profile)
    reordered = dict(reversed(list(profile.items())))
    assert claimed == _jcs_digest(reordered)


def test_catalog_digest_is_over_exact_representation_bytes() -> None:
    compact = b'{"catalog_version":"1"}'
    spaced = b'{"catalog_version": "1"}'
    assert _exact_digest(compact) != _exact_digest(spaced)


def test_fixture_mutations_do_not_share_state() -> None:
    profile = _profile("p", [_rule("allow", "allowed")])
    copy = deepcopy(profile)
    copy["rules"][0]["disposition"] = "prohibited"
    assert profile["rules"][0]["disposition"] == "allowed"
