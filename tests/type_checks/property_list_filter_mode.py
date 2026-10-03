"""Adopter contract: select SDK or platform property-list filtering."""

from typing import Literal

from adcp.decisioning import DecisioningPlatform, create_adcp_server_from_platform, serve
from adcp.decisioning.property_list import PropertyListFetcher, validate_property_list_config


def build(
    platform: DecisioningPlatform,
    mode: Literal["sdk", "platform"],
    fetcher: PropertyListFetcher | None = None,
) -> None:
    _, executor, _ = create_adcp_server_from_platform(
        platform, property_list_filter_mode=mode, property_list_fetcher=fetcher
    )
    executor.shutdown(wait=True)


def run(platform: DecisioningPlatform) -> None:
    serve(platform, property_list_filter_mode="platform")


validate_property_list_config(capability_enabled=True, fetcher=None, filter_mode="platform")
