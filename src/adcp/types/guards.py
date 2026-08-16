"""Type guards for ADCP discriminated union responses.

ADCP 3.0 responses use atomic semantics: a response contains EITHER
success data OR errors, never both. These type guards enable static
type narrowing with mypy and runtime branch detection.

Usage:
    from adcp.types.guards import is_success, is_error

    response = result.data  # CreateMediaBuyResponse (union type)

    if is_success(response):
        # mypy knows this is CreateMediaBuySuccessResponse
        print(response.media_buy_id)
    else:
        # error branch
        print(response.errors)

Generic guards work with any ADCP response union:
    from adcp.types.guards import is_adcp_error, is_adcp_success

    if is_adcp_error(response):
        print(response.errors)
"""

from __future__ import annotations

from typing import Any, TypeAlias, TypeGuard


def is_adcp_error(response: Any) -> bool:
    """Check if an ADCP response is an error response.

    Works with any ADCP response union type. Error responses
    have a non-empty ``errors`` field.

    Args:
        response: Any ADCP response object (success or error variant).

    Returns:
        True if the response contains errors.
    """
    errors = getattr(response, "errors", None)
    if errors is None:
        return False
    if isinstance(errors, list):
        return len(errors) > 0
    return True


def is_adcp_success(response: Any) -> bool:
    """Check if an ADCP response is a success response.

    Works with any ADCP response union type. Success responses
    do not have an ``errors`` field (or it is None/empty).

    Args:
        response: Any ADCP response object (success or error variant).

    Returns:
        True if the response is a success (no errors).
    """
    return not is_adcp_error(response)


# ============================================================================
# Typed guards for specific response types
# ============================================================================
# These provide static type narrowing via typing.TypeGuard.
# Import the specific guard for your response type to get mypy narrowing.

# --- Media Buy ---

from adcp.types.aliases import (  # noqa: E402
    ActivateSignalErrorResponse,
    ActivateSignalSuccessResponse,
    CalibrateContentErrorResponse,
    CalibrateContentSuccessResponse,
    CreateMediaBuyErrorResponse,
    CreateMediaBuySubmittedResponse,
    CreateMediaBuySuccessResponse,
    GetAccountFinancialsErrorResponse,
    GetAccountFinancialsSuccessResponse,
    GetCreativeFeaturesErrorResponse,
    GetCreativeFeaturesSuccessResponse,
    LegacyBuildCreativeErrorResponse,
    LegacyBuildCreativeResponse3,
    LegacyBuildCreativeResponse4,
    LegacyBuildCreativeResponse5,
    LegacyBuildCreativeSubmittedResponse,
    LegacyBuildCreativeSuccessResponse,
    LogEventErrorResponse,
    LogEventSuccessResponse,
    ProvidePerformanceFeedbackErrorResponse,
    ProvidePerformanceFeedbackSuccessResponse,
    SyncAccountsErrorResponse,
    SyncAccountsSuccessResponse,
    SyncCatalogsErrorResponse,
    SyncCatalogsSubmittedResponse,
    SyncCatalogsSuccessResponse,
    SyncCreativesErrorResponse,
    SyncCreativesSubmittedResponse,
    SyncCreativesSuccessResponse,
    SyncEventSourcesErrorResponse,
    SyncEventSourcesSuccessResponse,
    UpdateMediaBuyErrorResponse,
    UpdateMediaBuySubmittedResponse,
    UpdateMediaBuySuccessResponse,
    ValidateContentDeliveryErrorResponse,
    ValidateContentDeliverySuccessResponse,
)

