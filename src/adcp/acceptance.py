"""Advisory buyer resolution for seller acceptance-policy catalogs.

The seller remains authoritative for every transaction.  This module resolves
the optional discovery catalog as untrusted remote configuration, verifies all
content and registry pins, and reports only a conservative preflight outcome.
Missing information never becomes an allow decision.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

import httpx
import rfc8785
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from adcp.registry import RegistryClient
from adcp.signing._bounded_http import (
    ResponseTooLargeError,
    async_read_decoded_limited_bytes,
)
from adcp.signing.canonical import canonicalize_target_uri
from adcp.signing.ip_pinned_transport import build_async_ip_pinned_transport
from adcp.types import (
    AcceptanceContext,
    AcceptancePolicyCatalog,
    AcceptancePolicyDiscovery,
    AcceptancePolicyProfile,
    AcceptancePolicyProfileId,
    AcceptancePolicyProfileIds,
    AcceptancePolicyRequirement,
    AcceptancePolicyRule,
)
from adcp.types.core import Policy
from adcp.validation.schema_loader import get_named_validator

DEFAULT_ACCEPTANCE_CATALOG_TIMEOUT_SECONDS = 5.0
DEFAULT_ACCEPTANCE_CATALOG_MAX_BYTES = 1024 * 1024
DEFAULT_ACCEPTANCE_CATALOG_CACHE_ENTRIES = 64

_CATALOG_SCHEMA = "media-buy/acceptance-policy-catalog.json"
_DIAGNOSTIC_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")
AcceptancePolicySurface = Literal[
    "account",
    "media_buy",
    "creative",
    "landing_page",
    "targeting",
    "delivery",
    "format",
]

_SURFACES: frozenset[AcceptancePolicySurface] = frozenset(
    {"account", "media_buy", "creative", "landing_page", "targeting", "delivery", "format"}
)


class AcceptancePolicyDiagnosticCode(str, Enum):
    """Stable machine-readable reasons an assessment could not be conclusive."""

    capability_unavailable = "capability_unavailable"
    invalid_discovery = "invalid_discovery"
    invalid_profile_ids = "invalid_profile_ids"
    unsafe_catalog_url = "unsafe_catalog_url"
    catalog_timeout = "catalog_timeout"
    catalog_fetch_failed = "catalog_fetch_failed"
    catalog_too_large = "catalog_too_large"
    catalog_digest_mismatch = "catalog_digest_mismatch"
    catalog_invalid_json = "catalog_invalid_json"
    catalog_schema_invalid = "catalog_schema_invalid"
    duplicate_profile_id = "duplicate_profile_id"
    profile_unresolved = "profile_unresolved"
    profile_digest_mismatch = "profile_digest_mismatch"
    profile_invalid = "profile_invalid"
    policy_unresolved = "policy_unresolved"
    policy_version_mismatch = "policy_version_mismatch"
    policy_digest_mismatch = "policy_digest_mismatch"
    policy_invalid = "policy_invalid"
    invalid_region_alias = "invalid_region_alias"
    partial_coverage = "partial_coverage"
    incomplete_context = "incomplete_context"


class AcceptancePolicyDiagnostic(BaseModel):
    """Bounded diagnostic that never includes remote policy prose."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: AcceptancePolicyDiagnosticCode
    profile_id: str | None = Field(default=None, max_length=255)
    policy_id: str | None = Field(default=None, max_length=255)
    rule_id: str | None = Field(default=None, max_length=255)


class AcceptancePolicyOutcome(str, Enum):
    """Conservative advisory result for the contemplated seller action."""

    allowed = "allowed"
    prohibited = "prohibited"
    requires_disclosure = "requires_disclosure"
    requires_setup = "requires_setup"
    requires_review = "requires_review"
    unknown = "unknown"


class AcceptancePolicyResolution(BaseModel):
    """Verified catalog projection for seller-default and product profiles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog: AcceptancePolicyCatalog | None = None
    profiles: tuple[AcceptancePolicyProfile, ...] = ()
    profile_ids: tuple[str, ...] = ()
    diagnostics: tuple[AcceptancePolicyDiagnostic, ...] = ()
    from_cache: bool = False

    @property
    def resolved(self) -> bool:
        return self.catalog is not None and not self.diagnostics


class AcceptancePolicyAssessment(BaseModel):
    """Advisory policy preflight; an exact seller response remains authoritative."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: AcceptancePolicyOutcome
    advisory: Literal[True] = True
    profile_ids: tuple[str, ...] = ()
    matching_rule_ids: tuple[str, ...] = ()
    requirements: tuple[AcceptancePolicyRequirement, ...] = ()
    diagnostics: tuple[AcceptancePolicyDiagnostic, ...] = ()


