# ruff: noqa: E501
"""MCP server integration helpers.

Provides utilities for registering ADCP handlers with MCP servers.

.. note::
    Function signatures in this module use ``ADCPHandler[Any]`` rather
    than a propagated ``TContext`` TypeVar. The rationale: these
    functions (``get_tools_for_handler``, ``create_mcp_tools``, etc.)
    treat the handler opaquely — they walk the MRO and dispatch by tool
    name without ever touching the ``context`` argument's typed fields.
    Binding a TypeVar here would force callers to narrow at the call
    site for no runtime benefit, and cascade the TypeVar through every
    plumbing function in :mod:`adcp.server.serve`. ``Any`` keeps the
    plumbing honest: the static type says "this code works with any
    ``ToolContext`` subclass," which is exactly true.
"""

from __future__ import annotations

import copy
import difflib
import logging
from collections.abc import Callable, Iterable
from typing import Any

from adcp.server._hooks import (
    PreValidationHookChain,
    PreValidationHookError,
    PreValidationHooks,
    _apply_pre_validation_hooks,
    _flatten_pre_validation_hooks,
)
from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.helpers import ResponseEnhancer, _apply_response_enhancer
from adcp.server.test_controller import SCENARIOS as _CONTROLLER_SCENARIOS
from adcp.types import (
    MEDIA_BUY_LEGACY_STATUS_VALUES,
    unwrap_enum_value,
)
from adcp.types.error_narrowing import narrow_union_errors
from adcp.validation.client_hooks import UnknownFieldPolicy, ValidationHookConfig
from adcp.validation.envelope import DEFAULT_UNNEGOTIATED_ADCP_VERSION
from adcp.validation.schema_loader import (
    get_mcp_schema,
    get_validator,
    list_validator_keys,
)

logger = logging.getLogger(__name__)


def _looks_like_sync_media_buy_success(method_name: str, result: dict[str, Any]) -> bool:
    return (
        method_name in {"create_media_buy", "update_media_buy"}
        and "media_buy_id" in result
        and "errors" not in result
        and "task_id" not in result
    )


def _is_adcp_31_or_newer(version: str | None) -> bool:
    if version is None:
        return True
    try:
        major_text, rest = version.split(".", 1)
        release_text = rest.split("-", 1)[0].split(".", 1)[0]
        return int(major_text) > 3 or (int(major_text) == 3 and int(release_text) >= 1)
    except (AttributeError, TypeError, ValueError):
        return True


def _normalize_sync_media_buy_response(
    result: dict[str, Any],
    *,
    adcp_version: str | None,
) -> None:
    raw_status = unwrap_enum_value(result.get("status"))
    media_buy_status = unwrap_enum_value(result.get("media_buy_status"))

    if _is_adcp_31_or_newer(adcp_version):
        if raw_status is not None:
            result["status"] = raw_status
        if media_buy_status is not None:
            result["media_buy_status"] = media_buy_status
        if media_buy_status is None and raw_status in MEDIA_BUY_LEGACY_STATUS_VALUES:
            result["media_buy_status"] = raw_status
            result["status"] = "completed"
        elif media_buy_status is not None and raw_status in {None, media_buy_status}:
            result["status"] = "completed"
        return

    # 3.0 buyers expect the media-buy lifecycle status at top-level
    # ``status``. This branch is intentionally narrow to create/update
    # success payloads so the normal task envelope can still be applied to
    # every other 3.1+ response.
    if media_buy_status is not None:
        result["status"] = media_buy_status
        result.pop("media_buy_status", None)
    elif raw_status is not None:
        result["status"] = raw_status


def _normalize_response_envelope(
    method_name: str,
    result: dict[str, Any],
    raw_params: dict[str, Any],
    *,
    adcp_version: str | None = None,
) -> None:
    """Populate beta 3 envelope defaults before serialization/validation.

    AdCP 3.1 requires ``status`` on every response and requires
    ``cache_scope`` on products/signals cacheable reads. The SDK can safely
    infer the public cache only when the request has no account. Account-scoped
    wholesale reads must be explicit so a seller doesn't accidentally label
    account-specific inventory as globally cacheable.
    """
    is_sync_media_buy_success = _looks_like_sync_media_buy_success(method_name, result)
    if is_sync_media_buy_success:
        _normalize_sync_media_buy_response(result, adcp_version=adcp_version)

    if "status" not in result and "task_id" not in result:
        if not (is_sync_media_buy_success and not _is_adcp_31_or_newer(adcp_version)):
            result["status"] = "completed"
    if (
        method_name in {"get_products", "get_signals"}
        and "cache_scope" not in result
        and raw_params.get("account") is None
    ):
        result["cache_scope"] = "public"


def _widen_media_buy_output_schema_for_legacy_statuses(
    tool_name: str,
    schema: dict[str, Any],
) -> None:
    """Advertise both 3.1 and negotiated 3.0 media-buy success shapes.

    The Pydantic success arm normalizes legacy lifecycle ``status`` values
    into ``media_buy_status`` at model-validation time, so its generated JSON
    schema naturally advertises only ``status: "completed"``. MCP
    ``outputSchema`` validates the actual negotiated wire dict, where a 3.0
    buyer legitimately receives ``status: "pending_creatives"`` with no
    ``media_buy_status``. Widen only the advertised schema; the runtime model
    remains the canonical 3.1 ergonomic shape.
    """
    if tool_name not in {"create_media_buy", "update_media_buy"}:
        return
    variants = schema.get("anyOf") or schema.get("oneOf") or []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        required = variant.get("required")
        properties = variant.get("properties")
        if not isinstance(required, list) or not isinstance(properties, dict):
            continue
        if "media_buy_id" not in required or "status" not in properties:
            continue

        status_schema = properties.get("status")
        if not (isinstance(status_schema, dict) and status_schema.get("const") == "completed"):
            continue

        variant["required"] = [field for field in required if field != "status"]
        properties["status"] = {
            "anyOf": [
                {"const": "completed", "type": "string"},
                {
                    "enum": sorted(MEDIA_BUY_LEGACY_STATUS_VALUES),
                    "title": "MediaBuyStatus",
                    "type": "string",
                },
            ],
            "description": (
                "Task envelope status for AdCP 3.1+ sync responses, or the "
                "media-buy lifecycle status for AdCP 3.0 compatibility."
            ),
        }


# MCP ToolAnnotations — behavioral hints for agent planning.
# RO = read-only (safe to call speculatively)
# MUT = mutating (creates or changes state)
# DEST = destructive (deletes state, not easily reversible)
# IDEMP = idempotent (safe to retry / call multiple times)
_RO: dict[str, bool] = {"readOnlyHint": True, "idempotentHint": True}
_MUT: dict[str, bool] = {"readOnlyHint": False, "destructiveHint": False}
_DEST: dict[str, bool] = {"readOnlyHint": False, "destructiveHint": True}
_IDEMP: dict[str, bool] = {"readOnlyHint": False, "idempotentHint": True}

