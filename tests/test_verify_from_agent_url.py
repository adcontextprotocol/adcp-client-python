"""``verify_from_agent_url`` — single-call resolver+verifier factory.

Tests the orchestration layer: resolver feeds a JWK set to a
:class:`StaticJwksResolver`, that JWKS resolver feeds the existing
:func:`verify_starlette_request` verifier, errors map cleanly between
the two hierarchies. Happy-path JWKS verification is covered
end-to-end in ``tests/conformance/signing/test_e2e_fastapi.py``; this
file focuses on the factory's value-add (composition + error mapping).
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.signing import agent_resolver
from adcp.signing.agent_resolver import (
    AgentResolution,
    AgentResolverError,
    AgentResolverErrorCode,
    verify_from_agent_url,
)
from adcp.signing.agent_resolver import request_signature_code
from adcp.signing.errors import (
    REQUEST_SIGNATURE_BRAND_JSON_UNREACHABLE,
    REQUEST_SIGNATURE_BRAND_JSON_URL_MISSING,
    REQUEST_SIGNATURE_CAPABILITIES_UNREACHABLE,
    REQUEST_SIGNATURE_JWKS_UNAVAILABLE,
    REQUEST_SIGNATURE_JWKS_UNTRUSTED,
    SignatureVerificationError,
)
from adcp.signing.replay import InMemoryReplayStore

# ---- Test seams ----


class _FakeStarletteRequest:
    """Duck-types just enough of Starlette's Request for
    :func:`verify_starlette_request`."""

    def __init__(
        self,
        *,
        method: str = "POST",
        url: str = "https://seller.example.com/mcp",
        headers=None,
        body: bytes = b"",
    ) -> None:
        self.method = method
        self.url = url
        self.headers = headers or {}
        self._body = body

    async def body(self) -> bytes:
        return self._body


_RESOLVED_AGENT = AgentResolution(
    agent_url="https://buyer.example.com/mcp",
    brand_json_url="https://example.com/.well-known/brand.json",
    agent_entry={
        "type": "sales",
        "url": "https://buyer.example.com/mcp",
        "jwks_uri": "https://example.com/.well-known/jwks.json",
    },
    jwks_uri="https://example.com/.well-known/jwks.json",
    jwks={"keys": []},
    fetched_at=0.0,
    trace=[],
)


# ---- Happy path: resolver feeds verifier ----


@pytest.mark.parametrize("protocol", ["mcp", "a2a"])
@pytest.mark.asyncio
async def test_factory_passes_resolved_jwks_to_verifier(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    """When the resolver returns a JWK set, the verifier is constructed
    with a :class:`StaticJwksResolver` over those keys, the resolved
    agent_url is forwarded as ``options.agent_url``, and the verifier's
    return value flows back through unchanged. Pins the wiring so a
    refactor of the inner ``VerifyOptions`` shape can't silently drop
    the JWKS.
    """
    seen: dict[str, Any] = {}

    async def fake_resolve(*args, **kwargs):
        seen["protocol"] = kwargs["protocol"]
        return _RESOLVED_AGENT.model_copy(
            update={
                "agent_entry": {
                    **_RESOLVED_AGENT.agent_entry,
                    "url": "HTTPS://BUYER.example.com:443/mcp",
                },
                "brand_json": {"agents": [_RESOLVED_AGENT.agent_entry]},
            }
        )

    async def fake_verify_starlette(request, *, options):  # type: ignore[no-untyped-def]
        seen["options"] = options
        seen["request"] = request
        return "verified-signer-sentinel"

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)
    monkeypatch.setattr("adcp.signing.middleware.verify_starlette_request", fake_verify_starlette)

    request = _FakeStarletteRequest()
    result = await verify_from_agent_url(
        request,
        "https://buyer.example.com/mcp",
        agent_type="sales",
        operation="get_products",
        protocol=protocol,
    )
    assert result == "verified-signer-sentinel"
    assert seen["protocol"] == protocol
    assert seen["options"].operation == "get_products"
    assert seen["options"].agent_url == "https://buyer.example.com/mcp"
    assert seen["options"].operator_brand_json == {"agents": [_RESOLVED_AGENT.agent_entry]}
    # JWKS resolver constructed from the resolution's jwks set.
    assert seen["options"].jwks_resolver is not None


# ---- Resolver failure mapping ----


@pytest.mark.asyncio
async def test_factory_reports_capabilities_unreachable_with_its_own_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capabilities fetch that failed reports
    ``request_signature_capabilities_unreachable``.

    The discovery-chain rejection table assigns that code to "capabilities fetch
    failed (DNS, TCP, TLS, timeout, non-2xx)" and gives it a retry discipline of
    its own — retry once after jittered backoff, do not negative-cache beyond
    60 s — which differs from ``jwks_unavailable``'s bounded exponential backoff.
    Reporting the JWKS code for a capabilities failure hands the caller the wrong
    retry policy and names a hop the verifier never reached. The original
    :class:`AgentResolverError` chains via ``__cause__``.
    """

    async def fake_resolve(*args, **kwargs):
        raise AgentResolverError("capabilities_unreachable", "503")

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)

    with pytest.raises(SignatureVerificationError) as exc:
        await verify_from_agent_url(
            _FakeStarletteRequest(),
            "https://buyer.example.com/mcp",
            agent_type="sales",
            operation="get_products",
        )
    assert exc.value.code == REQUEST_SIGNATURE_CAPABILITIES_UNREACHABLE
    assert exc.value.step == "resolve"
    assert isinstance(exc.value.__cause__, AgentResolverError)
    assert exc.value.__cause__.code == "capabilities_unreachable"


