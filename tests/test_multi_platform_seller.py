"""Unit tests for examples/multi_platform_seller/src/ mock platforms.

Covers:
  - list_creatives returns query_summary (schema required field)
  - list_creatives returns creatives synced via sync_creatives
  - list_creatives on a fresh platform returns an empty but valid response
  - re-syncing the same creative_id upserts (no duplicates)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# examples/ is not a package — add the multi_platform_seller src to sys.path.
_MULTI_PLATFORM_SRC = str(
    Path(__file__).parent.parent / "examples" / "multi_platform_seller" / "src"
)
if _MULTI_PLATFORM_SRC not in sys.path:
    sys.path.insert(0, _MULTI_PLATFORM_SRC)

from mock_guaranteed import MockGuaranteedPlatform  # noqa: E402
from mock_non_guaranteed import MockNonGuaranteedPlatform  # noqa: E402


def _ctx() -> Any:
    return MagicMock()


def _sync_req(creatives: list[dict[str, Any]]) -> Any:
    req = MagicMock()
    req.creatives = creatives
    return req


def _list_req() -> Any:
    return MagicMock()


def _creative(idx: int = 0) -> dict[str, Any]:
    return {
        "creative_id": f"cr_{idx}",
        "name": f"Banner {idx}",
        "format_id": {
            "agent_url": "https://creative.adcontextprotocol.org/",
            "id": "display_300x250",
        },
        "assets": [],
    }


class TestMockGuaranteedPlatformListCreatives:
    def test_empty_list_has_query_summary(self) -> None:
        platform = MockGuaranteedPlatform()
        result = platform.list_creatives(_list_req(), _ctx())
        assert "query_summary" in result
        assert result["query_summary"]["total_matching"] == 0
        assert result["query_summary"]["returned"] == 0

    def test_empty_list_has_pagination(self) -> None:
        platform = MockGuaranteedPlatform()
        result = platform.list_creatives(_list_req(), _ctx())
        assert "pagination" in result
        assert result["pagination"]["has_more"] is False

    def test_synced_creatives_appear_in_list(self) -> None:
        platform = MockGuaranteedPlatform()
        platform.sync_creatives(_sync_req([_creative(0), _creative(1)]), _ctx())
        result = platform.list_creatives(_list_req(), _ctx())
        assert result["query_summary"]["total_matching"] == 2
        assert result["query_summary"]["returned"] == 2
        ids = {c["creative_id"] for c in result["creatives"]}
        assert "cr_0" in ids
        assert "cr_1" in ids

    def test_creative_items_have_required_fields(self) -> None:
        platform = MockGuaranteedPlatform()
        platform.sync_creatives(_sync_req([_creative(0)]), _ctx())
        result = platform.list_creatives(_list_req(), _ctx())
        item = result["creatives"][0]
        assert "creative_id" in item
        assert "name" in item
        assert "format_id" in item
        assert "status" in item
        assert "created_date" in item
        assert "updated_date" in item

    def test_name_fallback_when_omitted(self) -> None:
        platform = MockGuaranteedPlatform()
        bare = {"creative_id": "cr_bare", "assets": []}
        platform.sync_creatives(_sync_req([bare]), _ctx())
        result = platform.list_creatives(_list_req(), _ctx())
        assert result["creatives"][0]["name"] == "creative_0"

    def test_resync_same_id_does_not_duplicate(self) -> None:
        platform = MockGuaranteedPlatform()
        platform.sync_creatives(_sync_req([_creative(0)]), _ctx())
        platform.sync_creatives(_sync_req([_creative(0)]), _ctx())
        result = platform.list_creatives(_list_req(), _ctx())
        assert result["query_summary"]["total_matching"] == 1

    def test_sandbox_false_in_response(self) -> None:
        platform = MockGuaranteedPlatform()
        result = platform.list_creatives(_list_req(), _ctx())
        assert result.get("sandbox") is False


class TestMockNonGuaranteedPlatformListCreatives:
    def test_empty_list_has_query_summary(self) -> None:
        platform = MockNonGuaranteedPlatform()
        result = platform.list_creatives(_list_req(), _ctx())
        assert "query_summary" in result
        assert result["query_summary"]["total_matching"] == 0
        assert result["query_summary"]["returned"] == 0

    def test_empty_list_has_pagination(self) -> None:
        platform = MockNonGuaranteedPlatform()
        result = platform.list_creatives(_list_req(), _ctx())
        assert "pagination" in result
        assert result["pagination"]["has_more"] is False

    def test_synced_creatives_appear_in_list(self) -> None:
        platform = MockNonGuaranteedPlatform()
        platform.sync_creatives(_sync_req([_creative(0)]), _ctx())
        result = platform.list_creatives(_list_req(), _ctx())
        assert result["query_summary"]["total_matching"] == 1
        assert result["creatives"][0]["creative_id"] == "cr_0"

    def test_creative_items_have_required_fields(self) -> None:
        platform = MockNonGuaranteedPlatform()
        platform.sync_creatives(_sync_req([_creative(0)]), _ctx())
        result = platform.list_creatives(_list_req(), _ctx())
        item = result["creatives"][0]
        assert "creative_id" in item
        assert "name" in item
        assert "format_id" in item
        assert "status" in item
        assert "created_date" in item
        assert "updated_date" in item

    def test_resync_same_id_does_not_duplicate(self) -> None:
        platform = MockNonGuaranteedPlatform()
        platform.sync_creatives(_sync_req([_creative(0)]), _ctx())
        platform.sync_creatives(_sync_req([_creative(0)]), _ctx())
        result = platform.list_creatives(_list_req(), _ctx())
        assert result["query_summary"]["total_matching"] == 1

    def test_sandbox_false_in_response(self) -> None:
        platform = MockNonGuaranteedPlatform()
        result = platform.list_creatives(_list_req(), _ctx())
        assert result.get("sandbox") is False
