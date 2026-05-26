"""Hello-seller — the canonical v6.0 DecisioningPlatform starting point.

A minimal :class:`SalesPlatform` adopter showing the full required surface:

* :class:`DecisioningCapabilities` declared on the class body
* :class:`SingletonAccounts` for the dev/single-tenant case
* Nine ``sales-non-guaranteed`` methods — five hard-required
  (``get_products``, ``create_media_buy``, ``update_media_buy``,
  ``sync_creatives``, ``get_media_buy_delivery``) plus four soft-required
  by the SalesPlatform Protocol in v6.0 rc.1+
  (``get_media_buys``, ``list_creative_formats``, ``list_creatives``,
  ``provide_performance_feedback``; in
  :data:`~adcp.decisioning.dispatch.RECOMMENDED_METHODS_PER_SPECIALISM`).
  The authoritative list is
  :data:`~adcp.decisioning.dispatch.REQUIRED_METHODS_PER_SPECIALISM` +
  :data:`~adcp.decisioning.dispatch.RECOMMENDED_METHODS_PER_SPECIALISM`;
  ``validate_platform`` checks both at server boot — hard-fail for the
  five, soft-warn for the four.

Run::

    uv run python examples/hello_seller.py

Then:

* MCP discovery: connect with any AdCP MCP buyer
* List tools: should advertise the 9 seller methods + the
  framework's protocol tools
* Call ``get_products``: returns one product
* Call ``create_media_buy``: returns the success envelope

# Minimum valid buyer payloads

The seller-side stubs below ARE valid; the bit that's not obvious is
what the BUYER must send. Each AdCP request type has fields the
schema requires that aren't on the example response. Common ones:

* ``idempotency_key`` (mutating tools) — string, ``min_length=16``,
  buyer-chosen replay-window key. Pad shorter test values with a
  random suffix (``"emma-test-create-media-buy-001"``).
* ``buying_mode`` on ``GetProductsRequest`` — ``"brief"`` |
  ``"signal"`` | ``"audience"`` | ``"performance"``.
* ``account`` on most mutating tools — ``{"account_id": "..."}``.
* ``packages`` on ``CreateMediaBuyRequest`` — non-empty list with
  ``buyer_ref`` + ``products`` + ``budget``.
* ``destinations`` on ``ActivateSignalRequest`` — non-empty list of
  ``{"type": "platform", "platform": "..."}``.

Minimum valid ``create_media_buy`` payload::

    {
      "account": {"account_id": "hello"},
      "buyer_ref": "buyer-1",
      "promoted_offering": "shoes",
      "packages": [{
        "buyer_ref": "pkg-1",
        "products": ["display-rotation"],
        "budget": {"total": 100, "currency": "USD"}
      }],
      "po_number": "PO-1",
      "idempotency_key": "buyer-create-mb-001-padding",
      "start_time": "2026-06-01T00:00:00Z",
      "end_time": "2026-06-30T23:59:59Z"
    }

For the full set of required fields per tool, see
``schemas/cache/media-buy/create-media-buy-request.json`` (and the
corresponding files for each tool) in the SDK distribution.
"""

from __future__ import annotations

from typing import Any

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    RequestContext,
    SingletonAccounts,
    disallowed_update_media_buy_mutations,
    serve,
)


