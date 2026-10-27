"""Terminal statuses and the observed media-buy graph (issue #1604)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

import adcp.decisioning
from adcp.decisioning import (
    CREATIVE_ASSET_TRANSITIONS,
    MEDIA_BUY_TERMINAL_STATUSES,
    MEDIA_BUY_TRANSITIONS,
    OBSERVED_MEDIA_BUY_TRANSITIONS,
    AdcpError,
    assert_media_buy_transition,
    is_observable_media_buy_transition,
    is_terminal_media_buy_status,
)
from adcp.types import MediaBuyStatus

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC_ENUM = _REPO_ROOT / "schemas/cache/3.2/enums/media-buy-status.json"

# The AdCP 3.2 lifecycle graph that the @adcp/client storyboard runner's
# ``status.monotonic`` invariant enforces (``src/lib/server/state-machine.ts``,
# ``LEGACY_MEDIA_BUY_TRANSITIONS``). Spelled out here so a change to either
# graph has to be made on purpose.
_PUBLISHED_3_2_GRAPH = {
    "pending_creatives": {"pending_start", "active", "paused", "canceled", "rejected"},
    "pending_start": {"active", "paused", "canceled", "rejected"},
    "active": {"paused", "completed", "canceled"},
    "paused": {"active", "completed", "canceled"},
    "completed": set(),
    "rejected": set(),
    "canceled": set(),
}

_PLATFORM_ONLY_EDGES = {
    ("pending_creatives", "active"),
    ("pending_creatives", "paused"),
    ("pending_start", "paused"),
}

# Moves an ad server can make that have no edge in AdCP 3.2 (issue #1604's
# table). The SDK does not bless them.
_UNPUBLISHED_PLATFORM_MOVES = [
    ("active", "pending_start"),
    ("completed", "active"),
    ("completed", "paused"),
    ("canceled", "active"),
    ("rejected", "pending_start"),
]


def _edges(graph: Mapping[str, Iterable[str]]) -> set[tuple[str, str]]:
    return {(src, dst) for src, legal in graph.items() for dst in legal}


class TestTerminalStatuses:
    def test_terminal_statuses_are_the_spec_terminals(self) -> None:
        assert MEDIA_BUY_TERMINAL_STATUSES == frozenset({"completed", "canceled", "rejected"})

    def test_terminal_statuses_are_derived_from_the_graph(self) -> None:
        assert MEDIA_BUY_TERMINAL_STATUSES == frozenset(
            status for status, legal in MEDIA_BUY_TRANSITIONS.items() if not legal
        )

    def test_graph_covers_the_spec_enum(self) -> None:
        enum = json.loads(_SPEC_ENUM.read_text())["enum"]
        assert set(MEDIA_BUY_TRANSITIONS) == set(enum)
        assert set(OBSERVED_MEDIA_BUY_TRANSITIONS) == set(enum)

    @pytest.mark.parametrize("status", ["completed", "canceled", "rejected"])
    def test_terminal_status_is_terminal(self, status: str) -> None:
        assert is_terminal_media_buy_status(status) is True

    @pytest.mark.parametrize("status", ["pending_creatives", "pending_start", "active", "paused"])
    def test_live_status_is_not_terminal(self, status: str) -> None:
        assert is_terminal_media_buy_status(status) is False

    @pytest.mark.parametrize("status", ["draft", "pending_approval", "", "COMPLETED"])
    def test_unknown_status_is_not_terminal(self, status: str) -> None:
        assert is_terminal_media_buy_status(status) is False

    def test_accepts_the_generated_enum(self) -> None:
        assert is_terminal_media_buy_status(MediaBuyStatus.completed) is True
        assert is_terminal_media_buy_status(MediaBuyStatus.active) is False

    def test_creative_graph_has_no_terminal_status(self) -> None:
        # Why there is no CREATIVE_TERMINAL_STATUSES: every creative status
        # can move on (rejected → processing on resubmit, archived → approved).
        assert all(CREATIVE_ASSET_TRANSITIONS.values())


class TestObservedGraph:
    def test_observed_graph_is_the_published_3_2_graph(self) -> None:
        assert {k: set(v) for k, v in OBSERVED_MEDIA_BUY_TRANSITIONS.items()} == (
            _PUBLISHED_3_2_GRAPH
        )

    def test_observed_is_a_strict_superset_of_requested(self) -> None:
        requested = _edges(MEDIA_BUY_TRANSITIONS)
        observed = _edges(OBSERVED_MEDIA_BUY_TRANSITIONS)
        assert requested < observed
        assert observed - requested == _PLATFORM_ONLY_EDGES

    def test_terminal_states_stay_terminal_when_observed(self) -> None:
        for status in MEDIA_BUY_TERMINAL_STATUSES:
            assert OBSERVED_MEDIA_BUY_TRANSITIONS[status] == frozenset()

    @pytest.mark.parametrize("edge", sorted(_edges(_PUBLISHED_3_2_GRAPH)))
    def test_every_published_edge_is_observable(self, edge: tuple[str, str]) -> None:
        assert is_observable_media_buy_transition(*edge) is True

    @pytest.mark.parametrize("edge", sorted(_PLATFORM_ONLY_EDGES))
    def test_platform_edges_are_not_requestable(self, edge: tuple[str, str]) -> None:
        with pytest.raises(AdcpError) as exc_info:
            assert_media_buy_transition(*edge)
        assert exc_info.value.code == "INVALID_STATE"

    @pytest.mark.parametrize("edge", _UNPUBLISHED_PLATFORM_MOVES)
    def test_unpublished_moves_are_not_observable(self, edge: tuple[str, str]) -> None:
        assert is_observable_media_buy_transition(*edge) is False

    @pytest.mark.parametrize("status", sorted(_PUBLISHED_3_2_GRAPH))
    def test_unchanged_status_is_not_a_transition(self, status: str) -> None:
        assert is_observable_media_buy_transition(status, status) is False

    def test_unknown_statuses_are_not_observable(self) -> None:
        assert is_observable_media_buy_transition("draft", "active") is False
        assert is_observable_media_buy_transition("active", "draft") is False

    def test_accepts_the_generated_enum(self) -> None:
        assert is_observable_media_buy_transition(
            MediaBuyStatus.pending_creatives, MediaBuyStatus.active
        )


def test_public_exports() -> None:
    for name in (
        "MEDIA_BUY_TERMINAL_STATUSES",
        "OBSERVED_MEDIA_BUY_TRANSITIONS",
        "is_observable_media_buy_transition",
        "is_terminal_media_buy_status",
    ):
        assert name in adcp.decisioning.__all__
        assert name in adcp.decisioning.state_machines.__all__
