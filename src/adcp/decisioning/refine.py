"""Refine flow scaffold for ``buying_mode='refine'`` on ``get_products``.

When a buyer sends ``GetProductsRequest`` with ``buying_mode='refine'``, the
spec requires the seller to:

* return ``refinement_applied[]`` in the response, **same length, same order**,
  echoing ``scope`` and the matching ``id`` field from each ``refine[]`` entry;
* not accept a ``brief`` (the refine list is the iteration mechanism); and
* surface per-entry ``status`` and ``notes`` so the buyer knows what the
  seller did.

The position-matched echo is mechanical: every adopter gets it right or
silently breaks orchestrators that cross-validate alignment.  This module
lifts that scaffold into the framework.

Adopter contract — implement an optional :meth:`SalesPlatform.refine_get_products`
returning a :class:`RefineResult`.  The framework constructs the wire
``refinement_applied[]`` from the adopter's per-entry outcomes plus the
request's refine entries.

Adopters who do **not** implement ``refine_get_products`` see no change;
buyers sending ``buying_mode='refine'`` to such a platform receive
``AdcpError(INVALID_REQUEST, field='buying_mode')`` with a message
identifying that refine is not supported by this seller.

Reference pattern: :mod:`adcp.decisioning.webhook_emit` — capability-gated
post-adapter side effect.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from adcp.types import (
        GetProductsRequest,
        GetProductsResponse,
    )

logger = logging.getLogger(__name__)

RefinementStatus = Literal["applied", "partial", "unable"]


@dataclass(frozen=True)
class RefinementOutcome:
    """Per-refine-entry decision returned by ``refine_get_products``.

    The adopter returns one outcome per entry in ``request.refine[]``, in
    the same order.  Framework constructs the wire ``refinement_applied[]``
    by zipping outcomes with the request's refine entries — adopter does
    NOT echo ``scope``, ``product_id``, or ``proposal_id`` manually.

    :param status: ``'applied'``, ``'partial'``, or ``'unable'`` per the
        wire enum (see :class:`adcp.types.generated_poc.bundled.media_buy`'s
        ``RefinementApplied.status``).
    :param notes: Adopter's explanation; recommended when status is
        ``'partial'`` or ``'unable'``.
    """

    status: RefinementStatus
    notes: str | None = None


@dataclass(frozen=True)
class RefineResult:
    """Adopter-returned shape from ``refine_get_products``.

    Framework projects this into the wire :class:`GetProductsResponse`,
    constructing ``refinement_applied[]`` from
    :attr:`per_refine_outcome` + the request's ``refine[]`` array.

    :param products: Updated product list (per spec, refine returns a
        revised set — drop omitted, add ``more_like_this`` discoveries,
        update pricing on remaining).
    :param proposals: Updated proposal list.  ``None`` when the seller
        does not produce proposals (sales-non-guaranteed without proposal
        mode).  Empty list when proposals were all omitted.
    :param per_refine_outcome: Exactly ``len(request.refine)`` entries,
        in the same order.  Mismatched length is a developer-facing error
        (raised by the framework before the response is built).
    """

    products: list[Any]
    proposals: list[Any] | None
    per_refine_outcome: list[RefinementOutcome] = field(default_factory=list)


def assert_buying_mode_consistent(req: GetProductsRequest) -> None:
    """Validate ``buying_mode`` against the wire spec's mutual-exclusion rules.

    Per the wire description on ``GetProductsRequest``:

    * ``buying_mode='wholesale'`` — ``brief`` MUST NOT be provided.
    * ``buying_mode='refine'`` — ``brief`` MUST NOT be provided; ``refine[]``
      drives iteration.
    * ``buying_mode='brief'`` — ``brief`` is required (handled by Pydantic
      validation upstream).

    Raises :class:`AdcpError(INVALID_REQUEST)` with the offending field on
    violation.  Called at the top of the ``get_products`` shim before any
    platform dispatch.
    """
    from adcp.decisioning.types import AdcpError

    mode = _coerce_enum_value(getattr(req, "buying_mode", None))
    brief = getattr(req, "brief", None)
    refine = getattr(req, "refine", None)

    if mode == "wholesale" and brief:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "buying_mode='wholesale' must not be combined with brief. "
                "Wholesale callers request raw inventory and apply their "
                "own audiences; the brief is only meaningful in 'brief' mode."
            ),
            field="brief",
        )

    if mode == "refine":
        if brief:
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "buying_mode='refine' must not be combined with brief. "
                    "The refine[] array drives iteration on a previous "
                    "get_products response."
                ),
                field="brief",
            )
        if not refine:
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "buying_mode='refine' requires a non-empty refine[] "
                    "array — the buyer must declare what to iterate on."
                ),
                field="refine",
            )


def build_refinement_applied(
    refines: list[Any],
    outcomes: list[RefinementOutcome],
) -> list[Any]:
    """Position-match the request's ``refine[]`` with the adopter's outcomes.

    Each ``refines[i]`` entry has a discriminated ``scope`` (``'request'``,
    ``'product'``, or ``'proposal'``).  This function emits a parallel
    ``refinement_applied[i]`` carrying the same scope, the matching ID
    field (``product_id`` / ``proposal_id``), and the adopter's
    ``status`` + ``notes``.

    :param refines: ``request.refine`` (length N).
    :param outcomes: Adopter's per-entry outcomes (must also be length N).
    :returns: Wire-shape ``RefinementApplied`` (RootModel) instances (one per entry).
    :raises ValueError: ``len(outcomes) != len(refines)``.  Developer-facing,
        not buyer-facing — adopter-side bug.
    """
    if len(outcomes) != len(refines):
        raise ValueError(
            f"refine_get_products returned {len(outcomes)} outcomes for "
            f"{len(refines)} refine entries — counts must match. The "
            "framework constructs refinement_applied[] by zipping these "
            "lists; mismatched lengths break the buyer's position-matched "
            "echo contract."
        )

    from adcp.types import (
        RefinementApplied,
        RefinementApplied1,
        RefinementApplied2,
        RefinementApplied3,
    )

    # The wire enum on RefinementApplied{1,2,3}.status is the discriminated
    # ``Status`` enum (``applied``/``partial``/``unable``).  Pydantic accepts
    # the matching string at runtime; the model_validate path coerces.
    status_field = {"applied": "applied", "partial": "partial", "unable": "unable"}

    out: list[Any] = []
    for entry, outcome in zip(refines, outcomes, strict=True):
        # Refine is a RootModel discriminated on `scope`; unwrap to the
        # variant.
        inner = getattr(entry, "root", entry)
        scope = getattr(inner, "scope", None)
        status_str = status_field[outcome.status]

        if scope == "request":
            applied: Any = RefinementApplied1.model_validate(
                {"scope": "request", "status": status_str, "notes": outcome.notes}
            )
        elif scope == "product":
            applied = RefinementApplied2.model_validate(
                {
                    "scope": "product",
                    "product_id": str(getattr(inner, "product_id")),
                    "status": status_str,
                    "notes": outcome.notes,
                }
            )
        elif scope == "proposal":
            applied = RefinementApplied3.model_validate(
                {
                    "scope": "proposal",
                    "proposal_id": str(getattr(inner, "proposal_id")),
                    "status": status_str,
                    "notes": outcome.notes,
                }
            )
        else:
            raise ValueError(
                f"Unknown refine scope {scope!r}; expected " "'request' | 'product' | 'proposal'."
            )
        out.append(RefinementApplied(root=applied))
    return out


def project_refine_response(
    result: RefineResult,
    refines: list[Any],
) -> GetProductsResponse:
    """Project a :class:`RefineResult` into a wire :class:`GetProductsResponse`.

    Builds ``refinement_applied[]`` from the request's ``refine[]`` and
    the adopter's ``per_refine_outcome``, then attaches ``products`` and
    ``proposals``.

    :raises ValueError: When ``len(per_refine_outcome) != len(refines)``
        (developer-facing — adopter contract violation).
    """
    from adcp.types import GetProductsResponse

    refinement_applied = build_refinement_applied(refines, result.per_refine_outcome)

    return GetProductsResponse(
        products=list(result.products),
        proposals=list(result.proposals) if result.proposals is not None else None,
        refinement_applied=refinement_applied,
    )


def has_refine_support(platform: Any) -> bool:
    """Return True when refine is reachable through this platform.

    A platform supports refine if it implements ``refine_get_products``
    directly OR — in the case of a ``PlatformRouter`` — if any of its
    wired ProposalManagers declares the refine capability. The router
    itself doesn't expose ``refine_get_products``; refine routes
    through the ProposalManager's ``refine_products`` method on the
    proposal-side surface.
    """
    if callable(getattr(platform, "refine_get_products", None)):
        return True
    proposal_managers = getattr(platform, "_proposal_managers", None)
    if isinstance(proposal_managers, dict):
        for manager in proposal_managers.values():
            caps = getattr(manager, "capabilities", None)
            if getattr(caps, "refine", False) and callable(
                getattr(manager, "refine_products", None)
            ):
                return True
    return False


def _coerce_enum_value(value: Any) -> str | None:
    """Return a plain string for an enum or string value (None passthrough)."""
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


__all__ = [
    "RefineResult",
    "RefinementOutcome",
    "RefinementStatus",
    "assert_buying_mode_consistent",
    "build_refinement_applied",
    "has_refine_support",
    "project_refine_response",
]
