"""Dispatch-side wiring for the v1.5 proposal lifecycle.

This is the *seam* where the framework intercepts buyer calls and runs
the proposal-lifecycle work between the wire envelope and the adopter's
:class:`ProposalManager` / :class:`DecisioningPlatform` methods.

Three integration points, one per buyer-facing dispatch path:

* :func:`maybe_intercept_finalize` — called from the ``get_products``
  shim. When the request carries a ``refine[i].action='finalize'`` for a
  finalize-capable manager, intercepts the call, calls
  ``manager.finalize_proposal``, commits the proposal, and projects the
  wire response.

* :func:`maybe_persist_draft_after_get_products` — called after the
  manager's ``get_products`` / ``refine_products`` returns. Walks the
  response for ``proposals[]`` and calls
  :meth:`ProposalStore.put_draft` for each one, validating
  ``overlap ⊆ wire`` per § D4 round-4.

* :func:`maybe_hydrate_recipes_for_create_media_buy` — called from the
  ``create_media_buy`` shim. When the request carries ``proposal_id``,
  hydrates the proposal record, validates expiry + capability overlap,
  populates ``ctx.recipes``. After the platform method succeeds, the
  caller invokes :func:`maybe_mark_consumed` to record the single-write
  hand-off per § D3.

* :func:`maybe_hydrate_recipes_for_media_buy_id` — called from the
  ``update_media_buy`` / ``get_media_buy_delivery`` shims. Looks up the
  proposal via ``get_by_media_buy_id`` reverse-index; populates
  ``ctx.recipes``. Re-runs capability validation per Resolutions §5.

All helpers are no-ops when the framework can't find a wired manager +
store for the resolved tenant — strictly additive on top of v1.

The helpers' router-detection posture is duck-typed: they look for
``proposal_manager_for_tenant`` / ``proposal_store_for_tenant`` on the
top-level platform. :class:`PlatformRouter` exposes these; non-router
platforms naturally don't, so they fall through to the v1 path with
zero behavioural change.

See ``docs/proposals/proposal-manager-v15-design.md`` § "Implementation
seams" — this module sits parallel to :mod:`adcp.decisioning.refine`
and :mod:`adcp.decisioning.webhook_emit`. Same architectural shape:
framework intercepts at a seam, does its work, dispatches.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

from adcp.decisioning.derive_packages import derive_packages_from_proposal
from adcp.decisioning.proposal_lifecycle import (
    consumed_log,
    detect_finalize_action,
    draft_persisted_log,
    enforce_proposal_expiry,
    finalize_succeeded_log,
    validate_capability_overlap,
    validate_overlap_subset_of_wire,
)
from adcp.decisioning.proposal_manager import (
    FinalizeProposalRequest,
    FinalizeProposalSuccess,
)
from adcp.decisioning.proposal_store import (
    ProposalRecord,
    _await_maybe,
)
from adcp.decisioning.recipe import Recipe
from adcp.decisioning.types import AdcpError, is_task_handoff

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.proposal_manager import ProposalManager
    from adcp.decisioning.proposal_store import ProposalStore
    from adcp.decisioning.task_registry import TaskRegistry

logger = logging.getLogger("adcp.decisioning.proposal_dispatch")


# ---------------------------------------------------------------------------
# Router introspection — detects per-tenant manager + store on a platform
# ---------------------------------------------------------------------------


def _resolve_manager_and_store(
    platform: Any,
    ctx: RequestContext[Any],
) -> tuple[ProposalManager | None, ProposalStore | None]:
    """Return ``(manager, store)`` for ``ctx``'s tenant, or ``(None, None)``.

    Duck-typed: looks for the two router accessors. Non-router platforms
    legitimately don't expose them; that's the v1 path.
    """
    tenant_id = _tenant_id(ctx)
    if tenant_id is None:
        return (None, None)
    manager = None
    store = None
    if hasattr(platform, "proposal_manager_for_tenant"):
        manager = platform.proposal_manager_for_tenant(tenant_id)
    if hasattr(platform, "proposal_store_for_tenant"):
        store = platform.proposal_store_for_tenant(tenant_id)
    return (manager, store)


def _tenant_id(ctx: RequestContext[Any]) -> str | None:
    """Pull ``tenant_id`` off ``ctx.account.metadata``. Mirrors the
    router's :func:`_tenant_id_from_ctx` helper."""
    account = getattr(ctx, "account", None)
    metadata = getattr(account, "metadata", None) if account is not None else None
    if isinstance(metadata, dict):
        tenant_id = metadata.get("tenant_id")
        if isinstance(tenant_id, str):
            return tenant_id
    return None


