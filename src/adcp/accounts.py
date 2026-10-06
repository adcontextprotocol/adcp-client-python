"""Buyer-side provisioning registry, scoped to a seller and buyer identity."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from pydantic import TypeAdapter

from adcp.account_identity import account_key, account_key_payload
from adcp.exceptions import (
    AccountNotFoundError,
    AccountPaymentRequiredError,
    AccountSetupRequiredError,
    classify_task_error,
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
    """A successful provisioning result; pending setup still means provisioned."""

    account_id: str | None
    status: str | None


class AccountStorage(Protocol):
    """Persist records under seller URI and complete natural key.

    An adapter MUST be private to one authenticated buyer identity. Sharing
    it across credentials requires a separate namespace for each buyer.
    """

    async def load(self, seller: str, key: str) -> AccountRecord | None: ...

    async def save(self, seller: str, key: str, record: AccountRecord) -> None: ...

    async def delete(self, seller: str, key: str) -> None: ...


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
                key.get(name) == value
                for name, value in echoed.items()
                if name in ref or name in {"brand", "operator"}
            )
        if matched:
            matches[account_key(key)] = key
    return next(iter(matches.values())) if len(matches) == 1 else None


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

    async def get(self, key: AccountReference | Mapping[str, Any]) -> AccountRecord | None:
        return await self._storage.load(self._seller, account_key(key))

    async def forget(self, key: AccountReference | Mapping[str, Any]) -> None:
        """Invalidate a stale record, e.g. after a seller reports a missing account."""
        await self._storage.delete(self._seller, account_key(key))

    async def ensure(
        self,
        key: AccountReference | Mapping[str, Any],
        *,
        billing: str,
        payment_terms: str | None = None,
        billing_entity: Any = None,
    ) -> AccountRecord:
        """Provision once through sync_accounts, retaining the ID and status.

        Billing and payment terms are supplied only on first provisioning;
        use sync_accounts explicitly for subsequent settings changes.
        """
        ref = account_key_payload(TypeAdapter(AccountReference).validate_python(key))
        if "account_id" in ref:
            raise ValueError("ensure requires a buyer-declared natural key")
        async with self._ensure_lock:
            existing = await self.get(ref)
            if existing is not None:
                return existing
            caps = await self._client.fetch_capabilities()
            if getattr(caps.account, "require_operator_auth", False) is True:
                self._raise_setup("sync_accounts", "Discover an account_id through list_accounts")
            entry = {**ref, "billing": billing}
            if payment_terms is not None:
                entry["payment_terms"] = payment_terms
            if billing_entity is not None:
                entry["billing_entity"] = _payload(billing_entity)
            result = await self._client.sync_accounts(
                SyncAccountsRequest.model_validate(
                    {
                        "idempotency_key": str(uuid.uuid4()),
                        "accounts": [entry],
                    }
                )
            )
            body = _payload(result.data)
            if not result.success or body.get("errors"):
                raise classify_task_error(
                    "sync_accounts",
                    body.get("errors")
                    or [
                        result.adcp_error
                        or {
                            "code": "ACCOUNT_NOT_FOUND",
                            "message": result.error or "Provisioning failed",
                        }
                    ],
                    agent_id=self._client.agent_config.id,
                )
            rows = body.get("accounts", [])
            if not rows or rows[0].get("action") == "failed":
                raise classify_task_error(
                    "sync_accounts",
                    (rows[0].get("errors") if rows else None)
                    or [{"code": "ACCOUNT_NOT_FOUND", "message": "Provisioning failed"}],
                    agent_id=self._client.agent_config.id,
                )
            record = await self.get(ref)
            if record is None:
                self._raise_setup("sync_accounts", "Seller did not confirm account provisioning")
            assert record is not None
            return record

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
            await self._storage.save(
                self._seller,
                account_key(key),
                AccountRecord(account_id=row.get("account_id"), status=row.get("status")),
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
                    type(request).model_fields["brand"].annotation
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
