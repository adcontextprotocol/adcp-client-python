"""One production seller composition, with actual provider and source contracts.

The adopter owns trusted source/product mappings, provider grants, the account
task and token verification. The SDK owns admission, bounded discovery, the
materializer, captured status, receipts, exact reads and optional notification
delivery. Migrate and drain older workers before starting this composition.
"""

from __future__ import annotations

from adcp.decisioning.registry import BuyerAgentRegistry
from adcp.reporting.materializer import (
    ReportingDestinationIO,
    ReportingMaterializerService,
    ReportingRevisionVerifierRegistry,
)
from adcp.reporting.outbox import (
    ReportingEnvelopeCipher,
    ReportingNotificationWorker,
    ReportingSubscriptionResolver,
)
from adcp.reporting.production import (
    PgReportingProductionStore,
    ReportingProductionConfigurationTask,
    ReportingProductionDestination,
    ReportingProductionOffering,
    ReportingProductionSigning,
    ReportingProductionSupport,
    production_notification_workers,
)
from adcp.reporting.projection import PgReportingStatusProjection
from adcp.reporting.receipts import ReceiptAccountResolver
from adcp.server import serve
from adcp.server.auth import BearerTokenAuth, auth_context_factory


async def compose_reporting(
    store: PgReportingProductionStore,
    *,
    destination: ReportingProductionDestination,
    registry: ReportingRevisionVerifierRegistry,
    offerings: tuple[ReportingProductionOffering, ...],
    configuration_task: ReportingProductionConfigurationTask,
    resolve_account: ReceiptAccountResolver,
    buyer_agents: BuyerAgentRegistry | None = None,
    consumer_status_enabled: bool = False,
    subscriptions: ReportingSubscriptionResolver | None = None,
    signing: ReportingProductionSigning | None = None,
    cipher: ReportingEnvelopeCipher | None = None,
) -> ReportingProductionSupport:
    """Prepare after draining old autonomous materializers/projectors/sweepers.

    Each offering names its actual producer, effective source offering and
    installed verifier. Its source implements configuration_binding() with a
    ReportingProductionSourceBinding.for_configuration(...) built from the
    trusted account/catalog mapping. Product IDs are explicit; neither SDK nor
    adopter may infer them from a report-definition ID.

    The destination implements configuration_binding() with the complete
    ReportingProductionDestinationBinding resolved from its provider grant.
    Credentials are acquired only inside separate write/readback sessions.
    A changed method or source generation requires a new admitted identity.

    The account task calls its supplied admit(ReportingConfigurationAdmission)
    for each ready/inactive reporting result, including replay. This completes
    the account's activation before ready can be returned. Discovery needs no
    first account, and workers do not require an adopter account inventory.
    """
    await store.create_schema()
    projection = PgReportingStatusProjection(
        store, consumer_status_enabled=consumer_status_enabled, revision_ownership=True
    )
    workers: tuple[ReportingNotificationWorker, ...] = ()
    if subscriptions is not None:
        if cipher is None or signing is None:
            raise ValueError(
                "notification delivery requires an envelope cipher and signing contract"
            )
        workers = production_notification_workers(
            store, projection, subscriptions=subscriptions, signing=signing, cipher=cipher
        )
    elif signing is not None or cipher is not None:
        raise ValueError("notification delivery requires the subscription resolver")
    return ReportingProductionSupport(
        ReportingMaterializerService(
            store, ReportingDestinationIO(registry, destination), destination
        ),
        projection,
        offerings=offerings,
        configuration_task=configuration_task,
        resolve_account=resolve_account,
        buyer_agents=buyer_agents,
        notification_workers=workers,
    )


def serve_reporting(
    support: ReportingProductionSupport,
    *,
    auth: BearerTokenAuth,
    public_url: str,
    allowed_hosts: tuple[str, ...],
) -> None:
    """Mount both authenticated transports, then start and drain the SDK lifecycle.

    Configure store notifications explicitly. Polling needs no HTTP worker;
    enabled notifications always retain atomic logical enqueue, even while
    recipient delivery is stopped. Historical accounts may additionally be
    activated by the operator with await support.activate(account_id=...).
    """
    serve(
        support.handler,
        name="reporting-production",
        transport="both",
        auth=auth,
        context_factory=auth_context_factory,
        public_url=public_url,
        allowed_hosts=allowed_hosts,
        on_startup=[support.start],
        on_shutdown=[support.aclose],
    )