def _has_finalize_capability(manager: ProposalManager | None) -> bool:
    """Per Resolutions §7: framework detects finalize via ``hasattr`` +
    capabilities flag. Putting ``finalize_proposal`` on the
    ``runtime_checkable`` Protocol body would force every v1 manager to
    declare it. Adopters opt in by both implementing AND declaring."""
    if manager is None:
        return False
    caps = getattr(manager, "capabilities", None)
    if not getattr(caps, "finalize", False):
        return False
    return callable(getattr(manager, "finalize_proposal", None))


# ---------------------------------------------------------------------------
# get_products — finalize interception
# ---------------------------------------------------------------------------


async def maybe_intercept_finalize(
    platform: Any,
    params: Any,
    ctx: RequestContext[Any],
    *,
    executor: ThreadPoolExecutor,
    registry: TaskRegistry,
) -> dict[str, Any] | None:
    """Intercept ``buying_mode='refine' + action='finalize'`` requests.

    Returns the wire response dict when the framework intercepts the
    finalize call; returns ``None`` when the framework should fall
    through to the standard ``get_products`` dispatch (no finalize entry,
    no finalize-capable manager, etc.).
    """
    finalize_entry = detect_finalize_action(params)
    if finalize_entry is None:
        return None

    refine_index, proposal_id, ask = finalize_entry
    field_path = f"refine[{refine_index}].proposal_id"
    manager, store = _resolve_manager_and_store(platform, ctx)
    if not _has_finalize_capability(manager) or store is None:
        return None

    account_id = ctx.account.id
    publisher_id = _tenant_id(ctx)
    raw = await _await_maybe(
        store.get(
            proposal_id,
            expected_account_id=account_id,
            expected_publisher_id=publisher_id,
        )
    )
    record = cast("ProposalRecord | None", raw)
    if record is None:
        raise AdcpError(
            "PROPOSAL_NOT_FOUND",
            message=(
                f"Proposal {proposal_id!r} not found. The buyer must call "
                "get_products with buying_mode='brief' or 'refine' to "
                "obtain a draft proposal_id before finalizing it."
            ),
            recovery="terminal",
            field=field_path,
        )
    from adcp.decisioning.proposal_store import ProposalState

    if record.state != ProposalState.DRAFT:
        raise AdcpError(
            "PROPOSAL_NOT_COMMITTED",
            message=(
                f"Proposal {proposal_id!r} is in state "
                f"{record.state.value!r}; only draft proposals can be "
                "finalized. Already-committed proposals should be "
                "accepted via create_media_buy(proposal_id=...) directly."
            ),
            recovery="correctable",
            field=field_path,
        )

    finalize_req = FinalizeProposalRequest(
        proposal_id=proposal_id,
        recipes=dict(record.recipes),
        proposal_payload=dict(record.proposal_payload),
        ask=ask,
        parent_request=params,
    )

    assert manager is not None
    method = manager.finalize_proposal  # type: ignore[attr-defined]
    if asyncio.iscoroutinefunction(method):
        result = await method(finalize_req, ctx)
    else:
        ctx_snapshot = contextvars.copy_context()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            executor,
            functools.partial(ctx_snapshot.run, method, finalize_req, ctx),
        )

    if is_task_handoff(result):
        from adcp.decisioning.dispatch import _project_handoff

        async def _commit_on_handoff_completion(success: Any) -> None:
            if not isinstance(success, FinalizeProposalSuccess):
                raise AdcpError(
                    "INTERNAL_ERROR",
                    message=(
                        f"finalize_proposal handoff fn returned "
                        f"{type(success).__name__}; expected "
                        "FinalizeProposalSuccess."
                    ),
                    recovery="terminal",
                )
            await _await_maybe(
                store.commit(
                    proposal_id,
                    expires_at=success.expires_at,
                    proposal_payload=dict(success.proposal),
                    expected_account_id=account_id,
                    expected_publisher_id=publisher_id,
                )
            )
            finalize_succeeded_log(
                proposal_id=proposal_id,
                account_id=account_id,
                expires_at=success.expires_at,
                path="handoff",
            )

        return await _project_handoff(
            result,
            ctx,
            method_name="finalize_proposal",
            registry=registry,
            executor=executor,
            on_complete=_commit_on_handoff_completion,
        )

    if not isinstance(result, FinalizeProposalSuccess):
        raise AdcpError(
            "INTERNAL_ERROR",
            message=(
                f"finalize_proposal returned {type(result).__name__}; "
                "expected FinalizeProposalSuccess or "
                "TaskHandoff[FinalizeProposalSuccess]."
            ),
            recovery="terminal",
        )

    await _await_maybe(
        store.commit(
            proposal_id,
            expires_at=result.expires_at,
            proposal_payload=dict(result.proposal),
            expected_account_id=account_id,
            expected_publisher_id=publisher_id,
        )
    )
    finalize_succeeded_log(
        proposal_id=proposal_id,
        account_id=account_id,
        expires_at=result.expires_at,
        path="inline",
    )

    return _project_finalize_response(
        params=params,
        committed_proposal=result.proposal,
        draft_record=record,
        finalize_proposal_id=proposal_id,
    )


