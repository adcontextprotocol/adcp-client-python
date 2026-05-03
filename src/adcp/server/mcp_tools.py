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

from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.test_controller import SCENARIOS as _CONTROLLER_SCENARIOS
from adcp.types.error_narrowing import narrow_union_errors
from adcp.validation.client_hooks import ValidationHookConfig

logger = logging.getLogger(__name__)

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
    # Media Buy Operations
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

    The inliner walks the schema tree and replaces each ``$ref`` with a
    deep copy of the referenced definition. Sibling keys on the ``$ref``
    node (``description``, ``title``) are merged on top of the resolved
    body. Note: this is an annotation-level override that matches what
    Pydantic actually emits at reference sites — it is NOT spec §8.2
    merge semantics (which would evaluate siblings as an implicit
    ``allOf``). If a future Pydantic version starts emitting
    assertion-level siblings (``type``, ``enum``, etc.) the merge
    would silently change validation; today it doesn't.

    Only handles local refs (``#/$defs/X``). External refs are left in
    place — Pydantic doesn't emit them for our request models, but if
    one ever appears it surfaces to the caller rather than being
    silently stripped.

    Cycles are protected by a ``seen`` set threaded through recursion.
    Pydantic request models don't generate cyclic refs today; the guard
    exists so a future schema shape can't turn inlining into a
    RecursionError. When the walk leaves at least one ``$ref``
    unresolved (cycle or dangling), ``$defs`` is kept in place so a
    spec-compliant client can still resolve what we couldn't.
    """
    defs = schema.get("$defs", {})
    # Track whether we emitted any $ref in the output — tells the
    # caller whether it's safe to drop $defs. Avoids a
    # stringify-the-whole-tree scan post-walk, and sidesteps false
    # positives from legitimate ``"$ref"`` values inside enum / const
    # / description strings.
    unresolved = [False]

    def _resolve(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if not ref.startswith("#/$defs/"):
                    # External ref (http://…, relative path). Pydantic
                    # doesn't emit these for our request models; leave
                    # untouched rather than risk silent corruption.
                    unresolved[0] = True
                    return {k: _resolve(v, seen) for k, v in node.items()}
                def_name = ref[len("#/$defs/") :]
                if def_name in seen:
                    # Cycle — leave the $ref intact so a spec-compliant
                    # client can still resolve via $defs.
                    unresolved[0] = True
                    return {k: _resolve(v, seen) for k, v in node.items()}
                body = defs.get(def_name)
                if body is None:
                    # Dangling ref — nothing in $defs matches. Leave
                    # the $ref for consumers to error on; preserving
                    # the shape is safer than silently stripping.
                    unresolved[0] = True
                    return {k: _resolve(v, seen) for k, v in node.items()}
                resolved = _resolve(copy.deepcopy(body), seen | {def_name})
                # Annotation-level merge — sibling description/title
                # on the $ref node wins over the resolved body's
                # same-named keys.
                merged = dict(resolved) if isinstance(resolved, dict) else resolved
                if isinstance(merged, dict):
                    for k, v in node.items():
                        if k == "$ref":
                            continue
                        merged[k] = _resolve(v, seen)
                return merged
            return {k: _resolve(v, seen) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(item, seen) for item in node]
        return node

    result = _resolve(schema, frozenset())
    if isinstance(result, dict) and not unresolved[0]:
        result.pop("$defs", None)
    assert isinstance(result, dict)
    return result


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


def _generate_pydantic_schemas() -> dict[str, dict[str, Any]]:
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
            AcquireRightsRequest,
            ActivateSignalRequest,
            BuildCreativeRequest,
            CalibrateContentRequest,
            CheckGovernanceRequest,
            ComplyTestControllerRequest,
            ContextMatchRequest,
            CreateCollectionListRequest,
            CreateContentStandardsRequest,
            CreateMediaBuyRequest,
            CreatePropertyListRequest,
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
            IdentityMatchRequest,
            ListAccountsRequest,
            ListCollectionListsRequest,
            ListContentStandardsRequest,
            ListCreativeFormatsRequest,
            ListCreativesRequest,
            ListPropertyListsRequest,
            LogEventRequest,
            PreviewCreativeRequest,
            ProvidePerformanceFeedbackRequest,
            ReportPlanOutcomeRequest,
            ReportUsageRequest,
            SiGetOfferingRequest,
            SiInitiateSessionRequest,
            SiSendMessageRequest,
            SiTerminateSessionRequest,
            SyncAccountsRequest,
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
    except ImportError:
        return {}

    # Map tool names to their Pydantic request types
    _tool_to_request: dict[str, Any] = {
        # Catalog
        "get_products": GetProductsRequest,
        "list_creative_formats": ListCreativeFormatsRequest,
        # Creative
        "sync_creatives": SyncCreativesRequest,
        "list_creatives": ListCreativesRequest,
        "build_creative": BuildCreativeRequest,
        "preview_creative": PreviewCreativeRequest,
        "get_creative_delivery": GetCreativeDeliveryRequest,
        # Media Buy
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
        "get_rights": GetRightsRequest,
        "acquire_rights": AcquireRightsRequest,
        "update_rights": UpdateRightsRequest,
        # TMP
        "context_match": ContextMatchRequest,
        "identity_match": IdentityMatchRequest,
    }

    schemas: dict[str, dict[str, Any]] = {}
    for tool_name, request_type in _tool_to_request.items():
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


def _generate_pydantic_output_schemas() -> dict[str, dict[str, Any]]:
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
            AcquireRightsResponse,
            ActivateSignalResponse,
            BuildCreativeResponse,
            CalibrateContentResponse,
            CheckGovernanceResponse,
            ComplyTestControllerResponse,
            ContextMatchResponse,
            CreateCollectionListResponse,
            CreateContentStandardsResponse,
            CreateMediaBuyResponse,
            CreatePropertyListResponse,
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
            IdentityMatchResponse,
            ListAccountsResponse,
            ListCollectionListsResponse,
            ListContentStandardsResponse,
            ListCreativeFormatsResponse,
            ListCreativesResponse,
            ListPropertyListsResponse,
            LogEventResponse,
            PreviewCreativeResponse,
            ProvidePerformanceFeedbackResponse,
            ReportPlanOutcomeResponse,
            ReportUsageResponse,
            SiGetOfferingResponse,
            SiInitiateSessionResponse,
            SiSendMessageResponse,
            SiTerminateSessionResponse,
            SyncAccountsResponse,
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
        )
    except ImportError:
        return {}

    _tool_to_response: dict[str, Any] = {
        # Catalog
        "get_products": GetProductsResponse,
        "list_creative_formats": ListCreativeFormatsResponse,
        # Creative
        "sync_creatives": SyncCreativesResponse,
        "list_creatives": ListCreativesResponse,
        "build_creative": BuildCreativeResponse,
        "preview_creative": PreviewCreativeResponse,
        "get_creative_delivery": GetCreativeDeliveryResponse,
        # Media Buy
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
        "get_rights": GetRightsResponse,
        "acquire_rights": AcquireRightsResponse,
        "update_rights": UpdateRightsResponse,
        # TMP
        "context_match": ContextMatchResponse,
        "identity_match": IdentityMatchResponse,
    }

    schemas: dict[str, dict[str, Any]] = {}
    for tool_name, response_type in _tool_to_response.items():
        schema = _model_to_json_schema(response_type, allow_root_union=True)
        if schema is None:
            logger.debug(
                "Pydantic output-schema generation failed for %s",
                tool_name,
            )
            continue
        schemas[tool_name] = schema

    return schemas


# Schemas are populated lazily on the first tools/list call to avoid
# heavy Pydantic type imports at module import time. Use .update() so
# external references bound before init (e.g. in tests) stay valid.
_PYDANTIC_SCHEMAS: dict[str, dict[str, Any]] = {}
_PYDANTIC_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {}
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


def _ensure_pydantic_schemas_applied() -> None:
    """Lazily populate Pydantic schemas and apply them to tool definitions.

    Safe to call multiple times — subsequent calls are no-ops. Called
    automatically by :func:`get_tools_for_handler` on first invocation.
    Tests that read :data:`_PYDANTIC_SCHEMAS` or ``ADCP_TOOL_DEFINITIONS``
    schema fields directly should call this first (or use the session-scoped
    conftest fixture that does so automatically).
    """
    global _schemas_applied
    if _schemas_applied:
        return
    _PYDANTIC_SCHEMAS.update(_generate_pydantic_schemas())
    _PYDANTIC_OUTPUT_SCHEMAS.update(_generate_pydantic_output_schemas())
    _apply_pydantic_schemas()
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


def get_tools_for_handler(
    handler: ADCPHandler[Any] | type[ADCPHandler[Any]],
    *,
    advertise_all: bool = False,
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

    Returns:
        Filtered list of tool definitions.
    """
    _ensure_pydantic_schemas_applied()
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
        return candidates

    always_on = _PROTOCOL_TOOLS | DISCOVERY_TOOLS
    return [
        tool
        for tool in candidates
        if tool["name"] in always_on or _is_method_overridden(cls, tool["name"])
    ]


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


def create_tool_caller(
    handler: ADCPHandler[Any],
    method_name: str,
    *,
    validation: ValidationHookConfig | None = None,
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

    Args:
        handler: The ADCP handler instance
        method_name: Name of the method to call
        validation: Optional :class:`ValidationHookConfig` with
            per-side modes (``strict`` / ``warn`` / ``off``). Omitting
            it disables server-side schema validation entirely.

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

    from adcp.exceptions import ADCPTaskError
    from adcp.server.helpers import inject_context
    from adcp.types import Error
    from adcp.validation.schema_errors import build_adcp_validation_error_payload
    from adcp.validation.schema_validator import (
        format_issues,
        validate_request,
        validate_response,
    )

    method = getattr(handler, method_name)
    params_model = _resolve_params_pydantic_model(method)

    # Opt-in server-side schema modes. ``None`` keeps validation off
    # entirely (zero overhead on the hot path) — the TS-port default for
    # ``createAdcpServer`` is the same: validation is an explicit opt-in.
    request_mode = validation.requests if validation is not None else None
    response_mode = validation.responses if validation is not None else None

    async def call_tool(params: dict[str, Any], context: ToolContext | None = None) -> Any:
        ctx = context if context is not None else ToolContext()
        raw_params = params  # Preserve the original dict for context echo.

        if request_mode is not None and request_mode != "off":
            outcome = validate_request(method_name, params)
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
        # Convert Pydantic models to JSON-safe dicts for MCP serialization
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json", exclude_none=True)
        # ADCP requires echoing context from request to response — read
        # from the raw dict the transport sent, not from the validated
        # model (which won't carry the wire ``context`` field).
        if isinstance(result, dict):
            inject_context(raw_params, result)

        if response_mode is not None and response_mode != "off" and isinstance(result, dict):
            # Skip validation when the handler returned the AdCP L3
            # error envelope (``{"adcp_error": {...}}``). That envelope
            # has its own shape enforced by the ``Error`` builder; the
            # per-tool response schema would false-positive on it and
            # convert a real protocol error into a fake VALIDATION_ERROR.
            if "adcp_error" not in result:
                outcome = validate_response(method_name, result)
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
        """
        self.handler = handler
        self._filtered_definitions = get_tools_for_handler(handler, advertise_all=advertise_all)
        self._tools: dict[str, Callable[..., Any]] = {}

        # Create tool callers only for filtered tools
        for tool_def in self._filtered_definitions:
            name = tool_def["name"]
            self._tools[name] = create_tool_caller(handler, name, validation=validation)

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

    Returns:
        MCPToolSet with tool definitions and handlers.
    """
    return MCPToolSet(handler, advertise_all=advertise_all, validation=validation)