# Tool definitions for all ADCP operations
ADCP_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    # Core Catalog Operations
    {
        "name": "get_products",
        "description": (
            "Search available advertising products matching campaign requirements. "
            "Returns products with pricing, formats, and delivery options. "
            "Use buying_mode='brief' for natural language or 'refine' for proposal negotiation. "
            "Products include product_ids needed for create_media_buy."
        ),
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "context": {"type": "object"},
                "filters": {"type": "object"},
                "pagination": {"type": "object"},
                "fields": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "list_products",
        "description": "List products using the AdCP 3.2 compact discovery lifecycle.",
        "annotations": _RO,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "request_proposals",
        "description": "Request seller proposals for selected products.",
        "annotations": _MUT,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "refine_proposals",
        "description": "Refine one or more seller proposals.",
        "annotations": _MUT,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "decline_proposals",
        "description": "Decline one or more seller proposals.",
        "annotations": _MUT,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_creative_formats",
        "description": "List available creative formats with asset requirements. Returns format_ids needed for sync_creatives.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "format_id": {"type": "string"},
                "pagination": {"type": "object"},
            },
        },
    },
    # Creative Operations
    {
        "name": "sync_creatives",
        "description": "Upload or update creative assets for a media buy. Idempotent: re-sending the same creative_id updates it. Returns approval status per creative.",
        "annotations": _IDEMP,
        "inputSchema": {
            "type": "object",
            "properties": {
                "creatives": {"type": "array"},
            },
            "required": ["creatives"],
        },
    },
    {
        "name": "list_creatives",
        "description": "List synced creatives with optional filtering by status, format, or media buy.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "filters": {"type": "object"},
                "pagination": {"type": "object"},
                "fields": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "build_creative",
        "description": "Generate a creative from a brief and brand assets. Returns a creative manifest with rendered assets.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "format_id": {"type": "string"},
                "assets": {"type": "array"},
            },
            "required": ["format_id", "assets"],
        },
    },
    {
        "name": "preview_creative",
        "description": "Preview a creative rendering before going live. Returns preview URLs or HTML for visual verification.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "format_id": {"type": "string"},
                "creative_manifest": {"type": "object"},
                "output_format": {"type": "string"},
            },
        },
    },
    {
        "name": "get_creative_delivery",
        "description": "Get creative delivery tags (VAST, HTML, etc.) for serving. Use after creatives are approved.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "media_buy_ids": {"type": "array", "items": {"type": "string"}},
                "creative_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "list_transformers",
        "description": "List creative transformers available for converting or adapting creative assets.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "transformer_ids": {"type": "array", "items": {"type": "string"}},
                "input_format_ids": {"type": "array"},
                "output_format_ids": {"type": "array"},
                "name_search": {"type": "string"},
                "brief": {"type": "string"},
                "expand_params": {"type": "array", "items": {"type": "string"}},
                "expand_pagination": {"type": "array"},
                "include_pricing": {"type": "boolean"},
                "account": {"type": "object"},
                "pagination": {"type": "object"},
            },
        },
    },
    # Media Buy Operations
    {
        "name": "buy_products",
        "description": "Commit a direct purchase of selected products.",
        "annotations": _MUT,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "accept_proposal",
        "description": "Accept a seller proposal and create its media buy.",
        "annotations": _MUT,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "control_media_buy",
        "description": "Apply pause, resume, cancel, budget, or other lifecycle controls.",
        "annotations": _MUT,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_media_buy",
        "description": "Create a new media buy with packages. Each package references a product_id from get_products and a pricing_option_id. Returns media_buy_id for tracking.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "packages": {"type": "array"},
                "proposal_id": {"type": "string"},
            },
        },
    },
    {
        "name": "update_media_buy",
        "description": "Update an existing media buy: pause, resume, cancel, or modify packages and budget. Requires revision for optimistic concurrency.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "media_buy_id": {"type": "string"},
                "packages": {"type": "array"},
            },
            "required": ["media_buy_id"],
        },
    },
    {
        "name": "get_media_buy_delivery",
        "description": "Get delivery metrics (impressions, clicks, spend) for active media buys. Returns totals and per-package breakdowns.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "media_buy_id": {"type": "string"},
                "metrics": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["media_buy_id"],
        },
    },
    {
        "name": "get_media_buys",
        "description": "List media buys with status, packages, and optional delivery snapshots. Filter by media_buy_ids.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "object"},
                "media_buy_ids": {"type": "array", "items": {"type": "string"}},
                "status_filter": {"type": "array", "items": {"type": "string"}},
                "pagination": {"type": "object"},
            },
        },
    },
    # Signal Operations
    {
        "name": "get_signals",
        "description": "Discover available audience signals for targeting. Use signal_spec for natural language search or signal_ids for exact lookup.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "filters": {"type": "object"},
                "pagination": {"type": "object"},
            },
        },
    },
    {
        "name": "activate_signal",
        "description": "Activate an audience signal to a destination (DSP platform or sales agent). Returns deployment status and activation keys.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "signal_id": {"type": "string"},
                "activation_key": {"type": "string"},
            },
            "required": ["signal_id"],
        },
    },
    # Account Operations
    {
        "name": "list_accounts",
        "description": "List advertiser accounts on this seller. Returns account_ids needed for other operations.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "filters": {"type": "object"},
                "pagination": {"type": "object"},
            },
        },
    },
    {
        "name": "sync_accounts",
        "description": "Create or update advertiser accounts. Idempotent: re-sending the same brand/operator pair updates the existing account.",
        "annotations": _IDEMP,
        "inputSchema": {
            "type": "object",
            "properties": {
                "accounts": {"type": "array"},
            },
            "required": ["accounts"],
        },
    },
    {
        "name": "get_account_financials",
        "description": "Get financial details for an account including balance, credit, and payment status.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "object"},
                "date_range": {"type": "object"},
            },
        },
    },
    {
        "name": "report_usage",
        "description": "Report usage metrics for billing reconciliation.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "object"},
                "usage": {"type": "array"},
            },
            "required": ["usage"],
        },
    },
    # Event Operations
    {
        "name": "log_event",
        "description": "Log conversion events (purchases, leads, etc.) for attribution and optimization.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "events": {"type": "array"},
            },
            "required": ["events"],
        },
    },
    {
        "name": "sync_event_sources",
        "description": "Register conversion tracking pixels or event endpoints. Idempotent.",
        "annotations": _IDEMP,
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_sources": {"type": "array"},
            },
            "required": ["event_sources"],
        },
    },
    {
        "name": "sync_audiences",
        "description": "Upload audience segments for targeting. Idempotent: re-sending updates existing segments.",
        "annotations": _IDEMP,
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "object"},
                "audiences": {"type": "array"},
            },
            "required": ["audiences"],
        },
    },
    {
        "name": "sync_catalogs",
        "description": "Upload product catalogs for dynamic ads. Supports multiple catalog types (product, store, hotel, etc.).",
        "annotations": _IDEMP,
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "object"},
                "catalogs": {"type": "array"},
            },
            "required": ["catalogs"],
        },
    },
    # Governance Sync
    {
        "name": "sync_governance",
        "description": "Register governance agents for accounts. Idempotent.",
        "annotations": _IDEMP,
        "inputSchema": {
            "type": "object",
            "properties": {
                "accounts": {"type": "array"},
            },
            "required": ["accounts"],
        },
    },
    # Feedback Operations
    {
        "name": "provide_performance_feedback",
        "description": "Send conversion or performance data back to the seller for optimization. Reference by media_buy_id or buyer_ref.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "media_buy_id": {"type": "string"},
                "feedback": {"type": "object"},
            },
            "required": ["media_buy_id", "feedback"],
        },
    },
    # V3 Protocol Discovery
    {
        "name": "get_adcp_capabilities",
        "description": "Get this agent's supported protocols, features, and configuration. Call first to understand what this seller can do.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "sync_agent_notification_configs",
        "description": "Replace the authenticated caller's agent-level notification subscribers. Idempotent.",
        "annotations": _IDEMP,
        "inputSchema": {
            "type": "object",
            "properties": {
                "idempotency_key": {"type": "string"},
                "notification_configs": {"type": "array"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["idempotency_key", "notification_configs"],
        },
    },
    {
        "name": "get_task_status",
        "description": "Get status, progress, and optional result details for an async task.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "include_history": {"type": "boolean"},
                "include_result": {"type": "boolean"},
                "context": {"type": "object"},
                "ext": {"type": "object"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List async tasks with optional filtering, sorting, pagination, and history expansion.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "filters": {"type": "object"},
                "sort": {"type": "object"},
                "pagination": {"type": "object"},
                "include_history": {"type": "boolean"},
                "context": {"type": "object"},
                "ext": {"type": "object"},
            },
        },
    },
    # V3 Content Standards
    {
        "name": "create_content_standards",
        "description": "Create content standards configuration for brand safety and compliance.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "rules": {"type": "array"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_content_standards",
        "description": "Get content standards configuration.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_standards_id": {"type": "string"},
            },
            "required": ["content_standards_id"],
        },
    },
    {
        "name": "list_content_standards",
        "description": "List content standards configurations.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "pagination": {"type": "object"},
            },
        },
    },
    {
        "name": "update_content_standards",
        "description": "Update content standards configuration.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_standards_id": {"type": "string"},
                "rules": {"type": "array"},
            },
            "required": ["content_standards_id"],
        },
    },
    {
        "name": "calibrate_content",
        "description": "Evaluate content against standards. Returns compliance assessment.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_standards_id": {"type": "string"},
                "content": {"type": "object"},
            },
            "required": ["content_standards_id", "content"],
        },
    },
    {
        "name": "validate_content_delivery",
        "description": "Validate that delivery meets content standards.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_standards_id": {"type": "string"},
                "delivery": {"type": "object"},
            },
            "required": ["content_standards_id", "delivery"],
        },
    },
    {
        "name": "get_media_buy_artifacts",
        "description": "Get compliance artifacts associated with a media buy.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "media_buy_id": {"type": "string"},
            },
            "required": ["media_buy_id"],
        },
    },
    # V3 Governance
    {
        "name": "get_creative_features",
        "description": "Get creative feature definitions for governance evaluation.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "creative_manifest": {"type": "object"},
                "account": {"type": "object"},
                "context": {"type": "object"},
            },
            "required": ["creative_manifest"],
        },
    },
    {
        "name": "sync_plans",
        "description": "Sync campaign governance plans. Idempotent.",
        "annotations": _IDEMP,
        "inputSchema": {
            "type": "object",
            "properties": {
                "plans": {"type": "array"},
            },
            "required": ["plans"],
        },
    },
    {
        "name": "check_governance",
        "description": "Check an action against campaign governance rules. Returns approved, denied, or conditions.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "media_buy_id": {"type": "string"},
                "phase": {"type": "string"},
                "caller": {"type": "string"},
                "tool": {"type": "string"},
                "payload": {"type": "object"},
                "governance_context": {"type": "object"},
            },
            "required": ["plan_id", "caller"],
        },
    },
    {
        "name": "report_plan_outcome",
        "description": "Report the outcome of a governed action for audit.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "outcome": {"type": "string"},
                "check_id": {"type": "string"},
                "seller_response": {"type": "object"},
                "delivery": {"type": "object"},
                "error": {"type": "object"},
            },
            "required": ["plan_id", "outcome"],
        },
    },
    {
        "name": "report_plan_adjustment",
        "description": "Report or review a commercial adjustment to a governed plan outcome. Idempotent.",
        "annotations": _IDEMP,
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "plan_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["action", "plan_id", "idempotency_key"],
        },
    },
    {
        "name": "get_plan_audit_logs",
        "description": "Get audit logs for governance decisions.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_ids": {"type": "array", "items": {"type": "string"}},
                "portfolio_plan_ids": {"type": "array", "items": {"type": "string"}},
                "include_entries": {"type": "boolean"},
            },
        },
    },
    # V3 Sponsored Intelligence
    {
        "name": "si_get_offering",
        "description": "Get sponsored intelligence offering details and capabilities.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "si_initiate_session",
        "description": "Start a sponsored intelligence conversational session.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "budget": {"type": "number"},
            },
        },
    },
    {
        "name": "si_send_message",
        "description": "Send a message in an active SI session.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["session_id", "message"],
        },
    },
    {
        "name": "si_terminate_session",
        "description": "End an SI session. Cannot be undone.",
        "annotations": _DEST,
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
            },
            "required": ["session_id"],
        },
    },
    # V3 Governance (Property Lists)
    {
        "name": "create_property_list",
        "description": "Create a property list for inclusion/exclusion targeting.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "base_properties": {"type": "array"},
                "filters": {"type": "object"},
                "brand": {"type": "object"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_property_list",
        "description": "Get a property list with optional resolution of dynamic filters.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "resolve": {"type": "boolean"},
                "pagination": {"type": "object"},
            },
            "required": ["list_id"],
        },
    },
    {
        "name": "list_property_lists",
        "description": "List property lists with optional filtering by principal or status.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "principal": {"type": "string"},
                "pagination": {"type": "object"},
            },
        },
    },
    {
        "name": "update_property_list",
        "description": "Update a property list name, description, or filters.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "filters": {"type": "object"},
                "brand": {"type": "object"},
            },
            "required": ["list_id"],
        },
    },
    {
        "name": "delete_property_list",
        "description": "Permanently delete a property list.",
        "annotations": _DEST,
        "inputSchema": {
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
            },
            "required": ["list_id"],
        },
    },
    # V3 Governance (Collection Lists)
    {
        "name": "create_collection_list",
        "description": "Create a collection list for governance filtering.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "base_collections": {"type": "array"},
                "filters": {"type": "object"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_collection_list",
        "description": "Get a collection list with optional resolution.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "resolve": {"type": "boolean"},
                "pagination": {"type": "object"},
            },
            "required": ["list_id"],
        },
    },
    {
        "name": "list_collection_lists",
        "description": "List collection lists with optional filtering by principal or status.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "principal": {"type": "string"},
                "pagination": {"type": "object"},
            },
        },
    },
    {
        "name": "update_collection_list",
        "description": "Update a collection list name, description, or filters.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "filters": {"type": "object"},
            },
            "required": ["list_id"],
        },
    },
    {
        "name": "delete_collection_list",
        "description": "Permanently delete a collection list.",
        "annotations": _DEST,
        "inputSchema": {
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
            },
            "required": ["list_id"],
        },
    },
    # V3 TMP
    {
        "name": "context_match",
        "description": (
            "Evaluate publisher placement context against buyer packages"
            " and return matching offers. Called at ad-request time."
        ),
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "property_rid": {"type": "string"},
                "placement_id": {"type": "string"},
                "property_type": {"type": "string"},
                "request_id": {"type": "string"},
                "type": {"type": "string"},
                "artifact_refs": {"type": "array"},
                "context_signals": {"type": "object"},
                "geo": {"type": "object"},
                "package_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["property_rid", "placement_id", "property_type", "request_id", "type"],
        },
    },
    {
        "name": "identity_match",
        "description": (
            "Evaluate user identity token against active packages"
            " for eligibility. Requires consent in regulated regions."
        ),
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "type": {"type": "string"},
                "user_token": {"type": "string"},
                "uid_type": {"type": "string"},
                "package_ids": {"type": "array", "items": {"type": "string"}},
                "consent": {"type": "object"},
            },
            "required": ["request_id", "type", "user_token", "uid_type", "package_ids"],
        },
    },
    # V3 Brand Rights
    {
        "name": "get_brand_identity",
        "description": "Get brand identity information (logos, colors, guidelines).",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "brand_id": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "use_case": {"type": "string"},
            },
            "required": ["brand_id"],
        },
    },
    {
        "name": "get_rights",
        "description": "Discover available brand rights for licensing.",
        "annotations": _RO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "uses": {"type": "array", "items": {"type": "string"}},
                "brand_id": {"type": "string"},
                "right_type": {"type": "string"},
                "countries": {"type": "array", "items": {"type": "string"}},
                "include_excluded": {"type": "boolean"},
                "pagination": {"type": "object"},
            },
            "required": ["query", "uses"],
        },
    },
    {
        "name": "acquire_rights",
        "description": (
            "Acquire rights for brand content usage."
            " Binding contractual action with financial obligations."
        ),
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "rights_id": {"type": "string"},
                "pricing_option_id": {"type": "string"},
                "buyer": {"type": "object"},
                "campaign": {"type": "object"},
                "revocation_webhook": {"type": "object"},
                "idempotency_key": {"type": "string"},
            },
            "required": [
                "rights_id",
                "pricing_option_id",
                "buyer",
                "campaign",
                "revocation_webhook",
            ],
        },
    },
    {
        "name": "update_rights",
        "description": (
            "Update terms of an existing rights acquisition."
            " Partial update — include only the fields to change"
            " (end_date, impression_cap, paused, or a compatible"
            " pricing_option_id swap). Rejects updates on expired or"
            " revoked acquisitions."
        ),
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "rights_id": {"type": "string"},
                "end_date": {"type": "string"},
                "impression_cap": {"type": "integer"},
                "pricing_option_id": {"type": "string"},
                "paused": {"type": "boolean"},
                "push_notification_config": {"type": "object"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["rights_id", "idempotency_key"],
        },
    },
    {
        "name": "verify_brand_claim",
        "description": "Verify a single brand claim.",
        "annotations": _RO,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "verify_brand_claims",
        "description": "Verify multiple brand claims.",
        "annotations": _RO,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "validate_input",
        "description": "Validate creative input against a format declaration.",
        "annotations": _RO,
        "inputSchema": {"type": "object", "properties": {}},
    },
    # V3 Compliance
    {
        "name": "comply_test_controller",
        "description": "Compliance test controller. Sandbox only, not for production use.",
        "annotations": _MUT,
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "object"},
                "scenario": {
                    "type": "string",
                    # Derived from test_controller.SCENARIOS so the static stub
                    # matches the dispatcher; the Pydantic-generated path also
                    # carries the new names because #292 ships them in the
                    # comply-test-controller-request schema.
                    "enum": ["list_scenarios"] + _CONTROLLER_SCENARIOS,
                },
                "params": {"type": "object"},
                "context": {"type": "object"},
            },
            "required": ["scenario"],
        },
    },
]


# Protocol discovery tool included for all handler types
_PROTOCOL_TOOLS: set[str] = {"get_adcp_capabilities"}


# Tools the AdCP spec allows callers to invoke without an authenticated
# principal. ``get_adcp_capabilities`` is the handshake tool — any client
# has to call it before auth to discover which ops the agent supports and
# what auth scheme to use. Everything else requires a principal.
#
# Sellers wiring their own auth middleware (the SDK explicitly punts auth
# to the transport layer — see :func:`adcp.server.create_mcp_server`)
# should import this and skip auth enforcement for any tool name in the
# set. Downstream MAY extend it for discovery tools outside the AdCP spec
# (e.g. a public ``list_public_formats`` surface). The base set is the
# spec-mandated floor, not a cap.
#
# Example::
#
#     from adcp.server import DISCOVERY_TOOLS
#
#     async def dispatch(self, request, call_next):
#         tool = _extract_tool_name(request)
#         if tool not in DISCOVERY_TOOLS:
#             self._require_valid_token(request)
#         return await call_next(request)
DISCOVERY_TOOLS: frozenset[str] = frozenset({"get_adcp_capabilities"})


