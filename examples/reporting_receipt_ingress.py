"""Authenticated durable receipts on the existing materializer store and mounts.

Install migrations during deployment, drain legacy materialization writers, and
supply the application's token validator and fresh account ACL. Receipt traffic
negotiates AdCP 3.2-rc.6. This composition does not activate production tier claims.
"""

from __future__ import annotations

from adcp.decisioning.registry import BuyerAgentRegistry
from adcp.reporting.feed import ReportingFeedStore
from adcp.reporting.receipts import (
    PgReportingReceiptStore,
    ReceiptAccountResolver,
    ReportingReceiptHandler,
)
from adcp.server import serve
from adcp.server.auth import BearerTokenAuth, auth_context_factory


async def compose_receipt_ingress(
    store: PgReportingReceiptStore,
    *,
    resolve_account: ReceiptAccountResolver,
    buyer_agents: BuyerAgentRegistry | None = None,
) -> ReportingReceiptHandler:
    """Validate the installed schema and bind the live authorization boundary.

    The resolver receives the exact account reference and canonical consumer on
    every request, including replay. Resolve natural account keys and check the
    current ACL there; return a storage account ID or raise
    ReportingReceiptError("UNAUTHORIZED"). It must never derive a consumer from
    tenancy, request fields, or RequestContext.caller_identity's cache key.

    This same store can be passed to compose_materializer in
    reporting_durable_materializer.py. No parallel materializer or readiness
    switch is needed. Schema installation is an explicit deployment operation.
    """
    await store.receipt_ingestion_ready()
    # PgReportingFeedStore is an additive subtype of the same receipt and
    # materializer store. Its actual schema enables the periods read route;
    # no adopter-maintained capability boolean or second facade is needed.
    if isinstance(store, ReportingFeedStore):
        await store.reporting_feed_ready()
    return ReportingReceiptHandler(
        store, resolve_account=resolve_account, buyer_agents=buyer_agents
    )


def serve_receipt_ingress(
    handler: ReportingReceiptHandler,
    *,
    auth: BearerTokenAuth,
    public_url: str,
    allowed_hosts: tuple[str, ...],
) -> None:
    """Use the production MCP and A2A mounts with one authenticated handler.

    Token validation supplies trusted Principal identities, never credentials in
    request bodies. Registry-backed deployments populate AuthInfo credentials
    through their verified adapter. Generic SDK idempotency middleware is
    automatically bypassed for this task; the store owns durable replay.
    """
    serve(
        handler,
        name="reporting-receipts",
        transport="both",
        auth=auth,
        context_factory=auth_context_factory,
        public_url=public_url,
        allowed_hosts=allowed_hosts,
    )
