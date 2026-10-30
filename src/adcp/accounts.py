"""Buyer-side provisioning registry, scoped to a seller and buyer identity."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from pydantic import TypeAdapter
from typing_extensions import TypeForm

from adcp.account_identity import account_key, account_key_payload
from adcp.account_outcomes import (
    AccountAmbiguous,
    AccountOutcome,
    AccountPending,
    AccountReady,
    AccountRejected,
    AccountVia,
    PendingAction,
    _errors,
)
from adcp.exceptions import (
    AccountNotFoundError,
    AccountPaymentRequiredError,
    AccountRequestError,
    AccountSetupRequiredError,
)
from adcp.types import AccountReference, SyncAccountsRequest

if TYPE_CHECKING:
    from pydantic import BaseModel

    from adcp.client import ADCPClient

AccountPolicy = Literal["off", "auto", "strict"]
"""Natural-key account policy for client task calls.

``off`` sends account references unchanged, as 8.0 did, and is the 8.x default.
``auto`` and ``strict`` opt in to registry-aware preflight; ``auto`` becomes
the default in the next major release.
"""

ACCOUNT_POLICIES: frozenset[str] = frozenset({"off", "auto", "strict"})

_PUBLIC_DISCOVERY_TASKS = frozenset(
    {
        "get_products",
        "list_products",
        "get_signals",
        "request_proposals",
        "refine_proposals",
        "decline_proposals",
    }
)


@dataclass(frozen=True)
class AccountRecord:
    """What the registry knows about one natural key at one seller.

    ``request_id``, ``request_state`` and ``pending_action`` track an
    ``account.requests`` request (or a ``sync_accounts`` account pending
    approval), so a repeat ``ensure`` returns its current state and never
    opens a duplicate.
    """

    account_id: str | None
    status: str | None
    wire_key: dict[str, Any] | None = None
    wire_version: str | None = None
    request_id: str | None = None
    request_state: str | None = None
    pending_action: dict[str, Any] | None = None


class AccountStorage(Protocol):
    """Persist records under seller URI and complete natural key.

    An adapter MUST be private to one authenticated buyer identity. Sharing
    it across credentials requires a separate namespace for each buyer.
    Persist the optional ``AccountRecord.wire_key`` and ``wire_version``
    fields to retain account continuity across seller upgrades and restarts.
    """

    async def load(self, seller: str, key: str) -> AccountRecord | None: ...

    async def save(self, seller: str, key: str, record: AccountRecord) -> None: ...

    async def delete(self, seller: str, key: str) -> None: ...


class ListableAccountStorage(AccountStorage, Protocol):
    """Optional: storage that can enumerate a seller's records.

    ``AccountRegistry.pending()`` and ``refresh()`` use it to find open
    requests across restarts; without it they see the keys this process
    touched.
    """

    async def items(self, seller: str) -> list[tuple[str, AccountRecord]]: ...


class InMemoryAccountStorage:
    """Default storage; pass a durable adapter to survive process restarts."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], AccountRecord] = {}

    async def load(self, seller: str, key: str) -> AccountRecord | None:
        return self._records.get((seller, key))

    async def save(self, seller: str, key: str, record: AccountRecord) -> None:
        self._records[seller, key] = record

    async def delete(self, seller: str, key: str) -> None:
        self._records.pop((seller, key), None)

    async def items(self, seller: str) -> list[tuple[str, AccountRecord]]:
        return [(key, record) for (owner, key), record in self._records.items() if owner == seller]


def _payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return cast(dict[str, Any], value.model_dump(mode="json", exclude_none=True))
    return dict(value) if isinstance(value, Mapping) else {}


