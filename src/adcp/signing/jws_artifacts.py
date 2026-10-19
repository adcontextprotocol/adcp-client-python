"""JWS artifacts on the issuer's Web Bot Auth key directory (experimental, AdCP 3.3).

``specs/agent-identity-3-3.md`` R4, as amended by adcontextprotocol/adcp#8299
(WG-approved 2026-10-19), moves three JWS artifacts to the signing agent's
Web Bot Auth (WBA) key directory. ``O`` is the canonical origin of the signing
agent's URL:

================================  ======================
Artifact                          ``O`` is the origin of
================================  ======================
governance context                ``iss``
designated-task response signing  ``agent_url``
rights attestation                ``rights_agent.url``
================================  ======================

* **Selection.** When the trust record derived from ``O`` (R3a step 1) lists
  ``O``, the JWS is verified with a key from ``O``'s key directory, never under
  the 3.2 profile. A record that cannot be read is not absence, so it never
  selects the 3.2 profile. An issuer whose ``O`` no trust record lists (the
  record does not list it, the operator never published one, or ``O`` has no
  registrable domain) stays on the 3.2 profile.
* **Key rules.** ``alg`` is ``EdDSA`` and ``kid`` is the key's JWK thumbprint.
  Purpose separation is by origin (D2), so the 3.2 key-purpose checks
  (``use``, ``key_ops``, ``adcp_use``) do not apply. Revocation is ``O``'s list.
* **History.** Historical verification of a governance context uses the
  governance key archive at ``O`` (``/.well-known/jwks-archive.json``).
* Every other check of the surface is unchanged and stays with the surface's
  verifier.

Profile selection never reads an unsigned field: callers pass the signing
agent URL from their own context (the governance agent they expect, the agent
they called, the rights agent named in the constraint they hold), never a
value read from the unverified token.

The components are the ones the request path uses: the trust record
evaluator (:class:`~adcp.signing.trust.TrustRecordEvaluator`), the key
directory resolver (:class:`~adcp.signing.wba.DiscoveredSignerKeys`) and the
origin revocation checker (:class:`~adcp.signing.wba.OriginRevocationChecker`).
The request-signature path itself still never reads a trust record.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import rfc8785
from joserfc import jws as jose_jws
from joserfc.errors import JoseError
from joserfc.jwk import OKPKey
from joserfc.jws import JWSRegistry

from adcp.signing._strict_json import StrictJsonError, parse_strict_json
from adcp.signing.canonical import canonical_origin
from adcp.signing.crypto import _PRIVATE_JWK_MEMBERS, b64url_decode, b64url_encode, import_jwk
from adcp.signing.etld import registrable_domain
from adcp.signing.jws import JwsMalformedError, _AdcpJwsRegistry, parse_compact_jws
from adcp.signing.signer_keys import WbaSignerKeyResolver
from adcp.signing.trust import (
    EvaluatedTrustRecord,
    TrustFetcher,
    TrustRecordError,
    TrustRecordEvaluator,
    TrustRecordUnavailableError,
    fetch_trust_record,
)
from adcp.signing.wba import (
    WbaRevocationStatus,
    _https_origin,
    thumbprint_key_id,
    wba_revocation_status,
)

JwsArtifact = Literal["governance_context", "designated_task_response", "rights_attestation"]
_ARTIFACTS: frozenset[str] = frozenset(
    {"governance_context", "designated_task_response", "rights_attestation"}
)

#: The one ``alg`` R4 allows: a WBA key directory holds Ed25519 keys only.
JWS_ARTIFACT_ALG = "EdDSA"
#: The governance key archive at an origin (``security.mdx`` §Revocation).
GOVERNANCE_KEY_ARCHIVE_PATH = "/.well-known/jwks-archive.json"

_MAX_TOKEN_BYTES = 64 * 1024
_MAX_ARCHIVE_KEYS = 4096

JwsArtifactErrorCode = Literal[
    # A trust record, key directory or archive could not be read. Retryable.
    "jws_key_unavailable",
    # No key at O (its directory, or for history its archive) has the kid.
    "jws_key_unknown",
    # alg is not EdDSA, kid is not the key's thumbprint, the header is not
    # the surface's, or the signature does not verify.
    "jws_invalid",
    # O's revocation list revokes the key.
    "jws_key_revoked",
    # O's revocation state cannot be established now, or (by default) O
    # serves no list. Retryable.
    "jws_revocation_unavailable",
]


class JwsArtifactError(ValueError):
    """A JWS artifact refused on the Web Bot Auth path, or an outage.

    ``code`` is a :data:`JwsArtifactErrorCode`; ``reason`` is for logs only.
    """

    def __init__(self, code: JwsArtifactErrorCode, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code: JwsArtifactErrorCode = code
        self.reason = reason

    @property
    def transient(self) -> bool:
        """An outage: retry later; never absence and never a verdict on the artifact."""
        return self.code in {"jws_key_unavailable", "jws_revocation_unavailable"}


@dataclass(frozen=True)
class JwsArtifactProfile:
    """The profile R4 selects for a JWS artifact from origin ``origin``.

    ``profile`` is ``"web-bot-auth"`` (the trust record derived from ``O``
    lists ``O``; ``record`` is that record) or ``"3.2"``, with ``reason``
    ``"unlisted"`` (the record does not list ``O``) or ``"no_trust_record"``
    (``O`` has no registrable domain, or its operator has never published a
    record).
    """

    profile: Literal["web-bot-auth", "3.2"]
    origin: str
    reason: Literal["listed", "unlisted", "no_trust_record"]
    record: EvaluatedTrustRecord | None = None


class JwsArtifactUses32ProfileError(Exception):
    """R4 selected the 3.2 profile: verify the artifact with its 3.2 verifier.

    Raised by :meth:`WbaJwsArtifactVerifier.verify` only when no trust record
    lists ``O``, never for a fetch failure.
    """

    def __init__(self, profile: JwsArtifactProfile) -> None:
        super().__init__(f"no trust record lists {profile.origin}; use the 3.2 profile")
        self.profile = profile


@dataclass(frozen=True)
class VerifiedJwsArtifact:
    """A JWS artifact whose key and signature verified on the Web Bot Auth path.

    Only the key, the signature and the protected-header rules R4 adds are
    checked: the payload's claims are the surface's to check. ``revocation``
    is the key's state in ``O``'s list (``"current"``, or ``"no_list"`` when
    the caller accepted an origin that serves none).
    """

    origin: str
    kid: str
    key: Mapping[str, Any]
    key_source: Literal["directory", "archive"]
    protected_header: Mapping[str, Any]
    payload: bytes
    revocation: WbaRevocationStatus


class GovernanceKeyArchive(Protocol):
    """Look up a key retired from ``origin``'s directory in its governance key archive.

    Returns the public JWK whose RFC 7638 thumbprint is ``kid``, or ``None``
    when the archive holds no such key (or the origin serves no archive).
    Raises :class:`JwsArtifactError` (``jws_key_unavailable``) when the
    archive cannot be read.
    """

    async def __call__(self, origin: str, kid: str) -> Mapping[str, Any] | None:
        raise NotImplementedError("look the thumbprint up in the origin's key archive")


def _signing_origin(signing_agent_url: str) -> str:
    """The canonical https origin ``O`` of a signing agent URL."""
    try:
        if not isinstance(signing_agent_url, str) or any(ord(c) < 0x21 for c in signing_agent_url):
            raise ValueError("signing agent URL must be a string without whitespace")
        parts = urlsplit(signing_agent_url)
        origin = canonical_origin(signing_agent_url)
    except ValueError as exc:
        raise JwsArtifactError("jws_invalid", "signing agent URL has no canonical origin") from exc
    if (
        not origin.startswith("https://")
        or parts.username is not None
        or parts.password is not None
    ):
        raise JwsArtifactError("jws_invalid", "signing agent URL is not an https origin")
    return origin


def _lists_origin(record: EvaluatedTrustRecord, origin: str) -> bool:
    """Whether ``record`` lists ``origin`` in ``agents[]``, with any role."""
    for agent in record.document.get("agents", []):
        url = agent.get("url") if isinstance(agent, Mapping) else None
        try:
            if isinstance(url, str) and canonical_origin(url) == origin:
                return True
        except ValueError:
            continue
    return False


async def select_jws_artifact_profile(
    signing_agent_url: str, records: TrustRecordEvaluator
) -> JwsArtifactProfile:
    """Apply R4's selection rule to the signing agent URL the caller trusts.

    The trust record derived from ``O`` (R3a step 1) decides: a record that
    lists ``O``, with any role, selects the Web Bot Auth key directory; a
    record that does not, an operator that has never published one, or an
    ``O`` with no registrable domain selects the 3.2 profile. A record that
    cannot be read (an outage, an invalid record, a 404/410 after the
    operator served one, or local capacity) raises :class:`JwsArtifactError`
    ``jws_key_unavailable``: a fetch failure never selects the 3.2 profile.
    """
    origin = _signing_origin(signing_agent_url)
    try:
        has_operator = registrable_domain(origin) is not None
    except ValueError:
        has_operator = False
    if not has_operator:
        return JwsArtifactProfile("3.2", origin, "no_trust_record")
    try:
        record = await records.evaluate(origin)
    except TrustRecordUnavailableError as exc:
        raise JwsArtifactError("jws_key_unavailable", "issuer trust record is unavailable") from exc
    except TrustRecordError:
        # Absent (404/410 before any 200) or no registrable domain.
        return JwsArtifactProfile("3.2", origin, "no_trust_record")
    if _lists_origin(record, origin):
        return JwsArtifactProfile("web-bot-auth", origin, "listed", record)
    return JwsArtifactProfile("3.2", origin, "unlisted", record)


def _default_registry() -> JWSRegistry:
    # strict_check_header=False: the surface decides which other header
    # members it allows; joserfc still refuses a crit it does not know.
    return _AdcpJwsRegistry(algorithms=[JWS_ARTIFACT_ALG], strict_check_header=False)


def _decode_header(b64_header: str) -> dict[str, Any]:
    try:
        header = parse_strict_json(b64url_decode(b64_header))
    except (StrictJsonError, ValueError, UnicodeError) as exc:
        raise JwsArtifactError("jws_invalid", "protected header is not strict JSON") from exc
    if not isinstance(header, dict):
        raise JwsArtifactError("jws_invalid", "protected header is not a JSON object")
    return header


def _public_ed25519_key(jwk: Mapping[str, Any]) -> dict[str, Any]:
    """The key's public Ed25519 members, after R4's key rules.

    ``use``, ``key_ops`` and ``adcp_use`` are not checked: purpose separation
    is by origin. Private members, a non-Ed25519 key, a non-canonical ``x``
    and an ``alg`` other than ``EdDSA`` are refused.
    """
    if _PRIVATE_JWK_MEMBERS & {str(name).lower() for name in jwk}:
        raise JwsArtifactError("jws_invalid", "key carries private key material")
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or not isinstance(jwk.get("x"), str):
        raise JwsArtifactError("jws_invalid", "key is not an Ed25519 public key")
    alg = jwk.get("alg")
    if alg is not None and alg != JWS_ARTIFACT_ALG:
        raise JwsArtifactError("jws_invalid", "key alg is not EdDSA")
    public = {"kty": "OKP", "crv": "Ed25519", "x": jwk["x"]}
    try:
        import_jwk(public)
    except ValueError as exc:
        raise JwsArtifactError("jws_invalid", "key x is not a canonical Ed25519 key") from exc
    return public


async def verify_wba_jws_artifact(
    token: object,
    origin: str,
    *,
    signer_keys: WbaSignerKeyResolver,
    revocation: Any,
    typ: str,
    registry: JWSRegistry | None = None,
    archive: GovernanceKeyArchive | None = None,
    revocation_absent: Literal["reject", "accept"] = "reject",
) -> VerifiedJwsArtifact:
    """Verify a compact JWS with a key from ``origin``'s key directory (R4).

    ``origin`` is ``O``, the profile :func:`select_jws_artifact_profile`
    selected. Checks, in order: the compact form; a strict-JSON protected
    header with ``alg`` exactly ``EdDSA``, ``typ`` exactly ``typ`` and no
    ``b64``; the ``kid``, resolved in ``O``'s directory through
    ``signer_keys`` (and, with ``archive``, in the governance key archive at
    ``O`` when the directory does not carry it); the key, which must be an
    Ed25519 public key whose RFC 7638 thumbprint is the ``kid``; the
    signature, verified by ``joserfc`` with ``registry`` (which also decides
    ``crit``); and the key's state in ``O``'s revocation list.

    ``revocation`` is a :class:`~adcp.signing.wba.WbaRevocationStatusChecker`
    (usually the :class:`~adcp.signing.wba.OriginRevocationChecker` of the
    request path) or a legacy ``(origin, keyid) -> bool`` callable. A revoked
    key is ``jws_key_revoked``. A list that is stale or cannot be checked is
    ``jws_revocation_unavailable``, and so is an origin that serves no list
    unless ``revocation_absent="accept"``.

    No ``use``, ``key_ops`` or ``adcp_use`` check applies. Every other check
    of the surface (claims, audience, freshness, replay) is the caller's.
    Raises :class:`JwsArtifactError`.
    """
    if typ == "" or not isinstance(typ, str):
        raise ValueError("typ is required")
    if revocation_absent not in {"reject", "accept"}:
        raise ValueError("revocation_absent must be 'reject' or 'accept'")
    try:
        origin = _https_origin(origin)
    except ValueError as exc:
        raise JwsArtifactError("jws_key_unknown", "signing origin is not an https origin") from exc
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_BYTES:
        raise JwsArtifactError("jws_invalid", "JWS is missing or too large")
    try:
        b64_header, _b64_payload, _signature = parse_compact_jws(token)
    except JwsMalformedError as exc:
        raise JwsArtifactError("jws_invalid", "not a compact JWS") from exc
    header = _decode_header(b64_header)
    alg = header.get("alg")
    if type(alg) is not str or alg != JWS_ARTIFACT_ALG:
        raise JwsArtifactError("jws_invalid", "a Web Bot Auth JWS artifact uses alg EdDSA")
    if header.get("typ") != typ:
        raise JwsArtifactError("jws_invalid", "typ mismatch")
    if "b64" in header:
        raise JwsArtifactError("jws_invalid", "the b64 header is not supported")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid or len(kid) > 256:
        raise JwsArtifactError("jws_key_unknown", "kid is missing")

    key: Mapping[str, Any] | None = None
    key_source: Literal["directory", "archive"] = "directory"
    directory_failure: Exception | None = None
    try:
        resolved = await signer_keys(kid, signer_origin=origin)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # WbaDiscoveryError, local capacity, or a custom resolver's own
        # failure: an outage, never absence.
        directory_failure = exc
    else:
        if resolved is not None:
            try:
                same_origin = canonical_origin(resolved.agent_url) == origin
            except (ValueError, TypeError, AttributeError):
                same_origin = False
            if not same_origin:
                raise JwsArtifactError("jws_key_unknown", "key is not in the issuer's directory")
            key = resolved.jwk
    if key is None and archive is not None:
        try:
            archived = await archive(origin, kid)
        except (JwsArtifactError, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise JwsArtifactError(
                "jws_key_unavailable", "governance key archive is unavailable"
            ) from exc
        if archived is not None:
            key, key_source = archived, "archive"
    if key is None:
        if directory_failure is not None:
            raise JwsArtifactError(
                "jws_key_unavailable", "issuer key directory is unavailable"
            ) from directory_failure
        raise JwsArtifactError("jws_key_unknown", "no key at the issuer origin has this kid")
    public = _public_ed25519_key(key)
    try:
        thumbprint = thumbprint_key_id(public)
    except ValueError as exc:
        raise JwsArtifactError("jws_invalid", "key thumbprint cannot be computed") from exc
    if thumbprint != kid:
        raise JwsArtifactError("jws_invalid", "kid is not the key's JWK thumbprint")
    try:
        verified = jose_jws.deserialize_compact(
            token, OKPKey.import_key(public), registry=registry or _default_registry()
        )
    except (JoseError, TypeError, ValueError) as exc:
        raise JwsArtifactError("jws_invalid", "JWS signature verification failed") from exc

    status = await wba_revocation_status(revocation, origin, kid)
    if status == "revoked":
        raise JwsArtifactError("jws_key_revoked", "signing key is revoked by its origin")
    if status in {"stale", "unavailable"} or (
        status == "no_list" and revocation_absent == "reject"
    ):
        raise JwsArtifactError(
            "jws_revocation_unavailable", f"issuer revocation status is {status}"
        )
    return VerifiedJwsArtifact(
        origin=origin,
        kid=kid,
        key={**public, "kid": kid},
        key_source=key_source,
        protected_header=header,
        payload=verified.payload,
        revocation=status,
    )


@dataclass(frozen=True)
class WbaJwsArtifactVerifier:
    """The components R4 reuses, for the three JWS artifacts.

    * ``records``: the :class:`~adcp.signing.trust.TrustRecordEvaluator` that
      derives the trust record from ``O`` to select the profile;
    * ``signer_keys``: ``O``'s key directory, usually the
      :class:`~adcp.signing.wba.DiscoveredSignerKeys` of the request path;
    * ``revocation``: ``O``'s revocation list, usually the
      :class:`~adcp.signing.wba.OriginRevocationChecker` of the request path;
    * ``archive``: the governance key archive, for historical verification of
      a governance context (:class:`HttpGovernanceKeyArchive`). ``None``
      disables history.
    """

    records: TrustRecordEvaluator
    signer_keys: WbaSignerKeyResolver
    revocation: Any
    archive: GovernanceKeyArchive | None = None

    async def select(self, signing_agent_url: str) -> JwsArtifactProfile:
        """:func:`select_jws_artifact_profile` with this verifier's trust records."""
        return await select_jws_artifact_profile(signing_agent_url, self.records)

    async def verify(
        self,
        token: object,
        *,
        artifact: JwsArtifact,
        signing_agent_url: str,
        typ: str,
        registry: JWSRegistry | None = None,
        historical: bool = False,
        revocation_absent: Literal["reject", "accept"] = "reject",
    ) -> VerifiedJwsArtifact:
        """Select the profile for ``signing_agent_url`` and verify on the WBA path.

        ``signing_agent_url`` is the URL whose origin is ``O``: ``iss`` for a
        governance context, ``agent_url`` for designated-task response
        signing, ``rights_agent.url`` for a rights attestation, taken from
        the caller's own context. For a governance context the verified
        payload's ``iss`` must be at ``O``. ``historical`` looks a key the
        directory no longer carries up in the governance key archive at
        ``O``; it applies to a governance context only.

        Raises :class:`JwsArtifactUses32ProfileError` when no trust record lists
        ``O`` (verify under the 3.2 profile), and :class:`JwsArtifactError`
        for a refusal or an outage.
        """
        if artifact not in _ARTIFACTS:
            raise ValueError(f"unknown JWS artifact: {artifact!r}")
        if historical and artifact != "governance_context":
            raise ValueError("historical verification applies to a governance context only")
        if historical and self.archive is None:
            raise ValueError("historical verification requires a governance key archive")
        profile = await self.select(signing_agent_url)
        if profile.profile != "web-bot-auth":
            raise JwsArtifactUses32ProfileError(profile)
        verified = await verify_wba_jws_artifact(
            token,
            profile.origin,
            signer_keys=self.signer_keys,
            revocation=self.revocation,
            typ=typ,
            registry=registry,
            archive=self.archive if historical else None,
            revocation_absent=revocation_absent,
        )
        if artifact == "governance_context":
            try:
                claims = parse_strict_json(verified.payload)
                issuer = claims.get("iss") if isinstance(claims, dict) else None
                issuer_origin = canonical_origin(issuer) if isinstance(issuer, str) else None
            except (StrictJsonError, ValueError, UnicodeError):
                issuer_origin = None
            if issuer_origin != profile.origin:
                raise JwsArtifactError("jws_invalid", "iss is not at the issuer's origin")
        return verified


