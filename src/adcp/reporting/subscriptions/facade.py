"""The buyer's reporting-subscription write facade: ``client.reporting``.

``sync_principal`` treats ``reporting_subscriptions`` and
``reporting_destinations`` as complete desired state, so a raw write that
"adds one feed" deactivates every other subscription, and one that omits a
destination revokes it for every account. This facade is patch-shaped:

- :meth:`ReportingClient.subscribe` replaces one subscription and keeps every
  other entry as the seller stores it. It chooses ``subscription_version``,
  checks the request before sending it, writes under
  ``expected_configuration_version`` and verifies the readback.
- :meth:`ReportingClient.unsubscribe` is the only deactivation path.
- :meth:`ReportingClient.replace_subscriptions` is the expert complete-set
  write; deactivating what it omits needs ``deactivate_omitted=True``.

:func:`guard_sync_principal` is the guard ``ADCPClient.sync_principal``
applies to raw writes that carry either section.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from adcp.reporting.subscriptions.consumer import (
    PrincipalClient,
    ReportingSubscriptionError,
    ReportingSubscriptionReadback,
    _dump,
)
from adcp.reporting.subscriptions.registry import _semantic
from adcp.types import GetPrincipalRequest, SyncPrincipalRequest

SubscribeAction = Literal["created", "updated", "unchanged", "reactivated", "deactivated"]

#: Conflict retries with a fresh key, and the bound on the roster read.
_DEFAULT_ATTEMPTS = 3
_ROSTER_PAGES = 10


@dataclass(frozen=True)
class SubscribeResult:
    """The outcome of a facade write for one subscription."""

    action: SubscribeAction
    #: The seller's readback for this subscription.
    state: Any
    #: The version the SDK chose (or kept).
    subscription_version: int
    #: The readback of the destination the subscription names, if any.
    destination: Any | None
    readback: ReportingSubscriptionReadback


@dataclass(frozen=True)
class PreviewResult:
    """What :meth:`ReportingClient.subscribe` would do, validated by the seller (dry run)."""

    would_be: SubscribeAction
    subscription_version: int
    #: Offerings the selector resolves to among the seller's advertised ones.
    offering_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Principal:
    """The caller's current principal: what a write replaces."""

    kind: str
    configuration_version: str | None
    principal_kind: str | None
    subscriptions: tuple[Any, ...] = ()
    destinations: tuple[Any, ...] = ()

    def subscription(self, subscription_id: str) -> Any | None:
        return next(
            (s for s in self.subscriptions if _value(s, "subscription_id") == subscription_id),
            None,
        )

    def submitted_subscriptions(self) -> list[dict[str, Any]]:
        """Every subscription as submitted (its current version's configuration)."""
        return [_dump(_value(item, "configuration")) for item in self.subscriptions]

    def submitted_destinations(self) -> list[dict[str, Any]]:
        return [_dump(_value(item, "configuration")) for item in self.destinations]


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _text(value: Any) -> str:
    return str(getattr(value, "value", value) if value is not None else "")


def _active_subscription(state: Any) -> bool:
    return _text(_value(state, "state")) != "inactive"


def _active_destination(configuration: Mapping[str, Any]) -> bool:
    return configuration.get("active", True) is not False


def _refused(message: str, code: str, **kwargs: Any) -> ReportingSubscriptionError:
    return ReportingSubscriptionError(message, code=code, **kwargs)


def _invalid(message: str) -> ReportingSubscriptionError:
    """A request the SDK refuses before sending; the fix ends the message."""
    return ReportingSubscriptionError(message, code="INVALID_REQUEST", dispatched=False)