def _row_reference(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("account"):
        return cast(dict[str, Any], row["account"])
    # Read models may include both the seller handle and the natural identity.
    if row.get("brand") and row.get("operator"):
        return {name: value for name, value in row.items() if name != "account_id"}
    return row


def _sync_key(
    row: dict[str, Any], inputs: list[dict[str, Any]], *, buyer_selected_timezone: bool
) -> dict[str, Any] | None:
    """Match echoed identity, never response ordering, to a complete input key.

    Older sellers may omit optional dimensions in their results. Accept those
    results only when their supplied identity selects exactly one input key.
    """
    ref = _row_reference(row)
    try:
        echoed = account_key_payload(ref)
    except ValueError:
        return None
    if not row.get("account") and not buyer_selected_timezone:
        echoed.pop("timezone", None)
    matches: dict[str, dict[str, Any]] = {}
    for entry in inputs:
        try:
            key = account_key_payload(entry.get("account") or entry)
        except ValueError:
            continue
        if "account_id" in echoed:
            matched = key == echoed
        else:
            matched = all(
                (
                    all(key.get("brand", {}).get(part) == item for part, item in value.items())
                    if name == "brand" and isinstance(value, dict)
                    else key.get(name) == value
                )
                for name, value in echoed.items()
                if name in ref or name in {"brand", "operator"}
            )
        if matched:
            matches[account_key(key)] = key
    return next(iter(matches.values())) if len(matches) == 1 else None


_OPEN_REQUEST_STATES = frozenset({"in_review", "ambiguous"})
_SELLER_REVIEW = {"waiting_on": "seller", "kind": "review", "message": "The seller is reviewing"}


def _requests_supported(caps: Any) -> bool:
    """Whether the seller advertises ``account.requests`` (request_account)."""
    requests = getattr(getattr(caps, "account", None), "requests", None)
    if requests is None:
        return False
    supported = getattr(requests, "supported", None)
    if supported is None and hasattr(requests, "root"):
        supported = getattr(requests.root, "supported", None)
    if supported is None and isinstance(requests, Mapping):
        supported = requests.get("supported")
    return supported is True


def _key_payload(key: str) -> dict[str, Any]:
    import json

    return cast(dict[str, Any], json.loads(key))


def _request_outcome(
    wire: Mapping[str, Any], *, choose: Callable[[str, str], Awaitable[AccountOutcome]]
) -> AccountOutcome:
    """The outcome of one wire ``account_request`` record."""
    state = str(wire["state"])
    request_id = str(wire["request_id"])
    if state == "resolved":
        account = wire.get("account") if isinstance(wire.get("account"), Mapping) else None
        return AccountReady(
            account_id=str(wire["account_id"]),
            status=str((account or {}).get("status") or "active"),
            via="request_account",
            request_id=request_id,
            account=account,
        )
    if state == "in_review":
        return AccountPending(
            action=PendingAction.from_wire(wire.get("pending_action") or _SELLER_REVIEW),
            via="request_account",
            request_id=request_id,
        )
    if state == "ambiguous":
        return AccountAmbiguous(
            candidates=tuple(dict(item) for item in wire.get("candidates") or ()),
            request_id=request_id,
            _choose=choose,
        )
    return AccountRejected(
        errors=_errors(wire.get("errors"))
        or ({"code": "ACCOUNT_REQUEST_REJECTED", "message": "The seller declined the request"},),
        via="request_account",
        request_id=request_id,
    )


def has_natural_reference(request: Any) -> bool:
    """Return True when ``request`` carries a buyer-declared natural account key."""
    if "account" not in getattr(type(request), "model_fields", {}):
        return False
    ref = getattr(request, "account", None)
    return ref is not None and not _payload(ref).get("account_id")


class AccountRegistry:
    """Track provisioned keys for one seller. Exposed as ``client.accounts``."""

    def __init__(self, client: ADCPClient, storage: AccountStorage | None = None) -> None:
        self._client = client
        self._storage = storage if storage is not None else InMemoryAccountStorage()
        self._seller = client.agent_config.agent_uri
        self._ensure_lock = asyncio.Lock()
        #: Keys this process recorded, for storage that cannot enumerate.
        self._touched: set[str] = set()

    async def get(self, key: AccountReference | Mapping[str, Any]) -> AccountRecord | None:
        return await self._storage.load(self._seller, account_key(key))

    async def forget(self, key: AccountReference | Mapping[str, Any]) -> None:
        """Invalidate a stale record, e.g. after a seller reports a missing account."""
        await self._storage.delete(self._seller, account_key(key))

    async def preserve_wire_keys(
        self, operation: str, params: dict[str, Any], *, version: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Keep previously reduced account addresses stable across seller upgrades.

        Full application keys remain the storage address. A seller upgrading its
        schemas does not reconcile an existing account's identity, so subsequent
        requests must retain the natural key used to provision it. Diagnostics
        contain paths and versions only, never account identifiers.
        """
        from adcp._version import (
            is_adcp_version_at_least,
            normalize_to_release_precision,
            resolve_adcp_version_alias,
        )

        result = deepcopy(params)
        pending: list[dict[str, Any]] = []
        references: list[tuple[dict[str, Any], str]] = []
        if isinstance(result.get("account"), dict):
            references.append((result["account"], "/account"))
        if operation == "sync_accounts":
            references.extend(
                (entry, f"/accounts/{index}")
                for index, entry in enumerate(result.get("accounts") or [])
                if isinstance(entry, dict)
            )
        for reference, pointer in references:
            if reference.get("account_id"):
                continue
            try:
                record = await self.get(reference)
            except ValueError:
                continue
            if record is None or record.wire_key is None:
                continue
            # Only identity dimensions are replaced. Keep provisioning options
            # and mutable display metadata supplied by this request.
            for name in ("operator", "operator_unit", "currency", "timezone", "sandbox"):
                if name in record.wire_key:
                    reference[name] = deepcopy(record.wire_key[name])
                else:
                    reference.pop(name, None)
            brand = reference.get("brand", {})
            for name in ("domain", "brand_id", "countries"):
                if name in record.wire_key["brand"]:
                    brand[name] = deepcopy(record.wire_key["brand"][name])
                else:
                    brand.pop(name, None)
            reference["brand"] = brand
            normalized = resolve_adcp_version_alias(normalize_to_release_precision(version))
            if is_adcp_version_at_least(normalized, "3.2"):
                pending.append(
                    {
                        "pointer": pointer,
                        "provisioned_wire_version": record.wire_version,
                        "wire_version": version,
                    }
                )
        return result, pending

    async def ensure(
        self,
        key: AccountReference | Mapping[str, Any],
        *,
        billing: str | None = None,
        payment_terms: str | None = None,
        billing_entity: Any = None,
        currency: str | None = None,
        timezone: str | None = None,
    ) -> AccountOutcome:
        """Get an account for a natural key at this seller; returns an :data:`AccountOutcome`.

        Routes by capability: ``request_account`` when the seller advertises
        ``account.requests``, otherwise ``sync_accounts`` (which needs
        ``billing``). Either way the outcome is one of
        :class:`~adcp.account_outcomes.AccountReady`,
        :class:`~adcp.account_outcomes.AccountPending`,
        :class:`~adcp.account_outcomes.AccountAmbiguous` or
        :class:`~adcp.account_outcomes.AccountRejected`; pending, ambiguous and
        rejected are values, not errors. Only a failure to process the call
        raises :class:`~adcp.exceptions.AccountRequestError`.

        Idempotent per (seller, buyer identity, natural key): a repeat call
        returns the account, or the open request's current state, and never
        opens a duplicate. ``currency`` and ``timezone`` join the key when it
        does not carry them. Billing terms are sent only when an account is
        first requested; change them later with ``request_account`` or
        ``sync_accounts``.
        """
        raw = _payload(TypeAdapter(AccountReference).validate_python(key))
        for name, value in (("currency", currency), ("timezone", timezone)):
            if value is not None:
                raw.setdefault(name, value)
        ref = account_key_payload(TypeAdapter(AccountReference).validate_python(raw))
        if "account_id" in ref:
            raise ValueError("ensure requires a buyer-declared natural key")
        terms: dict[str, Any] = {}
        if billing is not None:
            terms["billing"] = billing
        if payment_terms is not None:
            terms["payment_terms"] = payment_terms
        if billing_entity is not None:
            terms["billing_entity"] = _payload(billing_entity)
        async with self._ensure_lock:
            caps = await self._client.fetch_capabilities()
            requests = _requests_supported(caps)
            existing = await self.get(ref)
            if existing is not None:
                outcome = await self._known_outcome(ref, existing, requests=requests)
                if outcome is not None:
                    return outcome
            if requests:
                return await self._request_account(ref, {**ref, **terms})
            if getattr(caps.account, "require_operator_auth", False) is True:
                self._raise_setup("sync_accounts", "Discover an account_id through list_accounts")
            if billing is None:
                raise ValueError(
                    "this seller provisions through sync_accounts, which needs billing=; "
                    "pass the invoiced party"
                )
            return await self._sync_account(ref, {**ref, **terms})

    async def _known_outcome(
        self, ref: dict[str, Any], record: AccountRecord, *, requests: bool
    ) -> AccountOutcome | None:
        """The outcome a stored record already answers, or ``None`` to request again."""
        if record.request_id and record.request_state in _OPEN_REQUEST_STATES and requests:
            refreshed = await self._read_requests([record.request_id])
            wire = refreshed.get(record.request_id)
            if wire is not None:
                return await self._record_request(ref, wire)
            # The seller no longer knows the request (expired or dropped).
            return None
        if record.request_state == "rejected" and record.account_id is None:
            return None
        if record.account_id is None and record.status is None:
            return None
        via: AccountVia = "request_account" if record.request_id else "sync_accounts"
        if record.status == "pending_approval":
            return AccountPending(
                action=PendingAction.from_wire(record.pending_action or _SELLER_REVIEW),
                via=via,
                request_id=record.request_id,
                account_id=record.account_id,
                status=record.status,
            )
        return AccountReady(
            account_id=record.account_id,
            status=record.status,
            via=via,
            request_id=record.request_id,
        )

    def _failure(self, operation: str, result: Any, body: Mapping[str, Any]) -> AccountRequestError:
        errors = body.get("errors") or [
            result.adcp_error
            or {"code": "SERVICE_UNAVAILABLE", "message": result.error or f"{operation} failed"}
        ]
        return AccountRequestError(operation, list(errors), agent_id=self._client.agent_config.id)

    async def _request_account(self, ref: dict[str, Any], entry: dict[str, Any]) -> AccountOutcome:
        from adcp.types import RequestAccountRequest

        result = await self._client.request_account(
            RequestAccountRequest.model_validate({"idempotency_key": str(uuid.uuid4()), **entry})
        )
        body = _payload(result.data)
        request = body.get("request")
        if not result.success or body.get("adcp_error") or not isinstance(request, Mapping):
            raise self._failure("request_account", result, body)
        return await self._record_request(ref, request)

    async def _choose(
        self, ref: dict[str, Any], request_id: str, account_id: str
    ) -> AccountOutcome:
        from adcp.types import RequestAccountRequest

        async with self._ensure_lock:
            result = await self._client.request_account(
                RequestAccountRequest.model_validate(
                    {
                        "idempotency_key": str(uuid.uuid4()),
                        "request_id": request_id,
                        "account_id": account_id,
                    }
                )
            )
            body = _payload(result.data)
            request = body.get("request")
            if not result.success or body.get("adcp_error") or not isinstance(request, Mapping):
                raise self._failure("request_account", result, body)
            return await self._record_request(ref, request)

    async def _record_request(self, ref: dict[str, Any], wire: Mapping[str, Any]) -> AccountOutcome:
        """Persist a request's state under its natural key and return its outcome."""
        outcome = _request_outcome(
            wire, choose=lambda request_id, account_id: self._choose(ref, request_id, account_id)
        )
        previous = await self.get(ref)
        ready = outcome if isinstance(outcome, AccountReady) else None
        await self._storage.save(
            self._seller,
            account_key(ref),
            AccountRecord(
                account_id=None if ready is None else ready.account_id,
                status=None if ready is None else ready.status,
                wire_key=previous.wire_key if previous is not None else None,
                wire_version=previous.wire_version if previous is not None else None,
                request_id=str(wire["request_id"]),
                request_state=str(wire["state"]),
                pending_action=(
                    outcome.action.to_wire() if isinstance(outcome, AccountPending) else None
                ),
            ),
        )
        self._touched.add(account_key(ref))
        return outcome

    async def _sync_account(self, ref: dict[str, Any], entry: dict[str, Any]) -> AccountOutcome:
        result = await self._client.sync_accounts(
            SyncAccountsRequest.model_validate(
                {"idempotency_key": str(uuid.uuid4()), "accounts": [entry]}
            )
        )
        body = _payload(result.data)
        if not result.success or body.get("errors"):
            raise self._failure("sync_accounts", result, body)
        rows = body.get("accounts", [])
        if not rows:
            raise self._failure("sync_accounts", result, body)
        row = rows[0]
        if row.get("action") == "failed" or row.get("status") == "rejected":
            return AccountRejected(
                errors=_errors(row.get("errors"))
                or ({"code": "ACCOUNT_REJECTED", "message": "The seller declined the account"},),
                via="sync_accounts",
            )
        record = await self.get(ref)
        if record is None:
            self._raise_setup("sync_accounts", "Seller did not confirm account provisioning")
        assert record is not None
        self._touched.add(account_key(ref))
        if record.status == "pending_approval":
            action = PendingAction.from_wire(row.get("setup") or _SELLER_REVIEW)
            await self._storage.save(
                self._seller,
                account_key(ref),
                replace(record, pending_action=action.to_wire()),
            )
            return AccountPending(
                action=action,
                via="sync_accounts",
                account_id=record.account_id,
                status=record.status,
            )
        return AccountReady(account_id=record.account_id, status=record.status, via="sync_accounts")

    async def _read_requests(self, request_ids: list[str]) -> dict[str, Mapping[str, Any]]:
        """Read the caller's own requests back through ``list_accounts``."""
        from adcp.types import ListAccountsRequest

        found: dict[str, Mapping[str, Any]] = {}
        cursor: str | None = None
        for _page in range(100):
            payload: dict[str, Any] = {"account_request_ids": request_ids}
            if cursor:
                payload["requests_pagination"] = {"cursor": cursor}
            result = await self._client.list_accounts(ListAccountsRequest.model_validate(payload))
            body = _payload(result.data)
            if not result.success or body.get("errors"):
                raise self._failure("list_accounts", result, body)
            await self.observe("list_accounts", payload, result, fetch_capabilities=False)
            for item in body.get("account_requests") or ():
                if isinstance(item, Mapping) and item.get("request_id"):
                    found[str(item["request_id"])] = item
            pagination = body.get("account_requests_pagination") or {}
            cursor = pagination.get("cursor") if pagination.get("has_more") else None
            if not cursor:
                break
        return found

    async def _known(self) -> list[tuple[str, AccountRecord]]:
        items = getattr(self._storage, "items", None)
        if callable(items):
            return list(await items(self._seller))
        known = []
        for key in sorted(self._touched):
            record = await self._storage.load(self._seller, key)
            if record is not None:
                known.append((key, record))
        return known

    async def pending(self) -> list[AccountOutcome]:
        """The outcomes still waiting: open requests and accounts pending approval.

        Read from storage; no network call. Use :meth:`refresh` to learn what moved.
        """
        result: list[AccountOutcome] = []
        for _key, record in await self._known():
            if record.request_state == "ambiguous":
                continue  # its candidates live with the seller; refresh() reads them
            if record.request_state == "in_review" or record.status == "pending_approval":
                result.append(
                    AccountPending(
                        action=PendingAction.from_wire(record.pending_action or _SELLER_REVIEW),
                        via="request_account" if record.request_id else "sync_accounts",
                        request_id=record.request_id,
                        account_id=record.account_id,
                        status=record.status,
                    )
                )
        return result

    async def refresh(self) -> list[AccountOutcome]:
        """Re-read open requests and pending accounts; return the outcomes whose state moved.

        Call it from a cron job or an ``account_request.changed`` webhook. One
        ``list_accounts`` read covers every open request (``account_request_ids``)
        and the first page of accounts.
        """
        known = await self._known()
        open_requests = {
            str(record.request_id): (key, record)
            for key, record in known
            if record.request_id and record.request_state in _OPEN_REQUEST_STATES
        }
        pending_accounts = {
            key: record
            for key, record in known
            if not record.request_id and record.status == "pending_approval"
        }
        if not open_requests and not pending_accounts:
            return []
        moved: list[AccountOutcome] = []
        if open_requests:
            wire = await self._read_requests(sorted(open_requests))
        else:
            from adcp.types import ListAccountsRequest

            listing = await self._client.list_accounts(ListAccountsRequest())
            await self.observe("list_accounts", {}, listing, fetch_capabilities=False)
            wire = {}
        for request_id, (key, record) in open_requests.items():
            current = wire.get(request_id)
            if current is None:
                continue
            ref = _key_payload(key)
            if str(current["state"]) == record.request_state and (
                current.get("pending_action") or None
            ) == (record.pending_action or None):
                continue
            moved.append(await self._record_request(ref, current))
        for key, record in pending_accounts.items():
            latest = await self._storage.load(self._seller, key)
            if latest is not None and latest.status != record.status:
                outcome = await self._known_outcome(_key_payload(key), latest, requests=False)
                if outcome is not None:
                    moved.append(outcome)
        return moved

    async def observe(
        self,
        operation: str,
        request: Any,
        result: Any,
        *,
        fetch_capabilities: bool = True,
    ) -> None:
        """Update from successful sync/list results; never learn from a dry run.

        With ``fetch_capabilities=False`` only cached capabilities are used,
        so observation never adds a network request; rows whose key depends
        on uncached capabilities are skipped rather than guessed.
        """
        if operation not in {"sync_accounts", "list_accounts"} or not result.success:
            return
        req = _payload(request)
        body = _payload(result.data)
        if req.get("dry_run") or body.get("dry_run"):
            return
        inputs = req.get("accounts", [])
        for row in body.get("accounts", []):
            if operation == "sync_accounts":
                caps = self._client.capabilities
                if (
                    caps is None
                    and fetch_capabilities
                    and row.get("timezone")
                    and row.get("brand")
                    and row.get("operator")
                ):
                    caps = await self._client.fetch_capabilities()
                timezone_caps = getattr(getattr(caps, "account", None), "timezone", None)
                key = _sync_key(
                    row,
                    inputs,
                    buyer_selected_timezone=getattr(timezone_caps, "account_selection", None)
                    == "buyer_selected",
                )
                if key is None:
                    continue
            else:
                try:
                    key = account_key_payload(_row_reference(row))
                except ValueError:
                    continue
            if row.get("action") == "failed":
                if any(
                    error.get("code") == "ACCOUNT_NOT_FOUND" for error in row.get("errors") or []
                ):
                    await self.forget(key)
                continue
            if operation == "list_accounts" and "account_id" not in key:
                caps = (
                    await self._client.fetch_capabilities()
                    if fetch_capabilities
                    else self._client.capabilities
                )
                if caps is None:
                    continue
                timezone_caps = getattr(getattr(caps, "account", None), "timezone", None)
                if getattr(timezone_caps, "account_selection", None) != "buyer_selected":
                    key.pop("timezone", None)
            if row.get("action") is None and operation == "sync_accounts":
                continue
            previous = await self.get(key)
            wire_key = previous.wire_key if previous is not None else None
            wire_version = previous.wire_version if previous is not None else None
            report = (getattr(result, "metadata", None) or {}).get("adcp_version_conversion", {})
            observed_version = report.get("wire_version")
            if (
                operation == "sync_accounts"
                and wire_key is None
                and "account_id" not in key
                and isinstance(observed_version, str)
                and observed_version in {"3.0", "3.1"}
            ):
                from adcp._version import resolve_adcp_version
                from adcp.compat.versions import convert_payload

                reduced = convert_payload(
                    "sync_accounts",
                    {"accounts": [key]},
                    direction="request",
                    # The outgoing request was reduced from the SDK's own
                    # release (3.3-beta.1), not from 3.2.
                    source_version=resolve_adcp_version(None),
                    target_version=observed_version,
                    # Recomputes the key the outgoing request already reduced;
                    # that conversion warned about these losses.
                    loss_log_level=logging.DEBUG,
                ).payload["accounts"][0]
                if account_key(reduced) != account_key(key):
                    wire_key = account_key_payload(reduced)
                    wire_version = observed_version
            status = row.get("status")
            await self._storage.save(
                self._seller,
                account_key(key),
                AccountRecord(
                    account_id=row.get("account_id"),
                    status=status,
                    wire_key=wire_key,
                    wire_version=wire_version,
                    request_id=previous.request_id if previous is not None else None,
                    request_state=previous.request_state if previous is not None else None,
                    pending_action=(
                        (row.get("setup") or previous.pending_action)
                        if previous is not None and status == "pending_approval"
                        else row.get("setup")
                        if status == "pending_approval"
                        else None
                    ),
                ),
            )

    async def prepare(self, operation: str, request: BaseModel, policy: AccountPolicy) -> BaseModel:
        """Apply account policy to a copy, preserving the caller's request.

        ``off`` returns the request unchanged with no capability fetch.
        """
        if policy not in ACCOUNT_POLICIES:
            raise ValueError("account_policy must be 'off', 'auto', or 'strict'")
        if policy == "off":
            return request
        if operation == "list_accounts" or "account" not in type(request).model_fields:
            return request
        ref = getattr(request, "account", None)
        wire = _payload(ref)
        # Explicit IDs are already seller-assigned handles and may be supplied
        # out of band. Never infer that a returned handle replaces a natural key.
        if wire.get("account_id"):
            return request
        caps = self._client.capabilities
        if ref is not None:
            caps = await self._client.fetch_capabilities()
        account_caps = getattr(caps, "account", None)
        required = type(request).model_fields["account"].is_required() or (
            operation in {"get_products", "list_products"}
            and getattr(account_caps, "required_for_products", False) is True
        )
        if ref is None:
            if required:
                self._raise_setup(
                    operation, "An account is required; provision it before this call"
                )
            return request
        requires_operator_auth = getattr(account_caps, "require_operator_auth", False) is True
        if requires_operator_auth and (
            required or policy == "strict" or operation not in _PUBLIC_DISCOVERY_TASKS
        ):
            self._raise_setup(operation, "This seller requires an account_id from list_accounts")
        record = None if requires_operator_auth else await self.get(wire)
        if (
            record is not None
            and operation in {"get_products", "list_products"}
            and record.status not in {"active", "payment_required"}
        ):
            if required or policy == "strict":
                self._raise_setup(operation, "Account setup must allow product discovery")
            # Inactive accounts can still browse the public feed where allowed.
            record = None
        if record is None:
            if required or policy == "strict" or operation not in _PUBLIC_DISCOVERY_TASKS:
                raise AccountNotFoundError(
                    operation,
                    [
                        {
                            "code": "ACCOUNT_NOT_FOUND",
                            "message": "Provision this key with client.accounts.ensure first",
                        }
                    ],
                    agent_id=self._client.agent_config.id,
                )
            updates: dict[str, Any] = {"account": None}
            if "brand" in type(request).model_fields and getattr(request, "brand", None) is None:
                brand = wire.get("brand")
                if operation == "list_products":
                    brand = account_key_payload(wire)["brand"]
                updates["brand"] = TypeAdapter(
                    cast(TypeForm[Any], type(request).model_fields["brand"].annotation)
                ).validate_python(brand)
            # Account-scoped version tokens cannot be reused for a public read.
            for name in ("if_wholesale_feed_version", "if_feed_version", "if_pricing_version"):
                if name in type(request).model_fields:
                    updates[name] = None
            return request.model_copy(update=updates)
        if operation in {"create_media_buy", "buy_products", "accept_proposal", "activate_signal"}:
            if record.status == "payment_required":
                raise AccountPaymentRequiredError(
                    operation,
                    [{"code": "ACCOUNT_PAYMENT_REQUIRED", "message": "Account needs payment"}],
                    agent_id=self._client.agent_config.id,
                )
            if record.status != "active":
                self._raise_setup(operation, "Account must be active before committing spend")
        return request

    def _raise_setup(self, operation: str, message: str) -> None:
        raise AccountSetupRequiredError(
            operation,
            [{"code": "ACCOUNT_SETUP_REQUIRED", "message": message}],
            agent_id=self._client.agent_config.id,
        )