# Type aliases for response unions
CreateMediaBuyResponse: TypeAlias = (
    CreateMediaBuySuccessResponse | CreateMediaBuyErrorResponse | CreateMediaBuySubmittedResponse
)
UpdateMediaBuyResponse: TypeAlias = (
    UpdateMediaBuySuccessResponse | UpdateMediaBuyErrorResponse | UpdateMediaBuySubmittedResponse
)
ActivateSignalResponse: TypeAlias = ActivateSignalSuccessResponse | ActivateSignalErrorResponse
LegacyBuildCreativeSuccessBranches: TypeAlias = (
    LegacyBuildCreativeSuccessResponse
    | LegacyBuildCreativeResponse3
    | LegacyBuildCreativeResponse4
    | LegacyBuildCreativeResponse5
)
LegacyBuildCreativeResponse: TypeAlias = (
    LegacyBuildCreativeSuccessBranches
    | LegacyBuildCreativeErrorResponse
    | LegacyBuildCreativeSubmittedResponse
)
SyncCreativesResponse: TypeAlias = (
    SyncCreativesSuccessResponse | SyncCreativesErrorResponse | SyncCreativesSubmittedResponse
)
SyncAccountsResponse: TypeAlias = SyncAccountsSuccessResponse | SyncAccountsErrorResponse
LogEventResponse: TypeAlias = LogEventSuccessResponse | LogEventErrorResponse
SyncCatalogsResponse: TypeAlias = (
    SyncCatalogsSuccessResponse | SyncCatalogsErrorResponse | SyncCatalogsSubmittedResponse
)
SyncEventSourcesResponse: TypeAlias = (
    SyncEventSourcesSuccessResponse | SyncEventSourcesErrorResponse
)


# --- Create Media Buy ---


def is_create_media_buy_submitted(
    response: CreateMediaBuyResponse,
) -> TypeGuard[CreateMediaBuySubmittedResponse]:
    """Check if a CreateMediaBuyResponse is the async submitted envelope.

    The submitted branch carries ``status == 'submitted'`` and a ``task_id``
    the buyer uses to poll ``tasks/get`` (or correlate with push-notification
    callbacks). It is neither a synchronous success nor a terminal error.
    """
    return getattr(response, "status", None) == "submitted" and hasattr(response, "task_id")


def is_create_media_buy_success(
    response: CreateMediaBuyResponse,
) -> TypeGuard[CreateMediaBuySuccessResponse]:
    """Check if a CreateMediaBuyResponse is a synchronous success.

    Returns False for the submitted (async) envelope — use
    ``is_create_media_buy_submitted`` for that branch.
    """
    if is_create_media_buy_submitted(response):
        return False
    return not is_adcp_error(response)


def is_create_media_buy_error(
    response: CreateMediaBuyResponse,
) -> TypeGuard[CreateMediaBuyErrorResponse]:
    """Check if a CreateMediaBuyResponse is an error.

    Returns False for the submitted (async) envelope, even if it carries
    advisory (non-blocking) errors. Use ``is_create_media_buy_submitted``
    for that branch.
    """
    if is_create_media_buy_submitted(response):
        return False
    return is_adcp_error(response)


# --- Update Media Buy ---


def is_update_media_buy_submitted(
    response: UpdateMediaBuyResponse,
) -> TypeGuard[UpdateMediaBuySubmittedResponse]:
    """Check if an UpdateMediaBuyResponse is the async submitted envelope.

    The submitted branch carries ``status == 'submitted'`` and a ``task_id``.
    It is neither a synchronous success nor a terminal error.
    """
    return getattr(response, "status", None) == "submitted" and hasattr(response, "task_id")


def is_update_media_buy_success(
    response: UpdateMediaBuyResponse,
) -> TypeGuard[UpdateMediaBuySuccessResponse]:
    """Check if an UpdateMediaBuyResponse is a synchronous success.

    Returns False for the submitted (async) envelope — use
    ``is_update_media_buy_submitted`` for that branch.
    """
    if is_update_media_buy_submitted(response):
        return False
    return not is_adcp_error(response)


def is_update_media_buy_error(
    response: UpdateMediaBuyResponse,
) -> TypeGuard[UpdateMediaBuyErrorResponse]:
    """Check if an UpdateMediaBuyResponse is an error.

    Returns False for the submitted (async) envelope, even if it carries
    advisory (non-blocking) errors. Use ``is_update_media_buy_submitted``
    for that branch.
    """
    if is_update_media_buy_submitted(response):
        return False
    return is_adcp_error(response)