def compact_jws_from_envelope(envelope: Mapping[str, Any]) -> str:
    """The compact JWS of a decoded-payload envelope, such as ``signed_response``.

    Designated-task response signing carries ``{protected, payload,
    signature}`` with the payload decoded (``response-payload-jws-envelope.json``).
    Its signing input is ``protected || "." || BASE64URL(JCS(payload))``.
    Pass the result to :meth:`WbaJwsArtifactVerifier.verify` with
    ``artifact="designated_task_response"``. The payload must already have
    been parsed with duplicate keys rejected.
    """
    protected, payload, signature = (
        envelope.get("protected"),
        envelope.get("payload"),
        envelope.get("signature"),
    )
    if not isinstance(protected, str) or not isinstance(signature, str):
        raise ValueError("envelope protected and signature must be base64url strings")
    if not isinstance(payload, Mapping):
        raise ValueError("envelope payload must be a JSON object")
    try:
        encoded = b64url_encode(rfc8785.dumps(dict(payload)))
    except (rfc8785.CanonicalizationError, TypeError, ValueError) as exc:
        raise ValueError("envelope payload is not canonical JSON") from exc
    return f"{protected}.{encoded}.{signature}"


@dataclass
class _ArchiveEntry:
    keys: dict[str, dict[str, Any]]
    fresh_until: float
    fetched_at: float