def _project_finalize_response(
    *,
    params: Any,
    committed_proposal: dict[str, Any],
    draft_record: ProposalRecord,
    finalize_proposal_id: str,
) -> dict[str, Any]:
    """Build the wire ``GetProductsResponse`` shape for an inline finalize."""
    refine_entries = list(getattr(params, "refine", None) or [])
    refinement_applied: list[dict[str, Any]] = []
    for entry in refine_entries:
        inner = getattr(entry, "root", entry)
        scope = getattr(inner, "scope", None)
        scope_str = str(getattr(scope, "value", scope)) if scope is not None else None
        if scope_str == "proposal":
            refinement_applied.append(
                {
                    "scope": "proposal",
                    "proposal_id": str(getattr(inner, "proposal_id", finalize_proposal_id)),
                    "status": "applied",
                }
            )
        elif scope_str == "product":
            refinement_applied.append(
                {
                    "scope": "product",
                    "product_id": str(getattr(inner, "product_id", "")),
                    "status": "applied",
                }
            )
        else:
            refinement_applied.append({"scope": "request", "status": "applied"})

    products_payload: list[Any] = []
    draft_payload = dict(draft_record.proposal_payload)
    if "products" in draft_payload and isinstance(draft_payload["products"], list):
        products_payload = []

    return {
        "products": products_payload,
        "proposals": [committed_proposal],
        "refinement_applied": refinement_applied,
    }


# ---------------------------------------------------------------------------
# get_products / refine_products — post-call draft persistence
# ---------------------------------------------------------------------------


async def maybe_persist_draft_after_get_products(
    platform: Any,
    response: Any,
    ctx: RequestContext[Any],
) -> None:
    """Persist proposals returned by ``get_products`` / ``refine_products``
    as drafts in the wired :class:`ProposalStore`.
    """
    manager, store = _resolve_manager_and_store(platform, ctx)
    if store is None:
        return

    proposals = _extract_list(response, "proposals")
    if not proposals:
        return

    products = _extract_list(response, "products") or []

    caps = getattr(manager, "capabilities", None) if manager is not None else None
    auto_commit = bool(getattr(caps, "auto_commit_on_put_draft", False))
    auto_commit_ttl = int(getattr(caps, "auto_commit_ttl_seconds", 7 * 24 * 3600))

    for proposal in proposals:
        proposal_id = _read(proposal, "proposal_id")
        if proposal_id is None:
            continue
        proposal_payload = _to_dict(proposal)
        _enrich_allocations_with_pricing_options(proposal_payload, products)
        recipes = _collect_recipes_from_products(products, proposal_payload)
        if recipes:
            validate_overlap_subset_of_wire(recipes=recipes, products=products)
        dispatch_publisher_id = _tenant_id(ctx)
        await _await_maybe(
            store.put_draft(
                proposal_id=str(proposal_id),
                account_id=ctx.account.id,
                publisher_id=dispatch_publisher_id,
                recipes=recipes,
                proposal_payload=proposal_payload,
            )
        )
        draft_persisted_log(
            proposal_id=str(proposal_id),
            account_id=ctx.account.id,
            recipes_count=len(recipes),
        )
        if auto_commit:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=auto_commit_ttl)
            await _await_maybe(
                store.commit(
                    str(proposal_id),
                    expires_at=expires_at,
                    proposal_payload=proposal_payload,
                    expected_account_id=ctx.account.id,
                    expected_publisher_id=dispatch_publisher_id,
                )
            )