class AcceptancePolicyRegistry(Protocol):
    """Minimal registry surface used to resolve immutable policy versions."""

    async def resolve_policy(self, policy_id: str, version: str | None = None) -> Policy | None:
        """Resolve one exact registry policy version."""


@dataclass(frozen=True)
class _ResolvedProfile:
    model: AcceptancePolicyProfile
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class _CatalogDocument:
    model: AcceptancePolicyCatalog
    raw: Mapping[str, Any]
    local_profiles: Mapping[str, _ResolvedProfile]
    registry_refs: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class _CacheEntry:
    document: _CatalogDocument
    expires_at: float


class _CatalogFailure(Exception):  # noqa: N818 - private control-flow sentinel
    def __init__(self, diagnostic: AcceptancePolicyDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.code.value)


def _diag(
    code: AcceptancePolicyDiagnosticCode,
    *,
    profile_id: str | None = None,
    policy_id: str | None = None,
    rule_id: str | None = None,
) -> AcceptancePolicyDiagnostic:
    return AcceptancePolicyDiagnostic(
        code=code,
        profile_id=_safe_diagnostic_id(profile_id),
        policy_id=_safe_diagnostic_id(policy_id),
        rule_id=_safe_diagnostic_id(rule_id),
    )


def _safe_diagnostic_id(value: str | None) -> str | None:
    if value is None or _DIAGNOSTIC_ID_RE.fullmatch(value) is None:
        return None
    return value


def _root_string(value: Any) -> str:
    root = getattr(value, "root", value)
    return str(getattr(root, "value", root))