# --- Activate Signal ---


def is_activate_signal_success(
    response: ActivateSignalResponse,
) -> TypeGuard[ActivateSignalSuccessResponse]:
    """Check if an ActivateSignalResponse is a success."""
    return not is_adcp_error(response)


def is_activate_signal_error(
    response: ActivateSignalResponse,
) -> TypeGuard[ActivateSignalErrorResponse]:
    """Check if an ActivateSignalResponse is an error."""
    return is_adcp_error(response)


# --- Build Creative ---


def is_build_creative_submitted(
    response: LegacyBuildCreativeResponse,
) -> TypeGuard[LegacyBuildCreativeSubmittedResponse]:
    """Check if a BuildCreativeResponse is the async submitted envelope."""
    return getattr(response, "status", None) == "submitted" and hasattr(response, "task_id")


def is_build_creative_success(
    response: LegacyBuildCreativeResponse,
) -> TypeGuard[LegacyBuildCreativeSuccessBranches]:
    """Check if a BuildCreativeResponse is a synchronous success."""
    if is_build_creative_submitted(response):
        return False
    return not is_adcp_error(response)


def is_build_creative_error(
    response: LegacyBuildCreativeResponse,
) -> TypeGuard[LegacyBuildCreativeErrorResponse]:
    """Check if a BuildCreativeResponse is an error."""
    if is_build_creative_submitted(response):
        return False
    return is_adcp_error(response)


# --- Sync Creatives ---


def is_sync_creatives_submitted(
    response: SyncCreativesResponse,
) -> TypeGuard[SyncCreativesSubmittedResponse]:
    """Check if a SyncCreativesResponse is the async submitted envelope."""
    return getattr(response, "status", None) == "submitted" and hasattr(response, "task_id")


def is_sync_creatives_success(
    response: SyncCreativesResponse,
) -> TypeGuard[SyncCreativesSuccessResponse]:
    """Check if a SyncCreativesResponse is a synchronous success."""
    if is_sync_creatives_submitted(response):
        return False
    return not is_adcp_error(response)


def is_sync_creatives_error(
    response: SyncCreativesResponse,
) -> TypeGuard[SyncCreativesErrorResponse]:
    """Check if a SyncCreativesResponse is an error."""
    if is_sync_creatives_submitted(response):
        return False
    return is_adcp_error(response)


# --- Performance Feedback ---


def is_performance_feedback_success(
    response: ProvidePerformanceFeedbackSuccessResponse | ProvidePerformanceFeedbackErrorResponse,
) -> TypeGuard[ProvidePerformanceFeedbackSuccessResponse]:
    """Check if a ProvidePerformanceFeedbackResponse is a success."""
    return not is_adcp_error(response)


def is_performance_feedback_error(
    response: ProvidePerformanceFeedbackSuccessResponse | ProvidePerformanceFeedbackErrorResponse,
) -> TypeGuard[ProvidePerformanceFeedbackErrorResponse]:
    """Check if a ProvidePerformanceFeedbackResponse is an error."""
    return is_adcp_error(response)


# --- Sync Accounts ---


def is_sync_accounts_success(
    response: SyncAccountsResponse,
) -> TypeGuard[SyncAccountsSuccessResponse]:
    """Check if a SyncAccountsResponse is a success."""
    return not is_adcp_error(response)


def is_sync_accounts_error(
    response: SyncAccountsResponse,
) -> TypeGuard[SyncAccountsErrorResponse]:
    """Check if a SyncAccountsResponse is an error."""
    return is_adcp_error(response)


# --- Log Event ---


def is_log_event_success(
    response: LogEventResponse,
) -> TypeGuard[LogEventSuccessResponse]:
    """Check if a LogEventResponse is a success."""
    return not is_adcp_error(response)