async def read_principal(client: PrincipalClient) -> _Principal:
    result = await client.get_principal(GetPrincipalRequest.model_validate({}))
    response = getattr(result, "data", None)
    if not getattr(result, "success", False) or response is None:
        raise ReportingSubscriptionError(
            f"get_principal failed: {getattr(result, 'error', None) or 'no response'}. "
            "Retry once the seller is reachable."
        )
    outcome = response.result
    kind = _text(getattr(outcome, "kind", ""))
    if kind == "failed":
        errors = list(getattr(outcome, "errors", None) or [])
        first = errors[0] if errors else None
        raise ReportingSubscriptionError(
            f"get_principal was rejected: {getattr(first, 'message', 'unknown error')}",
            code=None if first is None else (_text(getattr(first, "code", "")) or None),
        )
    configuration = getattr(outcome, "configuration", None)
    return _Principal(
        kind=kind,
        configuration_version=getattr(outcome, "configuration_version", None),
        principal_kind=_text(getattr(outcome, "principal_kind", None)) or None,
        subscriptions=tuple(getattr(configuration, "reporting_subscriptions", None) or ()),
        destinations=tuple(getattr(configuration, "reporting_destinations", None) or ()),
    )


def _deactivated_by(principal: _Principal, subscriptions: Sequence[Mapping[str, Any]]) -> list[str]:
    """Active subscriptions a complete-set write omits (explicit ``active: false`` is allowed)."""
    sent = {item.get("subscription_id") for item in subscriptions}
    return [
        str(_value(state, "subscription_id"))
        for state in principal.subscriptions
        if _active_subscription(state) and _value(state, "subscription_id") not in sent
    ]


def _revoked_by(principal: _Principal, destinations: Sequence[Mapping[str, Any]]) -> list[str]:
    """Active destinations a complete-set write omits."""
    sent = {item.get("destination_id") for item in destinations}
    return [
        str(item.get("destination_id"))
        for item in principal.submitted_destinations()
        if _active_destination(item) and item.get("destination_id") not in sent
    ]


def check_complete_set(
    principal: _Principal,
    configuration: Mapping[str, Any],
    *,
    allow_deactivation: bool,
    allow_revocation: bool,
) -> None:
    """Refuse, before sending, a complete-set write that deactivates or revokes by omission."""
    if "reporting_subscriptions" in configuration and not allow_deactivation:
        omitted = _deactivated_by(principal, configuration["reporting_subscriptions"] or [])
        if omitted:
            raise _refused(
                "This write omits active subscriptions "
                f"{', '.join(repr(i) for i in omitted)}, which deactivates them. "
                "Use client.reporting.subscribe() or unsubscribe(), or pass "
                "allow_deactivation=True.",
                "SUBSCRIPTION_WOULD_DEACTIVATE",
                would_deactivate=omitted,
            )
    if "reporting_destinations" in configuration and not allow_revocation:
        revoked = _revoked_by(principal, configuration["reporting_destinations"] or [])
        if revoked:
            raise _refused(
                f"This write omits destinations {', '.join(repr(i) for i in revoked)}. "
                "Omitting a destination revokes it for every account. Pass "
                "allow_revocation=True.",
                "DESTINATION_WOULD_REVOKE",
                would_revoke=revoked,
            )


async def guard_sync_principal(
    client: PrincipalClient,
    request: Any,
    *,
    allow_deactivation: bool = False,
    allow_revocation: bool = False,
) -> None:
    """The raw ``sync_principal`` guard: one read, only for writes that touch either section."""
    configuration = _dump(_value(request, "configuration") or {})
    touched = {"reporting_subscriptions", "reporting_destinations"} & set(configuration)
    if not touched or _value(request, "dry_run") is True:
        return
    if (allow_deactivation or "reporting_subscriptions" not in touched) and (
        allow_revocation or "reporting_destinations" not in touched
    ):
        return
    check_complete_set(
        await read_principal(client),
        configuration,
        allow_deactivation=allow_deactivation,
        allow_revocation=allow_revocation,
    )