def _enrich_allocations_with_pricing_options(
    proposal_payload: dict[str, Any],
    products: list[Any],
) -> None:
    """Populate ``allocation.pricing_option_id`` from the product's
    ``pricing_options[]`` when missing — but ONLY when the product has
    exactly one pricing option.
    """
    allocations = proposal_payload.get("allocations")
    if not isinstance(allocations, list):
        return
    product_to_single_option: dict[str, str] = {}
    for product in products:
        product_id = _read(product, "product_id")
        if product_id is None:
            continue
        pricing_options = _read(product, "pricing_options")
        if not isinstance(pricing_options, list) or len(pricing_options) != 1:
            continue
        option_id = _read(pricing_options[0], "pricing_option_id")
        if option_id:
            product_to_single_option[str(product_id)] = str(option_id)
    if not product_to_single_option:
        return
    for allocation in allocations:
        if not isinstance(allocation, dict):
            continue
        if allocation.get("pricing_option_id"):
            continue
        pid = allocation.get("product_id")
        if pid is None:
            continue
        single = product_to_single_option.get(str(pid))
        if single:
            allocation["pricing_option_id"] = single


def _collect_recipes_from_products(
    products: list[Any],
    proposal_payload: dict[str, Any],
) -> dict[str, Recipe]:
    """Walk ``products[]`` and extract typed :class:`Recipe` instances
    from their ``implementation_config``.
    """
    referenced: set[str] | None = None
    allocations = proposal_payload.get("allocations")
    if isinstance(allocations, list):
        referenced = {
            str(_read(a, "product_id")) for a in allocations if _read(a, "product_id") is not None
        }
    else:
        legacy_products = proposal_payload.get("products")
        if isinstance(legacy_products, list):
            referenced = {str(p) for p in legacy_products if isinstance(p, str)}

    recipes: dict[str, Recipe] = {}
    for product in products:
        product_id = _read(product, "product_id")
        if product_id is None:
            continue
        if referenced is not None and str(product_id) not in referenced:
            continue
        impl_config = _read(product, "implementation_config")
        if isinstance(impl_config, Recipe):
            recipes[str(product_id)] = impl_config
    return recipes


# ---------------------------------------------------------------------------
# create_media_buy — recipe hydration on proposal_id
# ---------------------------------------------------------------------------


async def maybe_hydrate_recipes_for_create_media_buy(
    platform: Any,
    params: Any,
    ctx: RequestContext[Any],
) -> ProposalRecord | None:
    """Reserve the proposal for consumption and hydrate ``ctx.recipes``."""
    proposal_id = _read(params, "proposal_id")
    if not proposal_id:
        return None
    proposal_id = str(proposal_id)

    manager, store = _resolve_manager_and_store(platform, ctx)
    if store is None:
        return None

    grace = 0
    if manager is not None:
        caps = getattr(manager, "capabilities", None)
        grace = int(getattr(caps, "expires_at_grace_seconds", 0) or 0)

    hydrate_publisher_id = _tenant_id(ctx)

    await enforce_proposal_expiry(
        proposal_id,
        proposal_store=store,
        expected_account_id=ctx.account.id,
        expected_publisher_id=hydrate_publisher_id,
        grace_seconds=grace,
        now=datetime.now(timezone.utc),
    )

    raw = await _await_maybe(
        store.try_reserve_consumption(
            proposal_id,
            expected_account_id=ctx.account.id,
            expected_publisher_id=hydrate_publisher_id,
        )
    )
    record = cast("ProposalRecord", raw)

    try:
        packages = _read(params, "packages") or []
        if (
            not packages
            and _has_allocations(record.proposal_payload)
            and _derivation_enabled(manager)
        ):
            derived = _derive_packages(
                manager=manager,
                proposal_payload=record.proposal_payload,
                total_budget=_read(params, "total_budget"),
                recipes=record.recipes,
            )
            if derived:
                try:
                    params.packages = derived
                except (AttributeError, ValueError, TypeError):
                    if isinstance(params, dict):
                        params["packages"] = derived
                    else:
                        raise
                packages = derived

        if packages:
            validate_capability_overlap(
                packages=list(packages),
                recipes=record.recipes,
                field_path_prefix="packages",
            )
    except Exception:
        try:
            await _await_maybe(
                store.release_consumption(
                    proposal_id,
                    expected_account_id=ctx.account.id,
                    expected_publisher_id=hydrate_publisher_id,
                )
            )
        except Exception:
            logger.exception(
                "Failed to release proposal reservation %s after "
                "post-reserve validation error; record may be stuck in "
                "CONSUMING until eviction.",
                proposal_id,
            )
        raise

    ctx.recipes = dict(record.recipes)
    return record


