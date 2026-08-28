#!/usr/bin/env python3
"""Reference ADCPHandler seller agent.

A complete, runnable seller for the AdCP media_buy_seller storyboard
(9 steps, all core tools). Used as the reference for the seller,
generative-seller, and retail-media skills.

Run:
    python examples/seller_agent.py

Validate:
    npx -y -p @adcp/client adcp storyboard run \\
        http://localhost:3001/mcp media_buy_seller --json
"""

from __future__ import annotations

import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from adcp import (
    ActionAvailabilityStatus,
    Creative,
    Format,
    Product,
    assess_update_media_buy_actions,
    project_available_actions,
)
from adcp.canonical_formats import (
    CanonicalFormatLegacyResolutionContext,
    LegacyFormatConversionContext,
    migrated_format_option_id,
)
from adcp.decisioning import assert_media_buy_transition
from adcp.server import (
    INSECURE_ALLOW_ALL,
    ADCPHandler,
    adcp_error,
    cancel_media_buy_response,
    serve,
)
from adcp.server.helpers import valid_actions_for_status
from adcp.server.responses import (
    capabilities_response,
    delivery_response,
    legacy_creative_formats_response,
    list_creatives_response,
    media_buy_response,
    media_buys_response,
    products_response,
    sync_accounts_response,
    sync_creatives_response,
    sync_governance_response,
    update_media_buy_response,
)
from adcp.server.test_controller import TestControllerError, TestControllerStore

PORT = int(os.environ.get("ADCP_PORT") or os.environ.get("PORT") or 3001)
AGENT_URL = f"http://localhost:{PORT}/mcp"
LEGACY_FORMAT_OWNER = "https://creative.adcontextprotocol.org/"
_DEMO_LEGACY_FORMAT_OWNERS = {
    LEGACY_FORMAT_OWNER.rstrip("/"),
    "https://your-platform.example.com",
}

# Spec-valid values for ``Product.channels`` (the canonical
# ``MediaChannelSchema`` enum from schemas/cache/enums/channels.json).
# Storyboard fixtures occasionally seed legacy channel names ("video")
# that aren't in the enum; ``seed_product`` filters incoming fixture
# channels against this set so the demo seller doesn't echo invalid
# values back through ``get_products`` and trip strict response
# validation.
_VALID_CHANNELS: frozenset[str] = frozenset(
    {
        "display",
        "olv",
        "social",
        "search",
        "ctv",
        "linear_tv",
        "radio",
        "streaming_audio",
        "podcast",
        "dooh",
        "ooh",
        "print",
        "cinema",
        "email",
        "gaming",
        "retail_media",
        "influencer",
        "affiliate",
        "product_placement",
        "sponsored_intelligence",
    }
)

accounts: dict[str, dict[str, Any]] = {}
media_buys: dict[str, dict[str, Any]] = {}
creatives: dict[str, dict[str, Any]] = {}
open_impairments: dict[tuple[str, str], dict[str, Any]] = {}
proposals: dict[str, dict[str, Any]] = {}
# Used when no account_id is present; single-tenant demo shortcut.
# Real sellers must scope directives and tasks by account_id.
_DEFAULT_ACCOUNT_ID = "__default__"

# Test-controller state (force_*/seed_* scenarios only)
plans: dict[str, dict[str, Any]] = {}
# Seeded creative formats keyed by the string format ID the storyboard supplies.
# list_creative_formats merges these in so storyboard references resolve.
seeded_creative_formats: dict[str, dict[str, Any]] = {}
# Explicit application-owned compatibility routes learned while upgrading
# legacy requests. This state is intentionally separate from canonical models.
legacy_routes_by_option_id: dict[str, dict[str, Any]] = {}