def _model_mapping(value: BaseModel | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return cast(
        Mapping[str, Any],
        value.model_dump(mode="json", by_alias=True, exclude_none=False),
    )


def _sha256_jcs(value: Mapping[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(rfc8785.dumps(dict(value))).hexdigest()}"


def _profile_digest(raw: Mapping[str, Any]) -> str:
    content = dict(raw)
    content.pop("content_digest", None)
    return _sha256_jcs(content)


def _digest_equal(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(
            left.encode("ascii", "strict"),
            right.encode("ascii", "strict"),
        )
    except (AttributeError, UnicodeEncodeError):
        return False


class AcceptancePolicyResolver:
    """Resolve and conservatively assess seller acceptance-policy discovery.

    Catalogs are cached only when the caller supplies the seller's advertised
    ``cache_ttl_seconds``.  The cache key includes both canonical URL and digest;
    call :meth:`invalidate_capabilities` when a ``capabilities.changed`` event is
    received.
    """

    def __init__(
        self,
        *,
        registry: AcceptancePolicyRegistry | None = None,
        timeout_seconds: float = DEFAULT_ACCEPTANCE_CATALOG_TIMEOUT_SECONDS,
        max_body_bytes: int = DEFAULT_ACCEPTANCE_CATALOG_MAX_BYTES,
        max_cache_entries: int = DEFAULT_ACCEPTANCE_CATALOG_CACHE_ENTRIES,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be greater than zero")
        if max_cache_entries <= 0:
            raise ValueError("max_cache_entries must be greater than zero")
        self._registry = registry or RegistryClient(timeout=timeout_seconds)
        self._owns_registry = registry is None
        self._timeout_seconds = timeout_seconds
        self._max_body_bytes = max_body_bytes
        self._max_cache_entries = max_cache_entries
        self._monotonic = monotonic
        self._now = now
        self._cache: OrderedDict[tuple[str, str, str | None], _CacheEntry] = OrderedDict()
        self._cache_lock = asyncio.Lock()

    async def __aenter__(self) -> AcceptancePolicyResolver:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally created registry client, if any."""
        if self._owns_registry and isinstance(self._registry, RegistryClient):
            await self._registry.close()

    async def invalidate_capabilities(self) -> None:
        """Drop every catalog cached under an older capability document."""
        async with self._cache_lock:
            self._cache.clear()

    async def resolve(
        self,
        discovery: AcceptancePolicyDiscovery | Mapping[str, Any] | None,
        *,
        product_profile_ids: (
            AcceptancePolicyProfileIds | Sequence[str | AcceptancePolicyProfileId]
        ) = (),
        cache_ttl_seconds: int | None = None,
        capabilities_version: str | None = None,
    ) -> AcceptancePolicyResolution:
        """Verify the catalog and resolve all selected profiles and policy pins."""
        resolution, _ = await self._resolve_selected_profiles(
            discovery,
            product_profile_ids=product_profile_ids,
            cache_ttl_seconds=cache_ttl_seconds,
            capabilities_version=capabilities_version,
        )
        return resolution

    async def _resolve_selected_profiles(
        self,
        discovery: AcceptancePolicyDiscovery | Mapping[str, Any] | None,
        *,
        product_profile_ids: AcceptancePolicyProfileIds | Sequence[str | AcceptancePolicyProfileId],
        cache_ttl_seconds: int | None,
        capabilities_version: str | None,
    ) -> tuple[AcceptancePolicyResolution, tuple[_ResolvedProfile, ...]]:
        if discovery is None:
            return (
                AcceptancePolicyResolution(
                    diagnostics=(_diag(AcceptancePolicyDiagnosticCode.capability_unavailable),)
                ),
                (),
            )
        try:
            discovery_model = TypeAdapter(AcceptancePolicyDiscovery).validate_python(discovery)
        except (ValidationError, ValueError, TypeError):
            return (
                AcceptancePolicyResolution(
                    diagnostics=(_diag(AcceptancePolicyDiagnosticCode.invalid_discovery),)
                ),
                (),
            )
        try:
            product_ids = self._normalize_product_profile_ids(product_profile_ids)
        except (ValidationError, ValueError, TypeError):
            return (
                AcceptancePolicyResolution(
                    diagnostics=(_diag(AcceptancePolicyDiagnosticCode.invalid_profile_ids),)
                ),
                (),
            )

        url = str(discovery_model.catalog_url)
        digest = discovery_model.catalog_digest
        try:
            document, from_cache = await self._load_catalog(
                url,
                digest,
                cache_ttl_seconds=cache_ttl_seconds,
                capabilities_version=capabilities_version,
            )
        except _CatalogFailure as exc:
            return AcceptancePolicyResolution(diagnostics=(exc.diagnostic,)), ()

        selected_ids: list[str] = []
        for value in discovery_model.default_profile_ids or ():
            selected_ids.append(_root_string(value))
        selected_ids.extend(product_ids)
        selected_ids = list(dict.fromkeys(selected_ids))
        if not selected_ids:
            return (
                AcceptancePolicyResolution(
                    catalog=document.model,
                    from_cache=from_cache,
                    diagnostics=(_diag(AcceptancePolicyDiagnosticCode.profile_unresolved),),
                ),
                (),
            )

        profiles: list[_ResolvedProfile] = []
        diagnostics: list[AcceptancePolicyDiagnostic] = []
        policy_cache: dict[tuple[str, str], Mapping[str, Any] | None] = {}
        for profile_id in selected_ids:
            resolved_profile: _ResolvedProfile | None = document.local_profiles.get(profile_id)
            if resolved_profile is None:
                ref = document.registry_refs.get(profile_id)
                if ref is None:
                    diagnostics.append(
                        _diag(
                            AcceptancePolicyDiagnosticCode.profile_unresolved,
                            profile_id=profile_id,
                        )
                    )
                    continue
                resolved_profile, issue = await self._resolve_registry_profile(ref, policy_cache)
                if issue is not None:
                    diagnostics.append(issue)
                    continue
            if resolved_profile is None:
                diagnostics.append(
                    _diag(
                        AcceptancePolicyDiagnosticCode.profile_unresolved,
                        profile_id=profile_id,
                    )
                )
                continue
            issue = await self._verify_profile_policy_refs(resolved_profile, policy_cache)
            if issue is not None:
                diagnostics.append(issue)
                continue
            profiles.append(resolved_profile)

        resolved_profiles = tuple(profiles)
        return (
            AcceptancePolicyResolution(
                catalog=document.model,
                profiles=tuple(profile.model for profile in resolved_profiles),
                profile_ids=tuple(profile.model.profile_id for profile in resolved_profiles),
                diagnostics=tuple(diagnostics),
                from_cache=from_cache,
            ),
            resolved_profiles,
        )

    async def assess(
        self,
        discovery: AcceptancePolicyDiscovery | Mapping[str, Any] | None,
        context: AcceptanceContext | Mapping[str, Any],
        *,
        applies_to: AcceptancePolicySurface,
        product_profile_ids: (
            AcceptancePolicyProfileIds | Sequence[str | AcceptancePolicyProfileId]
        ) = (),
        cache_ttl_seconds: int | None = None,
        capabilities_version: str | None = None,
    ) -> AcceptancePolicyAssessment:
        """Return an advisory outcome for one typed decision surface."""
        resolution, resolved_profiles = await self._resolve_selected_profiles(
            discovery,
            product_profile_ids=product_profile_ids,
            cache_ttl_seconds=cache_ttl_seconds,
            capabilities_version=capabilities_version,
        )
        if resolution.diagnostics or resolution.catalog is None or not resolution.profiles:
            return AcceptancePolicyAssessment(
                outcome=AcceptancePolicyOutcome.unknown,
                profile_ids=resolution.profile_ids,
                diagnostics=resolution.diagnostics,
            )
        if applies_to not in _SURFACES:
            return AcceptancePolicyAssessment(
                outcome=AcceptancePolicyOutcome.unknown,
                profile_ids=resolution.profile_ids,
                diagnostics=(_diag(AcceptancePolicyDiagnosticCode.incomplete_context),),
            )
        try:
            context_model = TypeAdapter(AcceptanceContext).validate_python(context)
        except (ValidationError, ValueError, TypeError):
            return AcceptancePolicyAssessment(
                outcome=AcceptancePolicyOutcome.unknown,
                profile_ids=resolution.profile_ids,
                diagnostics=(_diag(AcceptancePolicyDiagnosticCode.incomplete_context),),
            )

        return self._assess_profiles(resolved_profiles, context_model, applies_to)

    @staticmethod
    def _normalize_product_profile_ids(
        value: AcceptancePolicyProfileIds | Sequence[str | AcceptancePolicyProfileId],
    ) -> list[str]:
        raw = getattr(value, "root", value)
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise TypeError("product_profile_ids must be an array")
        if not raw:
            return []
        model = TypeAdapter(AcceptancePolicyProfileIds).validate_python(raw)
        return [_root_string(item) for item in model.root]

    async def _load_catalog(
        self,
        url: str,
        digest: str,
        *,
        cache_ttl_seconds: int | None,
        capabilities_version: str | None,
    ) -> tuple[_CatalogDocument, bool]:
        canonical_url = self._validate_catalog_url(url)
        key = (canonical_url, digest, capabilities_version)
        now = self._monotonic()
        async with self._cache_lock:
            self._prune_cache(now)
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at > now:
                self._cache.move_to_end(key)
                return cached.document, True

        body = await self._fetch_catalog(canonical_url)
        actual = f"sha256:{hashlib.sha256(body).hexdigest()}"
        if not _digest_equal(actual, digest):
            raise _CatalogFailure(_diag(AcceptancePolicyDiagnosticCode.catalog_digest_mismatch))
        try:
            parsed = json.loads(body)
        except (RecursionError, UnicodeDecodeError, json.JSONDecodeError):
            raise _CatalogFailure(
                _diag(AcceptancePolicyDiagnosticCode.catalog_invalid_json)
            ) from None
        if not isinstance(parsed, dict):
            raise _CatalogFailure(_diag(AcceptancePolicyDiagnosticCode.catalog_schema_invalid))
        validator = get_named_validator(_CATALOG_SCHEMA)
        if validator is None:
            raise _CatalogFailure(_diag(AcceptancePolicyDiagnosticCode.catalog_schema_invalid))
        try:
            schema_error = next(validator.iter_errors(parsed), None)
        except (RecursionError, TypeError, ValueError):
            raise _CatalogFailure(
                _diag(AcceptancePolicyDiagnosticCode.catalog_schema_invalid)
            ) from None
        if schema_error is not None:
            raise _CatalogFailure(_diag(AcceptancePolicyDiagnosticCode.catalog_schema_invalid))
        try:
            model = TypeAdapter(AcceptancePolicyCatalog).validate_python(parsed)
        except (ValidationError, ValueError, TypeError):
            raise _CatalogFailure(
                _diag(AcceptancePolicyDiagnosticCode.catalog_schema_invalid)
            ) from None
        document = self._build_catalog_document(model, parsed)

        if cache_ttl_seconds is not None and cache_ttl_seconds > 0:
            expires_at = self._monotonic() + cache_ttl_seconds
            async with self._cache_lock:
                self._cache[key] = _CacheEntry(document=document, expires_at=expires_at)
                self._cache.move_to_end(key)
                self._prune_cache(self._monotonic())
                while len(self._cache) > self._max_cache_entries:
                    self._cache.popitem(last=False)
        return document, False

    def _build_catalog_document(
        self,
        model: AcceptancePolicyCatalog,
        raw: Mapping[str, Any],
    ) -> _CatalogDocument:
        local_profiles: dict[str, _ResolvedProfile] = {}
        registry_refs: dict[str, Mapping[str, Any]] = {}
        raw_profiles = raw.get("profiles") or []
        for item in raw_profiles:
            assert isinstance(item, Mapping)
            profile_id = item.get("profile_id")
            if not isinstance(profile_id, str) or profile_id in local_profiles:
                raise _CatalogFailure(_diag(AcceptancePolicyDiagnosticCode.duplicate_profile_id))
            try:
                profile_model = TypeAdapter(AcceptancePolicyProfile).validate_python(item)
            except (ValidationError, ValueError, TypeError):
                raise _CatalogFailure(
                    _diag(
                        AcceptancePolicyDiagnosticCode.profile_invalid,
                        profile_id=profile_id,
                    )
                ) from None
            self._validate_profile(item, profile_model)
            local_profiles[profile_id] = _ResolvedProfile(profile_model, item)

        for item in raw.get("registry_profiles") or []:
            assert isinstance(item, Mapping)
            profile_id = item.get("profile_id")
            if (
                not isinstance(profile_id, str)
                or profile_id in registry_refs
                or profile_id in local_profiles
            ):
                raise _CatalogFailure(_diag(AcceptancePolicyDiagnosticCode.duplicate_profile_id))
            registry_refs[profile_id] = item
        return _CatalogDocument(
            model=model,
            raw=raw,
            local_profiles=local_profiles,
            registry_refs=registry_refs,
        )

    def _validate_profile(
        self,
        raw: Mapping[str, Any],
        model: AcceptancePolicyProfile,
    ) -> None:
        profile_id = model.profile_id
        try:
            calculated = _profile_digest(raw)
        except (TypeError, ValueError, rfc8785.CanonicalizationError):
            raise _CatalogFailure(
                _diag(AcceptancePolicyDiagnosticCode.profile_invalid, profile_id=profile_id)
            ) from None
        if not _digest_equal(calculated, model.content_digest):
            raise _CatalogFailure(
                _diag(
                    AcceptancePolicyDiagnosticCode.profile_digest_mismatch,
                    profile_id=profile_id,
                )
            )
        refs = [ref.policy_id for ref in model.policy_refs]
        if len(refs) != len(set(refs)):
            raise _CatalogFailure(
                _diag(AcceptancePolicyDiagnosticCode.profile_invalid, profile_id=profile_id)
            )
        rule_ids = [rule.rule_id for rule in model.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise _CatalogFailure(
                _diag(AcceptancePolicyDiagnosticCode.profile_invalid, profile_id=profile_id)
            )
        ref_ids = set(refs)
        aliases = set((raw.get("region_aliases") or {}).keys())
        for rule in model.rules:
            if any(_root_string(value) not in aliases for value in rule.jurisdiction_groups or ()):
                raise _CatalogFailure(
                    _diag(
                        AcceptancePolicyDiagnosticCode.invalid_region_alias,
                        profile_id=profile_id,
                        rule_id=rule.rule_id,
                    )
                )
            if any(_root_string(value) not in ref_ids for value in rule.policy_ids or ()):
                raise _CatalogFailure(
                    _diag(
                        AcceptancePolicyDiagnosticCode.profile_invalid,
                        profile_id=profile_id,
                        rule_id=rule.rule_id,
                    )
                )
        scope = raw.get("scope")
        if isinstance(scope, Mapping):
            if any(str(value) not in aliases for value in scope.get("jurisdiction_groups") or ()):
                raise _CatalogFailure(
                    _diag(
                        AcceptancePolicyDiagnosticCode.invalid_region_alias,
                        profile_id=profile_id,
                    )
                )

    async def _resolve_registry_profile(
        self,
        ref: Mapping[str, Any],
        policy_cache: dict[tuple[str, str], Mapping[str, Any] | None],
    ) -> tuple[_ResolvedProfile | None, AcceptancePolicyDiagnostic | None]:
        profile_id = cast(str, ref["profile_id"])
        policy_id = cast(str, ref["policy_id"])
        version = cast(str, ref["policy_version"])
        policy, issue = await self._verified_policy(policy_id, version, policy_cache)
        if issue is not None:
            return None, issue.model_copy(update={"profile_id": profile_id})
        assert policy is not None
        if not _digest_equal(cast(str, policy["content_digest"]), cast(str, ref["policy_digest"])):
            return None, _diag(
                AcceptancePolicyDiagnosticCode.policy_digest_mismatch,
                profile_id=profile_id,
                policy_id=policy_id,
            )
        raw_profile = policy.get("acceptance_profile")
        if not isinstance(raw_profile, Mapping):
            return None, _diag(
                AcceptancePolicyDiagnosticCode.profile_unresolved,
                profile_id=profile_id,
                policy_id=policy_id,
            )
        try:
            profile = TypeAdapter(AcceptancePolicyProfile).validate_python(raw_profile)
            self._validate_profile(raw_profile, profile)
        except (ValidationError, ValueError, TypeError, _CatalogFailure):
            return None, _diag(
                AcceptancePolicyDiagnosticCode.profile_invalid,
                profile_id=profile_id,
                policy_id=policy_id,
            )
        if profile.profile_id != profile_id or profile.version != ref["profile_version"]:
            return None, _diag(
                AcceptancePolicyDiagnosticCode.profile_unresolved,
                profile_id=profile_id,
                policy_id=policy_id,
            )
        if not _digest_equal(profile.content_digest, cast(str, ref["profile_digest"])):
            return None, _diag(
                AcceptancePolicyDiagnosticCode.profile_digest_mismatch,
                profile_id=profile_id,
                policy_id=policy_id,
            )
        return _ResolvedProfile(profile, raw_profile), None

    async def _verify_profile_policy_refs(
        self,
        profile: _ResolvedProfile,
        policy_cache: dict[tuple[str, str], Mapping[str, Any] | None],
    ) -> AcceptancePolicyDiagnostic | None:
        for ref in profile.model.policy_refs:
            policy, issue = await self._verified_policy(ref.policy_id, ref.version, policy_cache)
            if issue is not None:
                return issue.model_copy(update={"profile_id": profile.model.profile_id})
            assert policy is not None
            if not _digest_equal(cast(str, policy["content_digest"]), ref.content_digest):
                return _diag(
                    AcceptancePolicyDiagnosticCode.policy_digest_mismatch,
                    profile_id=profile.model.profile_id,
                    policy_id=ref.policy_id,
                )
        return None

    async def _verified_policy(
        self,
        policy_id: str,
        version: str,
        cache: dict[tuple[str, str], Mapping[str, Any] | None],
    ) -> tuple[Mapping[str, Any] | None, AcceptancePolicyDiagnostic | None]:
        key = (policy_id, version)
        if key not in cache:
            try:
                value = await asyncio.wait_for(
                    self._registry.resolve_policy(policy_id, version),
                    timeout=self._timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                value = None
            cache[key] = None if value is None else _model_mapping(value)
        policy = cache[key]
        if policy is None:
            return None, _diag(
                AcceptancePolicyDiagnosticCode.policy_unresolved,
                policy_id=policy_id,
            )
        if policy.get("policy_id") != policy_id or policy.get("version") != version:
            return None, _diag(
                AcceptancePolicyDiagnosticCode.policy_version_mismatch,
                policy_id=policy_id,
            )
        canonical = policy.get("canonical_content")
        claimed = policy.get("content_digest")
        if not isinstance(canonical, Mapping) or not isinstance(claimed, str):
            return None, _diag(
                AcceptancePolicyDiagnosticCode.policy_invalid,
                policy_id=policy_id,
            )
        try:
            calculated = _sha256_jcs(canonical)
        except (TypeError, ValueError, rfc8785.CanonicalizationError):
            return None, _diag(
                AcceptancePolicyDiagnosticCode.policy_invalid,
                policy_id=policy_id,
            )
        if not _digest_equal(calculated, claimed):
            return None, _diag(
                AcceptancePolicyDiagnosticCode.policy_digest_mismatch,
                policy_id=policy_id,
            )
        return policy, None

    def _validate_catalog_url(self, url: str) -> str:
        try:
            parts = urlsplit(url)
            if (
                len(url) > 2048
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in url)
                or parts.scheme.lower() != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.fragment
            ):
                raise ValueError
            return canonicalize_target_uri(parts.geturl())
        except (ValueError, UnicodeError):
            raise _CatalogFailure(
                _diag(AcceptancePolicyDiagnosticCode.unsafe_catalog_url)
            ) from None

    async def _fetch_catalog(self, url: str) -> bytes:
        async def fetch() -> bytes:
            try:
                transport = await asyncio.to_thread(build_async_ip_pinned_transport, url)
            except Exception:
                raise _CatalogFailure(
                    _diag(AcceptancePolicyDiagnosticCode.unsafe_catalog_url)
                ) from None
            async with httpx.AsyncClient(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=self._timeout_seconds,
                headers={"Accept": "application/json", "Accept-Encoding": "gzip, deflate"},
            ) as client:
                try:
                    async with client.stream("GET", url) as response:
                        if 300 <= response.status_code < 400:
                            raise _CatalogFailure(
                                _diag(AcceptancePolicyDiagnosticCode.unsafe_catalog_url)
                            )
                        if response.status_code < 200 or response.status_code >= 300:
                            raise _CatalogFailure(
                                _diag(AcceptancePolicyDiagnosticCode.catalog_fetch_failed)
                            )
                        try:
                            return await async_read_decoded_limited_bytes(
                                response,
                                limit=self._max_body_bytes,
                            )
                        except ResponseTooLargeError:
                            raise _CatalogFailure(
                                _diag(AcceptancePolicyDiagnosticCode.catalog_too_large)
                            ) from None
                        except ValueError:
                            raise _CatalogFailure(
                                _diag(AcceptancePolicyDiagnosticCode.catalog_fetch_failed)
                            ) from None
                except _CatalogFailure:
                    raise
                except httpx.HTTPError:
                    raise _CatalogFailure(
                        _diag(AcceptancePolicyDiagnosticCode.catalog_fetch_failed)
                    ) from None

        try:
            return await asyncio.wait_for(fetch(), timeout=self._timeout_seconds)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise _CatalogFailure(_diag(AcceptancePolicyDiagnosticCode.catalog_timeout)) from None

    def _prune_cache(self, now: float) -> None:
        for key in [key for key, entry in self._cache.items() if entry.expires_at <= now]:
            self._cache.pop(key, None)

    def _assess_profiles(
        self,
        profiles: Sequence[_ResolvedProfile],
        context: AcceptanceContext,
        applies_to: AcceptancePolicySurface,
    ) -> AcceptancePolicyAssessment:
        diagnostics: list[AcceptancePolicyDiagnostic] = []
        matched: list[AcceptancePolicyRule] = []
        uncertain = False
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        for profile in profiles:
            if profile.model.coverage.value == "partial":
                uncertain = True
                diagnostics.append(
                    _diag(
                        AcceptancePolicyDiagnosticCode.partial_coverage,
                        profile_id=profile.model.profile_id,
                    )
                )
            elif not self._complete_scope_covers(profile, context, applies_to):
                uncertain = True
                diagnostics.append(
                    _diag(
                        AcceptancePolicyDiagnosticCode.incomplete_context,
                        profile_id=profile.model.profile_id,
                    )
                )
            for rule in profile.model.rules:
                match = self._rule_match(profile, rule, context, applies_to, now)
                if match is True:
                    matched.append(rule)
                elif match is None:
                    uncertain = True
                    diagnostics.append(
                        _diag(
                            AcceptancePolicyDiagnosticCode.incomplete_context,
                            profile_id=profile.model.profile_id,
                            rule_id=rule.rule_id,
                        )
                    )

        prohibited = [rule for rule in matched if rule.disposition.value == "prohibited"]
        requirements = tuple(
            requirement
            for rule in matched
            if rule.disposition.value == "conditional"
            for requirement in (rule.requirements or ())
        )
        rule_ids = tuple(rule.rule_id for rule in matched)
        profile_ids = tuple(profile.model.profile_id for profile in profiles)
        if prohibited:
            outcome = AcceptancePolicyOutcome.prohibited
        elif uncertain:
            outcome = AcceptancePolicyOutcome.unknown
        elif requirements:
            outcome = self._requirements_outcome(requirements)
        else:
            outcome = AcceptancePolicyOutcome.allowed
        return AcceptancePolicyAssessment(
            outcome=outcome,
            profile_ids=profile_ids,
            matching_rule_ids=rule_ids,
            requirements=requirements,
            diagnostics=tuple(dict.fromkeys(diagnostics)),
        )

    def _complete_scope_covers(
        self,
        profile: _ResolvedProfile,
        context: AcceptanceContext,
        applies_to: AcceptancePolicySurface,
    ) -> bool:
        scope = profile.raw.get("scope")
        if not isinstance(scope, Mapping):
            return False
        if applies_to not in scope.get("applies_to", ()):
            return False
        subjects = context.subjects or ()
        if not subjects:
            return False
        categories = {subject.subject_category for subject in subjects}
        if not categories.issubset(set(scope.get("subject_categories", ()))):
            return False
        if scope.get("all_jurisdictions") is True:
            return True
        delivery = {_root_string(item) for item in context.delivery_jurisdictions or ()}
        if not delivery:
            return False
        covered = set(scope.get("jurisdictions", ()))
        aliases = profile.raw.get("region_aliases") or {}
        for group in scope.get("jurisdiction_groups", ()):
            covered.update(aliases.get(group, ()))
        return delivery.issubset(covered)

    def _rule_match(
        self,
        profile: _ResolvedProfile,
        rule: AcceptancePolicyRule,
        context: AcceptanceContext,
        applies_to: AcceptancePolicySurface,
        now: datetime,
    ) -> bool | None:
        if applies_to not in {str(value.value) for value in rule.applies_to}:
            return False
        if rule.effective_at is not None and now < rule.effective_at:
            return False
        if rule.expires_at is not None and now >= rule.expires_at:
            return False
        subjects = [
            subject
            for subject in context.subjects or ()
            if subject.subject_category == rule.subject_category
        ]
        if not context.subjects:
            return None
        if not subjects:
            return False
        if rule.subject_facets:
            expected = {_root_string(item) for item in rule.subject_facets}
            unknown_facets = False
            for subject in subjects:
                if not subject.subject_facets:
                    unknown_facets = True
                    continue
                if not expected.isdisjoint(
                    {_root_string(facet) for facet in subject.subject_facets}
                ):
                    break
            else:
                return None if unknown_facets else False
        if rule.advertiser_roles:
            expected_roles = {_root_string(item) for item in rule.advertiser_roles}
            actual_roles = {_root_string(item) for item in context.advertiser_roles or ()}
            if not actual_roles:
                return None
            if expected_roles.isdisjoint(actual_roles):
                return False
        if rule.jurisdictions or rule.jurisdiction_groups:
            expected_jurisdictions = {_root_string(item) for item in rule.jurisdictions or ()}
            aliases = profile.raw.get("region_aliases") or {}
            for group in rule.jurisdiction_groups or ():
                expected_jurisdictions.update(aliases.get(_root_string(group), ()))
            actual_jurisdictions = {
                _root_string(item) for item in context.delivery_jurisdictions or ()
            }
            if not actual_jurisdictions:
                return None
            if expected_jurisdictions.isdisjoint(actual_jurisdictions):
                return False
        return True

    @staticmethod
    def _requirements_outcome(
        requirements: Sequence[AcceptancePolicyRequirement],
    ) -> AcceptancePolicyOutcome:
        kinds = {
            str(requirement.model_dump(mode="json").get("kind", "")) for requirement in requirements
        }
        review = {
            "prior_authorization",
            "sales_assisted",
            "targeting_restriction",
            "creative_restriction",
            "destination_restriction",
            "format_restriction",
            "time_restriction",
            "custom",
        }
        setup = {
            "advertiser_verification",
            "advertiser_eligibility",
            "certification",
            "license",
            "account_setup",
        }
        if kinds & review:
            return AcceptancePolicyOutcome.requires_review
        if kinds & setup:
            return AcceptancePolicyOutcome.requires_setup
        return AcceptancePolicyOutcome.requires_disclosure


__all__ = [
    "AcceptancePolicyAssessment",
    "AcceptancePolicyDiagnostic",
    "AcceptancePolicyDiagnosticCode",
    "AcceptancePolicyOutcome",
    "AcceptancePolicyRegistry",
    "AcceptancePolicyResolution",
    "AcceptancePolicyResolver",
    "AcceptancePolicySurface",
    "DEFAULT_ACCEPTANCE_CATALOG_CACHE_ENTRIES",
    "DEFAULT_ACCEPTANCE_CATALOG_MAX_BYTES",
    "DEFAULT_ACCEPTANCE_CATALOG_TIMEOUT_SECONDS",
]