#: Every resolver failure and the ``request_signature_*`` code the discovery-chain
#: rejection table assigns it. A default arm in the mapping collapses the rows that
#: share a hop, so the table is asserted row by row rather than in aggregate.
_RESOLVER_CODE_TO_SPEC_CODE: list[tuple[AgentResolverErrorCode, str]] = [
    ("invalid_agent_url", REQUEST_SIGNATURE_JWKS_UNTRUSTED),
    ("capabilities_unreachable", REQUEST_SIGNATURE_CAPABILITIES_UNREACHABLE),
    ("capabilities_invalid", REQUEST_SIGNATURE_BRAND_JSON_URL_MISSING),
    ("brand_json_url_missing", REQUEST_SIGNATURE_BRAND_JSON_URL_MISSING),
    ("brand_json_resolution_failed", REQUEST_SIGNATURE_BRAND_JSON_UNREACHABLE),
    ("jwks_fetch_failed", REQUEST_SIGNATURE_JWKS_UNAVAILABLE),
]


@pytest.mark.parametrize(("resolver_code", "spec_code"), _RESOLVER_CODE_TO_SPEC_CODE)
def test_request_signature_code_maps_each_resolver_failure(
    resolver_code: AgentResolverErrorCode, spec_code: str
) -> None:
    assert request_signature_code(AgentResolverError(resolver_code, "boom")) == spec_code


def test_request_signature_code_covers_every_resolver_code() -> None:
    """The table above enumerates the whole resolver surface.

    A new member of ``AgentResolverErrorCode`` added without a row fails here, which
    is the companion to the exhaustive match in ``request_signature_code`` — mypy
    refuses the unhandled arm, and this refuses the untested one.
    """
    from typing import get_args

    from adcp.signing.agent_resolver import AgentResolverErrorCode

    assert set(get_args(AgentResolverErrorCode)) == {
        row[0] for row in _RESOLVER_CODE_TO_SPEC_CODE
    }


#: Every brand.json hop outcome and the code the table assigns it. ``jwks_origin_mismatch``
#: is absent on purpose: the document parsed and matched, so no row describes it.
_BRAND_CODE_TO_SPEC_CODE = [
    ("agent_not_found", "request_signature_agent_not_in_brand_json"),
    ("agent_ambiguous", "request_signature_brand_json_ambiguous"),
    ("invalid_body", "request_signature_brand_json_malformed"),
    ("schema_invalid", "request_signature_brand_json_malformed"),
    ("invalid_url", "request_signature_brand_json_malformed"),
    ("invalid_house", "request_signature_brand_json_malformed"),
    ("fetch_failed", "request_signature_brand_json_unreachable"),
    ("redirect_loop", "request_signature_brand_json_unreachable"),
    ("redirect_depth_exceeded", "request_signature_brand_json_unreachable"),
]


@pytest.mark.parametrize(("brand_code", "spec_code"), _BRAND_CODE_TO_SPEC_CODE)
def test_brand_json_hop_reports_its_own_outcome(brand_code: str, spec_code: str) -> None:
    """A brand.json failure names what went wrong with the document.

    Content the resolver rejected (an unusable URL or house object) is malformed;
    a redirect loop or depth cap is a fetch failure. Leaving either unmapped sends
    it to the hop's default and reports a fetch failure for a document that was
    fetched.
    """
    from adcp.signing.agent_resolver import _brand_resolution_error

    exc = _brand_resolution_error(_FakeBrandError(brand_code))
    assert exc.signature_code == spec_code
    assert request_signature_code(exc) == spec_code


