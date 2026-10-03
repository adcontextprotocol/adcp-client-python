"""The current public reporting model exposes an optional typed correlation ID."""

from typing_extensions import assert_type

from adcp.types import (
    AcceptProposalRequest,
    BuyProductsRequest,
    ControlMediaBuyRequest,
    CreateMediaBuyRequest,
    ReportingWebhook,
    UpdateMediaBuyRequest,
)

webhook = ReportingWebhook.model_validate(
    {
        "url": "https://buyer.example.com/reporting",
        "authentication": {
            "schemes": ["Bearer"],
            "credentials": "example_reporting_credential_32_chars",
        },
        "reporting_frequency": "daily",
        "operation_id": "op_reporting_001",
    }
)
assert_type(webhook.operation_id, str | None)

legacy = ReportingWebhook.model_validate(
    {
        "url": "https://buyer.example.com/reporting",
        "authentication": {
            "schemes": ["Bearer"],
            "credentials": "example_reporting_credential_32_chars",
        },
        "reporting_frequency": "daily",
    }
)
assert_type(legacy.operation_id, str | None)


def seller_reporting_id(
    request: (
        AcceptProposalRequest
        | BuyProductsRequest
        | ControlMediaBuyRequest
        | CreateMediaBuyRequest
        | UpdateMediaBuyRequest
    ),
) -> str | None:
    if request.reporting_webhook is None:
        return None
    assert_type(request.reporting_webhook.operation_id, str | None)
    return request.reporting_webhook.operation_id
