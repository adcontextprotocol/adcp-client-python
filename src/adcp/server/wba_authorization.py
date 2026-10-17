"""The authorization seam for verified Web Bot Auth origins (experimental, AdCP 3.3).

The transport middleware verifies signatures, checks revocation, renders the
signature-path statuses, and calls one :class:`WbaAuthorizer` to turn a
verified origin into a principal. Bindings, trust-record evaluation and spend
classification live behind that seam; :class:`OriginBindingGate` is the
shipped implementation, and sellers can inject their own.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from adcp.decisioning.types import AdcpError
    from adcp.signing.bindings import OriginBindingResolver
    from adcp.signing.trust import TrustRecordEvaluator
    from adcp.signing.verifier import VerifiedSigner

logger = logging.getLogger(__name__)


@runtime_checkable
class WbaAuthorizer(Protocol):
    """Decide authority for a verified Web Bot Auth origin.

    Return the principal bound to ``signer.agent_url``, or ``None`` for an
    identity-established origin (unbound, delisted, or with no revocation
    list): the middleware then serves it what an anonymous caller gets.
    Raise :class:`~adcp.decisioning.types.AdcpError` for an authorization
    outcome (``PERMISSION_DENIED``, or ``SERVICE_UNAVAILABLE`` for a
    retryable outage); the middleware renders it in the AdCP error envelope,
    never as a signature challenge. Any other exception is answered as
    ``PERMISSION_DENIED``.

    ``params`` are the operation's arguments from the verified body, for
    spend classification, or ``None`` when they cannot be read (classify as
    spend). ``signer.revocation`` holds the revocation state
    the middleware recorded.
    """

    async def __call__(
        self, signer: VerifiedSigner, operation: str, params: Mapping[str, Any] | None
    ) -> str | None:
        raise NotImplementedError("return the bound principal or None")


def _service_unavailable(message: str) -> AdcpError:
    from adcp.decisioning.types import AdcpError

    return AdcpError("SERVICE_UNAVAILABLE", message=message, recovery="transient")


class OriginBindingGate:
    """The shipped :class:`WbaAuthorizer`: bindings re-evaluated against trust records.

    An unbound origin is identity-established. A bound origin needs a
    revocation list and a current listing:

    * revocation ``"current"`` proceeds; ``"stale"`` fails spend with
      ``SERVICE_UNAVAILABLE`` and lets other calls proceed; ``"unavailable"``
      fails every call; no list or no check leaves the origin
      identity-established;
    * the operator's trust record, re-evaluated on every request through the
      evaluator's cache. Listed: the principal. Delisted (the last record read
      does not list it, or the operator never published one):
      identity-established, with no exception; the binding is kept, so the
      origin still cannot downgrade to RFC 9421. Unreadable (a 404 after a
      read included): spend fails with ``SERVICE_UNAVAILABLE``; other calls
      continue under the binding within the evaluator's outage-continuation
      bound, then also fail.

    ``spend_classifier(operation, params)`` defaults to
    :func:`adcp.server.signed_requests.is_wba_spend_committing`, a
    recommendation sellers can override with their own state.
    """

    def __init__(
        self,
        bindings: OriginBindingResolver | None,
        records: TrustRecordEvaluator | None,
        *,
        role: str = "adcp:buying",
        spend_classifier: Callable[[str, Mapping[str, Any] | None], bool] | None = None,
    ) -> None:
        if spend_classifier is None:
            from adcp.server.signed_requests import is_wba_spend_committing

            spend_classifier = is_wba_spend_committing
        self.bindings = bindings
        self.records = records
        self.role = role
        self.spend_classifier = spend_classifier

    async def __call__(
        self, signer: VerifiedSigner, operation: str, params: Mapping[str, Any] | None
    ) -> str | None:
        from adcp.signing.canonical import canonical_origin
        from adcp.signing.trust import TrustRecordError, TrustRecordMatchError

        if self.bindings is None or signer.agent_url is None:
            return None
        origin = canonical_origin(signer.agent_url)
        try:
            principal = await self.bindings(origin)
        except Exception as exc:
            logger.exception("origin binding lookup failed")
            raise _service_unavailable("Origin binding lookup is temporarily unavailable") from exc
        if principal is None:
            return None
        spend = self.spend_classifier(operation, params)
        if signer.revocation == "stale":
            if spend:
                raise _service_unavailable("Origin revocation status is temporarily unavailable")
        elif signer.revocation == "unavailable":
            raise _service_unavailable("Origin revocation status is temporarily unavailable")
        elif signer.revocation != "current":
            # No revocation list, or no check: never acts for its principal.
            return None
        if self.records is None:
            raise _service_unavailable("Origin binding evaluation is unavailable")
        listing = await self.records.check_listing(origin, role=self.role)
        if listing.record is not None:
            # A newer read of the owner's record (by any origin or claim), or
            # the record's deadline passing, supersedes this evaluation.
            try:
                await self.records.validate_snapshots(
                    (listing.record,), origin_or_domain=origin, role=self.role
                )
            except TrustRecordMatchError:
                return None
            except TrustRecordError as exc:
                raise _service_unavailable(
                    "Operator trust record is temporarily unavailable"
                ) from exc
        if listing.status == "listed":
            return principal
        if listing.status == "unlisted":
            logger.info(
                "bound origin is not listed by its operator",
                extra={"reason": type(listing.error).__name__},
            )
            return None
        if spend or not listing.within_grace:
            raise _service_unavailable("Operator trust record is temporarily unavailable")
        return principal


__all__ = ["OriginBindingGate", "WbaAuthorizer"]
