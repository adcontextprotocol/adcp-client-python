"""Demonstration of ergonomic type aliases.

This example shows how to use the semantic type aliases for better code clarity.
"""

from __future__ import annotations

# Import semantic aliases from the main package
from adcp import (
    CreateMediaBuyErrorResponse,
    CreateMediaBuySuccessResponse,
)


def handle_create_media_buy_response(
    response: CreateMediaBuySuccessResponse | CreateMediaBuyErrorResponse,
) -> None:
    """Handle a create media buy response with semantic types.

    Before ergonomic aliases (unclear):
        response: CreateMediaBuyResponse1 | CreateMediaBuyResponse2

    After ergonomic aliases (clear):
        response: CreateMediaBuySuccessResponse | CreateMediaBuyErrorResponse

    The semantic names make it immediately clear what each variant represents.
    """
    # Type narrowing with isinstance works perfectly
    if isinstance(response, CreateMediaBuySuccessResponse):
        print(f"✅ Success! Media buy created: {response.media_buy_id}")
        print(f"   Buyer reference: {response.buyer_ref}")
        print(f"   Packages: {len(response.packages)}")
    elif isinstance(response, CreateMediaBuyErrorResponse):
        print("❌ Error creating media buy:")
        for error in response.errors:
            print(f"   - {error.code}: {error.message}")


# Example usage
if __name__ == "__main__":
    # Success case
    success = CreateMediaBuySuccessResponse(
        media_buy_id="mb_12345",
        buyer_ref="ref_67890",
        packages=[],
    )
    handle_create_media_buy_response(success)

    print()

    # Error case
    error = CreateMediaBuyErrorResponse(
        errors=[
            {"code": "invalid_budget", "message": "Budget must be at least $100"},
            {"code": "missing_dates", "message": "Start and end dates required"},
        ]
    )
    handle_create_media_buy_response(error)
