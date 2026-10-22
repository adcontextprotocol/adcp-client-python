"""AdCP 3.3 R4 (adcontextprotocol/adcp#8299): JWS artifacts on the WBA key directory.

A governance context, designated-task response signing and a rights
attestation whose issuer origin ``O`` is listed by the trust record derived
from ``O`` are verified with an EdDSA key from ``O``'s key directory (``kid``
the JWK thumbprint, no key-purpose checks, revocation by ``O``'s list), and
historical governance verification uses the governance key archive at ``O``.
A fetch failure never selects the 3.2 profile; unlisted issuers stay on it.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

import pytest
import rfc8785
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from joserfc import jws as jose_jws
from joserfc.jwk import ECKey, OKPKey

from adcp.governance import (
    GovernanceAuthorizationFailure,
    InMemoryGovernanceReplayStore,
    build_governance_commitment,
    issue_governance_authorization,
    verify_governance_authorization,
    verify_historical_governance_context,
)
from adcp.signing.crypto import b64url_encode
from adcp.signing.jws import _AdcpJwsRegistry
from adcp.signing.jws_artifacts import (
    GOVERNANCE_KEY_ARCHIVE_PATH,
    HttpGovernanceKeyArchive,
    JwsArtifactError,
    JwsArtifactUses32ProfileError,
    WbaJwsArtifactVerifier,
    compact_jws_from_envelope,
    select_jws_artifact_profile,
)
from adcp.signing.signer_keys import ResolvedSignerKey
from adcp.signing.trust import TrustRecordEvaluator, TrustResponse, trust_record_url
from adcp.signing.wba import WbaDiscoveryError, public_directory_key

GOV = "https://gov.example.com"
GOV_ISSUER = GOV + "/governance"
BRAND_AGENT = "https://brand-agent.example.com"
RIGHTS_AGENT = "https://rights.example.com"
SELLER = "https://seller.example/sales"
BUYER = "https://buyer.example"
NOW = 1_000


# ---------------------------------------------------------------------------
# Fixtures: trust records, key directories, revocation lists
# ---------------------------------------------------------------------------


def _records(documents: dict[str, Any], fetched: list[str] | None = None) -> TrustRecordEvaluator:
    async def fetch(url, *, allow_private=False):
        if fetched is not None:
            fetched.append(url)
        document = documents.get(url)
        if isinstance(document, Exception):
            raise document
        if isinstance(document, TrustResponse):
            return document
        if document is None:
            return TrustResponse(b"", 404)
        return TrustResponse(json.dumps(document).encode())

    return TrustRecordEvaluator(fetcher=fetch, clock=lambda: NOW)


def _listing(origin: str, role: str = "adcp:governance") -> dict[str, Any]:
    return {trust_record_url(origin): {"agents": [{"url": origin, "roles": [role]}]}}


class Directory:
    """An origin-scoped key directory: (origin, thumbprint) -> JWK."""

    def __init__(self, keys: dict[str, list[Mapping[str, Any]]] | None = None) -> None:
        self.keys = {origin: list(jwks) for origin, jwks in (keys or {}).items()}
        self.calls: list[tuple[str, str]] = []
        self.failure: Exception | None = None

    async def __call__(self, keyid: str, *, signer_origin: str) -> ResolvedSignerKey | None:
        self.calls.append((signer_origin, keyid))
        if self.failure is not None:
            raise self.failure
        for jwk in self.keys.get(signer_origin, []):
            if jwk.get("kid") == keyid:
                return ResolvedSignerKey(signer_origin, dict(jwk))
        return None


class Revocation:
    def __init__(self, status: str = "current") -> None:
        self.state = status
        self.calls: list[tuple[str, str]] = []

    async def status(self, origin: str, keyid: str) -> str:
        self.calls.append((origin, keyid))
        return self.state


def _wba_key() -> tuple[ed25519.Ed25519PrivateKey, dict[str, Any]]:
    private = ed25519.Ed25519PrivateKey.generate()
    return private, public_directory_key(private)


def _verifier(
    documents: dict[str, Any],
    directory: Directory,
    revocation: Revocation | None = None,
    archive: Any = None,
) -> WbaJwsArtifactVerifier:
    return WbaJwsArtifactVerifier(
        records=_records(documents),
        signer_keys=directory,
        revocation=revocation or Revocation(),
        archive=archive,
    )


class ForbiddenJwks:
    """A 3.2 resolver that must not be consulted on the WBA path."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve(self, kid: str) -> dict[str, Any]:
        self.calls.append(kid)
        raise AssertionError("the 3.2 key chain was consulted")


def _governance_token(private: Any, kid: str, *, alg: str = "EdDSA", **changes: Any) -> str:
    options: dict[str, Any] = {
        "private_key": private,
        "key_id": kid,
        "alg": alg,
        "issuer": GOV_ISSUER,
        "subject": "opaque-action",
        "plan_hash": "opaque-plan-hash",
        "audience": SELLER,
        "caller": BUYER,
        "check_id": "check-1",
        "task": "create_media_buy",
        "payload": {"idempotency_key": "request-1", "amount": 1},
        "authorized_commitment": build_governance_commitment(1, "USD"),
        "issued_at": NOW,
        "expires_at": NOW + 600,
        "jti": "token-1",
    }
    options.update(changes)
    return issue_governance_authorization(**options)