def test_jwks_origin_mismatch_has_no_assigned_code() -> None:
    """Pins the one brand.json outcome the table does not describe, so adding a row
    for it is a deliberate choice rather than a silent default."""
    from adcp.signing.agent_resolver import _brand_resolution_error

    exc = _brand_resolution_error(_FakeBrandError("jwks_origin_mismatch"))
    assert exc.signature_code is None


def test_request_signature_code_only_emits_codes_the_pinned_enum_defines() -> None:
    """Every code the mapping emits is a member of the pinned
    ``request-signing-error-code`` enum, including the explicit
    ``signature_code`` values the brand.json hop attaches."""
    from adcp.signing.agent_resolver import _brand_resolution_error
    from adcp.types.generated_poc.enums.request_signing_error_code import (
        RequestSigningErrorCode,
    )

    pinned = {member.value for member in RequestSigningErrorCode}
    emitted = {row[1] for row in _RESOLVER_CODE_TO_SPEC_CODE}
    for brand_code in ("agent_not_found", "agent_ambiguous", "invalid_body", "fetch_failed"):
        emitted.add(request_signature_code(_brand_resolution_error(_FakeBrandError(brand_code))))
    assert emitted <= pinned, emitted - pinned


class _FakeBrandError(Exception):
    """A :class:`BrandJsonResolverError`-shaped stand-in: ``_brand_resolution_error``
    reads only ``code`` and ``str()``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def test_request_signature_code_honours_an_explicit_signature_code() -> None:
    """The brand.json hop fans one resolver code out to four spec codes, carried
    on the exception rather than derived from ``code``."""
    exc = AgentResolverError(
        "brand_json_resolution_failed",
        "two entries matched",
        signature_code="request_signature_brand_json_ambiguous",
    )
    assert request_signature_code(exc) == "request_signature_brand_json_ambiguous"


@pytest.mark.asyncio
async def test_factory_maps_invalid_agent_url_to_jwks_untrusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``invalid_agent_url`` is a trust-boundary rejection (URL didn't
    canonicalize / scheme banned / SSRF-banned host) — the verifier-side
    semantic is ``JWKS_UNTRUSTED``, NOT ``JWKS_UNAVAILABLE``. Pins
    this discrimination so a refactor of the mapping table doesn't
    silently downgrade the security signal.
    """

    async def fake_resolve(*args, **kwargs):
        raise AgentResolverError("invalid_agent_url", "scheme: http not https")

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)

    with pytest.raises(SignatureVerificationError) as exc:
        await verify_from_agent_url(
            _FakeStarletteRequest(),
            "http://buyer.example.com/mcp",
            agent_type="sales",
            operation="get_products",
        )
    assert exc.value.code == REQUEST_SIGNATURE_JWKS_UNTRUSTED


@pytest.mark.asyncio
async def test_factory_reports_a_brand_json_fetch_failure_against_that_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brand.json fetch failure reports ``request_signature_brand_json_unreachable``.

    The table assigns that code to "brand.json fetch failed", with the same
    retry discipline as the capabilities hop. ``_brand_resolution_error`` already
    attaches it as an explicit ``signature_code`` on the real path; this asserts the
    derived default agrees, so a resolver error built either way reports one code.
    The receiver still sees a retryable discovery failure rather than an adversarial
    buyer — it now also learns which hop failed.
    """

    async def fake_resolve(*args, **kwargs):
        raise AgentResolverError(
            "brand_json_resolution_failed",
            "brand.json resolution failed: fetch_failed: HTTP 404",
        )

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)

    with pytest.raises(SignatureVerificationError) as exc:
        await verify_from_agent_url(
            _FakeStarletteRequest(),
            "https://buyer.example.com/mcp",
            agent_type="sales",
            operation="get_products",
        )
    assert exc.value.code == REQUEST_SIGNATURE_BRAND_JSON_UNREACHABLE


# ---- Verifier failure passes through ----


@pytest.mark.asyncio
async def test_factory_passes_verifier_errors_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the resolver succeeds but the verifier rejects (e.g.
    missing Signature header), the original
    :class:`SignatureVerificationError` propagates with its spec code
    intact — the factory does NOT remap verifier-side failures."""

    async def fake_resolve(*args, **kwargs):
        return _RESOLVED_AGENT

    async def fake_verify_starlette(request, *, options):  # type: ignore[no-untyped-def]
        raise SignatureVerificationError(
            "request_signature_required",
            step=0,
            message="Signature header missing",
        )

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)
    monkeypatch.setattr("adcp.signing.middleware.verify_starlette_request", fake_verify_starlette)

    with pytest.raises(SignatureVerificationError) as exc:
        await verify_from_agent_url(
            _FakeStarletteRequest(),
            "https://buyer.example.com/mcp",
            agent_type="sales",
            operation="get_products",
        )
    assert exc.value.code == "request_signature_required"
    assert exc.value.step == 0