class HelloSeller(DecisioningPlatform):
    """The canonical v6.0 sales-non-guaranteed adopter.

    Implements all nine methods of the ``sales-non-guaranteed`` surface: the
    five hard-required (``get_products``, ``create_media_buy``,
    ``update_media_buy``, ``sync_creatives``, ``get_media_buy_delivery``)
    and the four soft-required by the SalesPlatform Protocol in v6.0 rc.1+
    (``get_media_buys``, ``list_creative_formats``, ``list_creatives``,
    ``provide_performance_feedback``;
    see :data:`~adcp.decisioning.dispatch.RECOMMENDED_METHODS_PER_SPECIALISM`).

    ``validate_platform`` runs at boot and fails fast on any missing
    hard-required method; it soft-warns (or hard-fails in strict mode) for
    the four rc.1+ methods. This example passes all checks cleanly.
    """

    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        channels=["display"],
        pricing_models=["cpm"],
    )
    accounts = SingletonAccounts(account_id="hello")

    def get_products(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Return a single example product. Sync — no HITL."""
        return {
            "products": [
                {
                    "product_id": "display-rotation",
                    "name": "Display Rotation",
                    "description": "300x250 banner across our example properties",
                    "delivery_type": "non_guaranteed",
                    "publisher_properties": [
                        {"publisher_domain": "example.com", "selection_type": "all"},
                    ],
                    "format_ids": [
                        {
                            "agent_url": "https://creative.adcontextprotocol.org/",
                            "id": "display_300x250",
                        },
                    ],
                    "pricing_options": [
                        {
                            "pricing_option_id": "po-cpm-default",
                            "pricing_model": "cpm",
                            "floor_price": 5.0,
                            "currency": "USD",
                        },
                    ],
                    "reporting_capabilities": {
                        "available_metrics": ["impressions", "spend"],
                        "available_reporting_frequencies": ["daily"],
                        "date_range_support": "date_range",
                        "supports_webhooks": False,
                        "expected_delay_minutes": 60,
                        "timezone": "UTC",
                    },
                    "delivery_measurement": {"provider": "internal"},
                },
            ],
        }

    def create_media_buy(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Sync fast path — accept the request and return a media_buy_id.

        Production sellers branch on a budget/policy check here and
        return :meth:`ctx.handoff_to_task(fn)` for HITL review (see
        ``examples/hello_seller_async_handoff.py``). Hello-seller
        accepts everything; reject obviously-broken budgets via
        :class:`AdcpError`.
        """
        # Pre-flight: reject zero-budget requests with a structured
        # error so buyers get a clear correction signal. Real sellers
        # check against a published floor; this just demonstrates the
        # AdcpError raise-and-project pattern.
        packages = self._get_packages(req)
        if not packages:
            raise AdcpError(
                "INVALID_REQUEST",
                message="At least one package is required",
                field="packages",
                recovery="correctable",
            )

        return {
            "media_buy_id": f"mb_{ctx.account.id}_{len(packages)}",
            "status": "active",
            "packages": [
                {
                    "package_id": f"pkg_{i}",
                    "product_id": pkg.get("product_id", "display-rotation"),
                    "pricing_option_id": pkg.get("pricing_option_id", "po-cpm-default"),
                }
                for i, pkg in enumerate(packages)
            ],
        }

    def update_media_buy(
        self,
        media_buy_id: str,
        patch: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Sync update with explicit allowed-action validation.

        The ``(media_buy_id, patch, ctx)`` signature mirrors the
        :class:`SalesPlatform` Protocol (D1 arg-projection — the
        framework's handler.py shim splits the wire request shape
        into separate kwargs).
        """
        available_actions = [
            {"action": "pause", "mode": "self_serve"},
            {"action": "cancel", "mode": "self_serve"},
            {"action": "update_budget", "mode": "self_serve"},
            {"action": "update_pacing", "mode": "self_serve"},
        ]
        current_media_buy: dict[str, Any] = {
            "media_buy_id": media_buy_id,
            "status": "active",
            "available_actions": available_actions,
            "packages": [
                {
                    "package_id": "pkg_0",
                    "budget": 1000.0,
                    "pacing": "even",
                }
            ],
        }
        disallowed = disallowed_update_media_buy_mutations(
            patch,
            available_actions,
            current_media_buy=current_media_buy,
        )
        if disallowed:
            first = disallowed[0]
            raise AdcpError(
                "ACTION_NOT_ALLOWED",
                message=f"Update action '{first.action}' is not available for this media buy",
                recovery="correctable",
                field=", ".join(first.field_paths),
                details={
                    "requested_actions": [mutation.action for mutation in disallowed],
                    "allowed_actions": [action["action"] for action in available_actions],
                },
            )

        return {
            "media_buy_id": media_buy_id,
            "status": "active",
            "packages": [],
        }

    def sync_creatives(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Sync creative review — auto-approve every submitted
        creative. Production sellers run S&P review here and either
        return mixed approved/pending rows, or hand off the entire
        batch via :meth:`ctx.handoff_to_task` for trafficker
        review (see the async-handoff example)."""
        creatives = getattr(req, "creatives", None) or []
        return {
            "creatives": [
                {
                    "creative_id": (
                        c.creative_id if hasattr(c, "creative_id") else c.get("creative_id")
                    ),
                    "approval_status": "approved",
                }
                for c in creatives
            ],
        }

    def get_media_buy_delivery(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Stub delivery snapshot — flat zeros.

        Wire field is ``media_buy_deliveries`` per
        ``schemas/cache/media-buy/get-media-buy-delivery-response.json``.
        """
        return {
            "media_buy_deliveries": [
                {
                    "media_buy_id": getattr(req, "media_buy_id", "mb_unknown"),
                    "totals": {"impressions": 0, "spend": 0.0},
                },
            ],
        }

    # ---- v6.0 rc.1 recommended methods (RECOMMENDED_METHODS_PER_SPECIALISM) --
    # Staged for promotion to hard-required at v6.0 rc.1 GA. Stubs return the
    # minimal valid wire shape. Wire each to your production systems before
    # declaring rc.1 compliance (set ADCP_DECISIONING_STRICT_VALIDATE_PLATFORM=1
    # in CI to confirm the full surface is present).

    def get_media_buys(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """List media buys for the resolved account.

        Return all active media buys here. Wire to your order-management
        system in production; buyers use this for status polling and
        reconciliation.
        """
        return {"media_buys": []}

    def list_creative_formats(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Catalog of accepted creative formats.

        Return the full list of format definitions this seller accepts.
        Wire to your creative-spec registry in production; buyers use this
        to validate creatives before submission.
        """
        return {"formats": []}

    def list_creatives(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """List the seller's view of buyer-uploaded creatives.

        Return all creatives the buyer has synced. Wire to your creative
        asset store in production; buyers use this to check approval
        statuses and discover available creatives.

        ``query_summary`` and ``pagination`` are required by the spec
        envelope — fill ``total_matching``/``returned`` from the
        post-filter result count and ``has_more`` from the cursor state
        once you have a real store.
        """
        return {
            "query_summary": {"total_matching": 0, "returned": 0},
            "pagination": {"has_more": False},
            "creatives": [],
        }

    def provide_performance_feedback(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Buyer-supplied performance signal back to the seller.

        Acknowledge receipt and log to your analytics pipeline in
        production. Buyers send conversion events (clicks, installs,
        purchases) here so the seller can optimize pacing and targeting.
        """
        return {"success": True}

    # -------------------------------------------------------------------------

    @staticmethod
    def _get_packages(req: Any) -> list[dict[str, Any]]:
        """Pull the wire ``packages`` array from the request, tolerating
        both Pydantic and dict shapes (the framework's typed dispatch
        gives Pydantic; tests / scripts may pass dicts)."""
        if hasattr(req, "packages"):
            packages = req.packages or []
            return [p.model_dump() if hasattr(p, "model_dump") else dict(p) for p in packages]
        if isinstance(req, dict):
            return list(req.get("packages") or [])
        return []


if __name__ == "__main__":
    # serve() builds the PlatformHandler, allocates the executor +
    # registry, validates the platform at boot, and starts the MCP
    # server. Default port 3001 over streamable-http; override via
    # ``serve(seller, port=...)``.
    #
    # ``auto_emit_completion_webhooks=False`` opts out here because this
    # example has no signing key.  Production sellers want webhooks on so
    # buyers who register ``push_notification_config.url`` get sync-
    # completion notifications.  Pick a constructor and pass
    # ``webhook_supervisor=`` (retry + circuit breaker, recommended) or
    # ``webhook_sender=`` (transport only):
    #
    #   from adcp.webhook_sender import WebhookSender
    #   from adcp.webhook_supervisor import InMemoryWebhookDeliverySupervisor
    #
    #   # RFC 9421 JWK signing — AdCP spec baseline (recommended).
    #   # signing_jwk must be a dict with kid, alg, and adcp_use="webhook-signing":
    #   sender = WebhookSender.from_jwk(signing_jwk)
    #
    #   # Shared bearer token — no key management, requires TLS:
    #   sender = WebhookSender.from_bearer_token(os.environ["WEBHOOK_BEARER_TOKEN"])
    #
    #   # Standard Webhooks v1 — Svix / Resend / standardwebhooks.com interop:
    #   sender = WebhookSender.from_standard_webhooks_secret(
    #       os.environ["WHSEC"], key_id="whsec_v1",
    #   )
    #
    #   supervisor = InMemoryWebhookDeliverySupervisor(sender=sender)
    #   serve(HelloSeller(), name="hello-seller", webhook_supervisor=supervisor)
    #
    # See docs/handler-authoring.md#webhooks for the full wiring recipe.
    serve(HelloSeller(), name="hello-seller", auto_emit_completion_webhooks=False)