async def _verify_governance(token: str, verifier: WbaJwsArtifactVerifier | None, jwks: Any, **kw):
    options: dict[str, Any] = {
        "token": token,
        "expected_issuer": GOV_ISSUER,
        "expected_audience": SELLER,
        "authenticated_caller": BUYER,
        "expected_task": "create_media_buy",
        "payload": {"idempotency_key": "request-1", "amount": 1},
        "actual_commitment": build_governance_commitment(1, "USD"),
        "jwks": jwks,
        "replay_store": InMemoryGovernanceReplayStore(),
        "now": lambda: NOW,
        "web_bot_auth": verifier,
    }
    options.update(kw)
    return await verify_governance_authorization(**options)


# ---------------------------------------------------------------------------
# Selection (R4, R3a step 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["adcp:governance", "adcp:buying", "adcp:sales"])
async def test_a_record_that_lists_the_origin_with_any_role_selects_wba(role):
    profile = await select_jws_artifact_profile(GOV_ISSUER, _records(_listing(GOV, role)))
    assert (profile.profile, profile.origin, profile.reason) == ("web-bot-auth", GOV, "listed")


async def test_a_record_that_does_not_list_the_origin_selects_3_2():
    documents = {trust_record_url(GOV): {"agents": [{"url": "https://other.example.com"}]}}
    documents[trust_record_url(GOV)]["agents"][0]["roles"] = ["adcp:governance"]
    profile = await select_jws_artifact_profile(GOV_ISSUER, _records(documents))
    assert (profile.profile, profile.reason) == ("3.2", "unlisted")


async def test_an_operator_that_never_published_a_record_selects_3_2():
    profile = await select_jws_artifact_profile(GOV_ISSUER, _records({}))
    assert (profile.profile, profile.reason) == ("3.2", "no_trust_record")


@pytest.mark.parametrize("url", ["https://github.io/gov", "https://203.0.113.7/gov"])
async def test_an_origin_with_no_registrable_domain_selects_3_2_without_fetching(url):
    fetched: list[str] = []
    profile = await select_jws_artifact_profile(url, _records({}, fetched))
    assert (profile.profile, profile.reason) == ("3.2", "no_trust_record")
    assert fetched == []


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("connection reset"),
        TrustResponse(b"", 503),
        TrustResponse(b"{not json"),
        TrustResponse(b'{"agents": "not a list"}'),
    ],
    ids=["transport", "http-503", "not-json", "invalid-record"],
)
async def test_a_trust_record_fetch_failure_is_not_absence(failure):
    """A record that cannot be read never selects the 3.2 profile."""
    with pytest.raises(JwsArtifactError) as caught:
        await select_jws_artifact_profile(GOV_ISSUER, _records({trust_record_url(GOV): failure}))
    assert caught.value.code == "jws_key_unavailable"
    assert caught.value.transient


async def test_a_404_after_the_operator_published_is_an_outage_not_absence():
    documents: dict[str, Any] = dict(_listing(GOV))
    records = _records(documents)
    assert (await select_jws_artifact_profile(GOV_ISSUER, records)).profile == "web-bot-auth"
    records._cache.clear()  # the polling interval passes
    records._responses.clear()
    del documents[trust_record_url(GOV)]
    with pytest.raises(JwsArtifactError) as caught:
        await select_jws_artifact_profile(GOV_ISSUER, records)
    assert caught.value.code == "jws_key_unavailable"


@pytest.mark.parametrize("url", ["http://gov.example.com", "https://user@gov.example.com", "gov"])
async def test_a_signing_agent_url_must_be_an_https_origin(url):
    with pytest.raises(JwsArtifactError) as caught:
        await select_jws_artifact_profile(url, _records(_listing(GOV)))
    assert caught.value.code == "jws_invalid"


def _bound(*origins: str, calls: list[str] | None = None):
    async def lookup(origin: str) -> bool:
        if calls is not None:
            calls.append(origin)
        return origin in origins

    return lookup


_UNLISTED = {trust_record_url(GOV): {"agents": [{"url": BRAND_AGENT, "roles": ["adcp:buying"]}]}}


@pytest.mark.parametrize(
    ("url", "documents"),
    [(GOV_ISSUER, _UNLISTED), (GOV_ISSUER, {}), ("https://203.0.113.7/gov", {})],
    ids=["unlisted", "never-published", "no-registrable-domain"],
)
async def test_a_bound_origin_is_never_sent_to_3_2(url, documents):
    """R4, no downgrade: once O is bound (R5), the 3.2 profile is refused for O."""
    origin = url.rsplit("/", 1)[0]
    with pytest.raises(JwsArtifactError) as caught:
        await select_jws_artifact_profile(url, _records(documents), bound_origins=_bound(origin))
    assert caught.value.code == "jws_invalid"


async def test_an_unbound_origin_still_selects_3_2():
    calls: list[str] = []
    profile = await select_jws_artifact_profile(
        GOV_ISSUER, _records(_UNLISTED), bound_origins=_bound(BRAND_AGENT, calls=calls)
    )
    assert (profile.profile, profile.reason) == ("3.2", "unlisted")
    assert calls == [GOV]