class HttpGovernanceKeyArchive:
    """The governance key archive at an origin: ``{origin}/.well-known/jwks-archive.json``.

    ``security.mdx`` §Revocation has governance agents keep retired keys
    discoverable there, a JWK Set, for audit. Keys are matched by their
    computed RFC 7638 thumbprint, never by a ``kid`` member, and only Ed25519
    public keys are read; other keys are ignored. An entry carrying private
    key material makes the whole archive unusable, as it does a key
    directory. A 404/410 is an origin with no archive (``None``). Any other
    failure raises :class:`JwsArtifactError` ``jws_key_unavailable``, never
    absence.

    The default fetcher is the trust record fetcher: HTTPS only, IP-pinned
    with SSRF checks, no redirects, a 2 MiB body cap and a 10-second budget.
    A custom fetcher is a trusted seam and must enforce the same policy.
    Archives are cached per origin for ``cache_seconds`` in a bounded LRU of
    ``max_origins``; a key the cached archive lacks is refetched at most
    every ``miss_refetch_seconds``. Failures are not cached.
    """

    def __init__(
        self,
        *,
        fetcher: TrustFetcher = fetch_trust_record,
        allow_private: bool = False,
        cache_seconds: float = 300,
        miss_refetch_seconds: float = 30,
        max_origins: int = 256,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if type(max_origins) is not int or max_origins <= 0 or cache_seconds < 0:
            raise ValueError("invalid archive cache bounds")
        self._fetcher = fetcher
        self._allow_private = allow_private
        self._cache_seconds = float(cache_seconds)
        self._miss_refetch_seconds = float(miss_refetch_seconds)
        self._max_origins = max_origins
        self._clock = clock
        self._cache: OrderedDict[str, _ArchiveEntry] = OrderedDict()
        self._pending: dict[str, asyncio.Task[dict[str, dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()

    async def __call__(self, origin: str, kid: str) -> Mapping[str, Any] | None:
        try:
            origin = _https_origin(origin)
        except ValueError as exc:
            raise JwsArtifactError("jws_key_unknown", "archive origin is not https") from exc
        async with self._lock:
            entry = self._cache.get(origin)
            now = self._clock()
            # A miss refetches once the cooldown has passed, so a key rotated
            # into the archive is not reported unknown for a whole cache period.
            if (
                entry is not None
                and entry.fresh_until > now
                and (kid in entry.keys or now < entry.fetched_at + self._miss_refetch_seconds)
            ):
                self._cache.move_to_end(origin)
                keys = entry.keys
                task = None
            else:
                task = self._pending.get(origin)
                if task is None:
                    task = asyncio.create_task(self._refresh(origin))
                    task.add_done_callback(
                        lambda done: done.exception() if not done.cancelled() else None
                    )
                    self._pending[origin] = task
        if task is not None:
            keys = await asyncio.shield(task)
        found = keys.get(kid)
        return dict(found) if found is not None else None

    async def _refresh(self, origin: str) -> dict[str, dict[str, Any]]:
        try:
            keys = await self._fetch(origin)
            async with self._lock:
                now = self._clock()
                self._cache[origin] = _ArchiveEntry(keys, now + self._cache_seconds, now)
                self._cache.move_to_end(origin)
                while len(self._cache) > self._max_origins:
                    self._cache.popitem(last=False)
            return keys
        finally:
            async with self._lock:
                self._pending.pop(origin, None)

    async def _fetch(self, origin: str) -> dict[str, dict[str, Any]]:
        try:
            response = await self._fetcher(
                origin + GOVERNANCE_KEY_ARCHIVE_PATH, allow_private=self._allow_private
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise JwsArtifactError(
                "jws_key_unavailable", "governance key archive is unavailable"
            ) from exc
        if response.status in {404, 410}:
            return {}
        if response.status != 200:
            raise JwsArtifactError(
                "jws_key_unavailable",
                f"governance key archive is unavailable (HTTP {response.status})",
            )
        try:
            document = parse_strict_json(response.body)
        except StrictJsonError as exc:
            raise JwsArtifactError(
                "jws_key_unavailable", "governance key archive is not strict JSON"
            ) from exc
        entries = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(entries, list) or len(entries) > _MAX_ARCHIVE_KEYS:
            raise JwsArtifactError("jws_key_unavailable", "governance key archive is invalid")
        keys: dict[str, dict[str, Any]] = {}
        for jwk in entries:
            if not isinstance(jwk, dict):
                continue
            if _PRIVATE_JWK_MEMBERS & {str(name).lower() for name in jwk}:
                raise JwsArtifactError(
                    "jws_key_unavailable", "governance key archive carries private key material"
                )
            try:
                public = _public_ed25519_key(jwk)
                thumbprint = thumbprint_key_id(public)
            except (JwsArtifactError, TypeError, ValueError):
                continue
            keys.setdefault(thumbprint, {**public, "kid": thumbprint})
        return keys


__all__ = [
    "GOVERNANCE_KEY_ARCHIVE_PATH",
    "GovernanceKeyArchive",
    "HttpGovernanceKeyArchive",
    "JWS_ARTIFACT_ALG",
    "JwsArtifact",
    "JwsArtifactError",
    "JwsArtifactErrorCode",
    "JwsArtifactProfile",
    "JwsArtifactUses32ProfileError",
    "VerifiedJwsArtifact",
    "WbaJwsArtifactVerifier",
    "compact_jws_from_envelope",
    "select_jws_artifact_profile",
    "verify_wba_jws_artifact",
]
