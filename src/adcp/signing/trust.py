"""Experimental trust.json evaluation at binding, grants, and open onboarding.

No function in this module participates in transport key discovery. Successful
trust.json discovery supersedes legacy authorized_operators for that grantor.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache
from importlib.resources import files
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft7Validator, FormatChecker

from adcp.signing._bounded_http import async_read_limited_bytes
from adcp.signing._strict_json import StrictJsonError, parse_strict_json
from adcp.signing.bindings import OriginBindings
from adcp.signing.canonical import canonical_origin
from adcp.signing.etld import registrable_domain
from adcp.signing.jwks import SSRFValidationError, validate_uri_static
from adcp.signing.verifier import VerifiedSigner


class TrustRecordError(ValueError):
    """A missing, invalid or unavailable trust record provides no authority."""


class TrustRecordNotFoundError(TrustRecordError):
    """A definitive 404/410 provides no current operator membership."""

    operator_domain: str | None = None


class TrustRecordAbsentError(TrustRecordNotFoundError):
    """404/410 before any successful discovery permits configured legacy fallback."""


class TrustRecordUnavailableError(TrustRecordError):
    """A temporary outage cannot establish delisting or new spend authority."""


class TrustRecordMatchError(TrustRecordError):
    """A fetched record no longer lists the origin with its required role."""


class _TrustRecordCapacityError(TrustRecordUnavailableError):
    """Local backpressure proves nothing about authority and must not be cached."""


class _TrustRecordBodyError(TrustRecordError):
    """An observed HTTP 200 body was rejected before returning its bytes."""


class _TrustRecordReadError(TrustRecordUnavailableError):
    """An observed HTTP 200 publication could not finish streaming."""


@dataclass(frozen=True)
class TrustResponse:
    body: bytes
    status: int = 200


class TrustFetcher(Protocol):
    async def __call__(self, url: str, *, allow_private: bool = False) -> TrustResponse:
        raise NotImplementedError


def trust_record_url(origin_or_domain: str) -> str:
    """Derive the record from the pinned ICANN+PRIVATE PSL, never a caller pointer."""
    domain = registrable_domain(origin_or_domain)
    if domain is None:
        raise TrustRecordError("identity has no registrable operator domain")
    return f"https://{domain}/.well-known/trust.json"


async def fetch_trust_record(url: str, *, allow_private: bool = False) -> TrustResponse:
    _validate_fetch_target(url)
    from adcp.signing.ip_pinned_transport import build_async_ip_pinned_transport

    observed_200 = False

    async def fetch() -> TrustResponse:
        nonlocal observed_200
        transport = await asyncio.to_thread(
            build_async_ip_pinned_transport, url, allow_private=allow_private
        )
        fetch_timeout = httpx.Timeout(10, connect=5)
        async with httpx.AsyncClient(
            transport=transport,
            timeout=fetch_timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "GET", url, headers={"Accept": "application/json", "Accept-Encoding": "identity"}
            ) as response:
                # Status determines removal/outage before any body guard.
                # An oversized or encoded error page cannot replace policy.
                if response.status_code != 200:
                    return TrustResponse(b"", response.status_code)
                observed_200 = True
                try:
                    body = await async_read_limited_bytes(response, limit=2 * 1024 * 1024)
                except ValueError as exc:
                    raise _TrustRecordBodyError(
                        "trust record body is invalid or oversized"
                    ) from exc
                except Exception as exc:
                    raise _TrustRecordReadError("trust record body could not be fetched") from exc
                return TrustResponse(
                    body,
                    response.status_code,
                )

    try:
        return await asyncio.wait_for(fetch(), timeout=10)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        if observed_200:
            raise _TrustRecordReadError("trust record body fetch timed out") from exc
        raise


def _validate_fetch_target(url: str) -> None:
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise TrustRecordError("invalid trust fetch target") from exc
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or any(ord(c) <= 0x20 for c in url)
    ):
        raise TrustRecordError("trust fetches require HTTPS without userinfo or whitespace")
    try:
        validate_uri_static(url)
    except SSRFValidationError as exc:
        raise TrustRecordError("invalid trust fetch target") from exc


@lru_cache(maxsize=1)
def identity_schema_bundle() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(files("adcp.signing").joinpath("_identity_schemas/bundle.json").read_text()),
    )


def validate_identity_value(value: Mapping[str, Any], schema_path: str) -> None:
    """Validate wire conditionals against the offline, pinned preview schemas."""
    key = schema_path.removesuffix(".json").replace("/", "_").replace("-", "_")
    bundle = identity_schema_bundle()
    schema = {"$ref": f"#/definitions/{key}", "definitions": bundle["definitions"]}
    errors = list(Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(value))
    if errors:
        raise TrustRecordError("value does not conform to the experimental identity schema")


def validate_trust_record(document: Mapping[str, Any], *, operator_domain: str) -> dict[str, Any]:
    """Author/validate a record with one agent per canonical origin and no keys."""
    return _validate_trust_record(document, operator_domain=operator_domain)


def _validate_trust_record(
    document: Mapping[str, Any], *, operator_domain: str, reader: bool = False
) -> dict[str, Any]:
    schema_document = _trust_reader_schema_document(document) if reader else document
    validate_identity_value(schema_document, "trust/v1/trust.json")
    if "authoritative_location" in document:
        return dict(document)
    operator = registrable_domain(operator_domain)
    if operator is None:
        raise TrustRecordError("record owner has no registrable domain")
    seen: set[str] = set()
    for agent in document.get("agents", []):
        raw = agent["url"]
        parts = urlsplit(raw)
        if parts.username is not None or parts.password is not None:
            raise TrustRecordError("agent URLs must not contain userinfo")
        origin = canonical_origin(raw)
        if not origin.startswith("https://") or registrable_domain(origin) != operator:
            raise TrustRecordError("agent origin does not belong to the record owner")
        if origin in seen:
            raise TrustRecordError("a trust record must list one agent per origin")
        seen.add(origin)
    for grant in document.get("grants", []):
        if registrable_domain(grant["grantee"]) != grant["grantee"]:
            raise TrustRecordError("grant grantee must be an exact registrable domain")
    return dict(document)


def _trust_reader_schema_document(document: Mapping[str, Any]) -> Mapping[str, Any]:
    """Keep unknown advisory requirements without relaxing authority-bearing fields."""
    schema_document = copy.deepcopy(dict(document))
    known_kinds = identity_schema_bundle()["definitions"][
        "enums_agent_onboarding_requirement_kind"
    ]["enum"]

    def preserve_posture(profiles: Any) -> None:
        if not isinstance(profiles, dict):
            return
        adcp = profiles.get("adcp")
        posture = adcp.get("onboarding") if isinstance(adcp, dict) else None
        if not isinstance(posture, dict):
            return
        requirements = posture.get("requirements")
        if not isinstance(requirements, list):
            return
        # Requirements affect onboarding advice only. Future kinds cannot be
        # machine-satisfied; readers retain them for message/url fallback.
        posture["requirements"] = [
            requirement
            for requirement in requirements
            if not (
                isinstance(requirement, dict)
                and isinstance(requirement.get("kind"), str)
                and requirement["kind"] not in known_kinds
            )
        ]

    preserve_posture(schema_document.get("profiles"))
    agents = schema_document.get("agents")
    if isinstance(agents, list):
        for agent in agents:
            if isinstance(agent, dict):
                preserve_posture(agent.get("profiles"))
    return schema_document


@dataclass(frozen=True)
class EvaluatedTrustRecord:
    operator_domain: str
    source_url: str
    document: Mapping[str, Any]
    expires_at: float
    stale_on_outage: bool = False
    revision: int = 0

    def agent(self, origin: str, *, role: str) -> Mapping[str, Any]:
        origin = canonical_origin(origin)
        matches = [
            agent
            for agent in self.document.get("agents", [])
            if canonical_origin(agent["url"]) == origin
        ]
        if len(matches) != 1 or role not in matches[0]["roles"]:
            raise TrustRecordMatchError(
                "record must list exactly one matching agent with the required role"
            )
        return dict(matches[0])


@dataclass(frozen=True)
class _TrustRecordFailure:
    error_type: type[TrustRecordError]
    message: str
    retry_after: float


class TrustRecordEvaluator:
    """Fetch one authoritative-location hop and cache no longer than revocation polling.

    A non-200 fetch is unavailable except for definitive 404/410 absence or
    removal. Invalid 200 documents reject policy; outages preserve the last
    policy for explicit bounded grace. Custom fetchers enforce SSRF policy.
    """

    def __init__(
        self,
        *,
        fetcher: TrustFetcher = fetch_trust_record,
        allow_private: bool = False,
        polling_interval_seconds: float = 60,
        max_records: int = 256,
        max_history_records: int = 256,
        max_fetches_per_host: int = 4,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            not 0 < polling_interval_seconds <= 1800
            or type(max_records) is not int
            or max_records <= 0
            or type(max_history_records) is not int
            or max_history_records <= 0
            or type(max_fetches_per_host) is not int
            or max_fetches_per_host <= 0
        ):
            raise ValueError("invalid trust cache bounds")
        self._fetcher, self._allow_private, self._interval = (
            fetcher,
            allow_private,
            polling_interval_seconds,
        )
        self._max_records, self._clock = max_records, clock
        self._max_history_records = max_history_records
        # One authoritative record per eTLD+1. Pending owners reserve their
        # slot before any response can establish irreversible publication.
        self._history_owners: set[str] = set()
        # A previously used non-publisher also keeps its admission, so a
        # hosted-tenant flood cannot disable its configured legacy policy.
        # This is not reusable absence evidence during an outage.
        self._known_absent_owners: set[str] = set()
        self._cache: OrderedDict[str, EvaluatedTrustRecord] = OrderedDict()
        self._failures: OrderedDict[str, _TrustRecordFailure] = OrderedDict()
        self._pending: dict[str, asyncio.Task[EvaluatedTrustRecord]] = {}
        self._max_fetches_per_host = max_fetches_per_host
        self._responses: OrderedDict[str, tuple[float, TrustResponse]] = OrderedDict()
        self._response_pending: dict[str, asyncio.Task[tuple[float, TrustResponse]]] = {}
        self._hosts_inflight: dict[tuple[str, str], int] = {}
        self._host_totals: dict[str, int] = {}
        self._host_failures: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._seen_200: set[str] = set()
        # Authoritative history is never evicted. New operators are refused
        # before fetching when the permanent admission budget is full.
        self._last_policy: OrderedDict[str, EvaluatedTrustRecord | _TrustRecordFailure] = (
            OrderedDict()
        )
        self._policy_revision = 0
        self._lock = asyncio.Lock()

    @property
    def polling_interval_seconds(self) -> float:
        """The freshness cadence and basis for the four-interval outage grace."""
        return self._interval

    def current_time(self) -> float:
        """Read the evaluator's clock for a final authority deadline check."""
        return self._clock()

    def _remember_policy(
        self, domain: str, policy: EvaluatedTrustRecord | _TrustRecordFailure
    ) -> None:
        self._last_policy[domain] = policy
        self._last_policy.move_to_end(domain)

    async def cached_record(
        self, origin_or_domain: str, *, role: str | None = None
    ) -> EvaluatedTrustRecord:
        """Inspect latest policy during an outage, never fresh spend authority.

        A positive snapshot is usable only through freshness plus four polling
        intervals. Negative reads remain negative even beyond that window.
        Unknown history fails closed. No fetch is performed.
        """
        domain = urlsplit(trust_record_url(origin_or_domain)).hostname or ""
        async with self._lock:
            policy = self._last_policy.get(domain)
            if policy is None:
                raise TrustRecordUnavailableError("no prior trust record is available")
            if isinstance(policy, _TrustRecordFailure):
                error = policy.error_type(policy.message)
                if isinstance(error, TrustRecordNotFoundError):
                    error.operator_domain = domain
                raise error
            if role is not None:
                # Consult the latest document before its grace deadline: an
                # origin delisted by another request cannot regain authority.
                policy.agent(origin_or_domain, role=role)
            if self._clock() >= policy.expires_at + 4 * self._interval:
                raise TrustRecordUnavailableError("trust record outage grace has expired")
            return EvaluatedTrustRecord(
                policy.operator_domain,
                policy.source_url,
                copy.deepcopy(policy.document),
                policy.expires_at,
                stale_on_outage=True,
                revision=policy.revision,
            )

    async def snapshots_current(self, records: Sequence[EvaluatedTrustRecord]) -> bool:
        """Compare captured authority with every latest owner policy atomically.

        A newer read, removal, invalid document, or evicted history invalidates
        the captured decision. Re-evaluate before approving an older snapshot.
        """
        async with self._lock:
            return all(
                isinstance(
                    policy := self._last_policy.get(record.operator_domain), EvaluatedTrustRecord
                )
                and policy.revision == record.revision
                for record in records
            )

    async def validate_snapshots(
        self,
        records: Sequence[EvaluatedTrustRecord],
        *,
        origin_or_domain: str | None = None,
        role: str | None = None,
        legacy_unpublished_domain: str | None = None,
    ) -> None:
        """Recheck latest policy, revision and deadline before assigning authority.

        ``origin_or_domain``/``role`` match the first record's latest agent entry,
        even when another origin refreshed that owner's policy. This final
        guard never upgrades an older captured snapshot to a newer approval.
        """
        async with self._lock:
            if (
                legacy_unpublished_domain is not None
                and registrable_domain(legacy_unpublished_domain) in self._seen_200
            ):
                raise TrustRecordUnavailableError("trust publication superseded legacy authority")
            for index, record in enumerate(records):
                policy = self._last_policy.get(record.operator_domain)
                if policy is None:
                    raise TrustRecordUnavailableError("trust policy history was evicted")
                if isinstance(policy, _TrustRecordFailure):
                    error = policy.error_type(policy.message)
                    if isinstance(error, TrustRecordNotFoundError):
                        error.operator_domain = record.operator_domain
                    raise error
                if index == 0 and origin_or_domain is not None and role is not None:
                    policy.agent(origin_or_domain, role=role)
                if policy.revision != record.revision:
                    raise TrustRecordUnavailableError("trust policy changed during evaluation")
                deadline = record.expires_at + (4 * self._interval if record.stale_on_outage else 0)
                if self._clock() >= deadline:
                    raise TrustRecordUnavailableError("trust policy expired during evaluation")

    async def evaluate(
        self,
        origin_or_domain: str,
        *,
        role: str | None = None,
        allow_stale_on_outage: bool = False,
    ) -> EvaluatedTrustRecord:
        """Require fresh policy unless an existing non-spend caller opts into grace."""
        try:
            return await self._evaluate_fresh(origin_or_domain, role=role)
        except TrustRecordUnavailableError as outage:
            try:
                cached = await self.cached_record(origin_or_domain, role=role)
            except TrustRecordUnavailableError as history_error:
                raise history_error if allow_stale_on_outage else outage
            # Retained delisting/removal/content errors are checked above even
            # for spend. A cap must not disguise a known denial as an outage.
            if not allow_stale_on_outage:
                raise outage
            return cached

    async def _evaluate_fresh(
        self, origin_or_domain: str, *, role: str | None = None
    ) -> EvaluatedTrustRecord:
        url = trust_record_url(origin_or_domain)
        domain = urlsplit(url).hostname or ""
        async with self._lock:
            now = self._clock()
            record = self._cache.get(domain)
            if record is None or record.expires_at <= now:
                if (
                    domain not in self._history_owners
                    and len(self._history_owners) >= self._max_history_records
                ):
                    raise _TrustRecordCapacityError("trust history admission capacity exceeded")
                failure = self._failures.get(domain)
                if failure is not None and failure.retry_after > now:
                    self._failures.move_to_end(domain)
                    raise failure.error_type(failure.message)
                task = self._pending.get(domain)
                if task is None:
                    if len(self._pending) >= self._max_records:
                        raise _TrustRecordCapacityError("trust discovery capacity exceeded")
                    self._history_owners.add(domain)
                    task = asyncio.create_task(self._refresh(domain, url, now))
                    # One disconnected caller cannot cancel the shared fetch or
                    # leave a background task's exception unobserved.
                    task.add_done_callback(
                        lambda done: done.exception() if not done.cancelled() else None
                    )
                    self._pending[domain] = task
            else:
                self._cache.move_to_end(domain)
                task = None
        if task is not None:
            record = await asyncio.shield(task)
        assert record is not None
        if record.expires_at <= self._clock():
            raise TrustRecordUnavailableError("trust record expired while being evaluated")
        if role is not None:
            record.agent(origin_or_domain, role=role)
        # Callers cannot mutate cached authority through a returned mapping.
        import copy

        return EvaluatedTrustRecord(
            record.operator_domain,
            record.source_url,
            copy.deepcopy(record.document),
            record.expires_at,
            revision=record.revision,
        )

    async def _refresh(self, domain: str, url: str, now: float) -> EvaluatedTrustRecord:
        try:
            record = await self._fetch(domain, url, now)
            async with self._lock:
                self._policy_revision += 1
                record = replace(record, revision=self._policy_revision)
                self._cache[domain] = record
                self._remember_policy(domain, record)
                self._cache.move_to_end(domain)
                self._failures.pop(domain, None)
                while len(self._cache) > self._max_records:
                    self._cache.popitem(last=False)
            return record
        except _TrustRecordCapacityError:
            # A busy local fetch pool is not evidence of a counterparty outage.
            # Retry immediately once a slot becomes available.
            raise
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, TrustRecordError)
                else TrustRecordUnavailableError("trust record fetch failed")
            )
            async with self._lock:
                failure = _TrustRecordFailure(type(error), str(error), self._clock() + 60)
                self._failures[domain] = failure
                if isinstance(error, TrustRecordAbsentError):
                    self._known_absent_owners.add(domain)
                if not isinstance(error, (TrustRecordUnavailableError, TrustRecordAbsentError)):
                    # Removal and malformed policy supersede older authority;
                    # subsequent network/rate/capacity failures never undo it.
                    self._cache.pop(domain, None)
                    self._remember_policy(domain, failure)
                self._failures.move_to_end(domain)
                while len(self._failures) > self._max_records:
                    self._failures.popitem(last=False)
            if error is exc:
                raise
            raise error from exc
        finally:
            async with self._lock:
                self._pending.pop(domain, None)
                if (
                    domain not in self._seen_200
                    and domain not in self._last_policy
                    and domain not in self._known_absent_owners
                ):
                    self._history_owners.discard(domain)

    async def _fetch(self, domain: str, url: str, now: float) -> EvaluatedTrustRecord:
        source = url
        expires_at = now + self._interval
        for hop in range(2):
            _validate_fetch_target(source)
            try:
                response_expires, response = await self._fetch_response(source, owner_domain=domain)
                expires_at = min(expires_at, response_expires)
            except _TrustRecordCapacityError:
                raise
            except (_TrustRecordBodyError, _TrustRecordReadError):
                # Publication is established by status even if its body fails.
                # A later 404 cannot reopen authorized_operators fallback.
                self._seen_200.add(domain)
                raise
            except TrustRecordError:
                raise
            except Exception as exc:
                raise TrustRecordUnavailableError("trust record fetch failed") from exc
            if response.status in {404, 410} and hop == 0 and domain not in self._seen_200:
                raise TrustRecordAbsentError("no trust record is published")
            if response.status in {404, 410}:
                raise TrustRecordNotFoundError("operator trust record was removed")
            if response.status != 200:
                raise TrustRecordUnavailableError("trust record is temporarily unavailable")
            self._seen_200.add(domain)
            if len(response.body) > 2 * 1024 * 1024:
                raise TrustRecordError("trust record is unavailable or oversized")
            try:
                document = parse_strict_json(response.body)
            except StrictJsonError as exc:
                raise TrustRecordError("trust record is not strict JSON") from exc
            if not isinstance(document, dict):
                raise TrustRecordError("trust record must be an object")
            document = _validate_trust_record(document, operator_domain=domain, reader=True)
            pointer = document.get("authoritative_location")
            if pointer is None:
                return EvaluatedTrustRecord(domain, source, document, expires_at)
            if hop == 1:
                raise TrustRecordError("a second authoritative-location hop is forbidden")
            source = pointer
        raise TrustRecordError("trust record resolution failed")

    async def _fetch_response(self, url: str, *, owner_domain: str) -> tuple[float, TrustResponse]:
        """Share transport bytes while preserving each original owner's validation."""
        host = urlsplit(url).hostname or ""
        owner_host = (owner_domain, host)
        async with self._lock:
            now = self._clock()
            cached = self._responses.get(url)
            if cached is not None and cached[0] > now:
                self._responses.move_to_end(url)
                return cached
            if self._host_failures.get(owner_host, 0) > now:
                self._host_failures.move_to_end(owner_host)
                raise TrustRecordUnavailableError("trust fetch host is cooling down")
            task = self._response_pending.get(url)
            if task is None:
                if (
                    len(self._response_pending) >= self._max_records
                    or self._host_totals.get(host, 0) >= self._max_fetches_per_host
                ):
                    raise _TrustRecordCapacityError("trust fetch host capacity exceeded")
                task = asyncio.create_task(self._refresh_response(url, owner_host, now))
                task.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
                self._response_pending[url] = task
                self._hosts_inflight[owner_host] = self._hosts_inflight.get(owner_host, 0) + 1
                # Bound actual outbound pressure across owners, but do not
                # turn this temporary resource limit into cached denial.
                self._host_totals[host] = self._host_totals.get(host, 0) + 1
        return await asyncio.shield(task)

    async def _refresh_response(
        self, url: str, owner_host: tuple[str, str], now: float
    ) -> tuple[float, TrustResponse]:
        try:
            response = await self._fetcher(url, allow_private=self._allow_private)
            if response.status != 200:
                response = TrustResponse(b"", response.status)
            elif len(response.body) > 2 * 1024 * 1024:
                raise _TrustRecordBodyError("trust record is oversized")
            async with self._lock:
                ttl = self._interval if response.status == 200 else 60
                cached = (now + ttl, response)
                self._responses[url] = cached
                self._responses.move_to_end(url)
                while len(self._responses) > self._max_records:
                    self._responses.popitem(last=False)
                if response.status >= 500 or response.status == 429:
                    self._cool_down_host(owner_host)
            return cached
        except _TrustRecordCapacityError:
            raise
        except _TrustRecordReadError:
            async with self._lock:
                self._cool_down_host(owner_host)
            raise
        except Exception as exc:
            # Validation/content errors (including the streaming size and
            # encoding guards) describe one URL, not a host outage.
            if not isinstance(exc, ValueError):
                async with self._lock:
                    self._cool_down_host(owner_host)
                raise TrustRecordUnavailableError("trust record fetch failed") from exc
            if isinstance(exc, TrustRecordError):
                raise
            raise TrustRecordError("trust record content is invalid") from exc
        finally:
            async with self._lock:
                self._response_pending.pop(url, None)
                remaining = self._hosts_inflight[owner_host] - 1
                if remaining:
                    self._hosts_inflight[owner_host] = remaining
                else:
                    self._hosts_inflight.pop(owner_host, None)
                host = owner_host[1]
                host_remaining = self._host_totals[host] - 1
                if host_remaining:
                    self._host_totals[host] = host_remaining
                else:
                    self._host_totals.pop(host, None)

    def _cool_down_host(self, owner_host: tuple[str, str]) -> None:
        """A pointer selected by one owner cannot suppress another owner's fetches."""
        self._host_failures[owner_host] = self._clock() + 60
        self._host_failures.move_to_end(owner_host)
        while len(self._host_failures) > self._max_records:
            self._host_failures.popitem(last=False)

    async def bind(
        self,
        signer: VerifiedSigner,
        *,
        principal_id: str,
        bindings: OriginBindings,
        existing_rfc9421_principal: bool = False,
        migration_approved: bool = False,
        role: str = "adcp:buying",
    ) -> EvaluatedTrustRecord:
        """Explicitly bind a verified origin after evaluating its operator record."""
        if signer.profile != "web-bot-auth" or signer.agent_url is None:
            raise TrustRecordError("binding requires a verified Web Bot Auth identity")
        record = await self.evaluate(signer.agent_url, role=role)
        bindings.bind(
            signer.agent_url,
            principal_id,
            existing_rfc9421_principal=existing_rfc9421_principal,
            migration_approved=migration_approved,
        )
        return record