# JSON-RPC method names that MCP treats as handshake / capability-discovery
# and therefore allows pre-auth by spec:
#
# - ``initialize`` — session handshake (protocol version, client info).
# - ``notifications/initialized`` — client-to-server handshake-completion
#   notification. Gating this behind auth breaks the handshake state
#   machine.
# - ``tools/list`` — inventory advertisement (tool names, input schemas,
#   descriptions). One protocol layer below ``tools/call``, where
#   ``DISCOVERY_TOOLS`` applies.
#
# The set is intentionally minimal. Operators narrow it (remove
# ``tools/list`` when they consider the inventory sensitive); they should
# not extend it. Other MCP surfaces (``resources/*``, ``prompts/*``,
# ``logging/setLevel``, ``completion/complete``) are intentionally
# auth-gated — the AdCP SDK does not expose resources or prompts today,
# and adding them to the pre-auth set would unauthenticate data reads.
#
# Composed middleware gate (the recommended pre-auth posture)::
#
#     from adcp.server import DISCOVERY_METHODS, DISCOVERY_TOOLS
#
#     async def dispatch(self, request, call_next):
#         method, tool = _peek_jsonrpc(request)
#         is_discovery = method in DISCOVERY_METHODS or (
#             method == "tools/call" and tool in DISCOVERY_TOOLS
#         )
#         if not is_discovery:
#             self._require_valid_token(request)
#         return await call_next(request)
#
# Tool names and input schemas are treated as non-sensitive by default —
# they are public AdCP spec surface. Freeform description strings are
# the one leakage vector; operators who embed deployment hints there
# should either scrub or gate ``tools/list``.
DISCOVERY_METHODS: frozenset[str] = frozenset(
    {"initialize", "notifications/initialized", "tools/list"}
)


def validate_discovery_set(tools: Iterable[str]) -> None:
    """Fail-closed validation for an auth-optional tool set.

    Downstream that extends :data:`DISCOVERY_TOOLS` (``DISCOVERY_TOOLS |
    {"my_public_tool"}``) risks accidentally including a mutation tool,
    which would silently unauthenticate writes over HTTP. This helper
    asserts every name in the set resolves to a known ADCP tool whose
    annotations declare ``readOnlyHint: True`` — it refuses to pass
    anything mutating, destructive, or unknown.

    Call this at server startup on the effective set your middleware
    uses::

        from adcp.server import DISCOVERY_TOOLS, validate_discovery_set

        MY_DISCOVERY = DISCOVERY_TOOLS | {"list_public_formats"}
        validate_discovery_set(MY_DISCOVERY)  # raises early if misconfigured

    :raises ValueError: if any name in ``tools`` is unknown or resolves
        to a non-read-only tool.
    """
    by_name = {t["name"]: t for t in ADCP_TOOL_DEFINITIONS}
    unknown: list[str] = []
    mutating: list[str] = []
    for name in tools:
        tool = by_name.get(name)
        if tool is None:
            unknown.append(name)
            continue
        annotations = tool.get("annotations") or {}
        if not annotations.get("readOnlyHint"):
            mutating.append(name)
    problems: list[str] = []
    if unknown:
        problems.append(f"unknown tool(s): {sorted(unknown)}")
    if mutating:
        problems.append(
            f"non-read-only tool(s) {sorted(mutating)} — adding these to the "
            "auth-optional set would silently unauthenticate mutations"
        )
    if problems:
        raise ValueError("validate_discovery_set rejected the set: " + "; ".join(problems))


# Tools specific to each specialized handler type
_HANDLER_TOOLS: dict[str, set[str]] = {
    "GovernanceHandler": {
        "get_creative_features",
        "sync_plans",
        "check_governance",
        "report_plan_outcome",
        "report_plan_adjustment",
        "get_plan_audit_logs",
        "create_property_list",
        "get_property_list",
        "list_property_lists",
        "update_property_list",
        "delete_property_list",
        "create_collection_list",
        "get_collection_list",
        "list_collection_lists",
        "update_collection_list",
        "delete_collection_list",
    },
    "ContentStandardsHandler": {
        "create_content_standards",
        "get_content_standards",
        "list_content_standards",
        "update_content_standards",
        "calibrate_content",
        "validate_content_delivery",
        "get_media_buy_artifacts",
    },
    "SponsoredIntelligenceHandler": {
        "si_get_offering",
        "si_initiate_session",
        "si_send_message",
        "si_terminate_session",
    },
    "TmpHandler": {
        "context_match",
        "identity_match",
    },
    "BrandHandler": {
        "get_brand_identity",
        "get_rights",
        "acquire_rights",
        "update_rights",
    },
    "ComplianceHandler": {
        "comply_test_controller",
    },
    "ADCPHandler": {tool["name"] for tool in ADCP_TOOL_DEFINITIONS},
}

# Validate that all handler tool names reference real tools
_ALL_TOOL_NAMES = {t["name"] for t in ADCP_TOOL_DEFINITIONS}
for _handler_name, _tools in _HANDLER_TOOLS.items():
    _unknown = _tools - _ALL_TOOL_NAMES
    assert not _unknown, f"{_handler_name} references unknown tools: {_unknown}"


def register_handler_tools(handler_name: str, tools: Iterable[str]) -> None:
    """Register a handler-class-name → tool-set mapping with the framework.

    Public seam. ``get_tools_for_handler`` reads ``_HANDLER_TOOLS`` to
    filter ``tools/list`` per handler subclass; without registration, an
    ``ADCPHandler`` subclass that introduces a new specialism would fall
    through to its parent's tool set (typically ``ADCPHandler``'s
    full-spec surface), over-advertising. Codegen targets like
    ``adcp.decisioning.handler.PlatformHandler`` register here at class
    definition time via ``ADCPHandler.__init_subclass__``; hand-written
    custom bases call this directly before ``serve()``.
    Idempotent on equal input — calling twice with the same tool set
    is a no-op so module re-imports / reload-friendly test harnesses
    don't break.
    Conflicts raise. Unknown tool names raise with a closest-match
    suggestion (typo recovery for adopters working from spec memory).
    :param handler_name: The class name of the handler subclass —
        typically ``cls.__name__`` from inside ``__init_subclass__``.
    :param tools: Iterable of AdCP tool names this handler answers
        (members of ``ADCP_TOOL_DEFINITIONS``). Order doesn't matter.
    :raises ValueError: when ``handler_name`` is already registered with
        a different tool set, or when any tool name is not in the AdCP
        spec surface.
    """
    incoming = frozenset(tools)
    existing = _HANDLER_TOOLS.get(handler_name)
    if existing is not None:
        if frozenset(existing) == incoming:
            return
        raise ValueError(
            f"register_handler_tools({handler_name!r}, ...) called twice "
            f"with different tool sets. Existing: {sorted(existing)}; "
            f"incoming: {sorted(incoming)}. The framework can only hold "
            "one mapping per handler class — pick the canonical set."
        )
    unknown = incoming - _ALL_TOOL_NAMES
    if unknown:
        suggestions: list[str] = []
        for bad in sorted(unknown):
            close = difflib.get_close_matches(bad, _ALL_TOOL_NAMES, n=1)
            if close:
                suggestions.append(f"{bad!r} (did you mean {close[0]!r}?)")
            else:
                suggestions.append(repr(bad))
        raise ValueError(
            f"register_handler_tools({handler_name!r}, ...): unknown tool "
            f"name(s) {', '.join(suggestions)}. Tool names must match the "
            "AdCP spec — see ``adcp.server.mcp_tools.ADCP_TOOL_DEFINITIONS``."
        )
    _HANDLER_TOOLS[handler_name] = set(incoming)