# ---- key_origins consistency wiring ----


def _resolved_with_origins(key_origins: dict[str, str] | None) -> AgentResolution:
    """Variant of ``_RESOLVED_AGENT`` carrying a specific ``key_origins`` map.

    Used to pin the verifier-side wiring: ``verify_from_agent_url`` must
    surface ``identity.key_origins`` from the capabilities response into
    ``VerifyOptions.expected_key_origins`` so the verifier's
    ``_maybe_check_key_origin`` step actually engages. Without this the
    SDK's recommended buyer entrypoint silently skips the spec's
    shared-tenancy-spoof defense.
    """
    return AgentResolution(
        agent_url="https://buyer.example.com/mcp",
        brand_json_url="https://example.com/.well-known/brand.json",
        agent_entry={
            "type": "sales",
            "url": "https://buyer.example.com/mcp",
            "jwks_uri": "https://example.com/.well-known/jwks.json",
        },
        jwks_uri="https://example.com/.well-known/jwks.json",
        jwks={"keys": []},
        fetched_at=0.0,
        key_origins=key_origins,
        trace=[],
    )


@pytest.mark.asyncio
async def test_factory_threads_key_origins_into_verify_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the resolution carries an ``identity.key_origins`` map,
    ``VerifyOptions.expected_key_origins`` is set to it. Pins the
    production wiring against the bypass the original review flagged
    (Argus on PR #789): without this the SDK's recommended helper
    builds ``VerifyOptions`` with ``expected_key_origins=None`` and the
    spec's consistency check silently no-ops in production."""
    origins = {"request_signing": "https://example.com"}
    seen: dict[str, Any] = {}

    async def fake_resolve(*args, **kwargs):
        return _resolved_with_origins(origins)

    async def fake_verify_starlette(request, *, options):  # type: ignore[no-untyped-def]
        seen["options"] = options
        return "ok"

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)
    monkeypatch.setattr("adcp.signing.middleware.verify_starlette_request", fake_verify_starlette)

    await verify_from_agent_url(
        _FakeStarletteRequest(),
        "https://buyer.example.com/mcp",
        agent_type="sales",
        operation="get_products",
    )
    assert seen["options"].expected_key_origins == origins
    # Default ``signing_purpose`` is ``"request_signing"`` — the most
    # common buyer path. Webhook callers override.
    assert seen["options"].signing_purpose == "request_signing"