@dataclass(frozen=True)
class GrantAuthorization:
    authorized: bool
    reason: str | None = None
    legacy_fallback_allowed: bool = False
    expires_at: float | None = None
    error_code: Literal["PERMISSION_DENIED", "SERVICE_UNAVAILABLE"] = "PERMISSION_DENIED"
    include_grant_absent_details: bool = True
    outage_expires_at: float | None = None
    stale_on_outage: bool = False

    def require(self) -> None:
        """Only call after signer establishment/registry authorization, avoiding an oracle."""
        if not self.authorized:
            from adcp.decisioning.types import AdcpError

            if self.error_code == "SERVICE_UNAVAILABLE":
                raise AdcpError(
                    "SERVICE_UNAVAILABLE",
                    message="Trust record is temporarily unavailable",
                    recovery="transient",
                )
            raise AdcpError(
                "PERMISSION_DENIED",
                message="Agent has no authority for this brand",
                details=(
                    {"scope": "agent", "reason": "grant_absent"}
                    if self.include_grant_absent_details
                    else None
                ),
                recovery="correctable",
            )


@dataclass(frozen=True)
class GrantRequest:
    """Scoped claim passed to explicit seller and legacy authorization hooks."""

    agent_origin: str
    brand_domain: str
    account_operator: str
    scope: str
    brand_id: str | None
    countries: tuple[str, ...]


