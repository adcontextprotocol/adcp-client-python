from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from adcp.decisioning import AdcpError, InMemoryTaskRegistry, RequestContext
from adcp.decisioning.handler import PlatformHandler
from adcp.types import FormatId
from adcp.types.generated_poc.creative.list_creatives_response import ListCreativesResponse
from examples.multi_platform_seller.src.app import build_router
from examples.multi_platform_seller.src.mock_guaranteed import MockGuaranteedPlatform


def _create_guaranteed_buy(platform: MockGuaranteedPlatform) -> str:
    response = platform.create_media_buy(
        SimpleNamespace(
            buyer_ref="buyer-1",
            start_time="2026-05-01T00:00:00Z",
            end_time="2026-05-31T00:00:00Z",
            packages=[
                SimpleNamespace(
                    buyer_ref="pkg-1",
                    product_id="guaranteed-homepage-takeover",
                    budget=1000,
                )
            ],
        ),
        RequestContext(),
    )
    return str(response["media_buy_id"])


def test_multi_platform_router_advertises_account_discovery_tool() -> None:
    router = build_router()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        handler = PlatformHandler(
            router,
            executor=pool,
            registry=InMemoryTaskRegistry(),
        )
        advertised = handler.advertised_tools_for_instance()
    finally:
        pool.shutdown(wait=True)

    assert "list_accounts" in advertised


def test_guaranteed_get_media_buys_projects_storyboard_datetime_shape() -> None:
    platform = MockGuaranteedPlatform()
    media_buy_id = _create_guaranteed_buy(platform)

    response = platform.get_media_buys(
        SimpleNamespace(media_buy_ids=[media_buy_id]),
        RequestContext(),
    )

    buy = response["media_buys"][0]
    assert buy["start_time"] == "2026-05-01T00:00:00Z"
    assert buy["end_time"] == "2026-05-31T00:00:00Z"


def test_guaranteed_recancel_raises_not_cancellable() -> None:
    platform = MockGuaranteedPlatform()
    media_buy_id = _create_guaranteed_buy(platform)

    first = platform.update_media_buy(
        media_buy_id,
        SimpleNamespace(canceled=True),
        RequestContext(),
    )
    assert first["status"] == "canceled"

    with pytest.raises(AdcpError) as excinfo:
        platform.update_media_buy(
            media_buy_id,
            SimpleNamespace(canceled=True),
            RequestContext(),
        )

    assert excinfo.value.code == "NOT_CANCELLABLE"


def test_creative_format_projection_preserves_structured_format_id() -> None:
    platform = MockGuaranteedPlatform()
    platform.sync_creatives(
        SimpleNamespace(
            creatives=[
                SimpleNamespace(
                    creative_id="creative-0",
                    name="Creative 0",
                    format_id=FormatId(
                        agent_url="https://creative.adcontextprotocol.org/",
                        id="display_300x250",
                    ),
                ),
                SimpleNamespace(
                    creative_id="creative-1",
                    name="Creative 1",
                    format_id=FormatId(
                        agent_url="https://creative.adcontextprotocol.org/",
                        id="display_300x250",
                    ),
                )
            ]
        ),
        RequestContext(),
    )

    response = platform.list_creatives(
        SimpleNamespace(filters=SimpleNamespace(creative_ids=["creative-1"])),
        RequestContext(),
    )

    ListCreativesResponse.model_validate(response)
    assert [creative["creative_id"] for creative in response["creatives"]] == ["creative-1"]
    assert response["creatives"][0]["format_id"] == {
        "agent_url": "https://creative.adcontextprotocol.org/",
        "id": "display_300x250",
    }