#: Subscription arrays that are ``uniqueItems`` sets: a seller may return them
#: in any order, such as sorted, so the readback compares them as sets.
_SET_ARRAYS = frozenset({"account_ids", "exclude_account_ids", "operator_domains", "dimensions"})
#: Arrays of objects whose order carries no meaning: ``destination_overrides``
#: select disjoint accounts, so each sent entry matches any stored one.
_UNORDERED_ARRAYS = frozenset({"destination_overrides"})


def differing_path(sent: Any, stored: Any, path: str = "") -> str | None:
    """The first JSON Pointer at which ``stored`` lacks or changes a value the buyer sent.

    Fields the seller adds (defaults it fills) are ignored. Set-valued arrays
    (``account_ids``, ``exclude_account_ids``, ``operator_domains``,
    ``dimensions``) and ``destination_overrides`` compare without regard to
    order; every other array must match element for element.
    """
    if isinstance(sent, list):
        if not isinstance(stored, list) or len(stored) != len(sent):
            return path or "/"
        key = path.rsplit("/", 1)[-1]
        if key in _SET_ARRAYS and all(isinstance(item, (str, int)) for item in sent):
            if sorted(map(str, sent)) != sorted(map(str, stored)):
                return path or "/"
            return None
        if key in _UNORDERED_ARRAYS:
            unmatched = list(stored)
            for index, item in enumerate(sent):
                match = next(
                    (i for i, other in enumerate(unmatched) if differing_path(item, other) is None),
                    None,
                )
                if match is None:
                    return f"{path}/{index}"
                unmatched.pop(match)
            return None
        for index, item in enumerate(sent):
            found = differing_path(item, stored[index], f"{path}/{index}")
            if found is not None:
                return found
        return None
    if isinstance(sent, dict):
        if not isinstance(stored, dict):
            return path or "/"
        for key, value in sent.items():
            if value is None:
                continue
            found = differing_path(value, stored.get(key), f"{path}/{key}")
            if found is not None:
                return found
        return None
    return None if sent == stored else (path or "/")