def _scope_covers(scopes: Sequence[str], scope: str) -> bool:
    return scope in scopes or "*" in scopes or (scope.startswith("adcp:") and "adcp:all" in scopes)


def _trust_grant_failure(error: TrustRecordError, reason: str) -> GrantAuthorization:
    """Keep an outage distinct from an uncovered grant after identity establishment."""
    return GrantAuthorization(
        False,
        reason,
        error_code=(
            "SERVICE_UNAVAILABLE"
            if isinstance(error, TrustRecordUnavailableError)
            else "PERMISSION_DENIED"
        ),
        include_grant_absent_details=False,
    )


def _expired_grant_evaluation(reason: str) -> GrantAuthorization:
    return GrantAuthorization(
        False,
        reason,
        error_code="SERVICE_UNAVAILABLE",
        include_grant_absent_details=False,
    )


def _grant_matches(
    grant: Mapping[str, Any],
    *,
    grantee: str,
    origin: str,
    scope: str,
    brand_id: str | None,
    countries: frozenset[str],
    now: float,
) -> bool:
    if grant["grantee"] != grantee or not _scope_covers(grant["scopes"], scope):
        return False
    agents = grant.get("agents")
    if agents is not None and not any(canonical_origin(agent["url"]) == origin for agent in agents):
        return False
    if "countries" in grant and (not countries or not countries <= set(grant["countries"])):
        return False
    brands = grant.get("profiles", {}).get("adcp", {}).get("brands", ["*"])
    if "*" not in brands and (brand_id is None or brand_id not in brands):
        return False
    for field, before in (("valid_from", True), ("valid_until", False)):
        if field in grant:
            instant = datetime.fromisoformat(grant[field].replace("Z", "+00:00")).timestamp()
            if (before and now < instant) or (not before and now >= instant):
                return False
    return True


