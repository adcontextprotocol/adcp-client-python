"""What ``client.accounts.ensure`` returns: a discriminated account outcome (AdCP 3.3).

Pending, ambiguous and rejected are values, not errors. Only a failure to
process the call raises (:class:`~adcp.exceptions.AccountRequestError`)::

    outcome = await client.accounts.ensure(key, billing="operator")
    match outcome:
        case AccountReady(account_id=account_id): use(account_id)
        case AccountPending(action=action) if action.waiting_on == "buyer":
            show_to_human(format_pending_action(action))
        case AccountAmbiguous(candidates=candidates):
            outcome = await outcome.choose(pick(candidates))
        case AccountRejected(errors=errors): report(errors)

Agents should switch on :func:`next_step`, not on ``PendingAction.kind``,
which is extensible. The TypeScript SDK spells these ``AccountOutcome``,
``nextStep`` and ``formatPendingAction``.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "AccountChangeOutcome",
    "AccountMatched",
    "AccountSeparate",
    "AccountUpdated",
    "AccountAmbiguous",
    "AccountOutcome",
    "AccountPending",
    "AccountReady",
    "AccountRejected",
    "NextStep",
    "PendingAction",
    "format_pending_action",
    "next_step",
]

#: The provisioning path an outcome came from.
AccountVia = Literal["sync_accounts", "request_account"]
NextStep = Literal[
    "wait_for_seller", "send_human_to_url", "pick_candidate", "contact_seller", "none"
]


@dataclass(frozen=True)
class PendingAction:
    """Whose turn it is and what is needed (the wire ``pending_action``/``setup``).

    ``url`` is where a *human* completes the action. Show it to a person. It is
    not a bearer link, and an agent must never fetch it. ``message`` is
    untrusted seller text; render it through :func:`format_pending_action`.
    ``kind`` is extensible: switch on :func:`next_step` instead.
    """

    waiting_on: str
    kind: str
    message: str
    url: str | None = None
    expires_at: str | None = None

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> PendingAction:
        url = value.get("url")
        waiting_on = value.get("waiting_on") or ("buyer" if url else "seller")
        return cls(
            waiting_on=str(waiting_on),
            kind=str(value.get("kind") or "review"),
            message=str(value.get("message") or ""),
            url=None if url is None else str(url),
            expires_at=None if value.get("expires_at") is None else str(value["expires_at"]),
        )

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "waiting_on": self.waiting_on,
            "kind": self.kind,
            "message": self.message,
        }
        if self.url is not None:
            payload["url"] = self.url
        if self.expires_at is not None:
            payload["expires_at"] = self.expires_at
        return payload


@dataclass(frozen=True)
class AccountReady:
    """The account exists: use ``account_id`` on account-scoped tasks.

    ``status`` is the account's status, which may still need buyer action
    before spend (``payment_required``, for example).

    On the ``request_account`` path (AdCP 3.3.0-beta.3, adcp#8620):

    * ``resolution`` is ``matched``, ``created`` or ``updated``. A matched or
      created account carries its own money terms (:attr:`billing`,
      :attr:`billing_entity`, :attr:`payment_terms`), which can differ from
      the terms requested: read them before spending.
    * ``effective_at`` is when changed money terms take effect on an updated
      account; delivery before it is invoiced under the previous terms.
    * ``not_carried_over`` names the account-level configurations a separate
      new account (a change resolved as ``created``) does not have.
    """

    account_id: str | None
    status: str | None
    via: AccountVia
    request_id: str | None = None
    account: Mapping[str, Any] | None = None
    kind: Literal["ready"] = "ready"
    resolution: str | None = None
    effective_at: str | None = None
    not_carried_over: tuple[str, ...] = ()

    @property
    def billing(self) -> Any:
        """The account's ``billing`` (who is invoiced), as the seller resolved it."""
        return None if self.account is None else self.account.get("billing")

    @property
    def billing_entity(self) -> Any:
        """The account's ``billing_entity``, without write-only fields."""
        return None if self.account is None else self.account.get("billing_entity")

    @property
    def payment_terms(self) -> Any:
        """The account's ``payment_terms``, as the seller resolved them."""
        return None if self.account is None else self.account.get("payment_terms")


@dataclass(frozen=True)
class AccountPending:
    """Waiting on someone: :attr:`action` says who and what.

    ``request_id`` identifies an open ``request_account`` request (``None``
    on the ``sync_accounts`` path, where ``account_id`` may already exist).
    The registry keeps it, so :meth:`~adcp.accounts.AccountRegistry.refresh`
    reports when it moves and a repeat ``ensure`` never opens a duplicate.
    """

    action: PendingAction
    via: AccountVia
    request_id: str | None = None
    account_id: str | None = None
    status: str | None = None
    kind: Literal["pending"] = "pending"


@dataclass(frozen=True)
class AccountAmbiguous:
    """Several existing accounts match; pick one with :meth:`choose`.

    ``candidates`` are the wire ``account_candidate`` objects, limited to
    accounts the caller can already reach.
    """

    candidates: tuple[Mapping[str, Any], ...]
    request_id: str
    via: AccountVia = "request_account"
    kind: Literal["ambiguous"] = "ambiguous"
    _choose: Callable[[str, str], Awaitable[AccountOutcome]] | None = field(
        default=None, repr=False, compare=False
    )

    async def choose(self, account_id: str) -> AccountOutcome:
        """Continue the request with the chosen candidate; returns the new outcome."""
        if self._choose is None:
            raise RuntimeError("this outcome is not bound to an account registry")
        if account_id not in {str(item.get("account_id")) for item in self.candidates}:
            raise ValueError(f"{account_id!r} is not one of this request's candidates")
        return await self._choose(self.request_id, account_id)


@dataclass(frozen=True)
class AccountRejected:
    """The seller declined, or a buyer action expired. ``errors`` are wire errors."""

    errors: tuple[Mapping[str, Any], ...]
    via: AccountVia
    request_id: str | None = None
    kind: Literal["rejected"] = "rejected"

    @property
    def requires_new_account(self) -> bool:
        """``ACCOUNT_CHANGE_REQUIRES_NEW_ACCOUNT``: the seller would accept the change only
        as a separate account; resend with ``allow_separate_account=True``."""
        return any(
            str(item.get("code")) == "ACCOUNT_CHANGE_REQUIRES_NEW_ACCOUNT" for item in self.errors
        )


AccountOutcome = AccountReady | AccountPending | AccountAmbiguous | AccountRejected


@dataclass(frozen=True)
class AccountUpdated:
    """``client.accounts.change``: the named account changed in place.

    ``account_id`` is unchanged and ``revision`` is the account's new revision.
    ``effective_at`` is when changed money terms (``billing``,
    ``billing_entity``, ``payment_terms``) take effect: delivery before it is
    invoiced under the previous terms (AdCP 3.3.0-beta.3).
    """

    account_id: str
    revision: int | None = None
    effective_at: str | None = None
    account: Mapping[str, Any] | None = None
    request_id: str | None = None
    via: AccountVia = "request_account"
    kind: Literal["updated"] = "updated"


@dataclass(frozen=True)
class AccountSeparate:
    """``client.accounts.change``: the seller opened a separate account with the change.

    Nothing moved: ``previous_account_id`` is unchanged, and the registry
    still maps the natural ``key`` to it until you call
    ``client.accounts.use(key, account_id)``. ``not_carried_over`` names the
    account-level configurations the new account does not have, and
    ``subscriptions_not_covering`` the ``account_ids``-scoped reporting
    subscriptions that will not see it.
    """

    account_id: str
    previous_account_id: str
    key: Mapping[str, Any] | None = None
    account: Mapping[str, Any] | None = None
    not_carried_over: tuple[str, ...] = ()
    subscriptions_not_covering: tuple[str, ...] = ()
    request_id: str | None = None
    via: AccountVia = "request_account"
    kind: Literal["separate_account"] = "separate_account"


@dataclass(frozen=True)
class AccountMatched:
    """``client.accounts.change``: the change matched another existing account.

    As with :class:`AccountSeparate`, the registry is not re-pointed until
    ``client.accounts.use(key, account_id)``. Read the matched account's money
    terms from :attr:`account` before spending.
    """

    account_id: str
    previous_account_id: str
    key: Mapping[str, Any] | None = None
    account: Mapping[str, Any] | None = None
    request_id: str | None = None
    via: AccountVia = "request_account"
    kind: Literal["matched_existing"] = "matched_existing"


AccountChangeOutcome = (
    AccountUpdated | AccountSeparate | AccountMatched | AccountPending | AccountRejected
)


def next_step(outcome: AccountOutcome | AccountChangeOutcome) -> NextStep:
    """What to do next, independent of the extensible ``kind`` vocabulary.

    * ``none``: the account is ready.
    * ``wait_for_seller``: the seller is reviewing; call ``refresh()`` later
      or wait for the ``account_request.changed`` webhook.
    * ``send_human_to_url``: show ``action.url`` to a person (never fetch it).
    * ``pick_candidate``: choose one of ``candidates``.
    * ``contact_seller``: rejected, or a buyer action without a URL.
    """
    if isinstance(outcome, (AccountReady, AccountUpdated, AccountSeparate, AccountMatched)):
        return "none"
    if isinstance(outcome, AccountAmbiguous):
        return "pick_candidate"
    if isinstance(outcome, AccountRejected):
        return "contact_seller"
    if outcome.action.waiting_on == "buyer":
        return "send_human_to_url" if outcome.action.url else "contact_seller"
    return "wait_for_seller"


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")


def _untrusted(text: str, *, limit: int = 1000) -> str:
    cleaned = " ".join(_CONTROL.sub("", text).split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return cleaned.replace('"', "'")


def format_pending_action(action: PendingAction) -> str:
    """Safe text for a CLI or a chat: who is waiting, what is needed, and where.

    The seller's ``message`` is untrusted: control and bidirectional
    characters are removed, whitespace is collapsed, it is quoted and capped
    at 1000 characters. The URL is shown for a person to open, never followed.
    """
    who = "you" if action.waiting_on == "buyer" else "the seller"
    lines = [f"Waiting on {who}: {_untrusted(action.kind, limit=64)}."]
    if action.message:
        lines.append(f'Seller says: "{_untrusted(action.message)}"')
    if action.url:
        lines.append(f"A person completes it at {_untrusted(action.url, limit=2048)}")
    if action.expires_at:
        lines.append(f"Expires {_untrusted(action.expires_at, limit=64)}.")
    return "\n".join(lines)


def _errors(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))