def is_log_event_error(
    response: LogEventResponse,
) -> TypeGuard[LogEventErrorResponse]:
    """Check if a LogEventResponse is an error."""
    return is_adcp_error(response)


# --- Sync Catalogs ---


def is_sync_catalogs_submitted(
    response: SyncCatalogsResponse,
) -> TypeGuard[SyncCatalogsSubmittedResponse]:
    """Check if a SyncCatalogsResponse is the async submitted envelope."""
    return getattr(response, "status", None) == "submitted" and hasattr(response, "task_id")


def is_sync_catalogs_success(
    response: SyncCatalogsResponse,
) -> TypeGuard[SyncCatalogsSuccessResponse]:
    """Check if a SyncCatalogsResponse is a synchronous success."""
    if is_sync_catalogs_submitted(response):
        return False
    return not is_adcp_error(response)


def is_sync_catalogs_error(
    response: SyncCatalogsResponse,
) -> TypeGuard[SyncCatalogsErrorResponse]:
    """Check if a SyncCatalogsResponse is an error."""
    if is_sync_catalogs_submitted(response):
        return False
    return is_adcp_error(response)


# --- Get Account Financials ---


def is_get_account_financials_success(
    response: GetAccountFinancialsSuccessResponse | GetAccountFinancialsErrorResponse,
) -> TypeGuard[GetAccountFinancialsSuccessResponse]:
    """Check if a GetAccountFinancialsResponse is a success."""
    return not is_adcp_error(response)


def is_get_account_financials_error(
    response: GetAccountFinancialsSuccessResponse | GetAccountFinancialsErrorResponse,
) -> TypeGuard[GetAccountFinancialsErrorResponse]:
    """Check if a GetAccountFinancialsResponse is an error."""
    return is_adcp_error(response)


# --- Content Standards ---


def is_calibrate_content_success(
    response: CalibrateContentSuccessResponse | CalibrateContentErrorResponse,
) -> TypeGuard[CalibrateContentSuccessResponse]:
    """Check if a CalibrateContentResponse is a success."""
    return not is_adcp_error(response)


def is_validate_content_delivery_success(
    response: ValidateContentDeliverySuccessResponse | ValidateContentDeliveryErrorResponse,
) -> TypeGuard[ValidateContentDeliverySuccessResponse]:
    """Check if a ValidateContentDeliveryResponse is a success."""
    return not is_adcp_error(response)


# --- Creative Features ---


def is_get_creative_features_success(
    response: GetCreativeFeaturesSuccessResponse | GetCreativeFeaturesErrorResponse,
) -> TypeGuard[GetCreativeFeaturesSuccessResponse]:
    """Check if a GetCreativeFeaturesResponse is a success."""
    return not is_adcp_error(response)


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    # Generic guards
    "is_adcp_error",
    "is_adcp_success",
    # Media buy guards
    "is_create_media_buy_success",
    "is_create_media_buy_error",
    "is_create_media_buy_submitted",
    "is_update_media_buy_success",
    "is_update_media_buy_error",
    "is_update_media_buy_submitted",
    # Signal guards
    "is_activate_signal_success",
    "is_activate_signal_error",
    # Creative guards
    "is_build_creative_success",
    "is_build_creative_error",
    "is_build_creative_submitted",
    "is_sync_creatives_success",
    "is_sync_creatives_error",
    "is_sync_creatives_submitted",
    # Feedback guards
    "is_performance_feedback_success",
    "is_performance_feedback_error",
    # Account guards
    "is_sync_accounts_success",
    "is_sync_accounts_error",
    "is_get_account_financials_success",
    "is_get_account_financials_error",
    # Event guards
    "is_log_event_success",
    "is_log_event_error",
    # Catalog guards
    "is_sync_catalogs_success",
    "is_sync_catalogs_error",
    "is_sync_catalogs_submitted",
    "SyncEventSourcesResponse",
    # Content standards guards
    "is_calibrate_content_success",
    "is_validate_content_delivery_success",
    "is_get_creative_features_success",
]