class TrustGrantEvaluator:
    """Evaluate direct or one-intermediary demand grants against the brand's record.

    Checking every requested dimension at every link implements intersection
    without allowing a grantee to expand authority. Seller discretion is a
    separate explicit hook, never a brand.json fallback after HTTP 200.
    """

    def __init__(
        self,
        records: TrustRecordEvaluator,
        *,
        clock: Callable[[], float] | None = None,
        seller_authorization: Callable[[GrantRequest], Awaitable[bool]] | None = None,
        legacy_authorization: Callable[[GrantRequest], Awaitable[bool]] | None = None,
    ) -> None:
        self.records = records
        self._clock = clock if clock is not None else records._clock
        self._seller_authorization = seller_authorization
        self._legacy_authorization = legacy_authorization

    def current_time(self) -> float:
        """Read the evaluation clock before using a retained claim deadline."""
        return self._clock()

    def _record_deadline(self, record: EvaluatedTrustRecord) -> float:
        return record.expires_at + (
            4 * self.records.polling_interval_seconds if record.stale_on_outage else 0
        )

    async def _authorization(
        self,
        authorized: bool,
        records: Sequence[EvaluatedTrustRecord],
        *,
        grant_expiry: float = float("inf"),
        legacy_fallback_allowed: bool = False,
        agent_origin: str | None = None,
        legacy_unpublished_domain: str | None = None,
    ) -> GrantAuthorization:
        if authorized:
            try:
                await self.records.validate_snapshots(
                    records,
                    origin_or_domain=agent_origin,
                    role="adcp:buying" if agent_origin is not None else None,
                    legacy_unpublished_domain=legacy_unpublished_domain,
                )
            except TrustRecordNotFoundError as exc:
                if any(record.operator_domain == exc.operator_domain for record in records[1:]):
                    return GrantAuthorization(False, "grant_absent")
                return _trust_grant_failure(exc, "signer_trust_record_unavailable")
            except TrustRecordError as exc:
                return _trust_grant_failure(exc, "trust_record_changed_during_evaluation")
            if any(self._record_deadline(record) <= self._clock() for record in records):
                return _expired_grant_evaluation("trust_record_unavailable")
            if grant_expiry <= self._clock():
                return GrantAuthorization(False, "grant_absent")
        return GrantAuthorization(
            authorized,
            legacy_fallback_allowed=legacy_fallback_allowed,
            expires_at=(
                min([record.expires_at for record in records] + [grant_expiry]) if records else None
            ),
            outage_expires_at=(
                min(
                    [
                        record.expires_at + 4 * self.records.polling_interval_seconds
                        for record in records
                    ]
                    + [grant_expiry]
                )
                if authorized and records
                else None
            ),
            stale_on_outage=any(record.stale_on_outage for record in records),
        )

    async def check(
        self,
        *,
        agent_origin: str,
        brand_domain: str,
        account_operator: str,
        scope: str = "adcp:media_buying",
        brand_id: str | None = None,
        countries: Sequence[str] = (),
        signing_profile: Literal["web-bot-auth", "adcp/request-signing/v1"] = "web-bot-auth",
        allow_stale_on_outage: bool = False,
    ) -> GrantAuthorization:
        origin = canonical_origin(agent_origin)
        signer_operator = registrable_domain(origin)
        brand_operator = registrable_domain(brand_domain)
        operator = registrable_domain(account_operator)
        if signer_operator is None or brand_operator is None or operator is None:
            return GrantAuthorization(False, "grant_absent", include_grant_absent_details=False)
        request = GrantRequest(
            origin, brand_domain, account_operator, scope, brand_id, tuple(countries)
        )
        if signing_profile not in {"web-bot-auth", "adcp/request-signing/v1"}:
            raise ValueError("unsupported authenticated signing profile")
        signer_record: EvaluatedTrustRecord | None = None
        if signing_profile == "web-bot-auth":
            try:
                # WBA authority always requires a current operator role entry.
                signer_record = await self.records.evaluate(
                    origin, role="adcp:buying", allow_stale_on_outage=allow_stale_on_outage
                )
            except TrustRecordError as exc:
                return _trust_grant_failure(exc, "signer_trust_record_unavailable")
            if signer_operator == brand_operator == operator:
                return await self._authorization(True, [signer_record], agent_origin=origin)
        try:
            brand_record = await self.records.evaluate(
                brand_domain, allow_stale_on_outage=allow_stale_on_outage
            )
        except TrustRecordAbsentError:
            if self._legacy_authorization is not None:
                authorized = bool(await self._legacy_authorization(request))
                if (
                    signer_record is not None
                    and self._record_deadline(signer_record) <= self._clock()
                ):
                    return _expired_grant_evaluation("signer_trust_record_unavailable")
                if signer_record is not None:
                    return await self._authorization(
                        authorized,
                        [signer_record],
                        legacy_fallback_allowed=True,
                        agent_origin=origin,
                        legacy_unpublished_domain=brand_domain,
                    )
                return await self._authorization(
                    authorized,
                    [],
                    legacy_fallback_allowed=True,
                    legacy_unpublished_domain=brand_domain,
                )
            return GrantAuthorization(False, "trust_record_absent", legacy_fallback_allowed=True)
        except TrustRecordNotFoundError:
            return GrantAuthorization(False, "grant_absent")
        except TrustRecordError as exc:
            return _trust_grant_failure(exc, "trust_record_unavailable")
        if signer_record is None:
            # Legacy credentials retain their established onboarding path when
            # the brand has no trust record. Once the brand serves HTTP 200,
            # grants have one source and require the new operator role entry.
            try:
                signer_record = await self.records.evaluate(
                    origin, role="adcp:buying", allow_stale_on_outage=allow_stale_on_outage
                )
            except TrustRecordError as exc:
                return _trust_grant_failure(exc, "signer_trust_record_unavailable")
            if signer_operator == brand_operator == operator:
                return await self._authorization(True, [signer_record], agent_origin=origin)
        args: dict[str, Any] = {
            "origin": origin,
            "scope": scope,
            "brand_id": brand_id,
            "countries": frozenset(countries),
            "now": self._clock(),
        }
        direct = operator in {signer_operator, brand_operator}
        grantee = signer_operator if direct else operator
        first = [
            g
            for g in brand_record.document.get("grants", [])
            if _grant_matches(g, grantee=grantee, **args)
        ]
        links: list[list[Mapping[str, Any]]] = []
        used_records = [signer_record, brand_record]
        if direct:
            links = [first]
        elif first:
            try:
                intermediary = await self.records.evaluate(
                    operator, allow_stale_on_outage=allow_stale_on_outage
                )
            except TrustRecordNotFoundError:
                return GrantAuthorization(False, "grant_absent")
            except TrustRecordError as exc:
                return _trust_grant_failure(exc, "trust_record_unavailable")
            used_records.append(intermediary)
            # Fetching the intermediary may outlive an earlier grant. Evaluate
            # both links at the decision time, not at request receipt.
            args["now"] = self._clock()
            first = [g for g in first if _grant_matches(g, grantee=operator, **args)]
            second = [
                g
                for g in intermediary.document.get("grants", [])
                if _grant_matches(g, grantee=signer_operator, **args)
            ]
            links = [first, second]
        if any(self._record_deadline(record) <= self._clock() for record in used_records):
            return _expired_grant_evaluation("trust_record_unavailable")
        if not links or any(not link for link in links):
            exhaustive = brand_record.document.get("exclusive_grants", False) and any(
                _scope_covers(grant["scopes"], scope)
                for grant in brand_record.document.get("grants", [])
            )
            if not exhaustive and self._seller_authorization is not None:
                authorized = bool(await self._seller_authorization(request))
                expiry = min(self._record_deadline(record) for record in used_records)
                # Seller discretion cannot outlive the required operator role
                # evaluation while its application-owned policy lookup awaits.
                if expiry <= self._clock():
                    return _expired_grant_evaluation("trust_record_unavailable")
                return await self._authorization(authorized, used_records, agent_origin=origin)
            return GrantAuthorization(False, "grant_absent")
        # Every surviving grant covers the same requested dimensions. The
        # best path's expiry is the minimum of each link's best expiry. Avoid
        # materializing the cartesian product of untrusted grant arrays.
        grant_expiry = min(
            [
                max(
                    (
                        datetime.fromisoformat(g["valid_until"].replace("Z", "+00:00")).timestamp()
                        if "valid_until" in g
                        else float("inf")
                    )
                    for g in link
                )
                for link in links
            ]
        )
        if grant_expiry <= self._clock():
            return GrantAuthorization(False, "grant_absent")
        if any(self._record_deadline(record) <= self._clock() for record in used_records):
            return _expired_grant_evaluation("trust_record_unavailable")
        return await self._authorization(
            True, used_records, grant_expiry=grant_expiry, agent_origin=origin
        )


__all__ = [
    "GrantRequest",
    "EvaluatedTrustRecord",
    "GrantAuthorization",
    "TrustFetcher",
    "TrustGrantEvaluator",
    "TrustRecordAbsentError",
    "TrustRecordError",
    "TrustRecordMatchError",
    "TrustRecordNotFoundError",
    "TrustRecordUnavailableError",
    "TrustRecordEvaluator",
    "TrustResponse",
    "trust_record_url",
    "validate_trust_record",
    "validate_identity_value",
]
