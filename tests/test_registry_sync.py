"""Tests for RegistrySync change feed polling."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from adcp.exceptions import RegistryError
from adcp.registry_sync import (
    FileCursorStore,
    RegistrySync,
)
from adcp.types.registry import FeedEvent, FeedPage


def _make_event(
    event_id: str = "evt-1",
    event_type: str = "property.created",
    entity_type: str = "property",
    entity_id: str = "pub.com",
) -> FeedEvent:
    return FeedEvent(
        event_id=event_id,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        payload={},
        actor="system",
        created_at="2026-01-01T00:00:00Z",
    )


def _make_page(
    events: list[FeedEvent] | None = None,
    cursor: str | None = "cur-1",
    has_more: bool = False,
) -> FeedPage:
    return FeedPage(events=events or [], cursor=cursor, has_more=has_more)


async def _wait_for_call_count(get_count, expected: int) -> None:
    while get_count() < expected:
        await asyncio.sleep(0.01)


class TestFileCursorStore:
    @pytest.mark.asyncio
    async def test_load_returns_none_when_no_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileCursorStore(Path(tmpdir) / "cursor.json")
            assert await store.load() is None

    @pytest.mark.asyncio
    async def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cursor.json"
            store = FileCursorStore(path)
            await store.save("my-cursor-123")
            loaded = await store.load()
            assert loaded == "my-cursor-123"

    @pytest.mark.asyncio
    async def test_save_overwrites_previous(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cursor.json"
            store = FileCursorStore(path)
            await store.save("first")
            await store.save("second")
            assert await store.load() == "second"

    @pytest.mark.asyncio
    async def test_load_handles_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cursor.json"
            path.write_text("not json")
            store = FileCursorStore(path)
            assert await store.load() is None

    @pytest.mark.asyncio
    async def test_load_returns_none_when_cursor_key_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cursor.json"
            path.write_text(json.dumps({"other": "data"}))
            store = FileCursorStore(path)
            assert await store.load() is None


class TestRegistrySyncPollOnce:
    @pytest.mark.asyncio
    async def test_dispatches_events_to_matching_handlers(self):
        events = [
            _make_event("e1", "property.created"),
            _make_event("e2", "agent.updated"),
        ]
        mock_client = MagicMock()
        mock_client.get_feed = AsyncMock(return_value=_make_page(events, cursor="e2"))

        received: list[str] = []

        async def property_handler(event: FeedEvent) -> None:
            received.append(f"prop:{event.event_id}")

        async def all_handler(event: FeedEvent) -> None:
            received.append(f"all:{event.event_id}")

        sync = RegistrySync(mock_client, auth_token="sk_test")
        sync.on("property.*", property_handler)
        sync.on_all(all_handler)

        result = await sync.poll_once()
        assert len(result) == 2
        assert "prop:e1" in received
        assert "all:e1" in received
        assert "all:e2" in received
        # property handler should NOT fire for agent event
        assert "prop:e2" not in received

    @pytest.mark.asyncio
    async def test_saves_cursor_after_poll(self):
        mock_client = MagicMock()
        mock_client.get_feed = AsyncMock(
            return_value=_make_page([_make_event()], cursor="new-cursor")
        )

        mock_store = MagicMock()
        mock_store.load = AsyncMock(return_value=None)
        mock_store.save = AsyncMock()

        sync = RegistrySync(mock_client, auth_token="sk_test", cursor_store=mock_store)
        await sync.poll_once()

        mock_store.save.assert_called_once_with("new-cursor")
        assert sync.cursor == "new-cursor"

    @pytest.mark.asyncio
    async def test_loads_cursor_on_first_poll(self):
        mock_client = MagicMock()
        mock_client.get_feed = AsyncMock(return_value=_make_page([], cursor=None))

        mock_store = MagicMock()
        mock_store.load = AsyncMock(return_value="saved-cursor")
        mock_store.save = AsyncMock()

        sync = RegistrySync(mock_client, auth_token="sk_test", cursor_store=mock_store)
        await sync.poll_once()

        mock_store.load.assert_called_once()
        # Should pass saved cursor to get_feed
        call_args = mock_client.get_feed.call_args
        assert call_args.kwargs["cursor"] == "saved-cursor"

    @pytest.mark.asyncio
    async def test_resets_cursor_on_410(self):
        error = RegistryError("cursor_expired", status_code=410)
        mock_client = MagicMock()
        mock_client.get_feed = AsyncMock(side_effect=error)

        mock_store = MagicMock()
        mock_store.load = AsyncMock(return_value="old-cursor")
        mock_store.save = AsyncMock()

        sync = RegistrySync(mock_client, auth_token="sk_test", cursor_store=mock_store)
        result = await sync.poll_once()

        assert result == []
        assert sync.cursor is None
        mock_store.save.assert_called_once_with("")

    @pytest.mark.asyncio
    async def test_propagates_non_410_errors(self):
        error = RegistryError("auth failed", status_code=401)
        mock_client = MagicMock()
        mock_client.get_feed = AsyncMock(side_effect=error)

        mock_store = MagicMock()
        mock_store.load = AsyncMock(return_value=None)

        sync = RegistrySync(mock_client, auth_token="bad", cursor_store=mock_store)
        with pytest.raises(RegistryError) as exc_info:
            await sync.poll_once()
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_stop_processing(self):
        events = [_make_event("e1"), _make_event("e2")]
        mock_client = MagicMock()
        mock_client.get_feed = AsyncMock(return_value=_make_page(events, cursor="e2"))

        processed: list[str] = []

        async def bad_handler(event: FeedEvent) -> None:
            if event.event_id == "e1":
                raise ValueError("handler error")
            processed.append(event.event_id)

        sync = RegistrySync(mock_client, auth_token="sk_test")
        sync.on_all(bad_handler)
        result = await sync.poll_once()

        assert len(result) == 2
        assert "e2" in processed

    @pytest.mark.asyncio
    async def test_passes_types_filter(self):
        mock_client = MagicMock()
        mock_client.get_feed = AsyncMock(return_value=_make_page())

        mock_store = MagicMock()
        mock_store.load = AsyncMock(return_value=None)
        mock_store.save = AsyncMock()

        sync = RegistrySync(
            mock_client, auth_token="sk_test", cursor_store=mock_store, types="property.*,agent.*"
        )
        await sync.poll_once()

        call_args = mock_client.get_feed.call_args
        assert call_args.kwargs["types"] == "property.*,agent.*"

    @pytest.mark.asyncio
    async def test_cursor_persists_across_polls(self):
        """Second poll sends cursor from first poll."""
        mock_client = MagicMock()
        call_count = 0

        async def fake_get_feed(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_page([_make_event("e1")], cursor="cursor-1")
            return _make_page([], cursor=None)

        mock_client.get_feed = AsyncMock(side_effect=fake_get_feed)

        mock_store = MagicMock()
        mock_store.load = AsyncMock(return_value=None)
        mock_store.save = AsyncMock()

        sync = RegistrySync(mock_client, auth_token="sk", cursor_store=mock_store)
        await sync.poll_once()
        await sync.poll_once()

        second_call = mock_client.get_feed.call_args_list[1]
        assert second_call.kwargs["cursor"] == "cursor-1"

    @pytest.mark.asyncio
    async def test_empty_page_does_not_update_cursor(self):
        mock_client = MagicMock()
        mock_client.get_feed = AsyncMock(return_value=_make_page([], cursor=None))

        mock_store = MagicMock()
        mock_store.load = AsyncMock(return_value="existing-cursor")
        mock_store.save = AsyncMock()

        sync = RegistrySync(mock_client, auth_token="sk", cursor_store=mock_store)
        result = await sync.poll_once()

        assert result == []
        assert sync.cursor == "existing-cursor"
        mock_store.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_size_capped_at_10000(self):
        sync = RegistrySync(MagicMock(), auth_token="sk", batch_size=99999)
        assert sync._batch_size == 10000

    @pytest.mark.asyncio
    async def test_batch_size_passed_as_limit(self):
        mock_client = MagicMock()
        mock_client.get_feed = AsyncMock(return_value=_make_page())

        mock_store = MagicMock()
        mock_store.load = AsyncMock(return_value=None)
        mock_store.save = AsyncMock()

        sync = RegistrySync(
            mock_client,
            auth_token="sk",
            cursor_store=mock_store,
            batch_size=500,
        )
        await sync.poll_once()

        call_args = mock_client.get_feed.call_args
        assert call_args.kwargs["limit"] == 500

    @pytest.mark.asyncio
    async def test_exact_event_type_match(self):
        """Pattern 'agent.updated' matches only 'agent.updated'."""
        events = [
            _make_event("e1", "agent.updated"),
            _make_event("e2", "agent.created"),
        ]
        mock_client = MagicMock()
        mock_client.get_feed = AsyncMock(return_value=_make_page(events, cursor="e2"))

        received: list[str] = []

        async def handler(event: FeedEvent) -> None:
            received.append(event.event_id)

        sync = RegistrySync(mock_client, auth_token="sk")
        sync.on("agent.updated", handler)
        await sync.poll_once()

        assert received == ["e1"]

    @pytest.mark.asyncio
    async def test_multiple_handlers_same_pattern(self):
        events = [_make_event("e1", "property.created")]
        mock_client = MagicMock()
        mock_client.get_feed = AsyncMock(return_value=_make_page(events, cursor="e1"))

        counts = [0, 0]

        async def handler_a(event: FeedEvent) -> None:
            counts[0] += 1

        async def handler_b(event: FeedEvent) -> None:
            counts[1] += 1

        sync = RegistrySync(mock_client, auth_token="sk")
        sync.on("property.*", handler_a)
        sync.on("property.*", handler_b)
        await sync.poll_once()

        assert counts == [1, 1]

    @pytest.mark.asyncio
    async def test_no_match_pattern_not_called(self):
        events = [_make_event("e1", "property.created")]
        mock_client = MagicMock()
        mock_client.get_feed = AsyncMock(return_value=_make_page(events, cursor="e1"))

        received: list[str] = []

        async def handler(event: FeedEvent) -> None:
            received.append(event.event_id)

        sync = RegistrySync(mock_client, auth_token="sk")
        sync.on("brand.*", handler)
        await sync.poll_once()

        assert received == []


class TestRegistrySyncStartStop:
    @pytest.mark.asyncio
    async def test_stop_terminates_loop(self):
        call_count = 0
        events = [_make_event()]

        async def fake_get_feed(**kwargs):
            nonlocal call_count
            call_count += 1
            return _make_page(events if call_count == 1 else [], cursor=f"c{call_count}")

        mock_client = MagicMock()
        mock_client.get_feed = AsyncMock(side_effect=fake_get_feed)

        mock_store = MagicMock()
        mock_store.load = AsyncMock(return_value=None)
        mock_store.save = AsyncMock()

        sync = RegistrySync(
            mock_client,
            auth_token="sk_test",
            cursor_store=mock_store,
            poll_interval=0.01,
        )

        task = asyncio.create_task(sync.start())
        await asyncio.sleep(0.05)
        await sync.stop()
        await task

        assert call_count >= 1

    @pytest.mark.asyncio
    async def test_stop_before_start_is_safe(self):
        sync = RegistrySync(MagicMock(), auth_token="sk")
        await sync.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_continues_after_transient_error(self):
        call_count = 0

        async def fake_get_feed(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RegistryError("transient", status_code=503)
            return _make_page([], cursor=None)

        mock_client = MagicMock()
        mock_client.get_feed = AsyncMock(side_effect=fake_get_feed)

        mock_store = MagicMock()
        mock_store.load = AsyncMock(return_value=None)
        mock_store.save = AsyncMock()

        sync = RegistrySync(
            mock_client,
            auth_token="sk",
            cursor_store=mock_store,
            poll_interval=0.01,
        )

        task = asyncio.create_task(sync.start())
        try:
            await asyncio.wait_for(_wait_for_call_count(lambda: call_count, 2), timeout=1.0)
        finally:
            await sync.stop()
            await task

        # Should have recovered and polled again after the error
        assert call_count >= 2


class TestRegistrySyncExports:
    def test_all_types_importable(self):
        from adcp.registry_sync import (  # noqa: F401
            ChangeHandler,
            CursorStore,
            FileCursorStore,
            RegistrySync,
        )

    def test_feed_types_importable(self):
        from adcp.types.registry import (  # noqa: F401
            FeedEvent,
            FeedPage,
        )
