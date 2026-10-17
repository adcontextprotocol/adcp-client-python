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
    """A trust record that provides no authority.

    Two kinds matter for a bound origin. *Not listed*
    (:class:`TrustRecordMatchError`, :class:`TrustRecordAbsentError`): the
    operator's last record read does not list the origin with the role, or
    the operator has never published one. *Unavailable*
    (:class:`TrustRecordUnavailableError`): the record cannot be read now,
    including a 404/410 after the operator published one. A fetch failure is
    never absence and never a removal: the last record read stays
    authoritative.
    """


class TrustRecordNotFoundError(TrustRecordError):
    """Base of :class:`TrustRecordAbsentError`: a 404/410 before any 200.

    A 404/410 from an operator that has served a record is a discovery
    failure (:class:`TrustRecordUnavailableError`), never a removal.
    """


class TrustRecordAbsentError(TrustRecordNotFoundError):
    """404/410 before any successful discovery permits configured legacy fallback."""


class TrustRecordUnavailableError(TrustRecordError):
    """A record that cannot be read: an outage, never absence or a removal."""


class TrustRecordMatchError(TrustRecordError):
    """The last record read does not list the origin with its required role."""


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
    if not reader:
        # Authoring: publish grantees as registrable domains. Readers compare
        # any grantee as its registrable domain instead of rejecting it.
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
    """A validated trust record for ``operator_domain``.

    ``stale_on_outage`` marks the last record read, returned during an outage
    within the evaluator's outage-continuation bound; ``expires_at`` is then
    the end of that bound.
    """

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
class TrustListing:
    """The outcome of :meth:`TrustRecordEvaluator.check_listing`.

    ``status`` is ``"listed"``, ``"unlisted"`` (the last record read does not
    list the origin with the role, including an empty ``agents[]``; the
    operator never published a record; or the origin has no registrable
    domain) or ``"unavailable"`` (the record cannot be read now, and the last
    record read listed the origin, or there is none). ``within_grace`` marks
    an outage within the seller's outage-continuation bound, when ``record``
    is the last record read, marked ``stale_on_outage``: non-spend calls may
    continue under a binding; spend never does.
    """

    status: Literal["listed", "unlisted", "unavailable"]
    record: EvaluatedTrustRecord | None = None
    within_grace: bool = False
    error: TrustRecordError | None = None


@dataclass(frozen=True)
class _TrustRecordFailure:
    error_type: type[TrustRecordError]
    message: str
    retry_after: float


def _quota_group(domain: str) -> str:
    from adcp.signing.etld import quota_key

    return quota_key(domain) or domain