def _derivation_enabled(manager: ProposalManager | None) -> bool:
    if manager is None:
        return False
    if callable(getattr(manager, "derive_packages", None)):
        return True
    caps = getattr(manager, "capabilities", None)
    return bool(getattr(caps, "derive_packages_from_allocations", False))


def _has_allocations(proposal_payload: Any) -> bool:
    if not isinstance(proposal_payload, dict):
        return False
    allocations = proposal_payload.get("allocations")
    return isinstance(allocations, list) and len(allocations) > 0


def _derive_packages(
    *,
    manager: ProposalManager | None,
    proposal_payload: Any,
    total_budget: Any,
    recipes: Any,
) -> list[Any]:
    override = getattr(manager, "derive_packages", None) if manager is not None else None
    if callable(override):
        derived = override(
            proposal_payload=proposal_payload,
            total_budget=total_budget,
            recipes=recipes,
        )
        return list(derived) if derived is not None else []
    return list(
        derive_packages_from_proposal(
            proposal_payload,
            total_budget,
            recipes=recipes,
        )
    )


async def mark_proposal_consumed(
    platform: Any,
    proposal_record: ProposalRecord,
    *,
    media_buy_id: str,
    ctx: RequestContext[Any],
) -> None:
    """Finalize the two-phase consumption per § D3 + race fix."""
    _, store = _resolve_manager_and_store(platform, ctx)
    if store is None:
        return
    await _await_maybe(
        store.finalize_consumption(
            proposal_record.proposal_id,
            media_buy_id=media_buy_id,
            expected_account_id=proposal_record.account_id,
            expected_publisher_id=proposal_record.publisher_id,
        )
    )
    consumed_log(
        proposal_id=proposal_record.proposal_id,
        account_id=proposal_record.account_id,
        media_buy_id=media_buy_id,
    )


async def release_proposal_reservation(
    platform: Any,
    proposal_record: ProposalRecord,
    ctx: RequestContext[Any],
) -> None:
    """Roll back the consumption reservation: ``CONSUMING → COMMITTED``."""
    _, store = _resolve_manager_and_store(platform, ctx)
    if store is None:
        return
    try:
        await _await_maybe(
            store.release_consumption(
                proposal_record.proposal_id,
                expected_account_id=proposal_record.account_id,
                expected_publisher_id=proposal_record.publisher_id,
            )
        )
    except Exception:
        logger.exception(
            "Failed to release consumption reservation for proposal %s; "
            "the record may be stuck in CONSUMING until eviction. "
            "Original adapter exception will still propagate.",
            proposal_record.proposal_id,
        )


# ---------------------------------------------------------------------------
# update_media_buy / get_media_buy_delivery — recipe hydration via
# get_by_media_buy_id reverse-index
# ---------------------------------------------------------------------------


async def maybe_hydrate_recipes_for_media_buy_id(
    platform: Any,
    media_buy_id: str,
    ctx: RequestContext[Any],
    *,
    packages: list[Any] | None = None,
) -> ProposalRecord | None:
    """Hydrate ``ctx.recipes`` for post-acceptance buy operations."""
    if not media_buy_id:
        return None
    _, store = _resolve_manager_and_store(platform, ctx)
    if store is None:
        return None

    raw = await _await_maybe(
        store.get_by_media_buy_id(
            media_buy_id,
            expected_account_id=ctx.account.id,
            expected_publisher_id=_tenant_id(ctx),
        )
    )
    record = cast("ProposalRecord | None", raw)
    if record is None:
        return None

    if packages:
        validate_capability_overlap(
            packages=list(packages),
            recipes=record.recipes,
            field_path_prefix="packages",
        )

    ctx.recipes = dict(record.recipes)
    return record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _extract_list(obj: Any, name: str) -> list[Any]:
    value = _read(obj, name)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return list(value)


def _to_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "model_dump"):
        dumped = obj.model_dump(mode="json", exclude_none=True)
        return dict(dumped) if isinstance(dumped, dict) else {}
    return dict(getattr(obj, "__dict__", {}) or {})


__all__ = [
    "mark_proposal_consumed",
    "maybe_hydrate_recipes_for_create_media_buy",
    "maybe_hydrate_recipes_for_media_buy_id",
    "maybe_intercept_finalize",
    "maybe_persist_draft_after_get_products",
    "release_proposal_reservation",
]
