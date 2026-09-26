"""Turn-local source revocation and the SDK inline publication boundary.

Only denials are remembered, for the remainder of a scheduling turn. Every
dispatch and publication calls the adopter again; no authorization is cached.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import NoReturn

_REVOKED_ACCOUNTS: ContextVar[set[str] | None] = ContextVar(
    "reporting_source_revoked_accounts", default=None
)
_INLINE_PUBLICATION: ContextVar[
    tuple[str, Callable[[], AbstractAsyncContextManager[None]]] | None
] = ContextVar("reporting_inline_publication", default=None)


@contextmanager
def source_turn() -> Iterator[None]:
    """Share denials across configurations in this turn, never across turns."""
    if _REVOKED_ACCOUNTS.get() is not None:
        yield
        return
    token = _REVOKED_ACCOUNTS.set(set())
    try:
        yield
    finally:
        _REVOKED_ACCOUNTS.reset(token)


def account_revoked(account_id: str) -> bool:
    return account_id in (_REVOKED_ACCOUNTS.get() or ())


def source_revoked(account_id: str) -> NoReturn:
    from adcp.reporting.production.contracts import _SourceAuthorizationRevokedError

    revoked = _REVOKED_ACCOUNTS.get()
    if revoked is not None:
        revoked.add(account_id)
    raise _SourceAuthorizationRevokedError()


def require_account_work(account_id: str) -> None:
    if account_revoked(account_id):
        source_revoked(account_id)


@contextmanager
def bind_inline_publication(
    account_id: str, guard: Callable[[], AbstractAsyncContextManager[None]]
) -> Iterator[None]:
    """Carry the producer's lock and live check into its inline executor task."""
    token = _INLINE_PUBLICATION.set((account_id, guard))
    try:
        yield
    finally:
        _INLINE_PUBLICATION.reset(token)


@asynccontextmanager
async def inline_publication(account_id: str) -> AsyncIterator[None]:
    bound = _INLINE_PUBLICATION.get()
    if bound is None:
        yield
        return
    if bound[0] != account_id:
        raise ValueError("source publication must belong to the dispatched account")
    async with bound[1]():
        yield
