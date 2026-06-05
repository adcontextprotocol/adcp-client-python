from __future__ import annotations

import httpx
import pytest

from adcp import RegistryClient


@pytest.mark.asyncio
async def test_list_agents_forwards_measurement_and_verification_filters() -> None:
    seen: httpx.URL | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen = request.url
        return httpx.Response(200, json={"agents": [], "count": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        registry = RegistryClient(base_url="https://registry.example", client=http)
        agents = await registry.list_agents(
            capabilities=True,
            metric_id=["attention_units", "incremental_reach"],
            accreditation="MRC",
            q="attention",
            verification_mode=["spec", "live"],
            verified=True,
        )

    assert agents == []
    assert seen is not None
    params = seen.params
    assert params.get("capabilities") == "true"
    assert params.get_list("metric_id") == ["attention_units", "incremental_reach"]
    assert params.get("accreditation") == "MRC"
    assert params.get("q") == "attention"
    assert params.get_list("verification_mode") == ["spec", "live"]
    assert params.get("verified") == "true"