async def test_a_listed_origin_does_not_consult_the_bindings():
    calls: list[str] = []
    profile = await select_jws_artifact_profile(
        GOV_ISSUER, _records(_listing(GOV)), bound_origins=_bound(GOV, calls=calls)
    )
    assert profile.profile == "web-bot-auth"
    assert calls == []


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
async def test_a_binding_lookup_may_be_sync_or_async(sync):
    lookup = (lambda origin: origin == GOV) if sync else _bound(GOV)
    with pytest.raises(JwsArtifactError) as caught:
        await select_jws_artifact_profile(GOV_ISSUER, _records(_UNLISTED), bound_origins=lookup)
    assert caught.value.code == "jws_invalid"


async def test_a_binding_lookup_that_fails_is_unavailable():
    def lookup(origin: str) -> bool:
        raise RuntimeError("binding store down")

    with pytest.raises(JwsArtifactError) as caught:
        await select_jws_artifact_profile(GOV_ISSUER, _records(_UNLISTED), bound_origins=lookup)
    assert caught.value.code == "jws_key_unavailable"
    assert caught.value.transient


# ---------------------------------------------------------------------------
# Governance context, online
# ---------------------------------------------------------------------------


async def test_a_listed_issuer_verifies_on_its_key_directory_without_key_purpose_checks():
    private, jwk = _wba_key()
    # A WBA directory key carries no adcp_use / key_ops for governance; it may
    # even name another purpose. Purpose separation is by origin.
    directory_key = {**jwk, "adcp_use": "request-signing"}
    directory_key.pop("use")
    directory = Directory({GOV: [directory_key]})
    revocation = Revocation()
    jwks = ForbiddenJwks()
    checked: list[str] = []

    result = await _verify_governance(
        _governance_token(private, jwk["kid"]),
        _verifier(_listing(GOV), directory, revocation),
        jwks,
        revocation_checker=lambda kid: checked.append(kid) or False,
    )

    assert result.ok, result
    assert jwks.calls == []
    assert directory.calls == [(GOV, jwk["kid"])]
    assert revocation.calls == [(GOV, jwk["kid"])]
    # Key revocation is the origin's list; the 3.2 kid callback does not apply.
    assert checked == []


async def test_an_unlisted_issuer_stays_on_the_3_2_profile():
    from adcp.signing import StaticJwksResolver

    private = ed25519.Ed25519PrivateKey.generate()
    jwk = {
        **OKPKey.import_key(private).as_dict(private=False),
        "kid": "gov-2026",
        "use": "sig",
        "key_ops": ["verify"],
        "adcp_use": "governance-signing",
    }
    directory = Directory()
    verifier = _verifier({}, directory)

    result = await _verify_governance(
        _governance_token(private, "gov-2026"), verifier, StaticJwksResolver({"keys": [jwk]})
    )

    assert result.ok, result
    assert directory.calls == []


async def test_a_bound_issuer_is_refused_rather_than_verified_on_3_2():
    from adcp.signing import StaticJwksResolver

    private = ed25519.Ed25519PrivateKey.generate()
    jwk = {
        **OKPKey.import_key(private).as_dict(private=False),
        "kid": "gov-2026",
        "use": "sig",
        "key_ops": ["verify"],
        "adcp_use": "governance-signing",
    }
    verifier = WbaJwsArtifactVerifier(
        records=_records({}),
        signer_keys=Directory(),
        revocation=Revocation(),
        archive=Archive(),
        bound_origins=_bound(GOV),
    )
    token = _governance_token(private, "gov-2026")

    online = await _verify_governance(token, verifier, StaticJwksResolver({"keys": [jwk]}))
    historical = await verify_historical_governance_context(
        token=token, expected_issuer=GOV_ISSUER, web_bot_auth=verifier
    )

    assert (online.ok, online.error) == (False, "governance_token_invalid")
    assert (historical.ok, historical.error) == (False, "governance_token_invalid")
    assert historical.profile != "3.2"


@pytest.mark.parametrize("listed", [True, False], ids=["wba", "3.2"])
async def test_a_governance_token_without_a_kid_is_invalid(listed):
    private, jwk = _wba_key()
    encoded_header, payload, signature = _governance_token(private, jwk["kid"]).split(".")
    header = json.loads(base64.urlsafe_b64decode(encoded_header + "=" * (-len(encoded_header) % 4)))
    del header["kid"]
    token = ".".join((b64url_encode(json.dumps(header).encode()), payload, signature))
    verifier = _verifier(_listing(GOV) if listed else {}, Directory({GOV: [jwk]}))
    result = await _verify_governance(token, verifier, ForbiddenJwks())
    assert (result.ok, result.error) == (False, "governance_token_invalid")


async def test_the_3_2_profile_still_checks_key_purpose():
    from adcp.signing import StaticJwksResolver

    private, jwk = _wba_key()
    result = await _verify_governance(
        _governance_token(private, jwk["kid"]),
        _verifier({}, Directory({GOV: [jwk]})),
        StaticJwksResolver({"keys": [jwk]}),
    )
    assert not result.ok
    assert result.error == "governance_key_unknown"


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("connection reset"), TrustResponse(b"", 500)],
    ids=["transport", "http-500"],
)
async def test_a_trust_record_outage_never_falls_back_to_3_2(failure):
    private, jwk = _wba_key()
    jwks = ForbiddenJwks()
    result = await _verify_governance(
        _governance_token(private, jwk["kid"]),
        _verifier({trust_record_url(GOV): failure}, Directory({GOV: [jwk]})),
        jwks,
    )
    assert not result.ok
    assert result.error == "governance_jwks_unavailable"
    assert jwks.calls == []