@pytest.mark.asyncio
async def test_factory_uses_secure_replay_store_default_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The convenience factory reuses one per-origin bounded cache."""
    seen: list[Any] = []
    monkeypatch.setattr(agent_resolver, "_DEFAULT_REPLAY_STORE", InMemoryReplayStore())

    async def fake_resolve(*args, **kwargs):
        return _resolved_with_origins(None)

    async def fake_verify_starlette(request, *, options):  # type: ignore[no-untyped-def]
        seen.append(options.replay_store.claim("shared-kid", "shared-nonce", 60.0))
        return "ok"

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)
    monkeypatch.setattr("adcp.signing.middleware.verify_starlette_request", fake_verify_starlette)

    await verify_from_agent_url(
        _FakeStarletteRequest(),
        "https://buyer.example.com/mcp",
        agent_type="sales",
        operation="get_products",
    )
    await verify_from_agent_url(
        _FakeStarletteRequest(),
        "https://buyer.example.com/mcp",
        agent_type="sales",
        operation="get_products",
    )

    assert seen == ["claimed", "replayed"]


def test_default_replay_state_survives_many_counterparty_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_resolver,
        "_DEFAULT_REPLAY_STORE",
        InMemoryReplayStore(per_keyid_cap=10, global_cap=2_000),
    )
    original = agent_resolver._default_replay_store_for_origin("https://a.example:443")
    assert original.claim("kid", "nonce", 60.0) == "claimed"  # type: ignore[attr-defined]

    for index in range(1_025):
        partition = agent_resolver._default_replay_store_for_origin(
            f"https://origin-{index}.example:443"
        )
        assert partition.claim("kid", f"nonce-{index}", 60.0) == "claimed"  # type: ignore[attr-defined]

    same_origin = agent_resolver._default_replay_store_for_origin("https://a.example:443")
    assert same_origin.claim("kid", "nonce", 60.0) == "replayed"  # type: ignore[attr-defined]


def test_default_replay_store_namespaces_identical_key_and_nonce_by_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_resolver, "_DEFAULT_REPLAY_STORE", InMemoryReplayStore())
    first = agent_resolver._default_replay_store_for_origin("https://a.example:443")
    second = agent_resolver._default_replay_store_for_origin("https://b.example:443")

    assert first.claim("kid", "nonce", 60.0) == "claimed"  # type: ignore[attr-defined]
    assert second.claim("kid", "nonce", 60.0) == "claimed"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_factory_preserves_explicit_replay_store_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers can still explicitly opt out for compatibility or tests."""
    seen: dict[str, Any] = {}

    async def fake_resolve(*args, **kwargs):
        return _resolved_with_origins(None)

    async def fake_verify_starlette(request, *, options):  # type: ignore[no-untyped-def]
        seen["options"] = options
        return "ok"

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)
    monkeypatch.setattr("adcp.signing.middleware.verify_starlette_request", fake_verify_starlette)

    await verify_from_agent_url(
        _FakeStarletteRequest(),
        "https://buyer.example.com/mcp",
        agent_type="sales",
        operation="get_products",
        replay_store=None,
    )

    assert seen["options"].replay_store is None


@pytest.mark.asyncio
async def test_factory_passes_signing_purpose_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new ``signing_purpose`` kwarg propagates to ``VerifyOptions``
    so webhook-signing callers can route to ``identity.key_origins.webhook_signing``
    instead of ``request_signing``. Default is request-side; webhook
    paths must opt in."""
    seen: dict[str, Any] = {}

    async def fake_resolve(*args, **kwargs):
        return _resolved_with_origins({"webhook_signing": "https://hooks.example.com"})

    async def fake_verify_starlette(request, *, options):  # type: ignore[no-untyped-def]
        seen["options"] = options
        return "ok"

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)
    monkeypatch.setattr("adcp.signing.middleware.verify_starlette_request", fake_verify_starlette)

    await verify_from_agent_url(
        _FakeStarletteRequest(),
        "https://buyer.example.com/mcp",
        agent_type="sales",
        operation="webhook",
        signing_purpose="webhook_signing",
        posture="supported",
    )
    assert seen["options"].signing_purpose == "webhook_signing"
    assert seen["options"].posture == "supported"


@pytest.mark.asyncio
async def test_factory_resolver_carries_brand_json_source_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The JWKS resolver passed to the verifier MUST carry
    ``jwks_source = "brand_json"`` so the verifier's
    ``_maybe_check_key_origin`` step engages. A bare
    :class:`StaticJwksResolver` (which is what the pre-fix code used)
    would skip the check because ``jwks_source`` is absent — exactly
    the bypass Argus flagged on the original PR #789."""
    seen: dict[str, Any] = {}

    async def fake_resolve(*args, **kwargs):
        return _resolved_with_origins({"request_signing": "https://example.com"})

    async def fake_verify_starlette(request, *, options):  # type: ignore[no-untyped-def]
        seen["options"] = options
        return "ok"

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)
    monkeypatch.setattr("adcp.signing.middleware.verify_starlette_request", fake_verify_starlette)

    await verify_from_agent_url(
        _FakeStarletteRequest(),
        "https://buyer.example.com/mcp",
        agent_type="sales",
        operation="get_products",
    )
    resolver = seen["options"].jwks_resolver
    # The discriminant is on the class, not the instance — the verifier
    # reads it via ``type(resolver).jwks_source`` so subclassing carries
    # the marker without per-instance state.
    assert getattr(type(resolver), "jwks_source", None) == "brand_json"