class ReportingClient:
    """``client.reporting``: patch-shaped reporting-subscription writes.

    ``client`` needs ``get_principal`` and ``sync_principal``. With
    ``list_accounts`` and ``fetch_capabilities`` (an ``ADCPClient``), the
    subscription-id clash, operator and selector checks also run before
    anything is sent.
    """

    def __init__(
        self,
        client: Any,
        *,
        max_attempts: int = _DEFAULT_ATTEMPTS,
        idempotency_key: Callable[[], str] | None = None,
    ) -> None:
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be an integer in 1..10")
        self._client = client
        self._attempts = max_attempts
        self._key = idempotency_key or (lambda: f"rsub-{uuid4()}")

    # -- public API ---------------------------------------------------------------

    async def list_subscriptions(self) -> ReportingSubscriptionReadback:
        """The caller's subscriptions as the seller reports them (``get_principal``)."""
        principal = await read_principal(self._client)
        return ReportingSubscriptionReadback(
            configuration_version=principal.configuration_version,
            subscriptions=principal.subscriptions,
        )

    async def subscribe(
        self,
        subscription: Mapping[str, Any] | Any,
        *,
        register_destination: Mapping[str, Any] | Any | None = None,
        acknowledge_cross_operator: bool = False,
    ) -> SubscribeResult:
        """Create or update one subscription, keeping every other entry unchanged.

        ``subscription_version`` is optional: the SDK keeps it when only
        ``active``, ``replace_account_configs`` or the membership
        (``scope.account_ids``, ``exclude_account_ids``) change, which apply
        in place (adcp#8619), and bumps it when the selector, the scope kind,
        the destination or its overrides, ``reconciliation_mode`` or
        ``operator_domains`` change. A supplied
        version must be greater than the current one when the content
        changed. ``register_destination`` upserts a destination in the same
        atomic write.
        """
        wanted = _dump(subscription)
        destination = None if register_destination is None else _dump(register_destination)

        async def build(principal: _Principal) -> _Plan:
            return await self._plan_subscribe(
                principal,
                wanted,
                destination,
                acknowledge_cross_operator=acknowledge_cross_operator,
            )

        return await self._write(build)

    async def unsubscribe(self, subscription_id: str) -> SubscribeResult:
        """Deactivate one subscription (``active: false``); its obligations are retained."""

        async def build(principal: _Principal) -> _Plan:
            state = principal.subscription(subscription_id)
            if state is None:
                raise _invalid(
                    f"No subscription {subscription_id!r} exists for this principal. "
                    "Check list_subscriptions() for the ids."
                )
            current = _dump(_value(state, "configuration"))
            if not _active_subscription(state):
                return _Plan.unchanged(principal, current)
            return self._plan_replace_one(principal, {**current, "active": False}, "deactivated")

        return await self._write(build)

    async def preview(
        self,
        subscription: Mapping[str, Any] | Any,
        *,
        register_destination: Mapping[str, Any] | Any | None = None,
        acknowledge_cross_operator: bool = False,
    ) -> PreviewResult:
        """Validate what :meth:`subscribe` would write, as a seller dry run."""
        principal = await read_principal(self._client)
        plan = await self._plan_subscribe(
            principal,
            _dump(subscription),
            None if register_destination is None else _dump(register_destination),
            acknowledge_cross_operator=acknowledge_cross_operator,
        )
        if plan.configuration is not None:
            await self._send(principal, plan.configuration, key=self._key(), dry_run=True)
        return PreviewResult(plan.action, plan.version, plan.offering_ids)

    async def replace_subscriptions(
        self,
        subscriptions: Sequence[Mapping[str, Any] | Any],
        *,
        deactivate_omitted: bool = False,
        deactivate_all: bool = False,
        reporting_destinations: Sequence[Mapping[str, Any] | Any] | None = None,
        allow_revocation: bool = False,
    ) -> ReportingSubscriptionReadback:
        """Replace the complete subscription set (expert).

        Omitting an active subscription deactivates it, which needs
        ``deactivate_omitted=True``; an empty list needs ``deactivate_all=True``.
        Versions are taken as given.
        """
        sent = [_dump(item) for item in subscriptions]
        if not sent and not deactivate_all:
            raise _invalid(
                "An empty subscription list deactivates every subscription. "
                "Pass deactivate_all=True."
            )
        destinations = (
            None if reporting_destinations is None else [_dump(i) for i in reporting_destinations]
        )

        async def build(principal: _Principal) -> _Plan:
            configuration: dict[str, Any] = {"reporting_subscriptions": sent}
            if destinations is not None:
                configuration["reporting_destinations"] = destinations
            check_complete_set(
                principal,
                configuration,
                allow_deactivation=deactivate_omitted or deactivate_all,
                allow_revocation=allow_revocation,
            )
            return _Plan(configuration, "updated", 0, None, sent)

        plan_result = await self._write(build, single=False)
        return plan_result.readback

    # -- planning -------------------------------------------------------------------

    async def _plan_subscribe(
        self,
        principal: _Principal,
        wanted: dict[str, Any],
        destination: dict[str, Any] | None,
        *,
        acknowledge_cross_operator: bool,
    ) -> _Plan:
        subscription_id = wanted.get("subscription_id")
        if not isinstance(subscription_id, str) or not subscription_id:
            raise _invalid("subscribe() needs a subscription_id. Name the subscription.")
        wanted.setdefault("active", True)
        state = principal.subscription(subscription_id)
        current = None if state is None else _dump(_value(state, "configuration"))
        version, action = self._choose_version(wanted, current, state)
        wanted["subscription_version"] = version
        destinations = principal.submitted_destinations()
        destinations_changed = False
        if destination is not None:
            merged = [
                d
                for d in destinations
                if d.get("destination_id") != destination.get("destination_id")
            ] + [destination]
            if merged != destinations and destination not in destinations:
                destinations, destinations_changed = merged, True
        self._check_scope(wanted)
        named = self._check_destination(wanted, destinations)
        offering_ids = await self._check_seller(
            wanted, named, acknowledge_cross_operator=acknowledge_cross_operator
        )
        if action == "unchanged" and not destinations_changed:
            plan = _Plan.unchanged(principal, wanted)
            plan.offering_ids = offering_ids
            return plan
        plan = self._plan_replace_one(principal, wanted, action)
        if destinations_changed:
            assert plan.configuration is not None
            plan.configuration["reporting_destinations"] = destinations
        plan.offering_ids = offering_ids
        return plan

    @staticmethod
    def _choose_version(
        wanted: dict[str, Any], current: dict[str, Any] | None, state: Any | None
    ) -> tuple[int, SubscribeAction]:
        supplied = wanted.get("subscription_version")
        if supplied is not None and (type(supplied) is not int or supplied < 1):
            raise _invalid(
                "subscription_version must be a positive integer. Omit it to auto-version."
            )
        if current is None:
            return supplied or 1, "created"
        version = int(
            _value(state, "subscription_version") or current.get("subscription_version") or 1
        )
        reactivating = not _active_subscription(state) and wanted.get("active") is not False
        if _semantic({**current, "subscription_id": wanted["subscription_id"]}) != _semantic(
            wanted
        ):
            if supplied is not None and supplied <= version:
                raise _invalid(
                    f"Subscription {wanted['subscription_id']!r} changes its content, so its "
                    f"version must exceed {version}. Omit subscription_version to auto-version."
                )
            return supplied or version + 1, "reactivated" if reactivating else "updated"
        if supplied is not None and supplied != version:
            raise _invalid(
                f"Subscription {wanted['subscription_id']!r} is unchanged at version {version}. "
                "Omit subscription_version, or change the content."
            )
        if reactivating:
            return version, "reactivated"
        if (wanted.get("active") is False) != (not _active_subscription(state)) or wanted.get(
            "replace_account_configs"
        ) != current.get("replace_account_configs"):
            return version, "updated"
        return version, "unchanged"

    @staticmethod
    def _plan_replace_one(
        principal: _Principal, wanted: dict[str, Any], action: SubscribeAction
    ) -> _Plan:
        subscriptions = [
            wanted if item.get("subscription_id") == wanted["subscription_id"] else item
            for item in principal.submitted_subscriptions()
        ]
        if not any(item is wanted for item in subscriptions):
            subscriptions.append(wanted)
        return _Plan(
            {"reporting_subscriptions": subscriptions},
            action,
            int(wanted["subscription_version"]),
            wanted["subscription_id"],
            [wanted],
        )

    @staticmethod
    def _check_scope(wanted: Mapping[str, Any]) -> None:
        scope = wanted.get("scope") or {}
        if scope.get("all_authorized_accounts") is not True and not scope.get("account_ids"):
            raise _invalid(
                "The subscription scope must be explicit. Pass "
                '{"all_authorized_accounts": True} or {"account_ids": [...]}.'
            )

    @staticmethod
    def _check_destination(
        wanted: Mapping[str, Any], destinations: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any] | None:
        target = wanted.get("destination")
        if not target:
            return None
        destination_id = target.get("destination_id")
        named = next((d for d in destinations if d.get("destination_id") == destination_id), None)
        if named is None or not _active_destination(named):
            raise _invalid(
                f"Destination {destination_id!r} is not registered and active. Pass it as "
                "register_destination=..."
            )
        scope = wanted.get("scope") or {}
        several = (
            scope.get("all_authorized_accounts") is True or len(scope.get("account_ids") or ()) > 1
        )
        template = target.get("path_template")
        if several and isinstance(template, str) and "{account_id}" not in template:
            raise _invalid(
                "This subscription can cover several accounts, so its path_template must "
                "contain {account_id}. Add it to the template."
            )
        return dict(named)

    async def _check_seller(
        self,
        wanted: Mapping[str, Any],
        destination: Mapping[str, Any] | None,
        *,
        acknowledge_cross_operator: bool,
    ) -> tuple[str, ...]:
        """Checks that need the seller's roster or capabilities; skipped when unavailable."""
        accounts = await self._roster()
        if accounts is not None:
            subscription_id = wanted["subscription_id"]
            for account in accounts:
                configs = _value(account, "reporting_delivery_configs") or ()
                if any(
                    _value(_value(c, "configuration"), "delivery_config_id", None)
                    == subscription_id
                    or _value(c, "delivery_config_id") == subscription_id
                    for c in configs
                ):
                    raise _refused(
                        f"subscription_id {subscription_id!r} is already a delivery_config_id "
                        f"on account {_value(account, 'account_id')!r}. Choose a "
                        "subscription_id that no account-level delivery config uses.",
                        "SUBSCRIPTION_ID_CLASH",
                        delivery_config_id=subscription_id,
                        account_id=_text(_value(account, "account_id")),
                    )
        scope = wanted.get("scope") or {}
        # error-codes.json: an all_authorized_accounts subscription that names
        # a destination and no operator_domains follows every account the
        # principal is granted, including another operator's, into it.
        if (
            scope.get("all_authorized_accounts") is True
            and destination is not None
            and not wanted.get("operator_domains")
            and not acknowledge_cross_operator
        ):
            raise _refused(
                "An all-accounts subscription follows every grant, including accounts "
                "another operator grants later, into one destination. Add "
                "operator_domains, or pass acknowledge_cross_operator=True.",
                "SUBSCRIPTION_OPERATOR_SCOPE_REQUIRED",
            )
        return await self._resolve_selector(wanted, destination)

    async def _roster(self) -> list[Any] | None:
        list_accounts = getattr(self._client, "list_accounts", None)
        if list_accounts is None:
            return None
        from adcp.types import ListAccountsRequest

        accounts: list[Any] = []
        cursor: str | None = None
        try:
            for _ in range(_ROSTER_PAGES):
                payload: dict[str, Any] = {}
                if cursor:
                    payload["pagination"] = {"cursor": cursor}
                result = await list_accounts(ListAccountsRequest.model_validate(payload))
                data = getattr(result, "data", None)
                if not getattr(result, "success", False) or data is None:
                    return None
                accounts.extend(getattr(data, "accounts", None) or ())
                pagination = getattr(data, "pagination", None)
                cursor = getattr(pagination, "cursor", None)
                if not getattr(pagination, "has_more", False) or not cursor:
                    return accounts
        except Exception:  # noqa: BLE001 - the roster checks are best effort
            return None
        return None  # a partial roster is never used

    async def _resolve_selector(
        self, wanted: Mapping[str, Any], destination: Mapping[str, Any] | None
    ) -> tuple[str, ...]:
        fetch = getattr(self._client, "fetch_capabilities", None)
        if fetch is None:
            return ()
        try:
            capabilities = await fetch()
            dumped = capabilities.model_dump(mode="json", exclude_none=True)
        except Exception:  # noqa: BLE001 - the selector check is best effort
            return ()
        raw = (dumped.get("media_buy") or {}).get("reporting_delivery") or {}
        from adcp.reporting.subscriptions.resolution import (
            SubscriptionOffering,
            resolve_subscription_offerings,
        )

        offerings = []
        for item in raw.get("offerings") or ():
            try:
                offerings.append(SubscriptionOffering.from_offering(item))
            except (TypeError, ValueError):
                continue
        if not offerings:
            return ()
        try:
            resolution = resolve_subscription_offerings(
                wanted,
                offerings,
                product_offering_ids=[o.offering_id for o in offerings],
                destination=destination,
            )
        except (TypeError, ValueError):
            return ()
        if resolution.offering_ids:
            return resolution.offering_ids
        selector = wanted.get("selector") or {}
        failing = sorted(
            ((o.offering_id, _mismatches(o, selector, wanted, destination)) for o in offerings),
            key=lambda item: (len(item[1]), item[0].encode()),
        )
        fewest = len(failing[0][1]) if failing else 0
        nearest = tuple(item for item in failing if len(item[1]) == fewest)[:5]
        listed = ", ".join(f"{offering_id} ({', '.join(fails)})" for offering_id, fails in nearest)
        raise _refused(
            f"No advertised offering matches this selector. Nearest: {listed}. Change the "
            "selector; the nearest offerings and the attributes each one failed are listed.",
            "SUBSCRIPTION_SELECTOR_UNMATCHED",
            nearest=nearest,
        )

    # -- the guarded write ---------------------------------------------------------------

    async def _write(
        self, build: Callable[[_Principal], Awaitable[_Plan]], *, single: bool = True
    ) -> SubscribeResult:
        principal = await read_principal(self._client)
        for attempt in range(1, self._attempts + 1):
            plan = await build(principal)
            if plan.configuration is None:
                return plan.result(principal, self._readback(principal))
            key = self._key()
            outcome = await self._send(principal, plan.configuration, key=key)
            if outcome is not None:
                readback = ReportingSubscriptionReadback(
                    configuration_version=getattr(outcome, "configuration_version", None),
                    subscriptions=tuple(
                        getattr(
                            getattr(outcome, "configuration", None), "reporting_subscriptions", None
                        )
                        or ()
                    ),
                )
                destinations = tuple(
                    getattr(getattr(outcome, "configuration", None), "reporting_destinations", None)
                    or ()
                )
                self._verify(plan, readback)
                return plan.result(
                    _Principal(
                        "current",
                        readback.configuration_version,
                        principal.principal_kind,
                        readback.subscriptions,
                        destinations,
                    ),
                    readback,
                )
            if attempt < self._attempts:
                principal = await read_principal(self._client)
        raise _refused(
            f"The principal configuration changed during all {self._attempts} attempts; "
            "nothing was applied by the last one. Another writer is updating this "
            "principal; retry later.",
            "SUBSCRIPTION_CONFLICT_EXHAUSTED",
        )

    async def _send(
        self,
        principal: _Principal,
        configuration: Mapping[str, Any],
        *,
        key: str,
        dry_run: bool = False,
    ) -> Any | None:
        """One attempt; ``None`` on CONFLICT. A transport failure retries the same key once."""
        payload: dict[str, Any] = {"idempotency_key": key, "configuration": dict(configuration)}
        if principal.kind == "current" and principal.configuration_version is not None:
            payload["expected_configuration_version"] = principal.configuration_version
        if principal.principal_kind is not None and principal.kind in {"current", "recognized"}:
            payload["expected_principal_kind"] = principal.principal_kind
        if dry_run:
            payload["dry_run"] = True
        request = SyncPrincipalRequest.model_validate(payload)
        sync = (
            getattr(self._client, "_sync_principal_unguarded", None) or self._client.sync_principal
        )
        try:
            result = await sync(request)
        except (OSError, TimeoutError):
            result = await sync(request)  # same key and body: the seller replays
        response = getattr(result, "data", None)
        if not getattr(result, "success", False) or response is None:
            raise ReportingSubscriptionError(
                f"sync_principal failed: {getattr(result, 'error', None) or 'no response'}. "
                "Retry the same call; the SDK reuses the key only within one attempt."
            )
        outcome = response.result
        kind = _text(getattr(outcome, "kind", ""))
        if kind == "failed":
            errors = list(getattr(outcome, "errors", None) or [])
            if any(_text(getattr(e, "code", "")) == "CONFLICT" for e in errors):
                return None
            first = errors[0] if errors else None
            recovery = getattr(first, "recovery", None)
            raise ReportingSubscriptionError(
                f"sync_principal was rejected: {getattr(first, 'message', 'unknown error')}",
                code=None if first is None else (_text(getattr(first, "code", "")) or None),
                recovery=_text(recovery) or None,  # type: ignore[arg-type]
            )
        return outcome

    @staticmethod
    def _readback(principal: _Principal) -> ReportingSubscriptionReadback:
        return ReportingSubscriptionReadback(
            configuration_version=principal.configuration_version,
            subscriptions=principal.subscriptions,
        )

    @staticmethod
    def _verify(plan: _Plan, readback: ReportingSubscriptionReadback) -> None:
        """Every submitted subscription reads back at its version, state and content."""
        for sent in plan.verify:
            state = readback.get(sent["subscription_id"])

            def fail(detail: str) -> ReportingSubscriptionError:
                return _refused(
                    f"The seller's readback for subscription {sent['subscription_id']!r} "
                    f"{detail}. The seller altered a submitted field; contact the seller.",
                    "SUBSCRIPTION_READBACK_MISMATCH",
                    readback=readback,
                )

            if state is None or state.subscription_version != sent.get("subscription_version"):
                raise fail(f"does not carry version {sent.get('subscription_version')}")
            if (sent.get("active") is False) != (_text(state.state) == "inactive"):
                raise fail(f"does not reflect active: {sent.get('active')}")
            submitted = {k: v for k, v in sent.items() if k != "active"}
            stored = {k: v for k, v in _dump(state.configuration).items() if k != "active"}
            found = differing_path(submitted, stored)
            if found is not None:
                raise fail(f"stores a different configuration at {found}")