async def test_a_key_directory_outage_is_unavailable():
    private, jwk = _wba_key()
    directory = Directory({GOV: [jwk]})
    directory.failure = WbaDiscoveryError("key directory could not be resolved")
    jwks = ForbiddenJwks()
    result = await _verify_governance(
        _governance_token(private, jwk["kid"]), _verifier(_listing(GOV), directory), jwks
    )
    assert not result.ok
    assert result.error == "governance_jwks_unavailable"
    assert jwks.calls == []


async def test_a_key_another_origin_publishes_is_unknown():
    private, jwk = _wba_key()
    directory = Directory({"https://attacker.example.com": [jwk]})
    result = await _verify_governance(
        _governance_token(private, jwk["kid"]),
        _verifier(_listing(GOV), directory),
        ForbiddenJwks(),
    )
    assert not result.ok
    assert result.error == "governance_key_unknown"


async def test_a_kid_that_is_not_the_thumbprint_is_refused():
    private, jwk = _wba_key()
    # A resolver that answers for an alias kid: the kid must be the thumbprint.
    directory = Directory({GOV: [{**jwk, "kid": "gov-2026"}]})
    result = await _verify_governance(
        _governance_token(private, "gov-2026"), _verifier(_listing(GOV), directory), ForbiddenJwks()
    )
    assert not result.ok
    assert result.error == "governance_token_invalid"
    assert "thumbprint" in result.message


async def test_es256_is_refused_on_the_wba_path():
    private = ec.generate_private_key(ec.SECP256R1())
    key = ECKey.import_key(private)
    jwk = {**key.as_dict(private=False), "kid": key.thumbprint()}
    result = await _verify_governance(
        _governance_token(private, jwk["kid"], alg="ES256"),
        _verifier(_listing(GOV), Directory({GOV: [jwk]})),
        ForbiddenJwks(),
    )
    assert not result.ok
    assert result.error == "governance_token_invalid"


async def test_a_wrong_signature_is_refused():
    private, jwk = _wba_key()
    _other_private, other = _wba_key()
    # The token names the directory key but is signed by another key.
    token = _governance_token(_other_private, jwk["kid"])
    result = await _verify_governance(
        token, _verifier(_listing(GOV), Directory({GOV: [jwk, other]})), ForbiddenJwks()
    )
    assert not result.ok
    assert result.error == "governance_token_invalid"


@pytest.mark.parametrize("status", ["revoked", "stale", "unavailable", "no_list"])
async def test_the_origin_revocation_list_governs_the_key(status):
    private, jwk = _wba_key()
    result = await _verify_governance(
        _governance_token(private, jwk["kid"]),
        _verifier(_listing(GOV), Directory({GOV: [jwk]}), Revocation(status)),
        ForbiddenJwks(),
    )
    assert not result.ok
    assert result.error == "governance_token_revoked"


async def test_the_surface_checks_still_run_on_the_wba_path():
    private, jwk = _wba_key()
    verifier = _verifier(_listing(GOV), Directory({GOV: [jwk]}))
    token = _governance_token(private, jwk["kid"])

    wrong_audience = await _verify_governance(
        token, verifier, ForbiddenJwks(), expected_audience="https://other.example/sales"
    )
    assert not wrong_audience.ok and wrong_audience.error == "governance_token_not_applicable"

    revoked_jti = await _verify_governance(
        token, verifier, ForbiddenJwks(), is_jti_revoked=lambda issuer, jti: jti == "token-1"
    )
    assert not revoked_jti.ok and revoked_jti.error == "governance_token_revoked"

    expired = await _verify_governance(token, verifier, ForbiddenJwks(), now=lambda: NOW + 7200)
    assert not expired.ok and expired.error == "governance_token_expired"


# ---------------------------------------------------------------------------
# Governance context, historical (the governance key archive at O)
# ---------------------------------------------------------------------------


class Archive:
    def __init__(self, keys: list[Mapping[str, Any]] | None = None, failure: bool = False):
        self.keys = keys or []
        self.failure = failure
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, origin: str, kid: str) -> Mapping[str, Any] | None:
        self.calls.append((origin, kid))
        if self.failure:
            raise JwsArtifactError("jws_key_unavailable", "archive unavailable")
        return next((key for key in self.keys if key["kid"] == kid), None)


async def test_history_uses_the_archive_for_a_key_rotated_out_of_the_directory():
    old_private, old = _wba_key()
    _new_private, new = _wba_key()
    archive = Archive([old])
    verifier = _verifier(_listing(GOV), Directory({GOV: [new]}), archive=archive)
    # Long past expiry: historical verification does not check freshness.
    token = _governance_token(old_private, old["kid"], issued_at=10, expires_at=600)

    result = await verify_historical_governance_context(
        token=token, expected_issuer=GOV_ISSUER, web_bot_auth=verifier
    )

    assert result.ok, result
    assert result.key_source == "archive"
    assert result.claims["jti"] == "token-1"
    assert archive.calls == [(GOV, old["kid"])]


async def test_history_prefers_the_directory():
    private, jwk = _wba_key()
    archive = Archive()
    verifier = _verifier(_listing(GOV), Directory({GOV: [jwk]}), archive=archive)
    result = await verify_historical_governance_context(
        token=_governance_token(private, jwk["kid"]),
        expected_issuer=GOV_ISSUER,
        web_bot_auth=verifier,
    )
    assert result.ok and result.key_source == "directory"
    assert archive.calls == []


