"""Tests for PropertyRegistry local authorization cache."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from adcp.property_registry import PropertyRegistry
from adcp.types.registry import FederatedAgentWithDetails, FeedEvent


def _make_agent(
    url: str,
    name: str = "Agent",
    publisher_domains: list[str] | None = None,
) -> FederatedAgentWithDetails:
    return FederatedAgentWithDetails.model_validate(
        {
            "url": url,
            "name": name,
            "type": "sales",
            "publisher_domains": publisher_domains or [],
        }
    )


def _make_event(
    event_type: str,
    entity_id: str = "",
    payload: dict | None = None,
) -> FeedEvent:
    return FeedEvent(
        event_id="evt-1",
        event_type=event_type,
        entity_type=event_type.split(".")[0] if "." in event_type else "unknown",
        entity_id=entity_id,
        payload=payload or {},
        actor="system",
        created_at="2026-01-01T00:00:00Z",
    )


# ========================================================================
# Load tests
# ========================================================================


class TestLoad:
    @pytest.mark.asyncio
    async def test_builds_indexes(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(
            return_value=[
                _make_agent("https://a1.com", publisher_domains=["pub1.com", "pub2.com"]),
                _make_agent("https://a2.com", publisher_domains=["pub2.com", "pub3.com"]),
            ]
        )

        reg = PropertyRegistry(mock_client)
        await reg.load()

        assert reg.is_authorized("https://a1.com", "pub1.com")
        assert reg.is_authorized("https://a1.com", "pub2.com")
        assert reg.is_authorized("https://a2.com", "pub2.com")
        assert reg.is_authorized("https://a2.com", "pub3.com")
        assert not reg.is_authorized("https://a1.com", "pub3.com")
        assert not reg.is_authorized("https://a2.com", "pub1.com")

    @pytest.mark.asyncio
    async def test_get_domains(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(
            return_value=[
                _make_agent("https://a1.com", publisher_domains=["pub1.com", "pub2.com"]),
            ]
        )

        reg = PropertyRegistry(mock_client)
        await reg.load()

        domains = reg.get_domains("https://a1.com")
        assert isinstance(domains, frozenset)
        assert domains == {"pub1.com", "pub2.com"}

    @pytest.mark.asyncio
    async def test_get_agents(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(
            return_value=[
                _make_agent("https://a1.com", publisher_domains=["pub1.com"]),
                _make_agent("https://a2.com", publisher_domains=["pub1.com"]),
            ]
        )

        reg = PropertyRegistry(mock_client)
        await reg.load()

        agents = reg.get_agents("pub1.com")
        assert isinstance(agents, frozenset)
        assert agents == {"https://a1.com", "https://a2.com"}

    @pytest.mark.asyncio
    async def test_empty_agents(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(return_value=[])

        reg = PropertyRegistry(mock_client)
        await reg.load()

        assert reg.agent_count == 0
        assert reg.domain_count == 0
        assert reg.loaded is True

    @pytest.mark.asyncio
    async def test_agent_without_publisher_domains(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(
            return_value=[
                _make_agent("https://a1.com", publisher_domains=None),
                _make_agent("https://a2.com", publisher_domains=[]),
            ]
        )

        reg = PropertyRegistry(mock_client)
        await reg.load()

        assert reg.agent_count == 0
        assert reg.domain_count == 0

    @pytest.mark.asyncio
    async def test_counts(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(
            return_value=[
                _make_agent("https://a1.com", publisher_domains=["pub1.com", "pub2.com"]),
                _make_agent("https://a2.com", publisher_domains=["pub2.com"]),
            ]
        )

        reg = PropertyRegistry(mock_client)
        await reg.load()

        assert reg.agent_count == 2
        assert reg.domain_count == 2  # pub1.com, pub2.com


# ========================================================================
# Query tests
# ========================================================================


class TestQueries:
    @pytest.mark.asyncio
    async def test_unknown_agent_returns_empty(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(return_value=[])
        reg = PropertyRegistry(mock_client)
        await reg.load()

        assert reg.get_domains("https://unknown.com") == frozenset()

    @pytest.mark.asyncio
    async def test_unknown_domain_returns_empty(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(return_value=[])
        reg = PropertyRegistry(mock_client)
        await reg.load()

        assert reg.get_agents("unknown.com") == frozenset()

    def test_is_authorized_before_load(self):
        reg = PropertyRegistry(MagicMock())
        assert not reg.is_authorized("https://a.com", "d.com")
        assert reg.loaded is False


# ========================================================================
# Event handling tests
# ========================================================================


class TestEventHandling:
    @pytest.mark.asyncio
    async def test_authorization_created_adds_edge(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(return_value=[])
        reg = PropertyRegistry(mock_client)
        await reg.load()

        event = _make_event(
            "authorization.created",
            payload={"agent_url": "https://a1.com", "domain": "pub1.com"},
        )
        await reg._handle_event(event)

        assert reg.is_authorized("https://a1.com", "pub1.com")

    @pytest.mark.asyncio
    async def test_authorization_revoked_removes_edge(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(
            return_value=[
                _make_agent("https://a1.com", publisher_domains=["pub1.com"]),
            ]
        )
        reg = PropertyRegistry(mock_client)
        await reg.load()
        assert reg.is_authorized("https://a1.com", "pub1.com")

        event = _make_event(
            "authorization.revoked",
            payload={"agent_url": "https://a1.com", "domain": "pub1.com"},
        )
        await reg._handle_event(event)

        assert not reg.is_authorized("https://a1.com", "pub1.com")

    @pytest.mark.asyncio
    async def test_agent_deleted_removes_all_edges(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(
            return_value=[
                _make_agent("https://a1.com", publisher_domains=["pub1.com", "pub2.com"]),
            ]
        )
        reg = PropertyRegistry(mock_client)
        await reg.load()

        event = _make_event("agent.deleted", entity_id="https://a1.com")
        await reg._handle_event(event)

        assert not reg.is_authorized("https://a1.com", "pub1.com")
        assert not reg.is_authorized("https://a1.com", "pub2.com")
        assert reg.agent_count == 0

    @pytest.mark.asyncio
    async def test_agent_updated_refreshes_domains(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(
            return_value=[
                _make_agent("https://a1.com", publisher_domains=["old.com"]),
            ]
        )
        mock_client.get_agent_domains = AsyncMock(
            return_value={
                "agent_url": "https://a1.com",
                "properties": [{"domain": "new.com"}],
                "identifiers": [],
                "property_count": 1,
                "identifier_count": 0,
                "generated_at": "",
            }
        )

        reg = PropertyRegistry(mock_client)
        await reg.load()
        assert reg.is_authorized("https://a1.com", "old.com")

        event = _make_event(
            "agent.updated",
            entity_id="https://a1.com",
            payload={"url": "https://a1.com"},
        )
        await reg._handle_event(event)

        assert not reg.is_authorized("https://a1.com", "old.com")
        assert reg.is_authorized("https://a1.com", "new.com")

    @pytest.mark.asyncio
    async def test_property_deleted_removes_domain(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(
            return_value=[
                _make_agent("https://a1.com", publisher_domains=["pub1.com"]),
                _make_agent("https://a2.com", publisher_domains=["pub1.com"]),
            ]
        )
        reg = PropertyRegistry(mock_client)
        await reg.load()

        event = _make_event("property.deleted", entity_id="pub1.com")
        await reg._handle_event(event)

        assert not reg.is_authorized("https://a1.com", "pub1.com")
        assert not reg.is_authorized("https://a2.com", "pub1.com")
        assert reg.domain_count == 0

    @pytest.mark.asyncio
    async def test_unknown_event_ignored(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(
            return_value=[
                _make_agent("https://a1.com", publisher_domains=["pub1.com"]),
            ]
        )
        reg = PropertyRegistry(mock_client)
        await reg.load()

        event = _make_event("future.event.type", entity_id="whatever")
        await reg._handle_event(event)

        # State unchanged
        assert reg.is_authorized("https://a1.com", "pub1.com")

    @pytest.mark.asyncio
    async def test_malformed_authorization_payload_ignored(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(return_value=[])
        reg = PropertyRegistry(mock_client)
        await reg.load()

        # Missing required fields
        event = _make_event("authorization.created", payload={})
        await reg._handle_event(event)

        assert reg.agent_count == 0
        assert reg.domain_count == 0

    @pytest.mark.asyncio
    async def test_agent_refresh_failure_is_best_effort(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(
            return_value=[
                _make_agent("https://a1.com", publisher_domains=["pub1.com"]),
            ]
        )
        mock_client.get_agent_domains = AsyncMock(side_effect=Exception("network error"))

        reg = PropertyRegistry(mock_client)
        await reg.load()

        event = _make_event("agent.updated", entity_id="https://a1.com")
        await reg._handle_event(event)

        # State unchanged because refresh failed gracefully
        assert reg.is_authorized("https://a1.com", "pub1.com")


# ========================================================================
# Lifecycle tests
# ========================================================================


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_calls_load(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(return_value=[])

        reg = PropertyRegistry(mock_client)
        assert not reg.loaded

        # start without auth_token should just load, no sync
        await reg.start()
        assert reg.loaded
        mock_client.list_agents.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_skips_load_if_already_loaded(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(return_value=[])

        reg = PropertyRegistry(mock_client)
        await reg.load()
        mock_client.list_agents.reset_mock()

        await reg.start()
        mock_client.list_agents.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_without_auth_token_skips_sync(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(return_value=[])

        reg = PropertyRegistry(mock_client)
        await reg.start()

        assert reg._sync is None

    @pytest.mark.asyncio
    async def test_start_with_auth_creates_background_task(self):
        import asyncio

        from adcp.types.registry import FeedPage

        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(return_value=[])
        mock_client.get_feed = AsyncMock(
            return_value=FeedPage(events=[], cursor=None, has_more=False)
        )

        mock_store = MagicMock()
        mock_store.load = AsyncMock(return_value=None)
        mock_store.save = AsyncMock()

        reg = PropertyRegistry(
            mock_client,
            auth_token="sk_test",
            poll_interval=0.01,
            cursor_store=mock_store,
        )
        await reg.start()

        # start() should return immediately (not block)
        assert reg._sync is not None
        assert reg._task is not None

        await asyncio.sleep(0.05)
        await reg.stop()

        assert reg._task is None
        assert reg._sync is None

    @pytest.mark.asyncio
    async def test_context_manager(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(
            return_value=[
                _make_agent("https://a1.com", publisher_domains=["pub.com"]),
            ]
        )

        async with PropertyRegistry(mock_client) as reg:
            assert reg.loaded
            assert reg.is_authorized("https://a1.com", "pub.com")

    @pytest.mark.asyncio
    async def test_stop_without_start_is_safe(self):
        reg = PropertyRegistry(MagicMock())
        await reg.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_refresh_rebuilds_indexes(self):
        mock_client = MagicMock()
        call_count = 0

        async def fake_list_agents(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [_make_agent("https://a1.com", publisher_domains=["old.com"])]
            return [_make_agent("https://a2.com", publisher_domains=["new.com"])]

        mock_client.list_agents = AsyncMock(side_effect=fake_list_agents)

        reg = PropertyRegistry(mock_client)
        await reg.load()
        assert reg.is_authorized("https://a1.com", "old.com")

        await reg.refresh()
        assert not reg.is_authorized("https://a1.com", "old.com")
        assert reg.is_authorized("https://a2.com", "new.com")


# ========================================================================
# Export tests
# ========================================================================


class TestExports:
    def test_importable_from_adcp(self):
        from adcp import PropertyRegistry as PR  # noqa: F401, N817

    def test_importable_from_module(self):
        from adcp.property_registry import PropertyRegistry as PR  # noqa: F401, N817