@dataclass
class _Plan:
    configuration: dict[str, Any] | None
    action: SubscribeAction
    version: int
    subscription_id: str | None
    verify: list[dict[str, Any]] = field(default_factory=list)
    offering_ids: tuple[str, ...] = ()

    @classmethod
    def unchanged(cls, principal: _Principal, wanted: Mapping[str, Any]) -> _Plan:
        return cls(
            None,
            "unchanged",
            int(wanted.get("subscription_version") or 1),
            str(wanted.get("subscription_id")),
        )

    def result(
        self, principal: _Principal, readback: ReportingSubscriptionReadback
    ) -> SubscribeResult:
        state = (
            None if self.subscription_id is None else principal.subscription(self.subscription_id)
        )
        destination = None
        target = None if state is None else _value(_value(state, "configuration"), "destination")
        if target is not None:
            destination_id = _value(target, "destination_id")
            destination = next(
                (
                    d
                    for d in principal.destinations
                    if _value(d, "destination_id") == destination_id
                ),
                None,
            )
        return SubscribeResult(self.action, state, self.version, destination, readback)


def _mismatches(
    offering: Any,
    selector: Mapping[str, Any],
    wanted: Mapping[str, Any],
    destination: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Every selector attribute an offering fails, for SUBSCRIPTION_SELECTOR_UNMATCHED."""
    failed: list[str] = []
    if offering.feed_purpose != selector.get("feed_purpose"):
        failed.append("feed_purpose")
    if offering.period_duration != selector.get("period_duration"):
        failed.append("period_duration")
    if offering.reconciliation_mode != wanted.get("reconciliation_mode", "delivery_only"):
        failed.append("reconciliation_mode")
    if selector.get("required_finality") not in offering.supported_finality:
        failed.append("required_finality")
    if selector.get("alignment") is not None and offering.alignment != selector.get("alignment"):
        failed.append("alignment")
    pattern = None if destination is None else destination.get("pattern")
    if offering.method_pattern != pattern or (
        destination is not None and offering.method_transport != destination.get("transport")
    ):
        failed.append("destination method")
    wanted_dimensions = set(selector.get("dimensions") or ())
    if not wanted_dimensions <= set(offering.dimensions):
        failed.append("dimensions")
    return tuple(failed)


__all__ = [
    "PreviewResult",
    "ReportingClient",
    "SubscribeResult",
    "guard_sync_principal",
]