async def test_online_verification_never_reads_the_archive():
    old_private, old = _wba_key()
    archive = Archive([old])
    verifier = _verifier(_listing(GOV), Directory({GOV: []}), archive=archive)
    result = await _verify_governance(_governance_token(old_private, old["kid"]), verifier, None)
    assert not result.ok and result.error == "governance_key_unknown"
    assert archive.calls == []


async def test_history_refuses_a_key_in_neither_the_directory_nor_the_archive():
    private, jwk = _wba_key()
    verifier = _verifier(_listing(GOV), Directory({GOV: []}), archive=Archive())
    result = await verify_historical_governance_context(
        token=_governance_token(private, jwk["kid"]),
        expected_issuer=GOV_ISSUER,
        web_bot_auth=verifier,
    )
    assert not result.ok and result.error == "governance_key_unknown"


async def test_history_with_an_unreadable_archive_is_unavailable():
    private, jwk = _wba_key()
    verifier = _verifier(_listing(GOV), Directory({GOV: []}), archive=Archive(failure=True))
    result = await verify_historical_governance_context(
        token=_governance_token(private, jwk["kid"]),
        expected_issuer=GOV_ISSUER,
        web_bot_auth=verifier,
    )
    assert not result.ok and result.error == "governance_jwks_unavailable"


@pytest.mark.parametrize(
    ("status", "ok"), [("revoked", False), ("stale", False), ("no_list", True)]
)
async def test_history_still_applies_the_origin_revocation_list(status, ok):
    old_private, old = _wba_key()
    verifier = _verifier(
        _listing(GOV), Directory({GOV: []}), Revocation(status), archive=Archive([old])
    )
    result = await verify_historical_governance_context(
        token=_governance_token(old_private, old["kid"]),
        expected_issuer=GOV_ISSUER,
        web_bot_auth=verifier,
    )
    assert result.ok is ok
    if not ok:
        assert result.error == "governance_token_revoked"


async def test_history_reports_an_unlisted_issuer_as_3_2():
    private, jwk = _wba_key()
    verifier = _verifier({}, Directory(), archive=Archive([jwk]))
    result = await verify_historical_governance_context(
        token=_governance_token(private, jwk["kid"]),
        expected_issuer=GOV_ISSUER,
        web_bot_auth=verifier,
    )
    assert isinstance(result, GovernanceAuthorizationFailure)
    assert result.profile == "3.2"


async def test_history_requires_an_archive():
    with pytest.raises(ValueError, match="archive"):
        await verify_historical_governance_context(
            token="x.y.z", expected_issuer=GOV_ISSUER, web_bot_auth=_verifier({}, Directory())
        )


# ---------------------------------------------------------------------------
# The HTTP governance key archive
# ---------------------------------------------------------------------------


def _archive(body: Any, status: int = 200, calls: list[str] | None = None):
    async def fetch(url, *, allow_private=False):
        if calls is not None:
            calls.append(url)
        if isinstance(body, Exception):
            raise body
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return TrustResponse(raw, status)

    return HttpGovernanceKeyArchive(fetcher=fetch, clock=lambda: NOW)


async def test_the_archive_matches_keys_by_thumbprint_and_caches():
    _private, jwk = _wba_key()
    ec_key = ECKey.generate_key("P-256").as_dict(private=False)
    calls: list[str] = []
    archive = _archive(
        {"keys": [{**ec_key, "kid": "ec"}, {**jwk, "kid": "gov-2025-legacy-name"}]}, calls=calls
    )
    found = await archive(GOV, jwk["kid"])
    assert found == {"kty": "OKP", "crv": "Ed25519", "x": jwk["x"], "kid": jwk["kid"]}
    assert await archive(GOV, "gov-2025-legacy-name") is None
    assert calls == [GOV + GOVERNANCE_KEY_ARCHIVE_PATH]


@pytest.mark.parametrize("status", [404, 410])
async def test_an_origin_with_no_archive_has_no_archived_keys(status):
    _private, jwk = _wba_key()
    assert await _archive(b"", status)(GOV, jwk["kid"]) is None


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (RuntimeError("timeout"), 200),
        (b"", 503),
        (b"{bad", 200),
        ({"keys": "x"}, 200),
        ({"keys": [{"kty": "OKP", "crv": "Ed25519", "x": "AAAA", "d": "AAAA"}]}, 200),
    ],
    ids=["transport", "http-503", "not-json", "no-key-set", "private-key"],
)
async def test_an_unreadable_archive_is_unavailable(body, status):
    with pytest.raises(JwsArtifactError) as caught:
        await _archive(body, status)(GOV, "kid")
    assert caught.value.code == "jws_key_unavailable"


# ---------------------------------------------------------------------------
# Designated-task response signing and rights attestations
# ---------------------------------------------------------------------------


def _sign(private: Any, header: dict[str, Any], payload: bytes) -> str:
    registry = _AdcpJwsRegistry(algorithms=["EdDSA"], strict_check_header=False)
    return jose_jws.serialize_compact(
        header, payload, OKPKey.import_key(private), registry=registry
    )