@pytest.mark.asyncio
async def test_factory_passes_none_origins_when_capabilities_omit_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators on legacy 3.0 deployments don't advertise
    ``identity.key_origins``. The resolution carries ``None`` and the
    factory propagates it; the verifier-side check skips on
    ``expected_key_origins is None`` per the ``_maybe_check_key_origin``
    contract. This preserves back-compat — the new defense engages
    only when the operator opts in by publishing the origins map."""
    seen: dict[str, Any] = {}

    async def fake_resolve(*args, **kwargs):
        return _resolved_with_origins(None)

    async def fake_verify_starlette(request, *, options):  # type: ignore[no-untyped-def]
        seen["options"] = options
        return "ok"

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)
    monkeypatch.setattr("adcp.signing.middleware.verify_starlette_request", fake_verify_starlette)

    await verify_from_agent_url(
        _FakeStarletteRequest(),
        "https://buyer.example.com/mcp",
        agent_type="sales",
        operation="get_products",
    )
    assert seen["options"].expected_key_origins == {}


# ---- Integration: production resolver drives the real verifier path ----
#
# The mocked tests above pin the wiring; this test pins the END-TO-END:
# the production ``_BrandJsonStaticJwksResolver`` class drives the real
# ``_maybe_check_key_origin`` step (no mocking of ``verify_starlette_request``
# or the verifier internals). Catches the class of bug Argus flagged on
# the first pass: the subclass carried the ``jwks_source`` marker but
# not the ``jwks_uri`` attribute, so the verifier's
# ``getattr(resolver, "jwks_uri", None) → None`` produced an
# ``actual_origin=""`` mismatch on every legitimate signer.


def test_brand_json_static_resolver_carries_jwks_uri_attribute() -> None:
    """``_BrandJsonStaticJwksResolver`` MUST expose ``jwks_uri`` on the
    instance — the verifier reads it via ``getattr(resolver, "jwks_uri",
    None)`` and compares the host to the declared origin. Argus's first
    review caught this gap; the subclass needs the URI even though
    ``StaticJwksResolver.__init__`` doesn't accept it."""
    from adcp.signing.agent_resolver import _BrandJsonStaticJwksResolver

    resolver = _BrandJsonStaticJwksResolver(
        {"keys": []},
        jwks_uri="https://keys.brand.example/.well-known/jwks.json",
    )
    assert resolver.jwks_uri == "https://keys.brand.example/.well-known/jwks.json"
    assert type(resolver).jwks_source == "brand_json"


def test_maybe_check_key_origin_accepts_brand_json_resolver_with_matching_origin() -> None:
    """Drives the production ``_BrandJsonStaticJwksResolver`` against
    the real ``_maybe_check_key_origin`` step. Without the subclass's
    ``__init__`` storing ``jwks_uri``, this check would fire mismatch
    on every legitimate signer (the verifier would compare
    ``actual_origin=""`` against the declared origin and reject).
    The success path here is the regression test for that bypass."""
    from adcp.signing.agent_resolver import _BrandJsonStaticJwksResolver
    from adcp.signing.verifier import _maybe_check_key_origin

    resolver = _BrandJsonStaticJwksResolver(
        {"keys": []},
        jwks_uri="https://keys.brand.example/.well-known/jwks.json",
    )

    # No raise — host matches the declared origin.
    _maybe_check_key_origin(
        resolver=resolver,
        expected_key_origins={"request_signing": "https://keys.brand.example"},
        signing_purpose="request_signing",
        posture=None,
    )


def test_maybe_check_key_origin_rejects_mismatched_origin_on_brand_json_resolver() -> None:
    """Negative side of the integration check: same production resolver
    class, but the declared origin points elsewhere → step 7 raises
    ``request_signature_key_origin_mismatch`` with the host pair in the
    detail map. Pins the rejection direction (so a future refactor
    can't accidentally flip the comparison)."""
    from adcp.signing.agent_resolver import _BrandJsonStaticJwksResolver
    from adcp.signing.errors import REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH
    from adcp.signing.verifier import _maybe_check_key_origin

    resolver = _BrandJsonStaticJwksResolver(
        {"keys": []},
        jwks_uri="https://keys.brand.example/.well-known/jwks.json",
    )

    with pytest.raises(SignatureVerificationError) as exc_info:
        _maybe_check_key_origin(
            resolver=resolver,
            expected_key_origins={"request_signing": "https://attacker.example"},
            signing_purpose="request_signing",
            posture=None,
        )
    assert exc_info.value.code == REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH
    assert exc_info.value.detail is not None
    assert exc_info.value.detail["actual_origin"] == "keys.brand.example"
    assert exc_info.value.detail["expected_origin"] == "attacker.example"