class TrustRecordEvaluator:
    """Evaluate operator trust records at binding, grants and open onboarding.

    Never on the signature path. Derives ``https://{eTLD+1}/.well-known/trust.json``,
    follows at most one ``authoritative_location`` hop with no HTTP redirects,
    and caches a result no longer than the polling interval.

    Outage rules (security.mdx, Web Bot Auth identities):

    * A fetch failure is not absence. Any status other than 200 (a 404/410
      after the operator has served a record included), a transport error, an
      invalid record, or a local capacity refusal is *unavailable*. Only a
      404/410 from an operator that has never served a 200 is absence
      (:class:`TrustRecordAbsentError`).
    * The last record read stays authoritative. If it does not list an origin
      (operators retire agents by publishing an empty ``agents[]``), the
      origin stays delisted through any later failure.
    * Spend never continues on a stale record. Non-spend callers may opt in
      (``allow_stale_on_outage``) to the last record read within
      ``outage_continuation_seconds`` past its freshness: seller policy,
      default four polling intervals, ``0`` disables.

    Which operators have served a record, and the last record read from each,
    is authority history. It is kept in memory, bounded without eviction:
    ``max_history_records`` operators overall and
    ``max_history_records_per_operator`` per quota group (a registrable
    domain; every tenant of a PSL PRIVATE hosting suffix counts together).
    A new operator over quota is refused as unavailable before fetching, and
    no other operator's history is lost. Losing this history (a restart,
    another replica) turns a later 404 into absence and a known delisting into
    an outage; public deployments must persist it durably.

    Fetches to an operator's own hosts are budgeted per ``(operator, host)``
    (``max_fetches_per_host``). ``authoritative_location`` hops to hosts
    outside the operator's registrable domain share a separate, global
    per-host budget (``max_foreign_fetches_per_host``), so a hop never spends
    the target operator's own budget. Host cool-downs are scoped to
    ``(operator, host)`` and never follow content errors. A budget refusal is
    a retryable outage that is never cached as a failure. Custom fetchers
    enforce SSRF policy.
    """

    def __init__(
        self,
        *,
        fetcher: TrustFetcher = fetch_trust_record,
        allow_private: bool = False,
        polling_interval_seconds: float = 60,
        max_records: int = 256,
        max_history_records: int = 65536,
        max_history_records_per_operator: int = 64,
        max_fetches_per_host: int = 4,
        max_foreign_fetches_per_host: int = 16,
        outage_continuation_seconds: float | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        limits = (
            max_records,
            max_history_records,
            max_history_records_per_operator,
            max_fetches_per_host,
            max_foreign_fetches_per_host,
        )
        if not 0 < polling_interval_seconds <= 1800 or any(
            type(limit) is not int or limit <= 0 for limit in limits
        ):
            raise ValueError("invalid trust cache bounds")
        if outage_continuation_seconds is None:
            outage_continuation_seconds = 4 * polling_interval_seconds
        if outage_continuation_seconds < 0:
            raise ValueError("outage_continuation_seconds must not be negative")
        self._fetcher, self._allow_private, self._interval = (
            fetcher,
            allow_private,
            polling_interval_seconds,
        )
        self._outage_continuation = float(outage_continuation_seconds)
        self._max_records, self._clock = max_records, clock
        self._max_history_records = max_history_records
        self._max_history_per_operator = max_history_records_per_operator
        # Authority history admission: one slot per eTLD+1. Pending owners
        # reserve their slot before any response can establish publication.
        self._history_owners: set[str] = set()
        self._history_groups: dict[str, int] = {}
        # A previously absent operator keeps its admission, so a hosted-tenant
        # flood cannot disable its configured legacy policy.
        self._known_absent_owners: set[str] = set()
        self._cache: OrderedDict[str, EvaluatedTrustRecord] = OrderedDict()
        self._failures: OrderedDict[str, _TrustRecordFailure] = OrderedDict()
        self._pending: dict[str, asyncio.Task[EvaluatedTrustRecord]] = {}
        self._max_fetches_per_host = max_fetches_per_host
        self._max_foreign_fetches_per_host = max_foreign_fetches_per_host
        self._responses: OrderedDict[str, tuple[float, TrustResponse]] = OrderedDict()
        self._response_pending: dict[str, asyncio.Task[tuple[float, TrustResponse]]] = {}
        self._hosts_inflight: dict[tuple[str, str], int] = {}
        self._foreign_inflight: dict[str, int] = {}
        self._host_failures: OrderedDict[tuple[str, str], float] = OrderedDict()
        # Authority history: operators that served a 200, and the last valid
        # record read from each. Never evicted.
        self._seen_200: set[str] = set()
        self._last_record: dict[str, EvaluatedTrustRecord] = {}
        self._policy_revision = 0
        self._lock = asyncio.Lock()

    @property
    def polling_interval_seconds(self) -> float:
        """The freshness cadence (the revocation polling interval)."""
        return self._interval

    @property
    def outage_continuation_seconds(self) -> float:
        """Seller policy: how long past freshness a stale record serves non-spend calls."""
        return self._outage_continuation

    def current_time(self) -> float:
        """Read the evaluator's clock for a final authority deadline check."""
        return self._clock()

    def _stale_within_bound(self, last: EvaluatedTrustRecord) -> EvaluatedTrustRecord | None:
        until = last.expires_at + self._outage_continuation
        if self._outage_continuation <= 0 or self._clock() >= until:
            return None
        return EvaluatedTrustRecord(
            last.operator_domain,
            last.source_url,
            copy.deepcopy(last.document),
            until,
            stale_on_outage=True,
            revision=last.revision,
        )

    async def cached_record(
        self, origin_or_domain: str, *, role: str | None = None
    ) -> EvaluatedTrustRecord:
        """The last record read, during an outage; never fresh spend authority.

        With ``role``, a last record that does not list the origin raises
        :class:`TrustRecordMatchError` (a known delisting survives an outage).
        Without a last record, or past the outage-continuation bound, raises
        :class:`TrustRecordUnavailableError`. No fetch is performed.
        """
        domain = urlsplit(trust_record_url(origin_or_domain)).hostname or ""
        async with self._lock:
            last = self._last_record.get(domain)
            if last is None:
                raise TrustRecordUnavailableError("no prior trust record is available")
            if role is not None:
                last.agent(origin_or_domain, role=role)
            stale = self._stale_within_bound(last)
        if stale is None:
            raise TrustRecordUnavailableError("trust record outage grace has passed")
        return stale

    async def snapshots_current(self, records: Sequence[EvaluatedTrustRecord]) -> bool:
        """Whether every captured record is still the latest one read for its owner."""
        async with self._lock:
            return all(
                (latest := self._last_record.get(record.operator_domain)) is not None
                and latest.revision == record.revision
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
        """Recheck the latest record, revision and deadline before assigning authority.

        ``origin_or_domain``/``role`` match the first record's latest agent
        entry, even when another origin refreshed that owner's record. This
        final guard never upgrades an older captured snapshot to a newer
        approval.
        """
        async with self._lock:
            if (
                legacy_unpublished_domain is not None
                and registrable_domain(legacy_unpublished_domain) in self._seen_200
            ):
                raise TrustRecordUnavailableError("trust publication superseded legacy authority")
            for index, record in enumerate(records):
                latest = self._last_record.get(record.operator_domain)
                if latest is None:
                    raise TrustRecordUnavailableError("trust record history is unavailable")
                if index == 0 and origin_or_domain is not None and role is not None:
                    latest.agent(origin_or_domain, role=role)
                if latest.revision != record.revision:
                    raise TrustRecordUnavailableError("trust record changed during evaluation")
                if self._clock() >= record.expires_at:
                    raise TrustRecordUnavailableError("trust record expired during evaluation")

    async def evaluate(
        self,
        origin_or_domain: str,
        *,
        role: str | None = None,
        allow_stale_on_outage: bool = False,
    ) -> EvaluatedTrustRecord:
        """Return the operator's record; with ``role``, it must list the origin.

        Fresh unless the record cannot be read and ``allow_stale_on_outage``
        (a non-spend caller) opts into the last record read within the outage
        bound. Whatever the caller, a last record that does not list the
        origin raises :class:`TrustRecordMatchError`: a cap, an outage or a
        404 never turns a known delisting into an outage.
        """
        try:
            record = await self._evaluate_fresh(origin_or_domain)
        except TrustRecordUnavailableError as outage:
            try:
                stale = await self.cached_record(origin_or_domain, role=role)
            except TrustRecordUnavailableError as history_error:
                if allow_stale_on_outage:
                    raise history_error from outage
                raise outage from None
            if not allow_stale_on_outage:
                raise outage from None
            return stale
        if role is not None:
            record.agent(origin_or_domain, role=role)
        return record

    async def check_listing(self, origin: str, *, role: str) -> TrustListing:
        """Whether the origin's operator lists it with ``role``, under the outage rules."""
        try:
            record = await self.evaluate(origin, role=role, allow_stale_on_outage=True)
        except TrustRecordUnavailableError as exc:
            return TrustListing("unavailable", error=exc)
        except TrustRecordError as exc:
            # Not listed, never published, or no registrable domain.
            return TrustListing("unlisted", error=exc)
        if record.stale_on_outage:
            return TrustListing("unavailable", record=record, within_grace=True)
        return TrustListing("listed", record=record)

    def _admit_history(self, domain: str) -> None:
        """Reserve an authority-history slot for ``domain`` (lock held)."""
        if domain in self._history_owners:
            return
        group = _quota_group(domain)
        if (
            len(self._history_owners) >= self._max_history_records
            or self._history_groups.get(group, 0) >= self._max_history_per_operator
        ):
            raise _TrustRecordCapacityError("trust history admission capacity exceeded")
        self._history_owners.add(domain)
        self._history_groups[group] = self._history_groups.get(group, 0) + 1

    def _release_history(self, domain: str) -> None:
        """Release a reservation that established no history (lock held)."""
        if (
            domain in self._seen_200
            or domain in self._last_record
            or domain in self._known_absent_owners
            or domain not in self._history_owners
        ):
            return
        self._history_owners.discard(domain)
        group = _quota_group(domain)
        remaining = self._history_groups.get(group, 1) - 1
        if remaining > 0:
            self._history_groups[group] = remaining
        else:
            self._history_groups.pop(group, None)

    async def _evaluate_fresh(self, origin_or_domain: str) -> EvaluatedTrustRecord:
        url = trust_record_url(origin_or_domain)
        domain = urlsplit(url).hostname or ""
        async with self._lock:
            now = self._clock()
            record = self._cache.get(domain)
            if record is None or record.expires_at <= now:
                failure = self._failures.get(domain)
                if failure is not None and failure.retry_after > now:
                    self._failures.move_to_end(domain)
                    raise failure.error_type(failure.message)
                task = self._pending.get(domain)
                if task is None:
                    if len(self._pending) >= self._max_records:
                        raise _TrustRecordCapacityError("trust discovery capacity exceeded")
                    self._admit_history(domain)
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
        # Callers cannot mutate cached authority through a returned mapping.
        return replace(record, document=copy.deepcopy(record.document))

    async def _refresh(self, domain: str, url: str, now: float) -> EvaluatedTrustRecord:
        try:
            record = await self._fetch(domain, url, now)
            async with self._lock:
                self._policy_revision += 1
                record = replace(record, revision=self._policy_revision)
                self._cache[domain] = record
                self._last_record[domain] = record
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
                if isinstance(exc, (TrustRecordUnavailableError, TrustRecordAbsentError))
                else TrustRecordUnavailableError("trust record fetch failed")
            )
            async with self._lock:
                self._failures[domain] = _TrustRecordFailure(
                    type(error), str(error), self._clock() + 60
                )
                if isinstance(error, TrustRecordAbsentError):
                    self._known_absent_owners.add(domain)
                self._failures.move_to_end(domain)
                while len(self._failures) > self._max_records:
                    self._failures.popitem(last=False)
            if error is exc:
                raise
            raise error from exc
        finally:
            async with self._lock:
                self._pending.pop(domain, None)
                self._release_history(domain)

    async def _fetch(self, domain: str, url: str, now: float) -> EvaluatedTrustRecord:
        source = url
        expires_at = now + self._interval
        for hop in range(2):
            try:
                _validate_fetch_target(source)
            except TrustRecordError as exc:
                raise TrustRecordUnavailableError("invalid trust fetch target") from exc
            try:
                response_expires, response = await self._fetch_response(source, owner_domain=domain)
                expires_at = min(expires_at, response_expires)
            except _TrustRecordCapacityError:
                raise
            except (_TrustRecordBodyError, _TrustRecordReadError) as exc:
                # Publication is established by status even if its body fails.
                # A later 404 cannot reopen authorized_operators fallback.
                self._seen_200.add(domain)
                raise TrustRecordUnavailableError("trust record body could not be read") from exc
            except TrustRecordUnavailableError:
                raise
            except Exception as exc:
                raise TrustRecordUnavailableError("trust record fetch failed") from exc
            if response.status in {404, 410} and hop == 0 and domain not in self._seen_200:
                raise TrustRecordAbsentError("no trust record is published")
            if response.status != 200:
                # Including a 404/410 after the operator published a record:
                # a discovery failure, never a removal.
                raise TrustRecordUnavailableError(
                    f"trust record unavailable (HTTP {response.status})"
                )
            self._seen_200.add(domain)
            try:
                if len(response.body) > 2 * 1024 * 1024:
                    raise TrustRecordError("trust record is oversized")
                try:
                    document = parse_strict_json(response.body)
                except StrictJsonError as exc:
                    raise TrustRecordError("trust record is not strict JSON") from exc
                if not isinstance(document, dict):
                    raise TrustRecordError("trust record must be an object")
                document = _validate_trust_record(document, operator_domain=domain, reader=True)
            except TrustRecordError as exc:
                # An invalid record is unreadable: the last record read stands.
                raise TrustRecordUnavailableError("trust record is invalid") from exc
            pointer = document.get("authoritative_location")
            if pointer is None:
                return EvaluatedTrustRecord(domain, source, document, expires_at)
            if hop == 1:
                raise TrustRecordUnavailableError(
                    "a second authoritative-location hop is forbidden"
                )
            source = pointer
        raise TrustRecordUnavailableError("trust record resolution failed")

    async def _fetch_response(self, url: str, *, owner_domain: str) -> tuple[float, TrustResponse]:
        """Share transport bytes per URL while each owner validates them separately."""
        host = urlsplit(url).hostname or ""
        owner_host = (owner_domain, host)
        foreign = registrable_domain(host) != owner_domain
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
                if len(self._response_pending) >= self._max_records:
                    raise _TrustRecordCapacityError("trust discovery capacity exceeded")
                if foreign:
                    # Hops to another operator's host share one global budget
                    # per host, never the target operator's own budget.
                    if self._foreign_inflight.get(host, 0) >= self._max_foreign_fetches_per_host:
                        raise _TrustRecordCapacityError("trust fetch host capacity exceeded")
                elif self._hosts_inflight.get(owner_host, 0) >= self._max_fetches_per_host:
                    raise _TrustRecordCapacityError("trust fetch host capacity exceeded")
                task = asyncio.create_task(self._refresh_response(url, owner_host, now, foreign))
                task.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
                self._response_pending[url] = task
                if foreign:
                    self._foreign_inflight[host] = self._foreign_inflight.get(host, 0) + 1
                else:
                    self._hosts_inflight[owner_host] = self._hosts_inflight.get(owner_host, 0) + 1
        return await asyncio.shield(task)

    async def _refresh_response(
        self, url: str, owner_host: tuple[str, str], now: float, foreign: bool
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
            raise _TrustRecordBodyError("trust record content is invalid") from exc
        finally:
            async with self._lock:
                self._response_pending.pop(url, None)
                if foreign:
                    host = owner_host[1]
                    remaining = self._foreign_inflight.get(host, 1) - 1
                    if remaining > 0:
                        self._foreign_inflight[host] = remaining
                    else:
                        self._foreign_inflight.pop(host, None)
                else:
                    remaining = self._hosts_inflight.get(owner_host, 1) - 1
                    if remaining > 0:
                        self._hosts_inflight[owner_host] = remaining
                    else:
                        self._hosts_inflight.pop(owner_host, None)

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
        """Explicitly bind a verified origin after evaluating its operator record.

        The signer must carry a current revocation state
        (:attr:`VerifiedSigner.revocation` ``== "current"``): an identity that
        serves no revocation list, or whose list is stale or unchecked, stays
        identity-established and cannot be bound. The record must currently
        list the origin with ``role``, so an unlisted origin cannot be bound
        again, whatever the onboarding mode.
        """
        if signer.profile != "web-bot-auth" or signer.agent_url is None:
            raise TrustRecordError("binding requires a verified Web Bot Auth identity")
        if signer.revocation != "current":
            raise TrustRecordError("binding requires the origin to serve a current revocation list")
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


def _registrable(value: str) -> str | None:
    """The registrable domain, or ``None`` for any value without one."""
    try:
        return registrable_domain(value)
    except ValueError:
        return None


def _scope_covers(scopes: Sequence[str], scope: str) -> bool:
    return scope in scopes or "*" in scopes or (scope.startswith("adcp:") and "adcp:all" in scopes)


def _unavailable(reason: str) -> GrantAuthorization:
    """An outage: retryable SERVICE_UNAVAILABLE, never grant_absent."""
    return GrantAuthorization(
        False, reason, error_code="SERVICE_UNAVAILABLE", include_grant_absent_details=False
    )


def _unlisted(reason: str) -> GrantAuthorization:
    """No standing for the signer: PERMISSION_DENIED with no details."""
    return GrantAuthorization(False, reason, include_grant_absent_details=False)


def _instant(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


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
    # Grantees compare as registrable domains under the pinned PSL snapshot.
    if _registrable(grant["grantee"]) != grantee or not _scope_covers(grant["scopes"], scope):
        return False
    # agents[] narrows the link that reaches the signer: the signer's
    # canonical origin must be listed, or the chain ends at this link.
    agents = grant.get("agents")
    if agents is not None and not any(canonical_origin(agent["url"]) == origin for agent in agents):
        return False
    if "countries" in grant and (not countries or not countries <= set(grant["countries"])):
        return False
    brands = grant.get("profiles", {}).get("adcp", {}).get("brands", ["*"])
    if "*" not in brands and (brand_id is None or brand_id not in brands):
        return False
    if "valid_from" in grant and now < _instant(grant["valid_from"]):
        return False
    if "valid_until" in grant and now >= _instant(grant["valid_until"]):
        return False
    return True


@dataclass(frozen=True)
class BrandHouse:
    """The house a brand's verified brand.json names, and the brand's id there.

    ``brand_id`` is the brand's child-brand id in the house's portfolio; grant
    ``profiles.adcp.brands`` are checked against it. ``None`` keeps the
    claim's own ``brand_id``.
    """

    domain: str
    brand_id: str | None = None


HouseResolver = Callable[[str, "str | None"], Awaitable["BrandHouse | str | None"]]


class TrustGrantEvaluator:
    """Evaluate demand-side grants (L2 §Grants in trust.json, DR-0025).

    * One record authorizes: the house's. ``resolve_house(brand_domain,
      brand_id)`` returns the verified :class:`BrandHouse` the brand's
      brand.json names (a house redirect, or a ``house_domain`` the house
      reciprocates), with the brand's id in the house's portfolio, or just
      the house domain. Without the hook, or when it returns ``None``, the
      brand's own domain is the house. Grants in any other record are never
      consulted.
    * Domains compare as registrable domains under the pinned PSL snapshot.
      Authority is implicit when ``account.operator``, the brand's domain and
      the signer's operator share one.
    * Otherwise a chain must reach the signer's operator with
      ``account.operator`` on it: the house grants the signer's operator,
      which is ``account.operator``; the brand is ``account.operator`` (a
      self-operated brand) and its house grants the signer's operator; or the
      house grants ``account.operator``, whose own record grants the signer's
      operator. There is no third link.
    * Every link must cover the scope, brand, countries and agent narrowing
      (a narrowed link that does not list the signer ends the chain), and the
      check time must fall inside every link's ``valid_from``/``valid_until``
      window, so effective authority is the intersection along the chain.
    * A 200 from the house's trust.json makes it the only source of grants:
      ``legacy_authorization`` (brand.json ``authorized_operators``) runs only
      while the house has never published one. A record that cannot be read,
      including a 404/410 after it was read, is never absence and never
      ``grant_absent``: a non-spend claim (``allow_stale_on_outage``) is
      evaluated on the last records read within the evaluator's outage bound,
      and anything else gets the retryable ``SERVICE_UNAVAILABLE``.
    """

    def __init__(
        self,
        records: TrustRecordEvaluator,
        *,
        clock: Callable[[], float] | None = None,
        seller_authorization: Callable[[GrantRequest], Awaitable[bool]] | None = None,
        legacy_authorization: Callable[[GrantRequest], Awaitable[bool]] | None = None,
        resolve_house: HouseResolver | None = None,
    ) -> None:
        self.records = records
        self._clock = clock if clock is not None else records._clock
        self._seller_authorization = seller_authorization
        self._legacy_authorization = legacy_authorization
        self._resolve_house = resolve_house

    def current_time(self) -> float:
        """Read the evaluation clock before using a retained claim deadline."""
        return self._clock()

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
            except TrustRecordUnavailableError:
                return _unavailable("trust_record_changed_during_evaluation")
            except TrustRecordError:
                return _unlisted("signer_not_listed")
            if any(record.expires_at <= self._clock() for record in records):
                return _unavailable("trust_record_unavailable")
            if grant_expiry <= self._clock():
                return GrantAuthorization(False, "grant_absent")
        return GrantAuthorization(
            authorized,
            legacy_fallback_allowed=legacy_fallback_allowed,
            expires_at=(
                min([record.expires_at for record in records] + [grant_expiry]) if records else None
            ),
            outage_expires_at=(
                min([self._outage_deadline(record) for record in records] + [grant_expiry])
                if authorized and records
                else None
            ),
            stale_on_outage=any(record.stale_on_outage for record in records),
        )

    def _outage_deadline(self, record: EvaluatedTrustRecord) -> float:
        if record.stale_on_outage:
            return record.expires_at
        return record.expires_at + self.records.outage_continuation_seconds

    async def _signer_listing(
        self, origin: str, *, non_spend: bool
    ) -> EvaluatedTrustRecord | GrantAuthorization:
        listing = await self.records.check_listing(origin, role="adcp:buying")
        if listing.status == "listed" or (
            listing.status == "unavailable" and listing.within_grace and non_spend
        ):
            assert listing.record is not None
            return listing.record
        if listing.status == "unlisted":
            return _unlisted("signer_not_listed")
        return _unavailable("signer_trust_record_unavailable")

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
        """Evaluate one acts-for claim.

        ``allow_stale_on_outage`` marks a claim for an operation that commits
        no spend: only such a claim continues on the last records read while
        a record on the chain cannot be read.
        """
        if signing_profile not in {"web-bot-auth", "adcp/request-signing/v1"}:
            raise ValueError("unsupported authenticated signing profile")
        non_spend = allow_stale_on_outage
        try:
            origin = canonical_origin(agent_origin)
        except ValueError:
            return _unlisted("grant_absent")
        signer_operator = _registrable(origin)
        brand_operator = _registrable(brand_domain)
        operator = _registrable(account_operator)
        if signer_operator is None or brand_operator is None or operator is None:
            return _unlisted("grant_absent")
        request = GrantRequest(
            origin, brand_domain, account_operator, scope, brand_id, tuple(countries)
        )
        signer_record: EvaluatedTrustRecord | None = None
        if signing_profile == "web-bot-auth":
            # WBA authority always requires a current operator role entry.
            listing = await self._signer_listing(origin, non_spend=non_spend)
            if isinstance(listing, GrantAuthorization):
                return listing
            signer_record = listing
            if signer_operator == brand_operator == operator:
                return await self._authorization(True, [signer_record], agent_origin=origin)
        house = brand_domain
        if self._resolve_house is not None:
            try:
                resolved = await self._resolve_house(brand_domain, brand_id)
            except Exception:
                return _unavailable("brand_house_unavailable")
            if isinstance(resolved, BrandHouse):
                house = resolved.domain
                if resolved.brand_id is not None:
                    # Grants name the house's child-brand ids.
                    brand_id = resolved.brand_id
            elif isinstance(resolved, str):
                house = resolved
        if _registrable(house) is None:
            return _unlisted("grant_absent")
        try:
            brand_record = await self.records.evaluate(house, allow_stale_on_outage=non_spend)
        except TrustRecordAbsentError:
            if self._legacy_authorization is not None:
                authorized = bool(await self._legacy_authorization(request))
                if signer_record is not None and signer_record.expires_at <= self._clock():
                    # The callback cannot outlive the required operator listing.
                    return _unavailable("signer_trust_record_unavailable")
                if signer_record is not None:
                    return await self._authorization(
                        authorized,
                        [signer_record],
                        legacy_fallback_allowed=True,
                        agent_origin=origin,
                        legacy_unpublished_domain=house,
                    )
                return await self._authorization(
                    authorized,
                    [],
                    legacy_fallback_allowed=True,
                    legacy_unpublished_domain=house,
                )
            return GrantAuthorization(False, "trust_record_absent", legacy_fallback_allowed=True)
        except TrustRecordError:
            # Including a 404/410 after the house published: an outage, never
            # grant_absent and never a fallback to authorized_operators.
            return _unavailable("trust_record_unavailable")
        if signer_record is None:
            # Legacy credentials retain their established onboarding path when
            # the house has no trust record. Once the house serves HTTP 200,
            # grants have one source and require the new operator role entry.
            listing = await self._signer_listing(origin, non_spend=non_spend)
            if isinstance(listing, GrantAuthorization):
                return listing
            signer_record = listing
            if signer_operator == brand_operator == operator:
                return await self._authorization(True, [signer_record], agent_origin=origin)
        args: dict[str, Any] = {
            "origin": origin,
            "scope": scope,
            "brand_id": brand_id,
            "countries": frozenset(countries),
            "now": self._clock(),
        }
        grants = brand_record.document.get("grants", [])
        links: list[list[Mapping[str, Any]]] = []
        used_records = [signer_record, brand_record]
        if operator in {signer_operator, brand_operator}:
            # The house grants the signer's operator, which is account.operator;
            # or a self-operated brand (it is account.operator) whose house
            # grants the signer's operator directly.
            links = [[g for g in grants if _grant_matches(g, grantee=signer_operator, **args)]]
        else:
            first = [g for g in grants if _grant_matches(g, grantee=operator, **args)]
            if first:
                try:
                    intermediary: EvaluatedTrustRecord | None = await self.records.evaluate(
                        operator, allow_stale_on_outage=non_spend
                    )
                except TrustRecordAbsentError:
                    # The intermediary publishes no record: no second link.
                    intermediary = None
                except TrustRecordError:
                    return _unavailable("trust_record_unavailable")
                second: list[Mapping[str, Any]] = []
                if intermediary is not None:
                    used_records.append(intermediary)
                    # Fetching the intermediary may outlive an earlier grant.
                    # Evaluate both links at the decision time.
                    args["now"] = self._clock()
                    first = [g for g in first if _grant_matches(g, grantee=operator, **args)]
                    second = [
                        g
                        for g in intermediary.document.get("grants", [])
                        if _grant_matches(g, grantee=signer_operator, **args)
                    ]
                links = [first, second]
        if any(record.expires_at <= self._clock() for record in used_records):
            return _unavailable("trust_record_unavailable")
        if not links or any(not link for link in links):
            exhaustive = brand_record.document.get("exclusive_grants", False) and any(
                _scope_covers(grant["scopes"], scope) for grant in grants
            )
            if not exhaustive and self._seller_authorization is not None:
                authorized = bool(await self._seller_authorization(request))
                # Seller discretion cannot outlive the records it relied on.
                if any(record.expires_at <= self._clock() for record in used_records):
                    return _unavailable("trust_record_unavailable")
                return await self._authorization(authorized, used_records, agent_origin=origin)
            return GrantAuthorization(False, "grant_absent")
        # Every surviving grant covers the requested dimensions. The best path
        # expires at the minimum over links of each link's latest valid_until.
        # Avoid materializing the cartesian product of untrusted grant arrays.
        grant_expiry = min(
            max(_instant(g["valid_until"]) if "valid_until" in g else float("inf") for g in link)
            for link in links
        )
        if grant_expiry <= self._clock():
            return GrantAuthorization(False, "grant_absent")
        if any(record.expires_at <= self._clock() for record in used_records):
            return _unavailable("trust_record_unavailable")
        return await self._authorization(
            True, used_records, grant_expiry=grant_expiry, agent_origin=origin
        )


__all__ = [
    "BrandHouse",
    "GrantRequest",
    "EvaluatedTrustRecord",
    "GrantAuthorization",
    "HouseResolver",
    "TrustFetcher",
    "TrustGrantEvaluator",
    "TrustListing",
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