# ============================================================================
# Pydantic schema generation — spec-accurate input schemas
# ============================================================================


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve every local ``$ref`` into the referenced ``$defs`` body.

    Pydantic emits nested models as ``{"$ref": "#/$defs/Name"}`` with the
    actual shape under ``$defs``. That's spec-valid JSON Schema, but the
    MCP client ecosystem is mixed — several popular consumers (including
    some of the cheaper agent runtimes we see in validation runs) don't
    implement ``$ref`` resolution. Tool discovery that looks correct in
    MCP Inspector shows up as ``{}`` to those clients, producing silent
    "this tool takes no params" confusion.

    The inliner walks the schema tree and replaces each ``$ref`` with the
    referenced definition. Resolved definitions are memoized and reused inside
    the generated registry, avoiding repeated reference resolution and large
    duplicate object graphs. Definitions returned to handler callers are
    copied without aliases by :func:`get_tools_for_handler`, so callers can
    still safely mutate their tool definitions. Sibling keys on the ``$ref``
    node (``description``, ``title``) are merged onto a shallow copy of the
    resolved body. Note:
    this is an annotation-level override that matches what Pydantic actually
    emits at reference sites — it is NOT spec §8.2 merge semantics (which
    would evaluate siblings as an implicit ``allOf``). If a future Pydantic
    version starts emitting assertion-level siblings (``type``, ``enum``,
    etc.) the merge would silently change validation; today it doesn't.

    Only handles local refs (``#/$defs/X``). External refs are left in
    place — Pydantic doesn't emit them for our request models, but if
    one ever appears it surfaces to the caller rather than being
    silently stripped.

    Only definitions reachable from the schema body are traversed. Walking
    the whole ``$defs`` table before discarding it made startup proportional
    to the complete model graph rather than the advertised schema. Cycles
    are protected by the active-definition set. Pydantic request models
    don't generate cyclic refs today; the guard exists so a future schema
    shape can't turn inlining into a ``RecursionError``. When the walk leaves
    at least one ``$ref`` unresolved (cycle or dangling), an unmodified copy
    of ``$defs`` is kept so a spec-compliant client can still resolve what we
    couldn't.
    """
    defs = schema.get("$defs", {})
    # Track whether we emitted any $ref in the output — tells the
    # caller whether it's safe to drop $defs. Avoids a
    # stringify-the-whole-tree scan post-walk, and sidesteps false
    # positives from legitimate ``"$ref"`` values inside enum / const
    # / description strings.
    unresolved = [False]

    resolved_defs: dict[str, Any] = {}
    resolving_defs: set[str] = set()

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if not ref.startswith("#/$defs/"):
                    # External ref (http://…, relative path). Pydantic
                    # doesn't emit these for our request models; leave
                    # untouched rather than risk silent corruption.
                    unresolved[0] = True
                    return {k: _resolve(v) for k, v in node.items()}
                def_name = ref[len("#/$defs/") :]
                if def_name in resolving_defs:
                    # Cycle — leave the $ref intact so a spec-compliant
                    # client can still resolve via $defs.
                    unresolved[0] = True
                    return {k: _resolve(v) for k, v in node.items()}
                if def_name in resolved_defs:
                    resolved = resolved_defs[def_name]
                else:
                    body = defs.get(def_name)
                    if body is None:
                        # Dangling ref — nothing in $defs matches. Leave
                        # the $ref for consumers to error on; preserving
                        # the shape is safer than silently stripping.
                        unresolved[0] = True
                        return {k: _resolve(v) for k, v in node.items()}
                    resolving_defs.add(def_name)
                    try:
                        resolved = _resolve(body)
                    finally:
                        resolving_defs.remove(def_name)
                    resolved_defs[def_name] = resolved
                # Annotation-level merge — sibling description/title
                # on the $ref node wins over the resolved body's
                # same-named keys.
                if len(node) == 1:
                    return resolved
                merged = dict(resolved) if isinstance(resolved, dict) else resolved
                if isinstance(merged, dict):
                    for k, v in node.items():
                        if k == "$ref":
                            continue
                        merged[k] = _resolve(v)
                return merged
            return {k: _resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(item) for item in node]
        return node

    # Resolve the schema body, not the definition table itself. Definitions
    # are expanded on demand when a body reference reaches them; traversing
    # every definition here duplicates most of the work and retains large
    # temporary trees that are immediately discarded.
    schema_body = {key: value for key, value in schema.items() if key != "$defs"}
    result = _resolve(schema_body)
    if unresolved[0] and "$defs" in schema:
        result["$defs"] = copy.deepcopy(defs)
    assert isinstance(result, dict)
    return result


def _copy_json_without_aliases(value: Any) -> Any:
    """Copy JSON-like data while materializing shared branches separately."""
    if isinstance(value, dict):
        return {key: _copy_json_without_aliases(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json_without_aliases(item) for item in value]
    return value


def _model_to_json_schema(
    model_type: Any, *, allow_root_union: bool = False
) -> dict[str, Any] | None:
    """Generate a flat JSON Schema for a Pydantic model or union.

    * Plain ``BaseModel`` subclasses use ``model_json_schema()``.
    * Union / Optional types use ``TypeAdapter`` so discriminated unions
      and aliases (``CreateMediaBuyResponse = ...Response1 | ...Response2``)
      generate as ``anyOf``.
    * ``$ref`` nodes are inlined (see :func:`_inline_refs`) so MCP
      clients that don't resolve references see the full surface.

    When ``allow_root_union`` is ``False`` (the default — used for input
    schemas), schemas with a root-level ``anyOf`` / ``$ref`` return
    ``None`` so the caller falls back to a hand-crafted shape.
    Input schemas need ``type: "object"`` at the root so MCP clients can
    render the form. Output schemas can validly be a discriminated
    union, so ``allow_root_union=True`` keeps the ``anyOf``.

    Returns ``None`` on any failure — callers fall back to skipping.
    """
    try:
        from pydantic import TypeAdapter

        if isinstance(model_type, type) and hasattr(model_type, "model_json_schema"):
            schema = model_type.model_json_schema()
        else:
            adapter = TypeAdapter(model_type)
            schema = adapter.json_schema()
    except Exception:
        return None

    schema.pop("title", None)

    if not allow_root_union and ("anyOf" in schema or "$ref" in schema):
        return None

    try:
        return _inline_refs(schema)
    except Exception:
        return None


def _generate_pydantic_schemas(
    tool_names: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Generate JSON schemas from Pydantic request models.

    Maps tool names to their corresponding request Pydantic types,
    then generates JSON Schema via ``model_json_schema()``. This produces
    spec-accurate schemas with proper field types, descriptions,
    required fields, and nested ``$defs``.

    The result is applied to ``ADCP_TOOL_DEFINITIONS`` lazily on first
    ``tools/list`` call by :func:`_ensure_pydantic_schemas_applied`. Any tool whose generation
    fails (or whose request model has no mapping here) silently keeps
    its hand-crafted stub; ``tests/test_mcp_schema_drift.py`` guards
    against that regression by asserting every tool has an entry here.
    """
    try:
        from adcp.types import (
            AcceptProposalRequest,
            AcquireRightsRequest,
            ActivateSignalRequest,
            BuyProductsRequest,
            CalibrateContentRequest,
            CheckGovernanceRequest,
            ComplyTestControllerRequest,
            ContextMatchRequest,
            ControlMediaBuyRequest,
            CreateCollectionListRequest,
            CreateContentStandardsRequest,
            CreateMediaBuyRequest,
            CreatePropertyListRequest,
            DeclineProposalsRequest,
            DeleteCollectionListRequest,
            DeletePropertyListRequest,
            GetAccountFinancialsRequest,
            GetAdcpCapabilitiesRequest,
            GetBrandIdentityRequest,
            GetCollectionListRequest,
            GetContentStandardsRequest,
            GetCreativeDeliveryRequest,
            GetCreativeFeaturesRequest,
            GetMediaBuyArtifactsRequest,
            GetMediaBuyDeliveryRequest,
            GetMediaBuysRequest,
            GetPlanAuditLogsRequest,
            GetProductsRequest,
            GetPropertyListRequest,
            GetRightsRequest,
            GetSignalsRequest,
            GetTaskStatusRequest,
            IdentityMatchRequest,
            ListAccountsRequest,
            ListCollectionListsRequest,
            ListContentStandardsRequest,
            ListCreativesRequest,
            ListProductsRequest,
            ListPropertyListsRequest,
            ListTasksRequest,
            ListTransformersRequest,
            LogEventRequest,
            ProvidePerformanceFeedbackRequest,
            RefineProposalsRequest,
            ReportPlanAdjustmentRequest,
            ReportPlanOutcomeRequest,
            ReportUsageRequest,
            RequestProposalsRequest,
            SiGetOfferingRequest,
            SiInitiateSessionRequest,
            SiSendMessageRequest,
            SiTerminateSessionRequest,
            SyncAccountsRequest,
            SyncAgentNotificationConfigsRequest,
            SyncAudiencesRequest,
            SyncCatalogsRequest,
            SyncCreativesRequest,
            SyncEventSourcesRequest,
            SyncGovernanceRequest,
            SyncPlansRequest,
            UpdateCollectionListRequest,
            UpdateContentStandardsRequest,
            UpdateMediaBuyRequest,
            UpdatePropertyListRequest,
            UpdateRightsRequest,
            ValidateContentDeliveryRequest,
        )
        from adcp.types import _generated as gen
        from adcp.types.legacy import (
            LegacyBuildCreativeRequest as BuildCreativeRequest,
        )
        from adcp.types.legacy import (
            LegacyListCreativeFormatsRequest as ListCreativeFormatsRequest,
        )
        from adcp.types.legacy import (
            LegacyPreviewCreativeRequest as PreviewCreativeRequest,
        )
    except ImportError:
        return {}

    # Map tool names to their Pydantic request types
    _tool_to_request: dict[str, Any] = {
        # Catalog
        "get_products": GetProductsRequest,
        "list_products": ListProductsRequest,
        "request_proposals": RequestProposalsRequest,
        "refine_proposals": RefineProposalsRequest,
        "decline_proposals": DeclineProposalsRequest,
        "list_creative_formats": ListCreativeFormatsRequest,
        # Creative
        "sync_creatives": SyncCreativesRequest,
        "list_creatives": ListCreativesRequest,
        "build_creative": BuildCreativeRequest,
        "preview_creative": PreviewCreativeRequest,
        "validate_input": gen.ValidateInputRequest,
        "get_creative_delivery": GetCreativeDeliveryRequest,
        "list_transformers": ListTransformersRequest,
        # Media Buy
        "buy_products": BuyProductsRequest,
        "accept_proposal": AcceptProposalRequest,
        "control_media_buy": ControlMediaBuyRequest,
        "create_media_buy": CreateMediaBuyRequest,
        "update_media_buy": UpdateMediaBuyRequest,
        "get_media_buy_delivery": GetMediaBuyDeliveryRequest,
        "get_media_buys": GetMediaBuysRequest,
        # Signals
        "get_signals": GetSignalsRequest,
        "activate_signal": ActivateSignalRequest,
        # Account
        "list_accounts": ListAccountsRequest,
        "sync_accounts": SyncAccountsRequest,
        "get_account_financials": GetAccountFinancialsRequest,
        "report_usage": ReportUsageRequest,
        # Events & Catalogs
        "log_event": LogEventRequest,
        "sync_event_sources": SyncEventSourcesRequest,
        "sync_audiences": SyncAudiencesRequest,
        "sync_catalogs": SyncCatalogsRequest,
        "sync_governance": SyncGovernanceRequest,
        # Feedback
        "provide_performance_feedback": ProvidePerformanceFeedbackRequest,
        # Protocol Discovery
        "get_adcp_capabilities": GetAdcpCapabilitiesRequest,
        "sync_agent_notification_configs": SyncAgentNotificationConfigsRequest,
        "get_task_status": GetTaskStatusRequest,
        "list_tasks": ListTasksRequest,
        # Compliance
        "comply_test_controller": ComplyTestControllerRequest,
        # Content Standards
        "create_content_standards": CreateContentStandardsRequest,
        "get_content_standards": GetContentStandardsRequest,
        "list_content_standards": ListContentStandardsRequest,
        "update_content_standards": UpdateContentStandardsRequest,
        "calibrate_content": CalibrateContentRequest,
        "validate_content_delivery": ValidateContentDeliveryRequest,
        "get_media_buy_artifacts": GetMediaBuyArtifactsRequest,
        # Governance
        "get_creative_features": GetCreativeFeaturesRequest,
        "sync_plans": SyncPlansRequest,
        "check_governance": CheckGovernanceRequest,
        "report_plan_outcome": ReportPlanOutcomeRequest,
        "report_plan_adjustment": ReportPlanAdjustmentRequest,
        "get_plan_audit_logs": GetPlanAuditLogsRequest,
        # Property Lists
        "create_property_list": CreatePropertyListRequest,
        "get_property_list": GetPropertyListRequest,
        "list_property_lists": ListPropertyListsRequest,
        "update_property_list": UpdatePropertyListRequest,
        "delete_property_list": DeletePropertyListRequest,
        # Collection Lists
        "create_collection_list": CreateCollectionListRequest,
        "get_collection_list": GetCollectionListRequest,
        "list_collection_lists": ListCollectionListsRequest,
        "update_collection_list": UpdateCollectionListRequest,
        "delete_collection_list": DeleteCollectionListRequest,
        # Sponsored Intelligence
        "si_get_offering": SiGetOfferingRequest,
        "si_initiate_session": SiInitiateSessionRequest,
        "si_send_message": SiSendMessageRequest,
        "si_terminate_session": SiTerminateSessionRequest,
        # Brand
        "get_brand_identity": GetBrandIdentityRequest,
        "verify_brand_claim": gen.VerifyBrandClaimRequest,
        "verify_brand_claims": gen.VerifyBrandClaimsRequest,
        "get_rights": GetRightsRequest,
        "acquire_rights": AcquireRightsRequest,
        "update_rights": UpdateRightsRequest,
        # TMP
        "context_match": ContextMatchRequest,
        "identity_match": IdentityMatchRequest,
    }

    selected = set(tool_names) if tool_names is not None else None
    schemas: dict[str, dict[str, Any]] = {}
    for tool_name, request_type in _tool_to_request.items():
        if selected is not None and tool_name not in selected:
            continue
        # Input schemas must be flat ``type: "object"`` — root-level
        # ``anyOf`` / ``$ref`` schemas are skipped so the hand-crafted
        # stub stays in place.
        schema = _model_to_json_schema(request_type, allow_root_union=False)
        if schema is None:
            logger.debug(
                "Pydantic input-schema generation skipped for %s, using hand-crafted schema",
                tool_name,
            )
            continue
        schemas[tool_name] = schema

    return schemas


def _generate_pydantic_output_schemas(
    tool_names: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Generate JSON schemas from Pydantic response models.

    Mirror of :func:`_generate_pydantic_schemas` for the response side.
    Each AdCP tool has a corresponding ``Response`` type — for plain
    success responses this is a single ``BaseModel`` subclass; for tools
    that distinguish success / error / pending / rejected on the wire
    (``CreateMediaBuyResponse``, ``AcquireRightsResponse``, etc.) it's a
    union alias.

    Output schemas advertise the structured-content shape on
    ``tools/list`` (matches the TS port) so MCP clients can validate
    ``structuredContent`` without a separate spec lookup.

    Unlike input schemas, root-level ``anyOf`` is allowed — discriminated
    response unions are valid JSON Schema and clients that consume
    ``outputSchema`` already handle them.
    """
    try:
        from adcp.types import (
            AcceptProposalResponse,
            AcquireRightsResponse,
            ActivateSignalResponse,
            BuyProductsResponse,
            CalibrateContentResponse,
            CheckGovernanceResponse,
            ComplyTestControllerResponse,
            ContextMatchResponse,
            ControlMediaBuyResponse,
            CreateCollectionListResponse,
            CreateContentStandardsResponse,
            CreateMediaBuyResponse,
            CreatePropertyListResponse,
            DeclineProposalsResponse,
            DeleteCollectionListResponse,
            DeletePropertyListResponse,
            GetAccountFinancialsResponse,
            GetAdcpCapabilitiesResponse,
            GetBrandIdentityResponse,
            GetCollectionListResponse,
            GetContentStandardsResponse,
            GetCreativeDeliveryResponse,
            GetCreativeFeaturesResponse,
            GetMediaBuyArtifactsResponse,
            GetMediaBuyDeliveryResponse,
            GetMediaBuysResponse,
            GetPlanAuditLogsResponse,
            GetProductsResponse,
            GetPropertyListResponse,
            GetRightsResponse,
            GetSignalsResponse,
            GetTaskStatusResponse,
            IdentityMatchResponse,
            ListAccountsResponse,
            ListCollectionListsResponse,
            ListContentStandardsResponse,
            ListCreativesResponse,
            ListProductsResponse,
            ListPropertyListsResponse,
            ListTasksResponse,
            ListTransformersResponse,
            LogEventResponse,
            ProvidePerformanceFeedbackResponse,
            RefineProposalsResponse,
            ReportPlanAdjustmentResponse,
            ReportPlanOutcomeResponse,
            ReportUsageResponse,
            RequestProposalsResponse,
            SiGetOfferingResponse,
            SiInitiateSessionResponse,
            SiSendMessageResponse,
            SiTerminateSessionResponse,
            SyncAccountsResponse,
            SyncAgentNotificationConfigsResponse,
            SyncAudiencesResponse,
            SyncCatalogsResponse,
            SyncCreativesResponse,
            SyncEventSourcesResponse,
            SyncGovernanceResponse,
            SyncPlansResponse,
            UpdateCollectionListResponse,
            UpdateContentStandardsResponse,
            UpdateMediaBuyResponse,
            UpdatePropertyListResponse,
            UpdateRightsResponse,
            ValidateContentDeliveryResponse,
            ValidateInputResponse,
            VerifyBrandClaimResponse,
            VerifyBrandClaimsResponseBulk,
        )
        from adcp.types.legacy import (
            LegacyBuildCreativeResponse as BuildCreativeResponse,
        )
        from adcp.types.legacy import (
            LegacyListCreativeFormatsResponse as ListCreativeFormatsResponse,
        )
        from adcp.types.legacy import (
            LegacyPreviewCreativeResponse as PreviewCreativeResponse,
        )
    except ImportError:
        return {}

    _tool_to_response: dict[str, Any] = {
        # Catalog
        "get_products": GetProductsResponse,
        "list_products": ListProductsResponse,
        "request_proposals": RequestProposalsResponse,
        "refine_proposals": RefineProposalsResponse,
        "decline_proposals": DeclineProposalsResponse,
        "list_creative_formats": ListCreativeFormatsResponse,
        # Creative
        "sync_creatives": SyncCreativesResponse,
        "list_creatives": ListCreativesResponse,
        "build_creative": BuildCreativeResponse,
        "preview_creative": PreviewCreativeResponse,
        "validate_input": ValidateInputResponse,
        "get_creative_delivery": GetCreativeDeliveryResponse,
        "list_transformers": ListTransformersResponse,
        # Media Buy
        "buy_products": BuyProductsResponse,
        "accept_proposal": AcceptProposalResponse,
        "control_media_buy": ControlMediaBuyResponse,
        "create_media_buy": CreateMediaBuyResponse,
        "update_media_buy": UpdateMediaBuyResponse,
        "get_media_buy_delivery": GetMediaBuyDeliveryResponse,
        "get_media_buys": GetMediaBuysResponse,
        # Signals
        "get_signals": GetSignalsResponse,
        "activate_signal": ActivateSignalResponse,
        # Account
        "list_accounts": ListAccountsResponse,
        "sync_accounts": SyncAccountsResponse,
        "get_account_financials": GetAccountFinancialsResponse,
        "report_usage": ReportUsageResponse,
        # Events & Catalogs
        "log_event": LogEventResponse,
        "sync_event_sources": SyncEventSourcesResponse,
        "sync_audiences": SyncAudiencesResponse,
        "sync_catalogs": SyncCatalogsResponse,
        "sync_governance": SyncGovernanceResponse,
        # Feedback
        "provide_performance_feedback": ProvidePerformanceFeedbackResponse,
        # Protocol Discovery
        "get_adcp_capabilities": GetAdcpCapabilitiesResponse,
        "sync_agent_notification_configs": SyncAgentNotificationConfigsResponse,
        "get_task_status": GetTaskStatusResponse,
        "list_tasks": ListTasksResponse,
        # Compliance
        "comply_test_controller": ComplyTestControllerResponse,
        # Content Standards
        "create_content_standards": CreateContentStandardsResponse,
        "get_content_standards": GetContentStandardsResponse,
        "list_content_standards": ListContentStandardsResponse,
        "update_content_standards": UpdateContentStandardsResponse,
        "calibrate_content": CalibrateContentResponse,
        "validate_content_delivery": ValidateContentDeliveryResponse,
        "get_media_buy_artifacts": GetMediaBuyArtifactsResponse,
        # Governance
        "get_creative_features": GetCreativeFeaturesResponse,
        "sync_plans": SyncPlansResponse,
        "check_governance": CheckGovernanceResponse,
        "report_plan_outcome": ReportPlanOutcomeResponse,
        "report_plan_adjustment": ReportPlanAdjustmentResponse,
        "get_plan_audit_logs": GetPlanAuditLogsResponse,
        # Property Lists
        "create_property_list": CreatePropertyListResponse,
        "get_property_list": GetPropertyListResponse,
        "list_property_lists": ListPropertyListsResponse,
        "update_property_list": UpdatePropertyListResponse,
        "delete_property_list": DeletePropertyListResponse,
        # Collection Lists
        "create_collection_list": CreateCollectionListResponse,
        "get_collection_list": GetCollectionListResponse,
        "list_collection_lists": ListCollectionListsResponse,
        "update_collection_list": UpdateCollectionListResponse,
        "delete_collection_list": DeleteCollectionListResponse,
        # Sponsored Intelligence
        "si_get_offering": SiGetOfferingResponse,
        "si_initiate_session": SiInitiateSessionResponse,
        "si_send_message": SiSendMessageResponse,
        "si_terminate_session": SiTerminateSessionResponse,
        # Brand
        "get_brand_identity": GetBrandIdentityResponse,
        "verify_brand_claim": VerifyBrandClaimResponse,
        "verify_brand_claims": VerifyBrandClaimsResponseBulk,
        "get_rights": GetRightsResponse,
        "acquire_rights": AcquireRightsResponse,
        "update_rights": UpdateRightsResponse,
        # TMP
        "context_match": ContextMatchResponse,
        "identity_match": IdentityMatchResponse,
    }

    selected = set(tool_names) if tool_names is not None else None
    schemas: dict[str, dict[str, Any]] = {}
    for tool_name, response_type in _tool_to_response.items():
        if selected is not None and tool_name not in selected:
            continue
        schema = _model_to_json_schema(response_type, allow_root_union=True)
        if schema is None:
            logger.debug(
                "Pydantic output-schema generation failed for %s",
                tool_name,
            )
            continue
        # MCP requires ``outputSchema`` root-level ``type: "object"`` —
        # the schema describes ``CallToolResult.structuredContent`` which
        # is always a JSON object. Discriminated-union responses
        # (CreateMediaBuyResponse, AcquireRightsResponse, etc.) come
        # back from Pydantic as ``{"anyOf": [...]}`` with no ``type``,
        # which Zod-validated MCP clients reject. Every variant in the
        # union is itself an object, so adding ``"type": "object"``
        # at the root is semantically equivalent and MCP-spec-conformant.
        schema.setdefault("type", "object")
        _widen_media_buy_output_schema_for_legacy_statuses(tool_name, schema)
        schemas[tool_name] = schema

    return schemas


# Schemas are populated lazily on the first tools/list call to avoid
# heavy Pydantic type imports at module import time. Use .update() so
# external references bound before init (e.g. in tests) stay valid.
_PYDANTIC_SCHEMAS: dict[str, dict[str, Any]] = {}
_PYDANTIC_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {}
_schema_tools_attempted: set[str] = set()
_schemas_applied = False


def _apply_pydantic_schemas() -> None:
    """Apply Pydantic-generated input + output schemas to tool definitions.

    * ``inputSchema``: replaced when a Pydantic-generated schema is
      available (handles drift between hand-crafted stubs and the spec).
    * ``outputSchema``: added so ``tools/list`` advertises the structured
      response shape — matches the TS port and lets MCP clients validate
      ``structuredContent`` without a separate spec lookup.
    """
    for tool_def in ADCP_TOOL_DEFINITIONS:
        name = tool_def["name"]
        if name in _PYDANTIC_SCHEMAS:
            tool_def["inputSchema"] = _PYDANTIC_SCHEMAS[name]
        if name in _PYDANTIC_OUTPUT_SCHEMAS:
            tool_def["outputSchema"] = _PYDANTIC_OUTPUT_SCHEMAS[name]


def _ensure_pydantic_schemas_applied(tool_names: Iterable[str] | None = None) -> None:
    """Lazily populate Pydantic schemas and apply them to tool definitions.

    Mutates :data:`ADCP_TOOL_DEFINITIONS` in-place, replacing each tool's
    ``inputSchema`` with the Pydantic-generated schema and adding
    ``outputSchema``. Safe to call multiple times — subsequent calls are
    no-ops. Called automatically by :func:`create_mcp_tools` /
    :func:`get_tools_for_handler`; callers outside those paths (e.g. tests
    or doc generators) must invoke this before reading schema fields.
    """
    global _schemas_applied
    if tool_names is None:
        if _schemas_applied:
            return
        selected = {tool["name"] for tool in ADCP_TOOL_DEFINITIONS}
    else:
        selected = set(tool_names)

    pending = selected - _schema_tools_attempted
    if not pending:
        return
    _PYDANTIC_SCHEMAS.update(_generate_pydantic_schemas(pending))
    _PYDANTIC_OUTPUT_SCHEMAS.update(_generate_pydantic_output_schemas(pending))
    _schema_tools_attempted.update(pending)
    _apply_pydantic_schemas()
    if tool_names is None:
        _schemas_applied = True


def _is_sdk_base_class(cls_name: str) -> bool:
    """True when ``cls_name`` is registered in ``_HANDLER_TOOLS``.

    Used during MRO walks to identify the nearest SDK base whose
    method baselines a subclass override. Reads ``_HANDLER_TOOLS``
    live so that handler classes registered after import time —
    via :func:`register_handler_tools` or
    :meth:`ADCPHandler.__init_subclass__` reading
    ``advertised_tools`` — participate in override detection without
    requiring a frozen-set rebuild.
    """
    return cls_name in _HANDLER_TOOLS


def _is_method_overridden(handler_cls: type, method_name: str) -> bool:
    """True when ``handler_cls`` implements ``method_name`` rather than
    falling through to the SDK's ``not_supported`` default.

    Invariant: **the nearest SDK base in the MRO owns the baseline**.
    Walking stops at the first SDK base that defines ``method_name``;
    every other SDK base lower in the MRO is ignored. Specialized
    handler bases (``GovernanceHandler``, ``ContentStandardsHandler``,
    ``SponsoredIntelligenceHandler``, etc.) override the baseline from
    ``ADCPHandler`` with validation wrappers that delegate to abstract
    ``handle_<tool>`` methods. That baseline is what subclasses compose
    against — comparing against ``ADCPHandler`` directly would mis-flag
    the specialized wrappers as "overrides."

    Two override patterns count as implemented:

    1. **Direct**: ``handler_cls`` replaces the public method (typical
       when subclassing ``ADCPHandler`` directly — the subclass writes
       its own ``async def update_property_list(...)``).
    2. **Delegation**: ``handler_cls`` inherits the public method from a
       specialized SDK base unchanged, but provides a concrete
       ``handle_<method>`` where the SDK base declared it abstract.
       This is the documented pattern for ``GovernanceHandler``,
       ``ContentStandardsHandler``, and ``SponsoredIntelligenceHandler``.
       Without this branch, subclasses of those bases that follow the
       documented pattern would advertise zero tools.

    Returns ``False`` when the public method is inherited unchanged AND
    no concrete ``handle_<method>`` is provided below the SDK base — the
    tool will answer every call with ``not_supported`` and should not
    appear in ``tools/list``.

    Returns ``False`` for methods that don't exist on the handler at all
    (pathological case — every ADCP tool method is defined on
    ``ADCPHandler``).
    """
    method_name = {
        "build_creative": "build_creative_legacy",
        "list_creative_formats": "list_creative_formats_legacy",
        "preview_creative": "preview_creative_legacy",
    }.get(method_name, method_name)
    handler_method = getattr(handler_cls, method_name, None)
    if handler_method is None:
        return False

    # Find the nearest SDK base that defines the public method.
    sdk_base: type | None = None
    base_method: Any | None = None
    for base in handler_cls.__mro__[1:]:
        if not _is_sdk_base_class(base.__name__):
            continue
        found = base.__dict__.get(method_name)
        if found is None:
            continue
        sdk_base = base
        base_method = found
        break

    if sdk_base is None:
        # Method not owned by any SDK base. Custom surface — conservative:
        # treat as overridden so we don't silently drop it.
        return True

    # Pattern 1: direct override of the public method.
    if handler_method is not base_method:
        return True

    # Pattern 2: delegation via handle_<tool>. Specialized SDK bases
    # declare ``handle_<method>`` as an abstractmethod. If the subclass
    # (anywhere between ``sdk_base`` and ``handler_cls``) provides a
    # concrete implementation, the tool is implemented.
    handle_name = f"handle_{method_name}"
    sdk_handle = getattr(sdk_base, handle_name, None)
    if sdk_handle is None or not getattr(sdk_handle, "__isabstractmethod__", False):
        # SDK base doesn't use the handle_<tool> delegation pattern here;
        # the public method really is the baseline, and the subclass did
        # not override it.
        return False

    subclass_handle = getattr(handler_cls, handle_name, None)
    if subclass_handle is None:
        return False
    # If still abstract on the final class, the class itself is abstract
    # (ABC would refuse to instantiate it). Don't advertise.
    return not getattr(subclass_handle, "__isabstractmethod__", False)


def _resolve_handler_adcp_version(
    instance: ADCPHandler[Any] | None,
    explicit_version: str | None,
) -> str | None:
    """Resolve one trusted server pin for both discovery and dispatch."""
    if explicit_version is not None:
        return explicit_version
    if instance is None:
        return None
    getter = getattr(instance, "get_adcp_version", None)
    if callable(getter):
        try:
            candidate = getter()
        except Exception:
            candidate = None
        if isinstance(candidate, str):
            return candidate
    candidate = getattr(instance, "_adcp_version", None)
    return candidate if isinstance(candidate, str) else None


def get_tools_for_handler(
    handler: ADCPHandler[Any] | type[ADCPHandler[Any]],
    *,
    advertise_all: bool = False,
    adcp_version: str | None = None,
    _include_schemas: bool = True,
) -> list[dict[str, Any]]:
    """Return tool definitions the handler will actually answer.

    Walks the MRO to find the matching handler base class, so subclasses
    (e.g. ``MyGovernanceAgent(GovernanceHandler)``) get the correct tool
    set. ADCPHandler gets all tools. Unknown handlers get only protocol
    discovery (minimum privilege).

    By default, tools whose handler method is still the SDK's
    ``not_supported`` default (the subclass never overrode it) are
    filtered out — there's no point advertising a tool that answers
    every call with ``NOT_SUPPORTED``. This keeps ``tools/list`` small
    and protects agent clients from chasing non-functional tool surface.

    Always-advertised tools:
    - :data:`_PROTOCOL_TOOLS` (``get_adcp_capabilities``) — per-spec
      handshake requirement.
    - :data:`DISCOVERY_TOOLS` — auth-optional discovery tools the spec
      requires agents to expose.

    Escape hatch: pass ``advertise_all=True`` to restore the pre-#220
    behavior and advertise every tool in the handler-type's allowed
    set regardless of override state. Useful for spec-compliance
    storyboard tests and for agents that deliberately want to expose a
    ``not_supported`` tool (e.g. to signal "we know about X but don't
    implement it yet").

    Args:
        handler: The handler instance or class.
        advertise_all: When True, skip the override-based filter and
            advertise every tool allowed for the handler type.
        adcp_version: Trusted server protocol pin used to select request and
            response schemas. When omitted for an instance, the handler's
            ``get_adcp_version()`` / ``_adcp_version`` pin is used when
            available. Class-only introspection retains the current generated
            model surface.

    Returns:
        Filtered list of tool definitions.
    """
    cls = handler if isinstance(handler, type) else type(handler)
    instance = handler if not isinstance(handler, type) else None

    candidates: list[dict[str, Any]] = []
    for base in cls.__mro__:
        if base.__name__ in _HANDLER_TOOLS:
            allowed = _HANDLER_TOOLS[base.__name__] | _PROTOCOL_TOOLS
            candidates = [tool for tool in ADCP_TOOL_DEFINITIONS if tool["name"] in allowed]
            break
    else:
        candidates = [tool for tool in ADCP_TOOL_DEFINITIONS if tool["name"] in _PROTOCOL_TOOLS]

    # Per-instance specialism filter (Emma cross-cutting P1). When the
    # handler instance exposes ``advertised_tools_for_instance``, intersect
    # the candidate universe with the per-instance set BEFORE the
    # override-detection filter. This trims tools whose Protocol family
    # the platform didn't claim (sales-only adopter no longer advertises
    # ``acquire_rights``, ``build_creative``, etc.). Falls back to the
    # class-level universe when:
    #
    # * The handler is being inspected by class (no instance) — class-level
    #   advertisement preserves backwards compat for static introspection.
    # * The hook returns an empty set (adopter piloting a novel specialism
    #   slug not in :data:`SPECIALISM_TO_ADVERTISED_TOOLS`); muting the
    #   handler would be a worse foot-gun than over-advertising.
    if instance is not None and hasattr(instance, "advertised_tools_for_instance"):
        try:
            per_instance_set = instance.advertised_tools_for_instance()
        except Exception:
            # Defensive: never let an instance hook crash tools/list.
            per_instance_set = None
        if per_instance_set:
            always_on = _PROTOCOL_TOOLS | DISCOVERY_TOOLS
            candidates = [
                tool
                for tool in candidates
                if tool["name"] in always_on or tool["name"] in per_instance_set
            ]

    if advertise_all:
        selected = candidates
    else:
        always_on = _PROTOCOL_TOOLS | DISCOVERY_TOOLS
        selected = [
            tool
            for tool in candidates
            if tool["name"] in always_on or _is_method_overridden(cls, tool["name"])
        ]

    resolved_version = _resolve_handler_adcp_version(instance, adcp_version)

    if not _include_schemas:
        return [{"name": tool["name"]} for tool in selected]

    if resolved_version is None:
        # Pydantic schema generation is expensive for the full AdCP surface,
        # especially with the 3.2 model graph. Compile only the definitions
        # this handler will advertise.
        _ensure_pydantic_schemas_applied(tool["name"] for tool in selected)
        # The in-memory registry shares memoized schema subtrees to keep server
        # startup compact. Public definitions remain ordinary independently
        # mutable JSON values, matching the pre-memoization behavior.
        return [_copy_json_without_aliases(tool) for tool in selected]

    if not list_validator_keys(version=resolved_version):
        raise ValueError(
            f"no bundled AdCP schemas are available for adcp_version={resolved_version!r}"
        )

    # A pinned server advertises the exact bundled wire contract. This also
    # removes tools absent from that release (for example, the compact 3.2
    # lifecycle on a 3.1 endpoint) instead of leaking the process-global
    # current-model surface into tools/list.
    versioned: list[dict[str, Any]] = []
    for tool in selected:
        name = tool["name"]
        input_schema = get_mcp_schema(name, "request", version=resolved_version)
        if input_schema is None:
            continue
        definition = copy.deepcopy(tool)
        definition["inputSchema"] = input_schema
        output_schema = get_mcp_schema(name, "sync", version=resolved_version)
        if output_schema is not None:
            output_schema.setdefault("type", "object")
            definition["outputSchema"] = output_schema
        else:
            definition.pop("outputSchema", None)
        versioned.append(definition)
    return versioned


def _resolve_params_pydantic_model(method: Any) -> type[Any] | None:
    """Resolve the Pydantic model the handler expects for ``params``.

    Inspects the method's ``params`` annotation. Returns the Pydantic
    class when the annotation is:

    - A direct ``BaseModel`` subclass (``params: GetProductsRequest``).
    - A Union / Optional whose first member is a ``BaseModel`` subclass
      (``params: GetProductsRequest | dict[str, Any]``). This shape is
      what the specialized SDK handler bases declare — typed-dispatch
      treats the first Pydantic branch as the authoritative shape, so
      existing ``params: Request | dict`` annotations keep working.

    Returns ``None`` for ``dict``, missing annotation, or forward
    references that fail to resolve — the dispatcher then falls back
    to the legacy dict path.

    The result is computed once at ``create_tool_caller`` setup time
    (not per request) and captured in the closure returned to the
    transport layer; warnings fire at server boot, not per call.
    Forward-compat with PEP 749 (3.14 lazy annotations): ``get_type_hints``
    is the supported migration target for runtime annotation
    resolution, so this code keeps working as the language evolves.
    """
    import typing
    from types import UnionType

    from pydantic import BaseModel

    try:
        hints = typing.get_type_hints(method)
    except Exception as exc:  # forward-ref failure, missing import, etc.
        # WARNING (not debug): silent dict-path fallback hides shim
        # crashes on ``params.<field>`` access when the typed annotation
        # didn't resolve. Author's choice: declare ``params: dict`` for
        # the dict path, or ensure the typed annotation's class is
        # importable at the method's module scope (not under
        # ``TYPE_CHECKING``).
        #
        # Surface the failing name explicitly so adopters don't have to
        # parse the method repr — ``NameError`` exposes it on
        # ``.name`` on 3.10+. Falls back to ``str(exc)`` for other
        # exception classes (rare).
        failing_name = getattr(exc, "name", None) or str(exc)
        logger.warning(
            "typed params annotation failed to resolve for %r "
            "(unresolved name: %s); falling back to dict dispatch. "
            "If this method declares ``params: <PydanticModel>``, "
            "import that model at the method's module scope (not "
            "under ``TYPE_CHECKING``); otherwise declare "
            "``params: dict[str, Any]`` to silence this warning.",
            method,
            failing_name,
        )
        return None
    annotation = hints.get("params")
    if annotation is None:
        return None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is UnionType:
        for arg in typing.get_args(annotation):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                return arg
    return None


def _normalize_unknown_field_policy(
    policy: UnknownFieldPolicy | str | None,
) -> UnknownFieldPolicy:
    if policy is None:
        return UnknownFieldPolicy.IGNORE
    if isinstance(policy, UnknownFieldPolicy):
        return policy
    return UnknownFieldPolicy(policy)


def _allowed_top_level_fields(
    method_name: str,
    *,
    version: str | None,
    params_model: type[Any] | None,
) -> set[str] | None:
    validator = get_validator(method_name, "request", version=version)
    if validator is not None:
        schema = getattr(validator, "schema", None)
        if isinstance(schema, dict):
            properties = schema.get("properties")
            if isinstance(properties, dict):
                return {str(name) for name in properties}

    if params_model is None:
        return None

    model_fields = getattr(params_model, "model_fields", None)
    if not isinstance(model_fields, dict):
        return None

    allowed: set[str] = set()
    for name, field in model_fields.items():
        allowed.add(str(name))
        alias = getattr(field, "alias", None)
        if isinstance(alias, str):
            allowed.add(alias)
    return allowed


def _apply_unknown_field_policy(
    method_name: str,
    params: dict[str, Any],
    *,
    policy: UnknownFieldPolicy,
    version: str | None,
    params_model: type[Any] | None,
) -> dict[str, Any]:
    if policy == UnknownFieldPolicy.IGNORE:
        return params

    allowed = _allowed_top_level_fields(method_name, version=version, params_model=params_model)
    if allowed is None:
        return params

    unknown = [key for key in params if key not in allowed]
    if not unknown:
        return params

    if policy == UnknownFieldPolicy.STRIP:
        return {key: value for key, value in params.items() if key in allowed}

    first = unknown[0]
    from adcp.exceptions import ADCPTaskError
    from adcp.types import Error

    raise ADCPTaskError(
        operation=method_name,
        errors=[
            Error(
                code="INVALID_REQUEST",
                field=first,
                message=f"Unexpected top-level field {first!r} for {method_name}",
                details={"unknown_fields": unknown, "policy": policy.value},
            )
        ],
    )


def _creative_capabilities_for_handler(handler: Any) -> Any:
    """Return deterministic server-owned creative capability evidence.

    Capability discovery is negotiated per caller.  In particular, a 3.0
    discovery response suppresses the 3.1-only ``canonical_creatives`` field.
    Never retain that negotiated response on a shared handler: doing so makes
    the dialect selected for one caller depend on which caller discovered the
    server most recently.
    """

    explicit = getattr(handler, "adcp_capabilities", None)
    if explicit is not None:
        return explicit
    declared = getattr(handler, "_declared_creative_capabilities", None)
    if declared is not None:
        return declared
    platform = getattr(handler, "_platform", None)
    declared = getattr(platform, "capabilities", None)
    media_buy = getattr(declared, "media_buy", None)
    if media_buy is not None:
        return {"media_buy": media_buy}
    return getattr(handler, "_framework_adcp_capabilities", None)


def create_tool_caller(
    handler: ADCPHandler[Any],
    method_name: str,
    *,
    validation: ValidationHookConfig | None = None,
    pre_validation_hook: PreValidationHookChain | None = None,
    default_unnegotiated_adcp_version: str | None = DEFAULT_UNNEGOTIATED_ADCP_VERSION,
    response_enhancer: ResponseEnhancer | None = None,
) -> Callable[..., Any]:
    """Create a tool caller function for an ADCP handler method.

    Automatically injects context passthrough: if the request contains a
    ``context`` field, it is echoed back in the response (ADCP requirement).
    Handlers no longer need to call ``inject_context()`` manually.

    **Typed params (closes #214).** When the handler method declares its
    ``params`` parameter as a Pydantic model (e.g.
    ``params: GetProductsRequest``), the dispatcher deserialises the raw
    dict into the model before calling the handler — giving authors
    IDE autocomplete, Pydantic validation at the boundary, and typed
    attribute access instead of ``params.get(...)``. Handlers still
    declaring ``params: dict[str, Any]`` keep working unchanged. A
    Pydantic ``ValidationError`` surfaces as a structured
    ``INVALID_REQUEST`` AdCP error so callers see a spec-typed recovery
    classification rather than a raw stack trace.

    **Schema-driven validation (issue #249).** When ``validation`` is
    supplied, the dispatcher validates incoming requests and outgoing
    responses against the bundled AdCP JSON schemas. Request failures
    raise ``ADCPTaskError(VALIDATION_ERROR)`` before the handler runs,
    so malformed payloads never hit business logic. Response failures
    either raise ``VALIDATION_ERROR`` (strict) or log a warning
    (warn). Defaults to off on the server side — the client-side
    hooks already catch drift for SDK-built clients, and enabling
    server validation is a deliberate opt-in for authors who want
    dispatcher-level enforcement.

    **Pre-validation hooks (issues #614, #859).** When
    ``pre_validation_hook`` is supplied, each hook is called with
    ``(tool_name, shallow_copy_of_args)`` and must return a ``dict`` that
    replaces the wire args before schema validation and Pydantic
    ``model_validate`` run. Pass either one hook or an ordered sequence of
    hooks; sequences run left-to-right. The framework passes a shallow
    copy of the incoming params dict to each hook, so a hook may mutate
    its argument freely or return a brand-new dict — either style is safe.
    The original wire params are captured before the copy is made, so
    context echo always reflects what the buyer sent. Use this to apply
    spec-mandated defaults for pre-v3 buyers that omit required fields
    (e.g. ``buying_mode``, ``format_id`` shape coercion, ``asset_type``
    inference). The hook runs on every call; keep it fast.
    Exceptions from the hook surface as ``INVALID_REQUEST`` — do not raise
    for missing-but-defaultable fields, only for structurally unusable args.

    **Unknown-field policy (issue #858).** When
    ``validation=ValidationHookConfig(unknown_fields=...)`` is supplied,
    unsupported top-level tool arguments are handled after pre-validation
    hooks and legacy adapters, but before request schema validation and
    Pydantic coercion. ``"reject"`` raises ``INVALID_REQUEST``,
    ``"strip"`` removes unsupported keys, and ``"ignore"`` preserves the
    current permissive behavior.

    .. note::
        For the specific case of buyers omitting ``account``, see issue
        #623 ("Typed dispatcher rejects valid request when ``account`` is
        omitted") — that will be the canonical spec-level fix for that
        field. Once #623 lands you can drop any ``account`` placeholder
        hook entry.

    Args:
        handler: The ADCP handler instance
        method_name: Name of the method to call
        validation: Optional :class:`ValidationHookConfig` with
            per-side modes (``strict`` / ``warn`` / ``off``). Omitting
            it disables server-side schema validation entirely.
        pre_validation_hook: Optional callable or ordered sequence of
            callables ``(tool_name, args) -> args`` invoked on the raw wire
            dict before schema + Pydantic validation. See the
            **Pre-validation hooks** section above.
        default_unnegotiated_adcp_version: Release-precision version to use
            when the buyer supplies no version envelope. MCP uses ``"3.0"``
            for legacy compatibility. A2A passes ``None`` so omitted version
            means the current SDK wire shape.
        response_enhancer: Optional server-wide :data:`ResponseEnhancer`
            applied to every successful response after context echo and
            before schema validation. See :data:`ResponseEnhancer` for the
            two supported arities and the failure/idempotency semantics.

    Returns:
        Async callable ``call_tool(params, context=None)``. The ``context``
        parameter is optional — transports that can extract caller identity
        from their auth layer (A2A's ``ServerCallContext.user``, custom
        FastMCP auth middleware, etc.) should pass a populated
        :class:`ToolContext` so the server middleware layer (idempotency
        per-principal scoping, audit logging) gets the real principal. When
        no context is supplied, a bare :class:`ToolContext` is used.
    """
    from pydantic import ValidationError

    from adcp.canonical_formats import (
        CanonicalFormatLegacyResolutionError,
        CreativeDialect,
        CreativeDialectError,
        LegacyCreativeProjectionError,
        normalize_legacy_creative_request,
        project_canonical_response_to_legacy,
        resolve_creative_dialect,
    )
    from adcp.compat.legacy import LEGACY_ADAPTER_VERSIONS, get_legacy_adapter
    from adcp.exceptions import ADCPTaskError
    from adcp.server.helpers import inject_context
    from adcp.types import Error
    from adcp.validation.envelope import UnsupportedVersionError, detect_wire_version
    from adcp.validation.schema_errors import build_adcp_validation_error_payload
    from adcp.validation.schema_validator import (
        format_issues,
        validate_request,
        validate_response,
    )

    adopter_method_name = {
        "build_creative": "build_creative_legacy",
        "list_creative_formats": "list_creative_formats_legacy",
        "preview_creative": "preview_creative_legacy",
    }.get(method_name, method_name)
    method = getattr(handler, adopter_method_name)
    params_model = _resolve_params_pydantic_model(method)

    # Opt-in server-side schema modes. ``None`` keeps validation off
    # entirely (zero overhead on the hot path) — the TS-port default for
    # ``createAdcpServer`` is the same: validation is an explicit opt-in.
    request_mode = validation.requests if validation is not None else None
    response_mode = validation.responses if validation is not None else None
    unknown_field_policy = _normalize_unknown_field_policy(
        validation.unknown_fields if validation is not None else None
    )
    pre_validation_hooks = _flatten_pre_validation_hooks(pre_validation_hook)

    async def call_tool(params: dict[str, Any], context: ToolContext | None = None) -> Any:
        ctx = context if context is not None else ToolContext()

        raw_params = params  # Preserve original wire params for context echo.

        if pre_validation_hooks:
            try:
                params = _apply_pre_validation_hooks(
                    pre_validation_hooks, method_name, dict(params)
                )
            except PreValidationHookError as exc:
                raise ADCPTaskError(
                    operation=method_name,
                    errors=[
                        Error(
                            code="INVALID_REQUEST",
                            message=str(exc),
                        )
                    ],
                ) from exc

        # Wire-version detection: read ``adcp_version`` / ``adcp_major_version``
        # off the post-hook params (legacy buyers may rely on a hook to
        # populate the envelope, so this runs after pre_validation_hook).
        # ``None`` initially means the buyer didn't claim a version.
        # After legacy shape probes run, native unnegotiated traffic is
        # pinned to 3.0 compatibility because those buyers predate the
        # release-precision ``adcp_version`` field and the 3.1 status split.
        # An explicit unsupported claim always fails before validation or
        # handler dispatch. Only an omitted envelope may continue into the
        # legacy shape probes below.
        try:
            wire_version = detect_wire_version(params)
        except UnsupportedVersionError as exc:
            raise ADCPTaskError(
                operation=method_name,
                errors=[
                    Error(
                        code="VERSION_UNSUPPORTED",
                        message=str(exc),
                        # Preserve the wire field's original type so buyer
                        # telemetry sees the same shape it sent (int for
                        # ``adcp_major_version``, str for ``adcp_version``).
                        details={
                            "claimed_version": exc.wire_value,
                            "supported_versions": list(exc.supported),
                        },
                    )
                ],
            ) from exc

        # Shape-based legacy detection (issue: real v2.5 buyers can't
        # send ``adcp_version`` — the field didn't exist in the v2.5
        # schema). When the envelope is empty and a legacy adapter
        # registers an ``is_legacy_shape`` probe, run it. A match
        # promotes ``wire_version`` to the probe's version so the
        # adapter path below fires normally. Bias is conservative:
        # probes return ``True`` only on fields v3 doesn't emit
        # (``brand_manifest``, ``creative_ids``, bare-string
        # ``format_id``). False positives downgrade a real v3 buyer
        # to legacy validation, which is the worst outcome.
        if wire_version is None:
            for candidate in LEGACY_ADAPTER_VERSIONS:
                candidate_adapter = get_legacy_adapter(candidate, method_name)
                if candidate_adapter is None:
                    continue
                probe = candidate_adapter.is_legacy_shape
                if probe is None:
                    continue
                try:
                    matched = probe(params) if isinstance(params, dict) else False
                except Exception:  # noqa: BLE001 — defensive: probes are pure-ish
                    matched = False
                if matched:
                    logger.info(
                        "Detected %s wire shape for %s (no envelope version "
                        "supplied); routing through legacy adapter.",
                        candidate,
                        method_name,
                    )
                    wire_version = candidate
                    break

        # A major-only envelope (``adcp_major_version: 3``) does not select
        # the 3.0 release. Discovery must still advertise the current 3.x
        # native surface; only release-precision ``adcp_version`` can suppress
        # a release-scoped feature.
        wire_release_was_negotiated = isinstance(params, dict) and isinstance(
            params.get("adcp_version"), str
        )
        if wire_version is None:
            wire_version = default_unnegotiated_adcp_version

        ctx.resolved_adcp_version = wire_version

        # Legacy-version routing: if the buyer claims (or shape-detected)
        # a version handled via the adapter path (e.g. ``"2.5"``),
        # validate the params against the legacy schema first, *then*
        # translate to the current shape. Pre-adapter validation
        # surfaces structural errors with the legacy schema's field
        # paths — far easier for the buyer to act on than a v3
        # field-path error after a confusing translation. Post-adapter
        # validation (further down) catches translator bugs against
        # the SDK pin.
        legacy_adapter: Any = None
        if wire_version in LEGACY_ADAPTER_VERSIONS:
            legacy_adapter = get_legacy_adapter(wire_version, method_name)
            if legacy_adapter is None:
                raise ADCPTaskError(
                    operation=method_name,
                    errors=[
                        Error(
                            code="INVALID_REQUEST",
                            message=(
                                f"Tool {method_name!r} is not available on "
                                f"AdCP {wire_version}; upgrade to a "
                                f"supported version or call a tool exposed "
                                f"on this legacy surface."
                            ),
                            details={"legacy_version": wire_version},
                        )
                    ],
                )

            # Pre-adapter validation against the legacy schema.
            # Only runs when validation is enabled at all
            # (``request_mode != off`` AND a config is supplied) — keeps
            # the zero-overhead path for adopters who haven't opted in.
            # ``strict`` rejects; ``warn`` logs and proceeds so the
            # adapter still gets to translate (matching the existing
            # post-adapter contract).
            if request_mode is not None and request_mode != "off":
                pre_outcome = validate_request(method_name, params, version=wire_version)
                if not pre_outcome.valid:
                    summary = format_issues(pre_outcome.issues)
                    if request_mode == "strict":
                        payload = build_adcp_validation_error_payload(
                            method_name, "request", pre_outcome.issues
                        )
                        # Annotate with the wire version so adopter
                        # telemetry knows which schema rejected.
                        payload_details = dict(payload.get("details") or {})
                        payload_details["claimed_version"] = wire_version
                        payload["details"] = payload_details
                        raise ADCPTaskError(
                            operation=method_name,
                            errors=[Error(**payload)],
                        )
                    logger.warning(
                        "Schema validation warning (pre-adapter %s) for %s: %s",
                        wire_version,
                        method_name,
                        summary,
                    )

            try:
                params = legacy_adapter.adapt_request(params)
            except Exception as exc:
                raise ADCPTaskError(
                    operation=method_name,
                    errors=[
                        Error(
                            code="INVALID_REQUEST",
                            message=(
                                f"Legacy adapter for {method_name!r} at "
                                f"AdCP {wire_version} failed: "
                                f"{type(exc).__name__}: {exc}"
                            ),
                        )
                    ],
                ) from exc
            # Adapter output is validated against the SDK pin
            # (catches translator bugs with v3 field paths). The
            # ``post_adapter_validator_version`` name documents
            # which side of the adapter this value plays.
            post_adapter_validator_version: str | None = None
        else:
            post_adapter_validator_version = wire_version
        response_validator_version = None if legacy_adapter is not None else wire_version

        creative_boundary_tools = {
            "create_media_buy",
            "get_creative_delivery",
            "get_media_buy_delivery",
            "get_media_buys",
            "get_products",
            "list_creatives",
            "sync_creatives",
            "update_media_buy",
        }
        legacy_projection_sources: list[Any] = []
        if method_name in creative_boundary_tools and isinstance(params, dict) and wire_version:
            try:
                dialect = (
                    CreativeDialect.LEGACY
                    if not str(wire_version).startswith("3.")
                    else resolve_creative_dialect(
                        wire_version,
                        capabilities=_creative_capabilities_for_handler(handler),
                        request=params,
                    )
                )
                if dialect is CreativeDialect.LEGACY:
                    params = normalize_legacy_creative_request(
                        params,
                        legacy_format_converter=getattr(handler, "legacy_format_converter", None),
                        projection_sources=legacy_projection_sources,
                    )
                    # The normalized object is canonical handler input, not a
                    # valid instance of the caller's legacy wire schema.
                    post_adapter_validator_version = None
            except (CreativeDialectError, LegacyCreativeProjectionError) as exc:
                raise ADCPTaskError(
                    operation=method_name,
                    errors=[
                        Error(
                            code="INVALID_REQUEST",
                            message=str(exc),
                            suggestion=(
                                "Publish a canonical format declaration or configure the "
                                "server's legacy_format_converter"
                            ),
                        )
                    ],
                ) from exc

        if isinstance(params, dict):
            params = _apply_unknown_field_policy(
                method_name,
                params,
                policy=unknown_field_policy,
                version=post_adapter_validator_version,
                params_model=params_model,
            )

        if request_mode is not None and request_mode != "off":
            outcome = validate_request(method_name, params, version=post_adapter_validator_version)
            if not outcome.valid:
                summary = format_issues(outcome.issues)
                if request_mode == "strict":
                    payload = build_adcp_validation_error_payload(
                        method_name, "request", outcome.issues
                    )
                    raise ADCPTaskError(
                        operation=method_name,
                        errors=[Error(**payload)],
                    )
                logger.warning(
                    "Schema validation warning (request) for %s: %s",
                    method_name,
                    summary,
                )

        call_params: Any = params
        if params_model is not None and isinstance(params, dict):
            try:
                call_params = params_model.model_validate(params)
            except ValidationError as exc:
                # Surface as a structured AdCP error so MCP clients see
                # INVALID_REQUEST with a field-level pointer instead of
                # a raw Pydantic traceback. translate_error maps this
                # to ToolError (MCP) / ServerError (A2A) per transport.
                #
                # Strip ``input``/``ctx``/``url`` from the Pydantic error
                # details — they echo the raw offending value verbatim
                # (``input`` in particular). In multi-hop agent chains the
                # response flows through intermediaries, so echoing the
                # user-supplied value is a PII/secret-leak vector: a
                # mistyped API key or secret-shaped idempotency_key could
                # land in the broker's logs. The field path in
                # ``Error.field`` is all clients need to programmatically
                # locate the bad value in their own request.
                errors_list = exc.errors(
                    include_input=False, include_context=False, include_url=False
                )
                # Narrow discriminated-union failures to the variant
                # the user actually intended (Stability AI Emma P2:
                # 60-line dump → focused error). For non-union
                # failures the function is a no-op.
                #
                # Defensive: if the narrowing helper itself raises
                # (heuristic edge case, future pydantic format
                # change), keep the original error list rather than
                # 500'ing the wire path. The narrowed-error UX is a
                # nice-to-have; correctness is surfacing SOME error.
                try:
                    errors_list = list(narrow_union_errors(errors_list))
                except Exception:
                    logger.warning(
                        "narrow_union_errors raised on %s — passing through "
                        "unfiltered errors. This is a bug in the narrowing "
                        "heuristic, NOT in the validation itself.",
                        method_name,
                        exc_info=True,
                    )
                first: dict[str, Any] = dict(errors_list[0]) if errors_list else {}
                field_path = ".".join(str(loc) for loc in first.get("loc", ()))
                message = first.get("msg", "validation failed")
                suggestion = (
                    f"Invalid value for field {field_path!r}: {message}"
                    if field_path
                    else f"Request validation failed: {message}"
                )
                raise ADCPTaskError(
                    operation=method_name,
                    errors=[
                        Error(
                            code="INVALID_REQUEST",
                            field=field_path or None,
                            message=suggestion,
                            details={"validation_errors": errors_list},
                        )
                    ],
                ) from exc
        result = await method(call_params, ctx)
        if method_name == "get_adcp_capabilities":
            if isinstance(result, dict):
                from adcp.canonical_formats.dialect import canonical_creatives_capability
                from adcp.server.responses import _apply_canonical_creatives_capability

                # Capture only the adopter's raw, version-independent feature
                # declaration. Do not retain the negotiated response: 3.0
                # intentionally removes this field and must not poison another
                # caller's 3.1 routing decision.
                declared_canonical = canonical_creatives_capability(result)
                if declared_canonical is None:
                    supported_protocols = result.get("supported_protocols")
                    if isinstance(supported_protocols, list) and "media_buy" in supported_protocols:
                        # The canonical framework default is the declaration
                        # even when a negotiated 3.0 response must suppress the
                        # 3.1-only feature field on the wire.
                        declared_canonical = True
                if declared_canonical is not None:
                    setattr(
                        handler,
                        "_declared_creative_capabilities",
                        {
                            "media_buy": {
                                "features": {
                                    "canonical_creatives": declared_canonical,
                                }
                            }
                        },
                    )
                _apply_canonical_creatives_capability(
                    result,
                    # Discovery without a release envelope advertises the
                    # server's current native surface. Only an explicit
                    # negotiated 3.0 request suppresses the 3.1 feature.
                    adcp_version=(wire_version if wire_release_was_negotiated else None),
                )
        if (
            method_name in creative_boundary_tools
            and wire_version
            and (
                not str(wire_version).startswith("3.")
                or resolve_creative_dialect(
                    wire_version,
                    capabilities=_creative_capabilities_for_handler(handler),
                    request=raw_params,
                )
                is CreativeDialect.LEGACY
            )
        ):
            try:
                result = project_canonical_response_to_legacy(
                    result,
                    resolver=getattr(handler, "canonical_format_legacy_resolver", None),
                    sources=legacy_projection_sources,
                )
            except CanonicalFormatLegacyResolutionError as exc:
                raise ADCPTaskError(
                    operation=method_name,
                    errors=[
                        Error(
                            code="INTERNAL_ERROR",
                            message=str(exc),
                            suggestion=(
                                "Configure canonical_format_legacy_resolver or return a "
                                "same-process projection retaining its original tuple"
                            ),
                        )
                    ],
                ) from exc
        # Convert Pydantic models to JSON-safe dicts for MCP serialization
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json", exclude_none=True)
        # ADCP requires echoing context from request to response — read
        # from the raw dict the transport sent, not from the validated
        # model (which won't carry the wire ``context`` field).
        if isinstance(result, dict):
            _normalize_response_envelope(
                method_name,
                result,
                raw_params,
                adcp_version=response_validator_version,
            )
            inject_context(raw_params, result)
            # Run the seller's response enhancer AFTER ``inject_context``
            # (so it sees the credential-stripped echo envelope and can't
            # re-introduce a credential) and BEFORE ``validate_response``
            # (so any conformance-breaking mutation surfaces as a
            # VALIDATION_ERROR rather than shipping malformed). This single
            # site covers framework tools, custom tools
            # (``get_task_status`` / ``list_tasks``), and
            # ``get_adcp_capabilities`` on both MCP and A2A. The L3 error
            # envelope is enhanced on the dedicated error paths
            # (``build_mcp_error_result`` / ``_send_adcp_error``), so skip
            # it here to avoid a double pass.
            if "adcp_error" not in result:
                _apply_response_enhancer(response_enhancer, method_name, result, ctx)

        if response_mode is not None and response_mode != "off" and isinstance(result, dict):
            # Skip validation when the handler returned the AdCP L3
            # error envelope (``{"adcp_error": {...}}``). That envelope
            # has its own shape enforced by the ``Error`` builder; the
            # per-tool response schema would false-positive on it and
            # convert a real protocol error into a fake VALIDATION_ERROR.
            if "adcp_error" not in result:
                outcome = validate_response(method_name, result, version=response_validator_version)
                if not outcome.valid:
                    summary = format_issues(outcome.issues)
                    logger.warning(
                        "Schema validation warning (response) for %s: %s",
                        method_name,
                        summary,
                    )
                    if response_mode == "strict":
                        payload = build_adcp_validation_error_payload(
                            method_name, "response", outcome.issues
                        )
                        raise ADCPTaskError(
                            operation=method_name,
                            errors=[Error(**payload)],
                        )

        # Legacy adapter response rewrite: when the buyer is on a legacy
        # wire shape and the adapter declares a ``normalize_response``
        # callable, translate the current-shape response back so the
        # buyer sees the dict shape they expected. Runs *after*
        # validation (which validated the current shape) so a malformed
        # legacy rewrite doesn't mask handler bugs.
        if legacy_adapter is not None and legacy_adapter.normalize_response is not None:
            if isinstance(result, dict) and "adcp_error" not in result:
                try:
                    result = legacy_adapter.normalize_response(result)
                except Exception as exc:
                    raise ADCPTaskError(
                        operation=method_name,
                        errors=[
                            Error(
                                code="INTERNAL_ERROR",
                                message=(
                                    f"Legacy response normalizer for "
                                    f"{method_name!r} at AdCP "
                                    f"{wire_version} failed: "
                                    f"{type(exc).__name__}: {exc}"
                                ),
                            )
                        ],
                    ) from exc
        if (
            legacy_adapter is not None
            and isinstance(result, dict)
            and result.get("status") == "completed"
        ):
            result.pop("status")
        return result

    return call_tool


class MCPToolSet:
    """Collection of MCP tools from an ADCP handler.

    Provides tool definitions and handlers for registering with an MCP server.
    """

    def __init__(
        self,
        handler: ADCPHandler[Any],
        *,
        advertise_all: bool = False,
        validation: ValidationHookConfig | None = None,
        pre_validation_hooks: PreValidationHooks | None = None,
        response_enhancer: ResponseEnhancer | None = None,
        adcp_version: str | None = None,
    ):
        """Create tool set from handler.

        Args:
            handler: ADCP handler instance.
            advertise_all: When True, advertise every tool the handler
                type supports — even those whose method is still the
                SDK's ``not_supported`` default. See
                :func:`get_tools_for_handler` for the default behavior
                (override-filtered advertisement).
            validation: Opt-in schema validation config applied to every
                tool caller. See :func:`create_tool_caller`.
            pre_validation_hooks: Optional dict mapping tool name to a
                ``(tool_name, args) -> args`` callable or ordered sequence.
                Applied before schema + Pydantic validation. See
                :func:`create_tool_caller`.
            response_enhancer: Optional server-wide :data:`ResponseEnhancer`
                applied to every successful response. See
                :func:`create_tool_caller`.
        """
        self.handler = handler
        resolved_adcp_version = _resolve_handler_adcp_version(handler, adcp_version)
        self._filtered_definitions = get_tools_for_handler(
            handler,
            advertise_all=advertise_all,
            adcp_version=resolved_adcp_version,
        )
        self._tools: dict[str, Callable[..., Any]] = {}

        # Create tool callers only for filtered tools
        for tool_def in self._filtered_definitions:
            name = tool_def["name"]
            hook = (pre_validation_hooks or {}).get(name)
            self._tools[name] = create_tool_caller(
                handler,
                name,
                validation=validation,
                pre_validation_hook=hook,
                response_enhancer=response_enhancer,
                default_unnegotiated_adcp_version=(
                    resolved_adcp_version or DEFAULT_UNNEGOTIATED_ADCP_VERSION
                ),
            )

    @property
    def tool_definitions(self) -> list[dict[str, Any]]:
        """Get MCP tool definitions filtered by handler type."""
        return list(self._filtered_definitions)

    async def call_tool(self, name: str, params: dict[str, Any]) -> Any:
        """Call a tool by name.

        Args:
            name: Tool name
            params: Tool parameters

        Returns:
            Tool result

        Raises:
            KeyError: If tool not found
        """
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return await self._tools[name](params)

    def get_tool_names(self) -> list[str]:
        """Get list of available tool names."""
        return list(self._tools.keys())


def create_mcp_tools(
    handler: ADCPHandler[Any],
    *,
    advertise_all: bool = False,
    validation: ValidationHookConfig | None = None,
    pre_validation_hooks: PreValidationHooks | None = None,
    response_enhancer: ResponseEnhancer | None = None,
    adcp_version: str | None = None,
) -> MCPToolSet:
    """Create MCP tools from an ADCP handler.

    This is the main entry point for MCP server integration.

    Example with mcp library:
        from mcp.server import Server
        from adcp.server import ContentStandardsHandler, create_mcp_tools

        class MyHandler(ContentStandardsHandler):
            # ... implement methods

        handler = MyHandler()
        tools = create_mcp_tools(handler)

        server = Server("my-content-agent")

        @server.list_tools()
        async def list_tools():
            return tools.tool_definitions

        @server.call_tool()
        async def call_tool(name: str, arguments: dict):
            return await tools.call_tool(name, arguments)

    Args:
        handler: ADCP handler instance.
        advertise_all: When True, advertise every tool the handler type
            supports — even those whose method is still the SDK's
            ``not_supported`` default. See :func:`get_tools_for_handler`.
        validation: Opt-in schema validation config. When supplied,
            every tool caller validates requests and responses against
            the bundled AdCP JSON schemas. See
            :func:`create_tool_caller` for mode semantics.
        pre_validation_hooks: Optional dict mapping tool name to a
            ``(tool_name, args) -> args`` callable or ordered sequence.
            Applied before schema + Pydantic validation. See
            :func:`create_tool_caller`.
        response_enhancer: Optional server-wide :data:`ResponseEnhancer`
            applied to every successful response. See
            :func:`create_tool_caller`.
        adcp_version: Trusted server protocol pin for version-scoped
            ``tools/list`` schemas. Decorator-built handlers carry this pin
            automatically; class-based handlers can pass it here.

    Returns:
        MCPToolSet with tool definitions and handlers.
    """
    return MCPToolSet(
        handler,
        advertise_all=advertise_all,
        validation=validation,
        pre_validation_hooks=pre_validation_hooks,
        response_enhancer=response_enhancer,
        adcp_version=adcp_version,
    )