@pytest.mark.asyncio
async def test_factory_construction_threads_jwks_uri_into_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``verify_from_agent_url`` MUST construct the wrapper with
    ``jwks_uri=resolution.jwks_uri`` so the verifier's consistency
    check has a real host to compare. The bug Argus traced on the
    first review pass was exactly this construction site missing the
    keyword."""
    seen: dict[str, Any] = {}

    async def fake_resolve(*args, **kwargs):
        return _resolved_with_origins({"request_signing": "https://example.com"})

    async def fake_verify_starlette(request, *, options):  # type: ignore[no-untyped-def]
        seen["options"] = options
        return "ok"

    monkeypatch.setattr(agent_resolver, "async_resolve_agent", fake_resolve)
    monkeypatch.setattr("adcp.signing.middleware.verify_starlette_request", fake_verify_starlette)

    await verify_from_agent_url(
        _FakeStarletteRequest(),
        "https://buyer.example.com/mcp",
        agent_type="sales",
        operation="get_products",
    )
    resolver = seen["options"].jwks_resolver
    # Production resolver MUST carry the URI as an instance attribute,
    # not just the source-marker class attribute.
    assert resolver.jwks_uri == "https://example.com/.well-known/jwks.json"


# ---- BrandSourcedJwksResolver Protocol (Argus follow-up) ----


def test_brand_json_static_resolver_satisfies_brand_sourced_protocol() -> None:
    """``_BrandJsonStaticJwksResolver`` MUST satisfy
    :class:`BrandSourcedJwksResolver` at runtime. The Protocol surfaces
    the duck-typed ``jwks_source`` + ``jwks_uri`` contract as a typed
    predicate so verifier-side ``isinstance`` checks work and adopters
    declaring custom brand.json-walking resolvers can opt in by
    setting the two attributes (no inheritance required)."""
    from adcp.signing import BrandSourcedJwksResolver
    from adcp.signing.agent_resolver import _BrandJsonStaticJwksResolver

    resolver = _BrandJsonStaticJwksResolver(
        {"keys": []},
        jwks_uri="https://keys.brand.example/.well-known/jwks.json",
    )
    assert isinstance(resolver, BrandSourcedJwksResolver)


def test_static_jwks_resolver_does_not_satisfy_brand_sourced_protocol() -> None:
    """A bare :class:`StaticJwksResolver` MUST NOT satisfy the
    BrandSourcedJwksResolver Protocol — it carries neither
    ``jwks_source`` nor ``jwks_uri``. The check skip on absence is
    the back-compat path for adopter resolvers that predate the
    discriminant."""
    from adcp.signing import BrandSourcedJwksResolver, StaticJwksResolver

    resolver = StaticJwksResolver({"keys": []})
    assert not isinstance(resolver, BrandSourcedJwksResolver)


# ---- Misconfig warnings (Argus first-pass follow-ups) ----


def test_brand_json_source_without_capabilities_map_uses_shared_origin_posture() -> None:
    from adcp.signing.agent_resolver import _BrandJsonStaticJwksResolver
    from adcp.signing.verifier import _maybe_check_key_origin

    resolver = _BrandJsonStaticJwksResolver(
        {"keys": []},
        jwks_uri="https://keys.brand.example/jwks.json",
    )
    _maybe_check_key_origin(
        resolver=resolver,
        expected_key_origins=None,
        signing_purpose="request_signing",
        posture=None,
    )


def test_legacy_resolver_with_expected_origins_emits_deprecation_warning() -> None:
    """A resolver without a ``jwks_source`` attribute (a pre-#776
    adopter resolver) paired with ``expected_key_origins`` set is
    misconfig in the other direction — the SDK silently downgrades
    to no-check. Surface as :class:`DeprecationWarning` so the adopter
    sees the upgrade signal on next deploy."""
    import warnings as _w

    from adcp.signing import StaticJwksResolver
    from adcp.signing.verifier import _maybe_check_key_origin

    # StaticJwksResolver carries no jwks_source — legacy adopter shape.
    resolver = StaticJwksResolver({"keys": []})
    with _w.catch_warnings(record=True) as caught:
        _w.simplefilter("always")
        _maybe_check_key_origin(
            resolver=resolver,
            expected_key_origins={"request_signing": "https://example.com"},
            signing_purpose="request_signing",
            posture=None,
        )
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1
    assert "jwks_source" in str(deprecations[0].message)


def test_no_warning_when_both_attributes_align() -> None:
    """The happy path — brand_json resolver + supplied origins — must
    not emit either warning. Pin the no-noise direction so a future
    refactor can't accidentally fire warnings on legitimate verifier
    invocations."""
    import warnings as _w

    from adcp.signing.agent_resolver import _BrandJsonStaticJwksResolver
    from adcp.signing.verifier import _maybe_check_key_origin

    resolver = _BrandJsonStaticJwksResolver(
        {"keys": []},
        jwks_uri="https://keys.brand.example/jwks.json",
    )
    with _w.catch_warnings(record=True) as caught:
        _w.simplefilter("always")
        _maybe_check_key_origin(
            resolver=resolver,
            expected_key_origins={"request_signing": "https://keys.brand.example"},
            signing_purpose="request_signing",
            posture=None,
        )
    misconfig = [
        w
        for w in caught
        if issubclass(w.category, (UserWarning, DeprecationWarning))
        and "jwks_source" in str(w.message)
    ]
    assert misconfig == []


# ---- _extract_key_origins length cap (Argus follow-up nit #1) ----


def test_extract_key_origins_caps_oversized_entries() -> None:
    """Each origin value must be clamped at 512 bytes — well above any
    legitimate ``scheme+host+port`` shape but tight enough that a
    pathological multi-kilobyte value from the 64 KiB capabilities body
    doesn't propagate through downstream comparisons. Oversized entries
    are SKIPPED (not truncated — a truncated host would silently match
    the wrong domain)."""
    from adcp.signing.agent_resolver import _extract_key_origins

    huge = "https://" + "x" * 1024 + ".com"
    legit = "https://keys.brand.com"
    result = _extract_key_origins(
        {
            "identity": {
                "key_origins": {
                    "request_signing": legit,
                    "webhook_signing": huge,  # skipped
                }
            }
        }
    )
    assert result == {"request_signing": legit}


# ---- Diagnostic host-only fallback (Argus follow-up nit #2) ----


def test_mismatch_detail_uses_host_only_fallback_on_canonicalization_failure() -> None:
    """When ``_origin_host`` can't canonicalize one side (e.g. spaces
    in the host), the mismatch ``expected_origin`` / ``actual_origin``
    detail values must still be HOST-SHAPED — not the full raw URL.
    Previous behavior leaked the full URL into the host-labeled field,
    inconsistent with the success path. Now the diagnostic uses a
    best-effort host extraction via ``_extract_host``."""
    from adcp.signing.errors import REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH
    from adcp.signing.key_origins import check_key_origin_consistency

    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="https://keys.brand.example/jwks.json",
            key_origins={"request_signing": "not a host with spaces"},
            purpose="request_signing",
        )
    assert exc_info.value.code == REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH
    detail = exc_info.value.detail
    assert detail is not None
    # actual_origin canonicalizes cleanly to the host (no URL form).
    assert detail["actual_origin"] == "keys.brand.example"
    # expected_origin failed to canonicalize but still doesn't leak the
    # full raw string with quoting artifacts — empty string at worst,
    # never the full URL.
    assert "/" not in detail["expected_origin"]


# ---- jwks_uri=None routes to JWKS_UNAVAILABLE (Argus follow-up #4) ----


def test_maybe_check_key_origin_jwks_uri_none_routes_to_jwks_unavailable() -> None:
    """A brand-json resolver that hasn't populated ``jwks_uri`` (cold
    cache + failed refresh, or a misconfigured custom resolver) is a
    resolver-side I/O failure, not an origin mismatch. The verifier
    must surface ``REQUEST_SIGNATURE_JWKS_UNAVAILABLE`` so dashboards
    aggregate this cold-cache shape with other resolver-fetch
    failures rather than with adversarial origin-mismatch traffic."""
    from adcp.signing.errors import REQUEST_SIGNATURE_JWKS_UNAVAILABLE
    from adcp.signing.verifier import _maybe_check_key_origin

    class _BrandJsonResolverWithNoJwksUri:
        jwks_source = "brand_json"
        # ``jwks_uri`` deliberately absent / None — cold cache shape.
        jwks_uri = None

        def __call__(self, keyid: str) -> dict | None:  # type: ignore[type-arg]
            return None

    with pytest.raises(SignatureVerificationError) as exc_info:
        _maybe_check_key_origin(
            resolver=_BrandJsonResolverWithNoJwksUri(),  # type: ignore[arg-type]
            expected_key_origins={"request_signing": "https://keys.brand.example"},
            signing_purpose="request_signing",
            posture=None,
        )
    assert exc_info.value.code == REQUEST_SIGNATURE_JWKS_UNAVAILABLE
    assert exc_info.value.detail == {"purpose": "request_signing"}