async def test_a_designated_task_response_verifies_on_the_agent_url_origin():
    private, jwk = _wba_key()
    payload = {
        "typ": "adcp-response-payload+jws",
        "task": "verify_brand_claim",
        "brand_domain": "nova.example",
        "agent_url": BRAND_AGENT + "/mcp",
        "request_hash": "sha256:" + "A" * 43,
        "iat": NOW,
        "exp": NOW + 300,
        "response": {"verification_status": "owned"},
    }
    header = {"alg": "EdDSA", "kid": jwk["kid"], "typ": "adcp-response-payload+jws"}
    compact = _sign(private, header, rfc8785.dumps(payload))
    protected, _encoded, signature = compact.split(".")
    envelope = {"protected": protected, "payload": payload, "signature": signature}
    verifier = _verifier(_listing(BRAND_AGENT, "adcp:brand"), Directory({BRAND_AGENT: [jwk]}))

    verified = await verifier.verify(
        compact_jws_from_envelope(envelope),
        artifact="designated_task_response",
        signing_agent_url=payload["agent_url"],
        typ="adcp-response-payload+jws",
    )

    assert verified.origin == BRAND_AGENT
    assert verified.key_source == "directory"
    assert json.loads(verified.payload) == payload


async def test_a_rights_attestation_uses_the_rights_agent_origin():
    private, jwk = _wba_key()
    token = _sign(private, {"alg": "EdDSA", "kid": jwk["kid"], "typ": "test+jws"}, b"{}")
    # The holder's brand issues the attestation; its rights agent signs it.
    # A key the brand's own origin publishes does not verify it.
    directory = Directory({"https://nova-brand.com": [jwk]})
    documents = {**_listing(RIGHTS_AGENT, "adcp:rights"), **_listing("https://nova-brand.com")}
    verifier = _verifier(documents, directory)
    with pytest.raises(JwsArtifactError) as caught:
        await verifier.verify(
            token, artifact="rights_attestation", signing_agent_url=RIGHTS_AGENT, typ="test+jws"
        )
    assert caught.value.code == "jws_key_unknown"
    directory.keys[RIGHTS_AGENT] = [jwk]
    verified = await verifier.verify(
        token, artifact="rights_attestation", signing_agent_url=RIGHTS_AGENT, typ="test+jws"
    )
    assert verified.origin == RIGHTS_AGENT


async def test_an_unlisted_issuer_is_sent_to_its_3_2_verifier():
    private, jwk = _wba_key()
    token = _sign(private, {"alg": "EdDSA", "kid": jwk["kid"], "typ": "test+jws"}, b"{}")
    with pytest.raises(JwsArtifactUses32ProfileError) as caught:
        await _verifier({}, Directory({RIGHTS_AGENT: [jwk]})).verify(
            token, artifact="rights_attestation", signing_agent_url=RIGHTS_AGENT, typ="test+jws"
        )
    assert caught.value.profile.reason == "no_trust_record"


@pytest.mark.parametrize(
    ("header_changes", "message"),
    [
        ({"typ": "other+jws"}, "typ"),
        ({"alg": "Ed25519"}, "EdDSA"),
        ({"b64": True}, "b64"),
        ({"crit": ["unknown_ext"], "unknown_ext": 1}, "signature"),
    ],
    ids=["typ", "fully-specified-alg", "b64", "unknown-crit"],
)
async def test_protected_header_rules(header_changes, message):
    private, jwk = _wba_key()
    header = {"alg": "EdDSA", "kid": jwk["kid"], "typ": "test+jws", **header_changes}
    signing_input = ".".join(
        b64url_encode(part) for part in (json.dumps(header).encode(), b"{}")
    ).encode()
    token = f"{signing_input.decode()}.{b64url_encode(private.sign(signing_input))}"
    verifier = _verifier(_listing(RIGHTS_AGENT), Directory({RIGHTS_AGENT: [jwk]}))
    with pytest.raises(JwsArtifactError) as caught:
        await verifier.verify(
            token, artifact="rights_attestation", signing_agent_url=RIGHTS_AGENT, typ="test+jws"
        )
    assert caught.value.code == "jws_invalid"
    assert message in caught.value.reason


async def test_a_governance_context_iss_must_be_at_the_origin():
    from adcp.governance import _governance_jws_registry

    private, jwk = _wba_key()
    verifier = _verifier(_listing(GOV), Directory({GOV: [jwk]}))
    options: dict[str, Any] = {
        "artifact": "governance_context",
        "signing_agent_url": GOV_ISSUER,
        "typ": "adcp-gov+jws",
        "registry": _governance_jws_registry("EdDSA"),
    }
    verified = await verifier.verify(_governance_token(private, jwk["kid"]), **options)
    assert json.loads(verified.payload)["iss"] == GOV_ISSUER
    token = _governance_token(private, jwk["kid"], issuer="https://elsewhere.example.com")
    with pytest.raises(JwsArtifactError) as caught:
        await verifier.verify(token, **options)
    assert caught.value.code == "jws_invalid"
    assert "iss" in caught.value.reason


async def test_revocation_absent_may_be_accepted_where_no_spend_rides_on_it():
    private, jwk = _wba_key()
    token = _sign(private, {"alg": "EdDSA", "kid": jwk["kid"], "typ": "test+jws"}, b"{}")
    verifier = _verifier(
        _listing(RIGHTS_AGENT), Directory({RIGHTS_AGENT: [jwk]}), Revocation("no_list")
    )
    with pytest.raises(JwsArtifactError) as caught:
        await verifier.verify(
            token, artifact="rights_attestation", signing_agent_url=RIGHTS_AGENT, typ="test+jws"
        )
    assert caught.value.code == "jws_revocation_unavailable"
    verified = await verifier.verify(
        token,
        artifact="rights_attestation",
        signing_agent_url=RIGHTS_AGENT,
        typ="test+jws",
        revocation_absent="accept",
    )
    assert verified.revocation == "no_list"