def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _package_creative_ids(pkg: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for assignment in pkg.get("creative_assignments") or []:
        if isinstance(assignment, dict) and assignment.get("creative_id"):
            ids.append(str(assignment["creative_id"]))
    for creative in pkg.get("creatives") or []:
        if isinstance(creative, dict) and creative.get("creative_id"):
            ids.append(str(creative["creative_id"]))
    return list(dict.fromkeys(ids))


def _health_fields_for_media_buy(media_buy_id: str | None, mb: dict[str, Any]) -> dict[str, Any]:
    impaired_packages: dict[str, list[str]] = {}
    for pkg in mb.get("packages", []):
        package_id = pkg.get("package_id")
        if not package_id:
            continue
        creative_ids = _package_creative_ids(pkg)
        if not creative_ids:
            continue
        if any(
            creatives.get(creative_id, {}).get("status") in {"approved", "active"}
            for creative_id in creative_ids
        ):
            continue
        for creative_id in creative_ids:
            creative_status = creatives.get(creative_id, {}).get("status")
            if creative_status in {"rejected"}:
                package_ids = impaired_packages.setdefault(creative_id, [])
                if package_id not in package_ids:
                    package_ids.append(package_id)
    media_buy_key = media_buy_id or "__anonymous__"
    active_keys = {(media_buy_key, creative_id) for creative_id in impaired_packages}
    for key in [
        key for key in open_impairments if key[0] == media_buy_key and key not in active_keys
    ]:
        del open_impairments[key]

    impairments: list[dict[str, Any]] = []
    for creative_id, package_ids in impaired_packages.items():
        creative = creatives.get(creative_id, {})
        key = (media_buy_key, creative_id)
        if key not in open_impairments:
            open_impairments[key] = {
                "impairment_id": f"imp-{uuid.uuid4().hex[:8]}",
                "observed_at": creative.get("status_changed_at") or _now_z(),
            }
        impairment = open_impairments[key]
        impairments.append(
            {
                "impairment_id": impairment["impairment_id"],
                "resource_type": "creative",
                "resource_id": creative_id,
                "package_ids": package_ids,
                "transition": {"from": "approved", "to": "rejected"},
                "reason_code": "content_rejected",
                "reason": "Creative is no longer approved for delivery.",
                "observed_at": impairment["observed_at"],
                "remediation": "Assign an approved replacement creative.",
            }
        )
    if impairments:
        return {"health": "impaired", "impairments": impairments}
    return {"health": "ok", "impairments": []}


# Single-shot directives registered by force_create_media_buy_arm; keyed by account_id.
pending_directives: dict[str, dict[str, Any]] = {}
# Tasks registered when create_media_buy consumes a 'submitted' directive; keyed by task_id.
pending_task_completions: dict[str, dict[str, Any]] = {}


def _image_format_options(
    *,
    format_option_id: str,
    display_name: str,
    v1_format_id: str,
    width: int,
    height: int,
) -> list[dict[str, Any]]:
    """Build a v2 ``format_options[]`` entry pointing back at a v1 ``format_id``.

    Dual-emit pattern: this reference seller publishes the v1
    ``Product.format_ids[]`` for 3.0 buyers and the v2
    ``Product.format_options[]`` for 3.1 buyers. The two carry the
    same underlying format; the v2 declaration's ``v1_format_ref``
    asserts the pairing so SDKs running the v2 → v1 projection (see
    ``adcp.canonical_formats.project_product_to_v1``) round-trip
    format_ids back to the v1 emit.

    Adopters reading this file as a template SHOULD prefer
    publishing both shapes for the duration of the 3.0 → 3.1
    migration window; the storyboard runner exercises both paths
    against this reference.
    """
    return [
        {
            "format_kind": "image",
            "format_option_id": format_option_id,
            "display_name": display_name,
            "v1_format_ref": [{"agent_url": LEGACY_FORMAT_OWNER, "id": v1_format_id}],
            "params": {
                "sizes": [{"width": width, "height": height}],
                "asset_source": "buyer_uploaded",
                "ssl_required": True,
                "image_formats": ["jpg", "png", "gif"],
            },
        }
    ]


def _seeded_format_options(
    *,
    product_id: str,
    name: str,
    format_ids: list[Any],
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for i, fmt in enumerate(format_ids):
        if not isinstance(fmt, dict):
            continue
        v1_format_id = fmt.get("id") or "display_300x250"
        v1_agent_url = fmt.get("agent_url") or LEGACY_FORMAT_OWNER
        option_id = f"storyboard_{product_id}_{i}"
        display_name = f"{name} - {v1_format_id}"
        if "video" in v1_format_id:
            options.append(
                {
                    "format_kind": "video_hosted",
                    "format_option_id": option_id,
                    "display_name": display_name,
                    "v1_format_ref": [{"agent_url": v1_agent_url, "id": v1_format_id}],
                    "params": {},
                }
            )
            continue

        width, height = (970, 250) if "970x250" in v1_format_id else (300, 250)
        option = _image_format_options(
            format_option_id=option_id,
            display_name=display_name,
            v1_format_id=v1_format_id,
            width=width,
            height=height,
        )[0]
        option["v1_format_ref"][0]["agent_url"] = v1_agent_url
        options.append(option)
    if options:
        return options
    return _image_format_options(
        format_option_id=f"storyboard_{product_id}_0",
        display_name=f"{name} - display_300x250",
        v1_format_id="display_300x250",
        width=300,
        height=250,
    )


def _canonical_product(product: dict[str, Any]) -> Product:
    """Validate a catalog record at the canonical SDK boundary.

    The example keeps the original legacy tuple beside each declaration in its
    in-memory catalog so it can demonstrate negotiated 3.0/3.1 delivery.  A
    ``Format`` captures that tuple as private compatibility state; the primary
    ``Product`` dump therefore remains canonical while the server can still
    project the response for a legacy caller in the same process.
    """

    payload = dict(product)
    payload.pop("format_ids", None)
    payload["format_options"] = [
        option if isinstance(option, Format) else Format.model_validate(option)
        for option in payload.get("format_options") or []
    ]

    placements: list[Any] = []
    for placement in payload.get("placements") or []:
        if not isinstance(placement, dict):
            placements.append(placement)
            continue
        canonical_placement = dict(placement)
        canonical_placement.pop("format_ids", None)
        if canonical_placement.get("format_options"):
            canonical_placement["format_options"] = [
                option if isinstance(option, Format) else Format.model_validate(option)
                for option in canonical_placement["format_options"]
            ]
        placements.append(canonical_placement)
    if placements:
        payload["placements"] = placements

    return Product.model_validate(payload)


def _legacy_format_converter(context: LegacyFormatConversionContext) -> dict[str, Any] | None:
    """Upgrade formats explicitly owned by this demo's legacy catalog."""

    ref = context.format_id
    if str(ref.agent_url).rstrip("/") not in _DEMO_LEGACY_FORMAT_OWNERS:
        return None
    option_id = migrated_format_option_id(ref)
    legacy_routes_by_option_id[option_id] = ref.model_dump(mode="json", exclude_none=True)
    if "video" in ref.id:
        return {"format_kind": "video_hosted", "params": {}}
    return {"format_kind": "image", "params": {}}


def _legacy_ref_for_option_id(option_id: str) -> dict[str, Any] | None:
    """Resolve an option only from captured request or catalog evidence."""

    captured = legacy_routes_by_option_id.get(option_id)
    if captured is not None:
        return dict(captured)
    for product in PRODUCTS:
        for option in product.get("format_options") or []:
            if not isinstance(option, dict):
                continue
            for raw_ref in option.get("v1_format_ref") or []:
                if isinstance(raw_ref, dict) and migrated_format_option_id(raw_ref) == option_id:
                    return dict(raw_ref)
    return None


def _canonical_format_legacy_resolver(
    context: CanonicalFormatLegacyResolutionContext,
) -> list[dict[str, Any]] | None:
    option_id = context.declaration.format_option_id
    if option_id is None:
        return None
    legacy_ref = _legacy_ref_for_option_id(option_id)
    return [legacy_ref] if legacy_ref is not None else None


def _canonical_listed_creative(
    creative_id: str,
    creative: dict[str, Any],
) -> tuple[Creative, Format]:
    """Return a canonical listed creative plus its explicit legacy route.

    This demo owns the default ``display_300x250`` mapping used when a seeded
    creative omits a format.  It is compatibility data supplied by the
    application, not a reverse inference by the SDK.
    """

    payload = dict(creative)
    supplied_ref = payload.get("format_option_ref")
    supplied_option_id = (
        supplied_ref.get("format_option_id") if isinstance(supplied_ref, dict) else None
    )
    legacy_value = payload.pop("format_id", None)
    if isinstance(legacy_value, dict):
        legacy_ref = {
            "agent_url": legacy_value.get("agent_url") or LEGACY_FORMAT_OWNER,
            "id": legacy_value.get("id") or "display_300x250",
        }
    elif isinstance(legacy_value, str):
        legacy_ref = {"agent_url": LEGACY_FORMAT_OWNER, "id": legacy_value}
    else:
        legacy_ref = (
            _legacy_ref_for_option_id(supplied_option_id)
            if isinstance(supplied_option_id, str)
            else None
        )
        legacy_ref = legacy_ref or {
            "agent_url": LEGACY_FORMAT_OWNER,
            "id": "display_300x250",
        }

    payload.setdefault("creative_id", creative_id)
    payload.setdefault("name", creative_id)
    payload.setdefault("status", "approved")
    payload.setdefault("created_date", payload.get("status_changed_at") or _now_z())
    payload.setdefault("updated_date", payload.get("status_changed_at") or _now_z())
    payload.setdefault("format_kind", "image")

    if isinstance(supplied_ref, dict) and isinstance(supplied_ref.get("format_option_id"), str):
        option_id = supplied_ref["format_option_id"]
        option_ref = dict(supplied_ref)
    else:
        option_id = migrated_format_option_id(legacy_ref)
        option_ref = {
            "scope": "publisher",
            "publisher_domain": "example.com",
            "format_option_id": option_id,
        }
        payload["format_option_ref"] = option_ref

    declaration_data: dict[str, Any] = {
        "format_option_id": option_id,
        "format_kind": payload["format_kind"],
        "params": payload.get("params") if isinstance(payload.get("params"), dict) else {},
        "v1_format_ref": [legacy_ref],
    }
    if option_ref.get("scope") == "publisher":
        declaration_data["publisher_domain"] = option_ref.get("publisher_domain") or "example.com"

    return Creative.model_validate(payload), Format.model_validate(declaration_data)


def _allowed_actions_for_packages(packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    products_by_id = {p.get("product_id"): p for p in PRODUCTS}
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for package in packages:
        product = products_by_id.get(package.get("product_id")) or {}
        for action in product.get("allowed_actions") or []:
            if not isinstance(action, dict):
                continue
            action_id = action.get("action")
            if not action_id or action_id in seen:
                continue
            seen.add(action_id)
            actions.append(action)
    return actions


def _resolve_available_actions(
    packages: list[dict[str, Any]],
    status: str,
) -> list[dict[str, Any]]:
    available: list[dict[str, Any]] = []
    for action in _allowed_actions_for_packages(packages):
        allowed_statuses = action.get("allowed_statuses")
        if allowed_statuses and status not in allowed_statuses:
            continue
        modes = action.get("modes") or []
        if not modes:
            continue
        item: dict[str, Any] = {
            "action": action["action"],
            "mode": modes[0],
        }
        for field in ("sla", "terms_ref"):
            if action.get(field) is not None:
                item[field] = action[field]
        available.append(item)
    return available


def _change_terms_for_buy(media_buy: dict[str, Any]) -> list[dict[str, Any]] | None:
    proposal = media_buy.get("accepted_proposal")
    if not isinstance(proposal, dict):
        return None
    commercial_terms = proposal.get("commercial_terms")
    if not isinstance(commercial_terms, dict) or "change_terms" not in commercial_terms:
        return None
    change_terms = commercial_terms.get("change_terms")
    return change_terms if isinstance(change_terms, list) else []


def _available_actions_for_buy(media_buy: dict[str, Any]) -> list[dict[str, Any]]:
    change_terms = _change_terms_for_buy(media_buy)
    if change_terms is not None:
        return project_available_actions(change_terms, media_buy["status"]).to_wire()
    return _resolve_available_actions(media_buy.get("packages", []), media_buy["status"])


def _attempted_action_for_update(
    params: dict[str, Any],
    mb: dict[str, Any],
) -> str | None:
    if params.get("canceled") is True:
        return "cancel"
    if params.get("paused") is True:
        return "pause"
    if params.get("paused") is False:
        return "resume"
    if params.get("end_time") is not None:
        return "extend_flight"

    existing_by_id = {p.get("package_id"): p for p in mb.get("packages", [])}
    for pkg_update in params.get("packages") or []:
        pkg_id = pkg_update.get("package_id")
        current = existing_by_id.get(pkg_id) if pkg_id else None
        if not current or pkg_update.get("budget") is None:
            continue
        current_budget = current.get("budget") or 0
        new_budget = pkg_update["budget"]
        if new_budget > current_budget:
            return "increase_budget"
        if new_budget < current_budget:
            return "decrease_budget"
    return None


def _action_not_allowed_response(
    *,
    attempted_action: str,
    reason: str,
    currently_available_actions: list[dict[str, Any]],
    compact: bool = False,
) -> dict[str, Any]:
    recovery = (
        "terminal"
        if reason in {"not_supported_on_product", "not_supported_on_buy"}
        else "correctable"
    )
    response: dict[str, Any] = {
        "errors": [
            {
                "code": "ACTION_NOT_ALLOWED",
                "message": f"Action '{attempted_action}' is not currently available",
                "recovery": recovery,
                "details": {
                    "attempted_action": attempted_action,
                    "reason": reason,
                    "currently_available_actions": currently_available_actions,
                },
            }
        ]
    }
    if compact:
        response["status"] = "failed"
    return response


def _requote_required_response(
    *,
    field: str,
    change_term_id: str,
    constraint: str,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "errors": [
            {
                "code": "REQUOTE_REQUIRED",
                "message": "Requested change exceeds the accepted commercial envelope",
                "recovery": "correctable",
                "details": {
                    "envelope_field": field,
                    "change_term_id": change_term_id,
                    "constraint": constraint,
                },
            }
        ],
    }


def _products_for_request(params: dict[str, Any]) -> list[dict[str, Any]]:
    brief = str(params.get("brief") or "").lower()
    if not brief:
        return PRODUCTS

    matches: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for product in PRODUCTS:
        product_id = str(product.get("product_id") or "")
        product_key = product_id.replace("-", " ").replace("_", " ").lower()
        product_name = str(product.get("name") or "").lower()
        if (product_key and product_key in brief) or (product_name and product_name in brief):
            matches.append(product)
        else:
            rest.append(product)
    if not matches:
        return PRODUCTS
    return matches + rest


PRODUCTS: list[dict[str, Any]] = [
    {
        "product_id": "premium-homepage",
        "name": "Homepage Takeover",
        "description": "Full-page homepage placement with 100% SOV",
        "delivery_type": "guaranteed",
        "publisher_properties": [{"publisher_domain": "example.com", "selection_type": "all"}],
        "format_ids": [{"agent_url": LEGACY_FORMAT_OWNER, "id": "display_970x250"}],
        "format_options": _image_format_options(
            format_option_id="example_billboard_970x250",
            display_name="Example.com Homepage — Billboard",
            v1_format_id="display_970x250",
            width=970,
            height=250,
        ),
        "pricing_options": [
            {
                "pricing_option_id": "po-cpm-homepage",
                "pricing_model": "cpm",
                "fixed_price": 15.00,
                "currency": "USD",
            }
        ],
        "reporting_capabilities": {
            "available_metrics": ["impressions", "spend", "clicks", "ctr"],
            "available_reporting_frequencies": ["hourly", "daily"],
            "date_range_support": "date_range",
            "supports_webhooks": False,
            "expected_delay_minutes": 60,
            "timezone": "UTC",
        },
        "delivery_measurement": {"provider": "internal"},
    },
    {
        "product_id": "run-of-site",
        "name": "Run of Site Display",
        "description": "300x250 display ads across example.com",
        "delivery_type": "non_guaranteed",
        "publisher_properties": [{"publisher_domain": "example.com", "selection_type": "all"}],
        "format_ids": [{"agent_url": LEGACY_FORMAT_OWNER, "id": "display_300x250"}],
        "format_options": _image_format_options(
            format_option_id="example_mrec_300x250",
            display_name="Example.com RoS — MREC",
            v1_format_id="display_300x250",
            width=300,
            height=250,
        ),
        "pricing_options": [
            {
                "pricing_option_id": "po-cpm-ros",
                "pricing_model": "cpm",
                "fixed_price": 5.00,
                "currency": "USD",
            }
        ],
        "reporting_capabilities": {
            "available_metrics": ["impressions", "spend", "clicks", "ctr"],
            "available_reporting_frequencies": ["hourly", "daily"],
            "date_range_support": "date_range",
            "supports_webhooks": False,
            "expected_delay_minutes": 60,
            "timezone": "UTC",
        },
        "delivery_measurement": {"provider": "internal"},
    },
    # Storyboard test fixtures referenced by @adcp/client compliance YAMLs.
    # The runner's media_buy_seller suite expects these product IDs to be
    # discoverable without an explicit seed_product call.
    {
        "product_id": "outdoor_display_q2",
        "name": "Outdoor Display Q2",
        "description": "Outdoor display inventory for Q2 storyboards",
        "delivery_type": "non_guaranteed",
        "publisher_properties": [{"publisher_domain": "example.com", "selection_type": "all"}],
        "format_ids": [{"agent_url": LEGACY_FORMAT_OWNER, "id": "display_300x250"}],
        "format_options": _image_format_options(
            format_option_id="storyboard_outdoor_display_300x250",
            display_name="Outdoor Display Q2 — MREC",
            v1_format_id="display_300x250",
            width=300,
            height=250,
        ),
        "pricing_options": [
            {
                "pricing_option_id": "cpm_standard",
                "pricing_model": "cpm",
                "fixed_price": 5.00,
                "currency": "USD",
            }
        ],
        "reporting_capabilities": {
            "available_metrics": ["impressions", "spend", "clicks", "ctr"],
            "available_reporting_frequencies": ["hourly", "daily"],
            "date_range_support": "date_range",
            "supports_webhooks": False,
            "expected_delay_minutes": 60,
            "timezone": "UTC",
        },
        "delivery_measurement": {"provider": "internal"},
    },
    {
        "product_id": "outdoor_video_q2",
        "name": "Outdoor Video Q2",
        "description": "Outdoor video inventory for Q2 storyboards",
        "delivery_type": "non_guaranteed",
        "publisher_properties": [{"publisher_domain": "example.com", "selection_type": "all"}],
        "format_ids": [{"agent_url": LEGACY_FORMAT_OWNER, "id": "display_300x250"}],
        "format_options": _image_format_options(
            format_option_id="storyboard_outdoor_video_300x250",
            display_name="Outdoor Video Q2 — MREC fallback",
            v1_format_id="display_300x250",
            width=300,
            height=250,
        ),
        "pricing_options": [
            {
                "pricing_option_id": "cpm_standard",
                "pricing_model": "cpm",
                "fixed_price": 8.00,
                "currency": "USD",
            }
        ],
        "reporting_capabilities": {
            "available_metrics": ["impressions", "spend", "clicks", "ctr"],
            "available_reporting_frequencies": ["hourly", "daily"],
            "date_range_support": "date_range",
            "supports_webhooks": False,
            "expected_delay_minutes": 60,
            "timezone": "UTC",
        },
        "delivery_measurement": {"provider": "internal"},
    },
    {
        "product_id": "sports_preroll_q2",
        "name": "Sports Preroll Q2",
        "description": "Sports preroll video inventory for Q2 storyboards",
        "delivery_type": "guaranteed",
        "publisher_properties": [{"publisher_domain": "example.com", "selection_type": "all"}],
        "format_ids": [{"agent_url": LEGACY_FORMAT_OWNER, "id": "display_970x250"}],
        "format_options": _image_format_options(
            format_option_id="storyboard_sports_preroll_970x250",
            display_name="Sports Preroll Q2 — Billboard",
            v1_format_id="display_970x250",
            width=970,
            height=250,
        ),
        "pricing_options": [
            {
                "pricing_option_id": "cpm_guaranteed",
                "pricing_model": "cpm",
                "fixed_price": 25.00,
                "currency": "USD",
            }
        ],
        "reporting_capabilities": {
            "available_metrics": ["impressions", "spend", "clicks", "ctr"],
            "available_reporting_frequencies": ["hourly", "daily"],
            "date_range_support": "date_range",
            "supports_webhooks": False,
            "expected_delay_minutes": 60,
            "timezone": "UTC",
        },
        "delivery_measurement": {"provider": "internal"},
    },
    {
        "product_id": "lifestyle_display_q2",
        "name": "Lifestyle Display Q2",
        "description": "Lifestyle display inventory for Q2 storyboards",
        "delivery_type": "non_guaranteed",
        "publisher_properties": [{"publisher_domain": "example.com", "selection_type": "all"}],
        "format_ids": [{"agent_url": LEGACY_FORMAT_OWNER, "id": "display_300x250"}],
        "format_options": _image_format_options(
            format_option_id="storyboard_lifestyle_display_300x250",
            display_name="Lifestyle Display Q2 — MREC",
            v1_format_id="display_300x250",
            width=300,
            height=250,
        ),
        "pricing_options": [
            {
                "pricing_option_id": "cpm_standard",
                "pricing_model": "cpm",
                "fixed_price": 6.00,
                "currency": "USD",
            }
        ],
        "reporting_capabilities": {
            "available_metrics": ["impressions", "spend", "clicks", "ctr"],
            "available_reporting_frequencies": ["hourly", "daily"],
            "date_range_support": "date_range",
            "supports_webhooks": False,
            "expected_delay_minutes": 60,
            "timezone": "UTC",
        },
        "delivery_measurement": {"provider": "internal"},
    },
]


class DemoSeller(ADCPHandler):
    legacy_format_converter = staticmethod(_legacy_format_converter)
    canonical_format_legacy_resolver = staticmethod(_canonical_format_legacy_resolver)

    async def get_adcp_capabilities(
        self, params: dict[str, Any], context: Any = None
    ) -> dict[str, Any]:
        response = capabilities_response(
            ["media_buy"],
            idempotency={"supported": False},
            compliance_testing={
                # AdCP 3.0.1's capabilities-response schema constrains this
                # enum to the original six scenarios. The new force_* and
                # seed_* scenarios (added to comply-test-controller-request
                # in 3.0.1) live on the dynamic list_scenarios response and
                # are reported there — not advertised here. Once the
                # capabilities schema's enum catches up, the rest land too.
                # force_session_status is schema-allowed even for media_buy
                # sellers; DemoStore provides a stub so list_scenarios
                # includes it and the storyboard runner's controller
                # detection check succeeds.
                "scenarios": [
                    "force_account_status",
                    "force_media_buy_status",
                    "force_creative_status",
                    "force_session_status",
                    "simulate_delivery",
                    "simulate_budget_spend",
                ],
            },
        )
        response["account"] = {
            "require_operator_auth": False,
            "supported_billing": ["operator", "advertiser", "agent"],
            "required_for_products": False,
            "account_financials": False,
            "sandbox": True,
        }
        response["media_buy"] = {
            "supported_pricing_models": ["cpm"],
            "buying_modes": ["brief", "refine"],
            # This compatibility fixture deliberately serves legacy storyboard
            # runners; ordinary framework construction defaults this to true.
            "features": {"canonical_creatives": False},
            "creative_sync": True,
            "reporting": True,
            "cancellation": True,
        }
        return response

    async def sync_accounts(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        results = []
        for acct in params.get("accounts", []):
            account_id = f"acct-{uuid.uuid4().hex[:8]}"
            accounts[account_id] = {
                "status": "active",
                "brand": acct.get("brand"),
                "operator": acct.get("operator"),
            }
            results.append(
                {
                    "account_id": account_id,
                    "brand": acct.get("brand"),
                    "operator": acct.get("operator"),
                    "action": "created",
                    "status": "active",
                    "account_scope": "operator_brand",
                }
            )
        return sync_accounts_response(results)

    async def sync_governance(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        results = []
        for entry in params.get("accounts", []):
            acct_ref = entry.get("account", {})
            agents = entry.get("governance_agents", [])
            results.append(
                {
                    "account": acct_ref,
                    "status": "synced",
                    "governance_agents": [{"url": a.get("url")} for a in agents],
                }
            )
        return sync_governance_response(results)

    async def get_products(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        products = _products_for_request(params)
        canonical_products = [_canonical_product(product) for product in products]
        if params.get("buying_mode") == "refine":
            proposal = params.get("proposal", {}) or {}
            proposal_id = proposal.get("proposal_id") or f"prop-{uuid.uuid4().hex[:8]}"
            incoming_packages = proposal.get("packages", []) or []
            proposals[proposal_id] = {
                "status": "draft",
                "packages": incoming_packages,
            }
            # proposal.json requires: proposal_id, name, allocations (minItems: 1).
            # Each allocation requires product_id + allocation_percentage (sum to 100).
            if incoming_packages:
                even_split = round(100 / len(incoming_packages), 2)
                allocations = [
                    {
                        "product_id": p["product_id"],
                        "allocation_percentage": even_split,
                    }
                    for p in incoming_packages
                ]
            else:
                allocations = [
                    {
                        "product_id": products[0]["product_id"],
                        "allocation_percentage": 100.0,
                    }
                ]
            return products_response(
                canonical_products,
                cache_scope="public",
                proposals=[
                    {
                        "proposal_id": proposal_id,
                        "name": proposal.get("name", "Draft proposal"),
                        "proposal_status": "draft",
                        "allocations": allocations,
                    }
                ],
            )
        return products_response(canonical_products, cache_scope="public")

    async def create_media_buy(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        account_id = (params.get("account") or {}).get("account_id") or _DEFAULT_ACCOUNT_ID
        directive = pending_directives.pop(account_id, None)
        if directive:
            arm = directive.get("arm")
            if arm == "input-required":
                # CreateMediaBuyInputRequired shape per AdCP spec.
                return {"reason": "APPROVAL_REQUIRED"}
            if arm == "submitted":
                # CreateMediaBuyResponse (submitted-task envelope) per AdCP spec.
                task_id = directive.get("task_id")
                if task_id:
                    pending_task_completions[task_id] = {
                        "state": "submitted",
                        "account_id": account_id,
                    }
                resp: dict[str, Any] = {"status": "submitted"}
                if task_id:
                    resp["task_id"] = task_id
                if directive.get("message"):
                    resp["message"] = directive["message"]
                return resp

        if not params.get("packages"):
            return adcp_error(
                "INVALID_REQUEST",
                "At least one package required",
                field="packages",
            )

        valid_ids = {p["product_id"] for p in PRODUCTS}
        packages = []
        for pkg in params["packages"]:
            product_id = pkg.get("product_id")
            if product_id not in valid_ids:
                return adcp_error(
                    "PRODUCT_NOT_FOUND",
                    f"Product '{product_id}' not found",
                    field="product_id",
                    suggestion="Use get_products to discover available products",
                )
            # Reject aggressive measurement_terms. The compliance runner
            # sends max_variance_percent=0 with a c30 window (unworkable)
            # on the rejection path, then retries with c7 + 10% variance
            # (and possibly a third-party vendor — vendor identity is
            # buyer's choice, not the seller's). Defensive coercion —
            # storyboard fixtures occasionally send measurement_terms as
            # a string or other non-dict shape; treat that as "no terms"
            # rather than crashing.
            raw_terms = pkg.get("measurement_terms")
            pkg_terms = raw_terms if isinstance(raw_terms, dict) else {}
            raw_billing = pkg_terms.get("billing_measurement")
            billing = raw_billing if isinstance(raw_billing, dict) else {}
            window = billing.get("measurement_window")
            variance = billing.get("max_variance_percent")
            if (variance is not None and variance < 5) or (
                window is not None and window not in ("c3", "c7")
            ):
                return adcp_error(
                    "TERMS_REJECTED",
                    "Measurement terms unworkable: variance must be >=5%, "
                    "measurement_window must be c3 or c7.",
                    field="measurement_terms",
                    recovery="correctable",
                )

            built_pkg: dict[str, Any] = {
                "package_id": f"pkg-{uuid.uuid4().hex[:8]}",
                "product_id": product_id,
                "pricing_option_id": pkg.get("pricing_option_id"),
                "budget": pkg.get("budget"),
            }
            # Persist caller-supplied package fields the runner expects to
            # round-trip on get_media_buys (targeting_overlay) or to drive
            # status transitions (creative_assignments, creatives,
            # measurement_terms).
            for field in (
                "targeting_overlay",
                "creative_assignments",
                "creatives",
                "measurement_terms",
                "context",
            ):
                if pkg.get(field) is not None:
                    built_pkg[field] = deepcopy(pkg[field]) if field == "context" else pkg[field]
            packages.append(built_pkg)

        has_creatives = any(
            pkg.get("creative_assignments") or pkg.get("creatives") for pkg in params["packages"]
        )
        status = "active" if has_creatives else "pending_creatives"
        available_actions = _resolve_available_actions(packages, status)

        mb_id = f"mb-{uuid.uuid4().hex[:8]}"
        confirmed_at = _now_z()
        media_buys[mb_id] = {
            "status": status,
            "currency": "USD",
            "packages": packages,
            "confirmed_at": confirmed_at,
            "revision": 1,
            "available_actions": available_actions,
        }
        if params.get("context") is not None:
            media_buys[mb_id]["context"] = deepcopy(params["context"])
        # Pull valid_actions from the SDK's authoritative state machine —
        # tracks any future spec churn without manual list maintenance.
        resp = media_buy_response(
            mb_id,
            packages,
            status=status,
            revision=1,
            confirmed_at=confirmed_at,
            valid_actions=valid_actions_for_status(status) or None,
        )
        if available_actions:
            resp["available_actions"] = available_actions
        return resp

    async def get_media_buys(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        requested_ids = params.get("media_buy_ids")
        results = []
        for mb_id, mb in media_buys.items():
            if requested_ids and mb_id not in requested_ids:
                continue
            total_budget = sum((pkg.get("budget") or 0) for pkg in mb.get("packages", []))
            result = {
                "media_buy_id": mb_id,
                "status": mb["status"],
                "confirmed_at": mb.get("confirmed_at") or _now_z(),
                "revision": mb.get("revision", 1),
                "currency": mb.get("currency", "USD"),
                "packages": mb.get("packages", []),
                "total_budget": total_budget,
                "valid_actions": valid_actions_for_status(mb["status"]),
                **_health_fields_for_media_buy(mb_id, mb),
            }
            if mb.get("context") is not None:
                result["context"] = mb["context"]
            available_actions = _available_actions_for_buy(mb)
            if available_actions:
                result["available_actions"] = available_actions
            if mb.get("accepted_proposal") is not None:
                accepted_proposal = deepcopy(mb["accepted_proposal"])
                result["accepted_proposal"] = accepted_proposal
                result["accepted_proposal_id"] = accepted_proposal["proposal_id"]
                result["accepted_proposal_terms_digest"] = accepted_proposal["terms_digest"]
            results.append(result)
        return media_buys_response(results)

    async def control_media_buy(
        self, params: dict[str, Any], context: Any = None
    ) -> dict[str, Any]:
        mb_id = params.get("media_buy_id")
        mb = media_buys.get(mb_id) if isinstance(mb_id, str) else None
        if mb is None or not isinstance(mb_id, str):
            error = adcp_error("MEDIA_BUY_NOT_FOUND", "Media buy not found")
            return {"status": "failed", **error}

        revision = mb.get("revision", 1)
        if params.get("revision") != revision:
            error = adcp_error("CONFLICT", "Revision mismatch - refetch and retry")
            return {"status": "failed", **error}

        current = deepcopy(mb)
        current["available_actions"] = _available_actions_for_buy(mb)
        proposal = mb.get("accepted_proposal")
        assessments = assess_update_media_buy_actions(
            params,
            current,
            proposal=proposal,
        )
        attempted = assessments[0] if assessments else None
        if attempted is None:
            error = adcp_error("INVALID_REQUEST", "No supported control field supplied")
            return {"status": "failed", **error}

        violated = next(
            (check for check in attempted.constraints if check.outcome.value == "violated"),
            None,
        )
        if violated is not None:
            return _requote_required_response(
                field=(
                    "total_budget.amount"
                    if attempted.action in {"increase_budget", "decrease_budget"}
                    else violated.field or "control"
                ),
                change_term_id=attempted.change_term_id or "unknown",
                constraint=violated.constraint,
            )

        if attempted.status is not ActionAvailabilityStatus.available_now:
            term = next(
                (
                    value
                    for value in (_change_terms_for_buy(mb) or [])
                    if value.get("action") == attempted.action
                ),
                None,
            )
            if (
                term is not None
                and term.get("allowed_statuses")
                and mb["status"] not in term["allowed_statuses"]
            ):
                reason = "wrong_status"
            elif term is not None and term.get("conditions"):
                reason = "condition_unresolved"
            else:
                reason = "not_supported_on_buy"
            return _action_not_allowed_response(
                attempted_action=attempted.action,
                reason=reason,
                currently_available_actions=current["available_actions"],
                compact=True,
            )

        if params.get("paused") is True:
            assert_media_buy_transition(mb["status"], "paused", media_buy_id=mb_id)
            mb["status"] = "paused"
        elif params.get("paused") is False:
            assert_media_buy_transition(mb["status"], "active", media_buy_id=mb_id)
            mb["status"] = "active"
        elif params.get("canceled") is True:
            assert_media_buy_transition(mb["status"], "canceled", media_buy_id=mb_id)
            mb["status"] = "canceled"
        if "total_budget" in params:
            mb["total_budget"] = deepcopy(params["total_budget"])

        mb["revision"] = revision + 1
        mb["available_actions"] = _available_actions_for_buy(mb)
        return {
            "status": "completed",
            "media_buy_id": mb_id,
            "revision": mb["revision"],
            "media_buy_status": mb["status"],
            "available_actions": mb["available_actions"],
        }

    async def update_media_buy(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        mb_id = params.get("media_buy_id")
        mb = media_buys.get(mb_id) if mb_id else None
        if not mb or not mb_id:
            if any(pkg.get("package_id") for pkg in params.get("packages") or []):
                return adcp_error(
                    "PACKAGE_NOT_FOUND",
                    f"Package not found in media buy {mb_id}",
                    field="package_id",
                )
            return adcp_error("MEDIA_BUY_NOT_FOUND", f"Media buy {mb_id} not found")

        if params.get("revision") and params["revision"] != mb.get("revision", 1):
            return adcp_error("CONFLICT", "Revision mismatch - refetch and retry")

        product_actions = _allowed_actions_for_packages(mb.get("packages", []))
        attempted_action = _attempted_action_for_update(params, mb)
        if product_actions and attempted_action:
            currently_available = mb.get("available_actions") or _resolve_available_actions(
                mb.get("packages", []),
                mb["status"],
            )
            available_by_action = {a.get("action"): a for a in currently_available}
            product_by_action = {a.get("action"): a for a in product_actions}
            if attempted_action not in product_by_action:
                return _action_not_allowed_response(
                    attempted_action=attempted_action,
                    reason="not_supported_on_product",
                    currently_available_actions=currently_available,
                )
            available = available_by_action.get(attempted_action)
            if available is None:
                return _action_not_allowed_response(
                    attempted_action=attempted_action,
                    reason="wrong_status",
                    currently_available_actions=currently_available,
                )
            if available.get("mode") != "self_serve":
                return _action_not_allowed_response(
                    attempted_action=attempted_action,
                    reason="mode_mismatch",
                    currently_available_actions=currently_available,
                )

        if params.get("packages"):
            existing_by_id = {p["package_id"]: p for p in mb.get("packages", [])}
            affected_packages = []
            for pkg_update in params["packages"]:
                pkg_id = pkg_update.get("package_id")
                if pkg_id and pkg_id not in existing_by_id:
                    return adcp_error(
                        "PACKAGE_NOT_FOUND",
                        f"Package '{pkg_id}' not found in media buy {mb_id}",
                        field="package_id",
                    )
                # Apply incoming targeting/budget/creative deltas to the
                # persisted package so a subsequent get_media_buys reflects
                # the change. Storyboard inventory_list_targeting/update
                # asserts targeting_overlay round-trips through this path.
                if pkg_id and pkg_id in existing_by_id:
                    target = existing_by_id[pkg_id]
                    for field in (
                        "targeting_overlay",
                        "creative_assignments",
                        "creatives",
                        "measurement_terms",
                        "budget",
                    ):
                        if pkg_update.get(field) is not None:
                            target[field] = pkg_update[field]
                    affected_packages.append(deepcopy(target))
        else:
            affected_packages = []

        status = mb["status"]
        if status == "pending_creatives" and params.get("packages"):
            if any(
                pkg.get("creative_assignments") or pkg.get("creatives")
                for pkg in params["packages"]
            ):
                mb["status"] = "active"
                status = "active"
                mb["available_actions"] = _resolve_available_actions(
                    mb.get("packages", []),
                    status,
                )
        if params.get("paused") is True and status == "active":
            mb["status"] = "paused"
            mb["available_actions"] = _resolve_available_actions(mb.get("packages", []), "paused")
        elif params.get("paused") is False and status == "paused":
            mb["status"] = "active"
            mb["available_actions"] = _resolve_available_actions(mb.get("packages", []), "active")
        elif params.get("canceled") is True:
            if status in ("completed", "rejected", "canceled"):
                return adcp_error("NOT_CANCELLABLE", f"Cannot cancel a {status} media buy")
            mb["status"] = "canceled"
            mb["available_actions"] = []
            mb["revision"] = mb.get("revision", 1) + 1
            return cancel_media_buy_response(mb_id, "buyer", revision=mb["revision"])

        mb["revision"] = mb.get("revision", 1) + 1
        resp = update_media_buy_response(
            mb_id,
            affected_packages=affected_packages or None,
            status=mb["status"],
            revision=mb["revision"],
            valid_actions=valid_actions_for_status(mb["status"]) or None,
        )
        if mb.get("available_actions"):
            resp["available_actions"] = mb["available_actions"]
        return resp

    async def list_creative_formats_legacy(
        self, params: dict[str, Any], context: Any = None
    ) -> dict[str, Any]:
        all_formats: list[dict[str, Any]] = [
            {
                "format_id": {
                    "agent_url": LEGACY_FORMAT_OWNER,
                    "id": "display_300x250",
                },
                "name": "Display 300x250",
                "renders": [{"role": "primary", "dimensions": {"width": 300, "height": 250}}],
                "assets": [
                    {
                        "item_type": "individual",
                        "asset_id": "image",
                        "asset_type": "image",
                        "required": True,
                        "accepted_media_types": [
                            "image/png",
                            "image/jpeg",
                        ],
                    }
                ],
            },
            {
                "format_id": {
                    "agent_url": LEGACY_FORMAT_OWNER,
                    "id": "display_970x250",
                },
                "name": "Display 970x250",
                "renders": [{"role": "primary", "dimensions": {"width": 970, "height": 250}}],
                "assets": [
                    {
                        "item_type": "individual",
                        "asset_id": "image",
                        "asset_type": "image",
                        "required": True,
                        "accepted_media_types": [
                            "image/png",
                            "image/jpeg",
                        ],
                    }
                ],
            },
        ]
        all_formats = all_formats + list(seeded_creative_formats.values())
        filter_ids = params.get("format_ids")
        if filter_ids:
            wanted = {(fid.get("agent_url"), fid["id"]) for fid in filter_ids if "id" in fid}
            formats = [
                f
                for f in all_formats
                if (f["format_id"].get("agent_url"), f["format_id"]["id"]) in wanted
            ]
        else:
            formats = all_formats
        return legacy_creative_formats_response(formats)

    async def sync_creatives(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        results = []
        for c in params.get("creatives", []):
            creative_id = c.get("creative_id") or f"c-{uuid.uuid4().hex[:8]}"
            creatives[creative_id] = {**c, "status": "approved", "status_changed_at": _now_z()}
            results.append(
                {
                    "creative_id": creative_id,
                    "action": "created",
                }
            )
        # Transition any media buys waiting on creatives to pending_start
        # now that creatives are approved (storyboard creative_fate_after_sync
        # asserts this). Real sellers would scope by media_buy_id linkage —
        # the example uses a single-tenant simplification.
        for mb in media_buys.values():
            if mb.get("status") == "pending_creatives":
                mb["status"] = "pending_start"
                mb["revision"] = mb.get("revision", 1) + 1
                mb.setdefault("confirmed_at", _now_z())
        return sync_creatives_response(results)

    async def list_creatives(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        filters = params.get("filters") or {}
        requested_ids = set(filters.get("creative_ids") or params.get("creative_ids") or [])
        requested_statuses = set(filters.get("statuses") or [])
        results: list[Creative] = []
        format_declarations: list[Format] = []
        for creative_id, creative in creatives.items():
            if requested_ids and creative_id not in requested_ids:
                continue
            listed = dict(creative)
            listed.setdefault("status", "approved")
            if requested_statuses and listed["status"] not in requested_statuses:
                continue
            canonical, declaration = _canonical_listed_creative(creative_id, listed)
            results.append(canonical)
            format_declarations.append(declaration)
        return list_creatives_response(results, format_declarations=format_declarations)

    async def get_media_buy_delivery(
        self, params: dict[str, Any], context: Any = None
    ) -> dict[str, Any]:
        requested_ids = params.get("media_buy_ids", [])
        deliveries = []
        for mb_id in requested_ids:
            if mb_id in media_buys:
                deliveries.append(
                    {
                        "media_buy_id": mb_id,
                        "status": "active",
                        "totals": {
                            "impressions": 45000,
                            "clicks": 680,
                            "spend": 540.00,
                            "viewability": {
                                "measurable_impressions": 42000,
                                "viewable_impressions": 31500,
                                "viewable_rate": 0.75,
                                "viewed_seconds": 12.5,
                                "standard": "mrc",
                            },
                        },
                        "by_package": [],
                    }
                )
        return delivery_response(
            deliveries,
            reporting_period={
                "start": "2026-04-01T00:00:00Z",
                "end": "2026-04-09T23:59:59Z",
            },
        )


class DemoStore(TestControllerStore):
    async def force_account_status(self, account_id: str, status: str) -> dict[str, Any]:
        acct = accounts.get(account_id)
        if not acct:
            raise TestControllerError("NOT_FOUND", f"Account {account_id} not found")
        prev = acct["status"]
        acct["status"] = status
        return {"previous_state": prev, "current_state": status}

    async def force_media_buy_status(
        self,
        media_buy_id: str,
        status: str,
        rejection_reason: str | None = None,
    ) -> dict[str, Any]:
        mb = media_buys.get(media_buy_id)
        if not mb:
            raise TestControllerError("NOT_FOUND", f"Media buy {media_buy_id} not found")
        prev = mb["status"]
        if prev in ("completed", "rejected", "canceled"):
            raise TestControllerError(
                "INVALID_TRANSITION",
                f"Cannot transition from {prev}",
                current_state=prev,
            )
        mb["status"] = status
        return {"previous_state": prev, "current_state": status}

    async def force_creative_status(
        self,
        creative_id: str,
        status: str,
        rejection_reason: str | None = None,
    ) -> dict[str, Any]:
        c = creatives.get(creative_id)
        if not c:
            c = {
                "creative_id": creative_id,
                "name": creative_id,
                "format_id": {
                    "agent_url": LEGACY_FORMAT_OWNER,
                    "id": "display_300x250",
                },
                "status": "unknown",
            }
            creatives[creative_id] = c
        prev = c.get("status", "unknown")
        if prev == "archived":
            raise TestControllerError(
                "INVALID_TRANSITION",
                "Cannot transition from archived",
                current_state=prev,
            )
        c["status"] = status
        c["status_changed_at"] = _now_z()
        return {"previous_state": prev, "current_state": status}

    async def simulate_delivery(
        self,
        media_buy_id: str,
        impressions: int | None = None,
        clicks: int | None = None,
        conversions: int | None = None,
        reported_spend: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if media_buy_id not in media_buys:
            raise TestControllerError("NOT_FOUND", f"Media buy {media_buy_id} not found")
        simulated: dict[str, Any] = {"media_buy_id": media_buy_id}
        if impressions is not None:
            simulated["impressions"] = impressions
        if clicks is not None:
            simulated["clicks"] = clicks
        if conversions is not None:
            simulated["conversions"] = conversions
        if reported_spend is not None:
            simulated["reported_spend"] = reported_spend
        return {"simulated": simulated, "cumulative": simulated}

    async def simulate_budget_spend(
        self,
        spend_percentage: float,
        account_id: str | None = None,
        media_buy_id: str | None = None,
    ) -> dict[str, Any]:
        return {"simulated": {"spend_percentage": spend_percentage}}

    async def force_session_status(
        self,
        session_id: str,
        status: str,
        termination_reason: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        # DemoSeller has no SI session state; return a canned transition so
        # the storyboard runner's controller-detection probe succeeds and the
        # force_session_status storyboard can run (it will simply report the
        # canned previous_state).
        return {"previous_state": "active", "current_state": status}

    async def force_create_media_buy_arm(
        self,
        arm: str,
        task_id: str | None = None,
        message: str | None = None,
        *,
        account: dict[str, Any] | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        account_id = (account or {}).get("account_id") or _DEFAULT_ACCOUNT_ID
        pending_directives[account_id] = {"arm": arm, "task_id": task_id, "message": message}
        forced: dict[str, Any] = {"arm": arm}
        if arm == "submitted" and task_id:
            forced["task_id"] = task_id
        return {"success": True, "forced": forced}

    async def force_task_completion(
        self,
        task_id: str,
        result: dict[str, Any],
        *,
        account: dict[str, Any] | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        task = pending_task_completions.get(task_id)
        if task is None:
            raise TestControllerError("NOT_FOUND", f"Task {task_id} not found")
        caller_id = (account or {}).get("account_id") or _DEFAULT_ACCOUNT_ID
        if task.get("account_id", _DEFAULT_ACCOUNT_ID) != caller_id:
            raise TestControllerError("NOT_FOUND", f"Task {task_id} not found")
        prev = task.get("state", "submitted")
        if prev == "completed":
            if task.get("result") != result:
                raise TestControllerError(
                    "INVALID_TRANSITION",
                    "Task already completed with different result",
                    current_state="completed",
                )
            return {
                "success": True,
                "previous_state": task.get("previous_state", "submitted"),
                "current_state": "completed",
            }
        pending_task_completions[task_id] = {
            **task,
            "state": "completed",
            "result": result,
            "previous_state": prev,
        }
        return {"success": True, "previous_state": prev, "current_state": "completed"}

    async def seed_product(
        self,
        fixture: dict[str, Any] | None = None,
        product_id: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        data = dict(fixture or {})
        pid = product_id or data.get("product_id") or f"seeded-{uuid.uuid4().hex[:8]}"
        data["product_id"] = pid
        # Filter ``channels`` to spec-valid values from the canonical
        # ``MediaChannelSchema`` enum. Upstream storyboard fixtures
        # occasionally ship legacy names like ``"video"`` that aren't
        # in the enum; surfacing them through get_products would fail
        # strict response validation.
        if "channels" in data:
            valid = [c for c in data.get("channels") or [] if c in _VALID_CHANNELS]
            if valid:
                data["channels"] = valid
            else:
                data.pop("channels", None)
        # Ensure schema-required fields are present so downstream validation
        # passes even when the runner sends a minimal fixture with only
        # product_id. Defaults are spec-valid (non-empty arrays where
        # ``minItems: 1`` applies, format_ids carrying agent_url) so the
        # storyboard runner's get-products-response.json validation succeeds
        # against any product the runner seeds.
        data.setdefault("name", pid)
        data.setdefault("description", f"Seeded product {pid}")
        data.setdefault("delivery_type", "non_guaranteed")
        data.setdefault(
            "publisher_properties",
            [{"publisher_domain": "example.com", "selection_type": "all"}],
        )
        data.setdefault(
            "format_ids",
            [{"agent_url": LEGACY_FORMAT_OWNER, "id": "display_300x250"}],
        )
        # Normalize any caller-supplied format_ids items that omit
        # agent_url. Storyboard fixtures commonly send
        # ``format_ids: [{"id": "..."}]`` — the bare id without the
        # canonical agent_url. The schema requires both fields, so fill
        # in the local AGENT_URL when missing.
        data["format_ids"] = [
            (
                {**fmt, "agent_url": fmt.get("agent_url") or LEGACY_FORMAT_OWNER}
                if isinstance(fmt, dict)
                else fmt
            )
            for fmt in data["format_ids"]
        ]
        if not data.get("format_options"):
            data["format_options"] = _seeded_format_options(
                product_id=pid,
                name=data["name"],
                format_ids=data["format_ids"],
            )
        data.setdefault(
            "pricing_options",
            [
                {
                    "pricing_option_id": f"storyboard_{pid}_cpm",
                    "pricing_model": "cpm",
                    "currency": "USD",
                    "fixed_price": 5.0,
                }
            ],
        )
        data.setdefault(
            "reporting_capabilities",
            {
                "available_metrics": ["impressions", "spend"],
                "available_reporting_frequencies": ["hourly", "daily"],
                "date_range_support": "date_range",
                "supports_webhooks": False,
                "expected_delay_minutes": 60,
                "timezone": "UTC",
            },
        )
        data.setdefault("delivery_measurement", {"provider": "internal"})
        for i, p in enumerate(PRODUCTS):
            if p.get("product_id") == pid:
                PRODUCTS[i] = data
                return {"product_id": pid}
        PRODUCTS.append(data)
        return {"product_id": pid}

    async def seed_pricing_option(
        self,
        fixture: dict[str, Any] | None = None,
        product_id: str | None = None,
        pricing_option_id: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        data = dict(fixture or {})
        po_id = (
            pricing_option_id
            or data.get("pricing_option_id")
            or f"po-seeded-{uuid.uuid4().hex[:8]}"
        )
        data["pricing_option_id"] = po_id
        for prod in PRODUCTS:
            if product_id and prod.get("product_id") != product_id:
                continue
            options: list[dict[str, Any]] = prod.setdefault("pricing_options", [])
            for i, opt in enumerate(options):
                if opt.get("pricing_option_id") == po_id:
                    options[i] = data
                    return {"pricing_option_id": po_id}
            options.append(data)
            return {"pricing_option_id": po_id}
        raise TestControllerError("NOT_FOUND", f"Product '{product_id}' not found")

    async def seed_creative(
        self,
        fixture: dict[str, Any] | None = None,
        creative_id: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        data = dict(fixture or {})
        cid = creative_id or data.get("creative_id") or f"c-seeded-{uuid.uuid4().hex[:8]}"
        data["creative_id"] = cid
        data.setdefault("status_changed_at", _now_z())
        creatives[cid] = data
        return {"creative_id": cid}

    async def seed_plan(
        self,
        fixture: dict[str, Any] | None = None,
        plan_id: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        data = dict(fixture or {})
        pid = plan_id or data.get("plan_id") or f"plan-seeded-{uuid.uuid4().hex[:8]}"
        data["plan_id"] = pid
        plans[pid] = data
        return {"plan_id": pid}

    async def seed_media_buy(
        self,
        fixture: dict[str, Any] | None = None,
        media_buy_id: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        data = dict(fixture or {})
        mb_id = media_buy_id or data.get("media_buy_id") or f"mb-seeded-{uuid.uuid4().hex[:8]}"
        data["media_buy_id"] = mb_id
        data.setdefault("status", "active")
        data.setdefault("currency", "USD")
        data.setdefault("packages", [])
        data.setdefault("confirmed_at", _now_z())
        data.setdefault("revision", 1)
        data["available_actions"] = _available_actions_for_buy(data)
        media_buys[mb_id] = data
        return {"media_buy_id": mb_id}

    async def seed_creative_format(
        self,
        fixture: dict[str, Any] | None = None,
        format_id: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        data = dict(fixture or {})
        fid = (
            format_id
            or (data.get("format_id") or {}).get("id")
            or f"fmt-seeded-{uuid.uuid4().hex[:8]}"
        )
        data.setdefault("format_id", {"agent_url": LEGACY_FORMAT_OWNER, "id": fid})
        data.setdefault("name", fid)
        data.setdefault("renders", [])
        data.setdefault("assets", [])
        seeded_creative_formats[fid] = data
        return {"format_id": fid}


if __name__ == "__main__":
    serve(
        DemoSeller(),
        name="demo-seller",
        port=PORT,
        test_controller=DemoStore(),
        # Demo example: bypass the comply_test_controller sandbox-mode gate
        # so storyboard runs work without an Account.mode-aware AccountStore.
        # Production sellers MUST populate Account.mode (live/sandbox/mock) on
        # resolved accounts and let the framework's gate enforce it.
        test_controller_account_resolver=INSECURE_ALLOW_ALL,
    )
