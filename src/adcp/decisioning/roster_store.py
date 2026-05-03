"""Roster-backed :class:`AccountStore` factory for ``resolution='explicit'``
publisher-curated platforms.

Use when the publisher curates accounts out-of-band (admin UI, config
file, publisher-managed DB row) and buyers pass ``account_id`` on every
request. The adopter holds a fixed allowlist of accounts; the SDK
provides the :class:`AccountStore` plumbing.

Pairs with the existing reference adapters. Pick by asking *who creates
the account?*:

* **Buyer self-onboards via** ``sync_accounts`` — multi-tenant adopter
  store (custom :class:`AccountStore` impl). Framework owns persistence;
  the buyer's first request to a tenant-scoped tool resolves from a
  prior sync. (LinkedIn, some retail-media operators.)
* **Upstream OAuth API owns the roster** — :class:`ExplicitAccounts`
  with a loader that calls the upstream ``/me/adaccounts``. Returns
  whatever the OAuth bearer is authorized for. (Snap, Meta, TikTok.)
* **Publisher ops curates the roster out-of-band** —
  :func:`create_roster_account_store` (this module). Adopter keeps the
  persistence layer; the SDK provides the AccountStore. (Most SSPs,
  broadcasters, and retail-media networks where AE/CSM provisions the
  account in an internal admin tool before the buyer ever calls.)

Design notes
------------

* **Roster IS the allowlist.** Auth-based filtering happens upstream of
  this layer — the framework's account-resolution gate enforces
  principal-vs-account scope. The store does not consult ``ctx`` to
  filter ``list``.
* **Immutable post-construction.** The input dict is copied into an
  internal :class:`MappingProxyType` so external mutation of the
  caller's dict cannot widen the allowlist after the fact. Adopters
  who need a dynamic roster wrap :func:`create_roster_account_store`
  in their own factory and rebuild on change.
* **Write paths fail closed per-entry.** :meth:`upsert` and
  :meth:`sync_governance` return ``PERMISSION_DENIED`` for every input
  entry rather than silently no-oping (which would lie to the buyer
  about their write succeeding) or operation-level raising (which
  would fail the whole batch on a single bad entry). Per-entry
  rejection matches the wire shape and lets the buyer correlate the
  failure to their request entry.
* **Resolve returns None on miss.** ``{brand, operator}``-shaped refs
  and ref-less calls return ``None`` — publisher-curated platforms
  expect explicit ids. Adopters who need a synth tenant for
  ``list_creative_formats`` / ``provide_performance_feedback`` /
  ``preview_creative``, or natural-key resolution, wrap ``resolve``.

Example::

    from adcp.decisioning import Account, create_roster_account_store

    store = create_roster_account_store(
        roster={
            "acct_alpha": Account(id="acct_alpha", name="Alpha", status="active"),
            "acct_beta":  Account(id="acct_beta",  name="Beta",  status="active"),
        },
    )
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Generic, Literal

from typing_extensions import TypeVar

from adcp.decisioning.helpers import ref_account_id
from adcp.decisioning.types import (
    Account,
    SyncAccountsResultRow,
    SyncGovernanceEntry,
    SyncGovernanceResultRow,
)

if TYPE_CHECKING:
    from adcp.decisioning.accounts import ResolveContext
    from adcp.types import AccountReference

__all__ = ["create_roster_account_store"]

#: Per-platform metadata generic. Defaults to ``dict[str, Any]`` for
#: adopters who don't define a typed metadata shape.
TMeta = TypeVar("TMeta", default=dict[str, Any])

_DENIED_MESSAGE = (
    "roster-backed account store does not support upsert; "
    "the publisher curates accounts out-of-band"
)
_DENIED_MESSAGE_GOVERNANCE = (
    "roster-backed account store does not support sync_governance; "
    "the publisher curates accounts out-of-band"
)


class _RosterAccountStore(Generic[TMeta]):
    """``AccountStore`` implementation backed by an immutable in-memory
    roster.

    Constructed via :func:`create_roster_account_store`. The class is
    not part of the public API; adopters reference the factory.
    """

    resolution: Literal["explicit"] = "explicit"

    def __init__(self, roster: Mapping[str, Account[TMeta]]) -> None:
        # Copy into a plain dict, then wrap in MappingProxyType so the
        # store's view is decoupled from the caller's input. Two layers
        # of protection: external mutation of the input dict can't
        # reach our copy, and adopter code that gets a reference to
        # ``self._roster`` can't mutate it either.
        copied: dict[str, Account[TMeta]] = dict(roster)
        for key, account in copied.items():
            if account.id != key:
                raise ValueError(
                    f"roster key {key!r} does not match Account.id "
                    f"{account.id!r}; every roster value's id must match "
                    f"its key"
                )
        self._roster: Mapping[str, Account[TMeta]] = MappingProxyType(copied)

    async def resolve(
        self,
        ref: AccountReference | None,
        ctx: ResolveContext | None = None,
    ) -> Account[TMeta] | None:
        """Resolve a wire reference to a roster :class:`Account`.

        ``account_id``-arm refs hit a dict lookup; misses, natural-key
        refs, and ref-less calls return ``None``. The framework
        projects ``None`` to ``ACCOUNT_NOT_FOUND`` on the wire.

        ``ctx`` is accepted for Protocol parity but unused — the
        roster is the allowlist, no auth-based filtering at this layer.
        """
        del ctx  # roster is the allowlist; no per-principal filtering
        account_id = ref_account_id(ref)
        if account_id is None:
            return None
        return self._roster.get(account_id)

    async def upsert(
        self,
        refs: list[AccountReference],
        ctx: ResolveContext | None = None,
    ) -> list[SyncAccountsResultRow]:
        """Reject every entry with ``PERMISSION_DENIED``.

        ``sync_accounts`` is a buyer-driven write path; on a roster-
        backed store the adopter curates accounts out-of-band, so the
        buyer cannot write. Per-entry rejection (not operation-level
        throw) so a multi-account batch sees the rejection per row,
        matching the wire shape.
        """
        del ctx
        rows: list[SyncAccountsResultRow] = []
        for ref in refs:
            brand = _ref_brand(ref)
            operator = _ref_operator(ref)
            rows.append(
                SyncAccountsResultRow(
                    brand=brand,
                    operator=operator,
                    action="failed",
                    status="failed",
                    errors=[
                        {
                            "code": "PERMISSION_DENIED",
                            "message": _DENIED_MESSAGE,
                            "recovery": "terminal",
                        }
                    ],
                )
            )
        return rows

    async def sync_governance(
        self,
        entries: list[SyncGovernanceEntry],
        ctx: ResolveContext | None = None,
    ) -> list[SyncGovernanceResultRow]:
        """Reject every entry with ``PERMISSION_DENIED``.

        ``sync_governance`` registers buyer-supplied governance agent
        endpoints per-account; on a roster-backed store the adopter
        doesn't model buyer-supplied governance bindings. Per-entry
        rejection so a multi-account batch surfaces the rejection per
        row.
        """
        del ctx
        return [
            SyncGovernanceResultRow(
                account=entry.account,
                status="failed",
                errors=[
                    {
                        "code": "PERMISSION_DENIED",
                        "message": _DENIED_MESSAGE_GOVERNANCE,
                        "recovery": "terminal",
                    }
                ],
            )
            for entry in entries
        ]

    # ``list`` is declared LAST in this class deliberately. Its name
    # shadows the built-in ``list`` in class scope, so any subsequent
    # method whose annotations use ``list[...]`` would resolve the
    # method, not the built-in. Keeping it at the end means
    # ``upsert``/``sync_governance`` annotations above resolve
    # correctly.
    async def list(
        self,
        filter: dict[str, Any] | None = None,
        ctx: ResolveContext | None = None,
    ) -> list[Account[TMeta]]:
        """Return every roster entry.

        Adopters who need filtering (status, sandbox, pagination) wrap
        ``list`` and post-filter the returned list — the roster store
        does not interpret ``filter`` because the typical roster
        cardinality (single-digit to low-thousands of accounts per
        publisher) is small enough that in-memory filtering at the
        adopter layer is fine.
        """
        del filter, ctx
        return list(self._roster.values())


def _ref_brand(ref: AccountReference | None) -> dict[str, Any]:
    """Extract ``brand`` from a natural-key ref; empty dict otherwise.

    The wire schema requires ``brand`` on every
    :class:`SyncAccountsResultRow`. For id-arm refs we don't have a
    brand, so we return an empty dict — the row is ``failed`` anyway,
    and the buyer correlates by request order.
    """
    if ref is None:
        return {}
    brand = getattr(ref, "brand", None)
    if brand is None:
        return {}
    if hasattr(brand, "model_dump"):
        return brand.model_dump(mode="json", exclude_none=True)  # type: ignore[no-any-return]
    if isinstance(brand, dict):
        return brand
    return {}


def _ref_operator(ref: AccountReference | None) -> str:
    """Extract ``operator`` from a natural-key ref; empty string
    otherwise. Same fallback rationale as :func:`_ref_brand`."""
    if ref is None:
        return ""
    operator = getattr(ref, "operator", None)
    return operator if isinstance(operator, str) else ""


def create_roster_account_store(
    *,
    roster: Mapping[str, Account[TMeta]],
) -> _RosterAccountStore[TMeta]:
    """Build an :class:`AccountStore` backed by a fixed publisher-
    curated roster.

    The returned object conforms to the :class:`AccountStore` Protocol
    plus the optional :class:`AccountStoreList`,
    :class:`AccountStoreUpsert`, and :class:`AccountStoreSyncGovernance`
    Protocols. ``upsert`` and ``sync_governance`` fail closed with
    ``PERMISSION_DENIED`` per entry — adopters who need to support
    write paths use a custom :class:`AccountStore` implementation
    instead.

    :param roster: Mapping from ``account_id`` → :class:`Account`. Each
        value's ``id`` MUST match its key; mismatch raises
        :class:`ValueError` at construction. The mapping is copied into
        an internal immutable view, so subsequent mutation of the
        caller's dict does not affect the store.

    :returns: An :class:`AccountStore` whose:

        * :meth:`resolve` returns the roster entry for an
          ``account_id``-arm ref, ``None`` otherwise.
        * :meth:`list` returns every roster entry.
        * :meth:`upsert` rejects every input entry with
          ``PERMISSION_DENIED``.
        * :meth:`sync_governance` rejects every input entry with
          ``PERMISSION_DENIED``.

    :raises ValueError: When any roster value's ``id`` does not match
        its dict key.
    """
    return _RosterAccountStore(roster)