async def test_historical_verification_is_for_governance_contexts_only():
    verifier = _verifier({}, Directory(), archive=Archive())
    with pytest.raises(ValueError, match="governance context"):
        await verifier.verify(
            "x.y.z",
            artifact="rights_attestation",
            signing_agent_url=RIGHTS_AGENT,
            typ="test+jws",
            historical=True,
        )


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


def _raw_token(private: Any, header: Any, payload: bytes = b"{}") -> str:
    raw_header = header if isinstance(header, bytes) else json.dumps(header).encode()
    signing_input = f"{b64url_encode(raw_header)}.{b64url_encode(payload)}"
    return f"{signing_input}.{b64url_encode(private.sign(signing_input.encode()))}"


async def _verify_rights(verifier: WbaJwsArtifactVerifier, token: object) -> Any:
    return await verifier.verify(
        token, artifact="rights_attestation", signing_agent_url=RIGHTS_AGENT, typ="test+jws"
    )


@pytest.mark.parametrize(
    ("token", "code"),
    [
        (None, "jws_invalid"),
        ("a" * 70000, "jws_invalid"),
        ("not-a-jws", "jws_invalid"),
        ("header-only", "jws_invalid"),
    ],
)
async def test_malformed_tokens_are_invalid(token, code):
    _private, jwk = _wba_key()
    verifier = _verifier(_listing(RIGHTS_AGENT), Directory({RIGHTS_AGENT: [jwk]}))
    with pytest.raises(JwsArtifactError) as caught:
        await _verify_rights(verifier, token)
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("header", "code"),
    [
        (b'{"alg":"EdDSA","alg":"EdDSA","typ":"test+jws"}', "jws_invalid"),
        (b'["EdDSA"]', "jws_invalid"),
        ({"alg": "EdDSA", "typ": "test+jws"}, "jws_invalid"),
        ({"alg": "EdDSA", "typ": "test+jws", "kid": 7}, "jws_invalid"),
    ],
    ids=["duplicate-member", "not-an-object", "no-kid", "non-string-kid"],
)
async def test_protected_header_shape(header, code):
    private, jwk = _wba_key()
    verifier = _verifier(_listing(RIGHTS_AGENT), Directory({RIGHTS_AGENT: [jwk]}))
    with pytest.raises(JwsArtifactError) as caught:
        await _verify_rights(verifier, _raw_token(private, header))
    assert caught.value.code == code


@pytest.mark.parametrize(
    "change",
    [
        {"d": "AAAA"},
        {"alg": "ES256"},
        {"crv": "X25519"},
        {"x": "not+base64url"},
    ],
    ids=["private-member", "key-alg", "not-ed25519", "bad-x"],
)
async def test_a_resolver_key_must_be_an_ed25519_public_key(change):
    private, jwk = _wba_key()
    directory = Directory({RIGHTS_AGENT: [{**jwk, **change}]})
    verifier = _verifier(_listing(RIGHTS_AGENT), directory)
    token = _raw_token(private, {"alg": "EdDSA", "kid": jwk["kid"], "typ": "test+jws"})
    with pytest.raises(JwsArtifactError) as caught:
        await _verify_rights(verifier, token)
    assert caught.value.code == "jws_invalid"


async def test_a_resolver_answering_for_another_origin_is_refused():
    private, jwk = _wba_key()

    async def wrong_origin(keyid: str, *, signer_origin: str) -> ResolvedSignerKey:
        return ResolvedSignerKey("https://attacker.example.com", jwk)

    verifier = WbaJwsArtifactVerifier(
        records=_records(_listing(RIGHTS_AGENT)), signer_keys=wrong_origin, revocation=Revocation()
    )
    token = _raw_token(private, {"alg": "EdDSA", "kid": jwk["kid"], "typ": "test+jws"})
    with pytest.raises(JwsArtifactError) as caught:
        await _verify_rights(verifier, token)
    assert caught.value.code == "jws_key_unknown"


@pytest.mark.parametrize(("revoked", "code"), [(True, "jws_key_revoked"), (None, None)])
async def test_a_legacy_boolean_revocation_callable_is_accepted(revoked, code):
    private, jwk = _wba_key()

    async def legacy(origin: str, keyid: str) -> bool:
        return bool(revoked)

    verifier = WbaJwsArtifactVerifier(
        records=_records(_listing(RIGHTS_AGENT)),
        signer_keys=Directory({RIGHTS_AGENT: [jwk]}),
        revocation=legacy,
    )
    token = _raw_token(private, {"alg": "EdDSA", "kid": jwk["kid"], "typ": "test+jws"})
    if code is None:
        assert (await _verify_rights(verifier, token)).revocation == "current"
        return
    with pytest.raises(JwsArtifactError) as caught:
        await _verify_rights(verifier, token)
    assert caught.value.code == code


async def test_selection_skips_malformed_agent_entries():
    documents = {
        trust_record_url(GOV): {
            "agents": [
                {"url": "https://other.example.com", "roles": ["adcp:buying"]},
                {"url": GOV, "roles": ["adcp:governance"]},
            ]
        }
    }
    records = _records(documents)
    profile = await select_jws_artifact_profile(GOV_ISSUER, records)
    assert profile.profile == "web-bot-auth"
    assert profile.record is not None
    from adcp.signing.jws_artifacts import _lists_origin

    record = profile.record
    broken = type(record)(
        record.operator_domain, record.source_url, {"agents": [{"roles": []}, {"url": 7}]}, 0
    )
    assert not _lists_origin(broken, GOV)


@pytest.mark.parametrize("url", [7, "https://gov.example.com/\n"])
async def test_a_signing_agent_url_must_be_a_clean_string(url):
    with pytest.raises(JwsArtifactError) as caught:
        await select_jws_artifact_profile(url, _records({}))
    assert caught.value.code == "jws_invalid"


async def test_argument_errors():
    verifier = _verifier({}, Directory())
    with pytest.raises(ValueError, match="unknown JWS artifact"):
        await verifier.verify("x.y.z", artifact="other", signing_agent_url=GOV, typ="t")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="archive"):
        await verifier.verify(
            "x.y.z", artifact="governance_context", signing_agent_url=GOV, typ="t", historical=True
        )
    from adcp.signing.jws_artifacts import verify_wba_jws_artifact

    with pytest.raises(ValueError, match="typ"):
        await verify_wba_jws_artifact(
            "x.y.z", GOV, signer_keys=Directory(), revocation=Revocation(), typ=""
        )
    with pytest.raises(ValueError, match="revocation_absent"):
        await verify_wba_jws_artifact(
            "x.y.z",
            GOV,
            signer_keys=Directory(),
            revocation=Revocation(),
            typ="t",
            revocation_absent="maybe",  # type: ignore[arg-type]
        )
    with pytest.raises(JwsArtifactError):
        await verify_wba_jws_artifact(
            "x.y.z",
            "http://gov.example.com",
            signer_keys=Directory(),
            revocation=Revocation(),
            typ="t",
        )
    with pytest.raises(ValueError):
        HttpGovernanceKeyArchive(max_origins=0)


@pytest.mark.parametrize(
    "envelope",
    [
        {"protected": 1, "payload": {}, "signature": "x"},
        {"protected": "x", "payload": [], "signature": "x"},
        {"protected": "x", "payload": {"n": float("nan")}, "signature": "x"},
    ],
)
def test_compact_jws_from_envelope_refuses_malformed_envelopes(envelope):
    with pytest.raises(ValueError):
        compact_jws_from_envelope(envelope)


async def test_the_archive_skips_unusable_entries_and_bounds_its_cache():
    _private, jwk = _wba_key()
    calls: list[str] = []

    async def fetch(url, *, allow_private=False):
        calls.append(url)
        body = {"keys": ["not-an-object", {"kty": "OKP", "crv": "Ed25519", "x": "!!"}, jwk]}
        return TrustResponse(json.dumps(body).encode())

    archive = HttpGovernanceKeyArchive(fetcher=fetch, max_origins=1, clock=lambda: NOW)
    assert (await archive(GOV, jwk["kid"])) is not None
    assert (await archive(RIGHTS_AGENT, jwk["kid"])) is not None
    assert (await archive(GOV, jwk["kid"])) is not None  # evicted, so fetched again
    assert len(calls) == 3
    with pytest.raises(JwsArtifactError):
        await archive("http://gov.example.com", jwk["kid"])


async def test_an_archive_or_resolver_that_raises_anything_is_unavailable():
    private, jwk = _wba_key()

    async def broken_archive(origin: str, kid: str) -> None:
        raise ConnectionError("archive host down")

    verifier = _verifier(_listing(GOV), Directory({GOV: []}), archive=broken_archive)
    result = await verify_historical_governance_context(
        token=_governance_token(private, jwk["kid"]),
        expected_issuer=GOV_ISSUER,
        web_bot_auth=verifier,
    )
    assert not result.ok and result.error == "governance_jwks_unavailable"

    directory = Directory({GOV: [jwk]})
    directory.failure = ConnectionError("custom resolver failed")
    result = await _verify_governance(
        _governance_token(private, jwk["kid"]), _verifier(_listing(GOV), directory), None
    )
    assert not result.ok and result.error == "governance_jwks_unavailable"


async def test_the_archive_skips_an_entry_with_a_non_string_alg():
    _private, jwk = _wba_key()
    _other_private, other = _wba_key()
    archive = _archive({"keys": [{**other, "alg": []}, jwk]})
    assert await archive(GOV, jwk["kid"]) is not None
    assert await archive(GOV, other["kid"]) is None


async def test_an_archive_miss_refetches_after_the_cooldown():
    _private, jwk = _wba_key()
    now = [NOW]
    bodies = [{"keys": []}, {"keys": [jwk]}]
    calls: list[str] = []

    async def fetch(url, *, allow_private=False):
        calls.append(url)
        return TrustResponse(json.dumps(bodies[min(len(calls) - 1, 1)]).encode())

    archive = HttpGovernanceKeyArchive(fetcher=fetch, clock=lambda: now[0])
    assert await archive(GOV, jwk["kid"]) is None
    assert await archive(GOV, jwk["kid"]) is None  # within the cooldown: cached
    assert len(calls) == 1
    now[0] += 31
    assert await archive(GOV, jwk["kid"]) is not None
    assert len(calls) == 2
    assert await archive(GOV, jwk["kid"]) is not None  # a hit stays cached
    assert len(calls) == 2
