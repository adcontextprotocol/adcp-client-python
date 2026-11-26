"""AdCP 3.3 ``platform_id`` / ``seller_id`` dual emission helpers (#1639)."""

from __future__ import annotations

from typing import Any

import pytest

import adcp
from adcp.platform_ids import _dual_emit_platform_ids, _dual_emitted, platform_id_of

# ---- platform_id_of ----


def test_platform_id_of_is_exported_from_package_root() -> None:
    from adcp import platform_id_of as exported

    assert exported is platform_id_of
    assert "platform_id_of" in adcp.__all__


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"platform_id": "p1"}, "p1"),
        ({"seller_id": "s1"}, "s1"),
        ({"platform_id": "p1", "seller_id": "s1"}, "p1"),
        ({"platform_id": "p1", "seller_id": "p1"}, "p1"),
        ({"platform_id": None, "seller_id": "s1"}, "s1"),
        ({}, None),
        ({"platform_id": None, "seller_id": None}, None),
    ],
)
def test_platform_id_of_mapping(row: dict[str, Any], expected: str | None) -> None:
    assert platform_id_of(row) == expected


def test_platform_id_of_generated_models() -> None:
    from pydantic import BaseModel, ConfigDict

    class _Row(BaseModel):
        model_config = ConfigDict(extra="allow")

        platform_id: str | None = None
        seller_id: str | None = None

    assert platform_id_of(_Row(platform_id="p1", seller_id="s1")) == "p1"
    assert platform_id_of(_Row(seller_id="s1")) == "s1"
    assert platform_id_of(_Row()) is None


def test_platform_id_of_real_generated_result_models() -> None:
    """The real generated rows expose whichever name the release defines."""
    from adcp.types import SyncAudiencesSuccessResponse

    response = SyncAudiencesSuccessResponse.model_validate(
        {"audiences": [{"audience_id": "a1", "action": "created", "seller_id": "s1"}]}
    )
    assert platform_id_of(response.audiences[0]) == "s1"
    response = SyncAudiencesSuccessResponse.model_validate(
        {"audiences": [{"audience_id": "a1", "action": "created", "platform_id": "p1"}]}
    )
    assert platform_id_of(response.audiences[0]) == "p1"


def test_platform_id_of_coerces_to_string() -> None:
    assert platform_id_of({"platform_id": 42}) == "42"


# ---- _dual_emit_platform_ids ----

TASKS = [("sync_event_sources", "event_sources"), ("sync_audiences", "audiences")]
DUAL_VERSIONS = ["3.3-rc.3", "3.3", None]
LEGACY_VERSIONS = ["3.2", "3.0"]


@pytest.mark.parametrize(("task", "key"), TASKS)
@pytest.mark.parametrize("version", DUAL_VERSIONS)
def test_dual_emit_at_3_3_fills_both_names(task: str, key: str, version: str | None) -> None:
    payload = {key: [{"platform_id": "p1"}, {"seller_id": "s2"}, {"platform_id": "p3"}]}
    _dual_emit_platform_ids(task, payload, adcp_version=version)
    assert payload[key] == [
        {"platform_id": "p1", "seller_id": "p1"},
        {"platform_id": "s2", "seller_id": "s2"},
        {"platform_id": "p3", "seller_id": "p3"},
    ]


@pytest.mark.parametrize(("task", "key"), TASKS)
@pytest.mark.parametrize("version", LEGACY_VERSIONS)
def test_dual_emit_before_3_3_keeps_only_seller_id(task: str, key: str, version: str) -> None:
    payload = {
        key: [{"platform_id": "p1"}, {"seller_id": "s2"}, {"platform_id": "p3", "seller_id": "p3"}]
    }
    _dual_emit_platform_ids(task, payload, adcp_version=version)
    assert payload[key] == [{"seller_id": "p1"}, {"seller_id": "s2"}, {"seller_id": "p3"}]


@pytest.mark.parametrize(("task", "key"), TASKS)
@pytest.mark.parametrize("version", [*DUAL_VERSIONS, *LEGACY_VERSIONS])
def test_dual_emit_identical_values_are_accepted(task: str, key: str, version: str | None) -> None:
    payload = {key: [{"platform_id": "x", "seller_id": "x"}]}
    _dual_emit_platform_ids(task, payload, adcp_version=version)
    assert payload[key][0]["seller_id"] == "x"


@pytest.mark.parametrize(("task", "key"), TASKS)
@pytest.mark.parametrize("version", [*DUAL_VERSIONS, *LEGACY_VERSIONS])
def test_dual_emit_mismatch_names_the_row(task: str, key: str, version: str | None) -> None:
    payload = {key: [{"platform_id": "ok"}, {"platform_id": "a", "seller_id": "b"}]}
    with pytest.raises(ValueError, match=rf"/{key}/1 "):
        _dual_emit_platform_ids(task, payload, adcp_version=version)


def test_dual_emit_leaves_unrelated_input_alone() -> None:
    audiences: dict[str, Any] = {
        "audiences": [{"audience_id": "a1"}, "not-a-row", None, {"platform_id": None}]
    }
    before = {
        "audiences": [dict(audiences["audiences"][0]), "not-a-row", None, {"platform_id": None}]
    }
    _dual_emit_platform_ids("sync_audiences", audiences, adcp_version="3.3-rc.3")
    assert audiences == before

    other = {"audiences": [{"platform_id": "p1"}], "event_sources": [{"platform_id": "p1"}]}
    snapshot = {k: [dict(r) for r in v] for k, v in other.items()}
    _dual_emit_platform_ids("get_products", other, adcp_version="3.3-rc.3")
    assert other == snapshot

    # Each task only touches its own array.
    _dual_emit_platform_ids("sync_audiences", other, adcp_version="3.3-rc.3")
    assert other["audiences"] == [{"platform_id": "p1", "seller_id": "p1"}]
    assert other["event_sources"] == [{"platform_id": "p1"}]


@pytest.mark.parametrize("payload", [{}, {"audiences": None}, {"audiences": "x"}, [], None, "s"])
def test_dual_emit_tolerates_missing_or_malformed_array(payload: Any) -> None:
    _dual_emit_platform_ids("sync_audiences", payload, adcp_version="3.3-rc.3")


# ---- _dual_emitted: the stored-artifact projection ----


@pytest.mark.parametrize(
    ("task", "key"), [("sync_event_sources", "event_sources"), ("sync_audiences", "audiences")]
)
@pytest.mark.parametrize("returned", [{"seller_id": "p1"}, {"platform_id": "p1"}])
def test_dual_emitted_returns_a_copy_with_both_names(
    task: str, key: str, returned: dict[str, Any]
) -> None:
    original = {key: [dict(returned)], "extra": {"n": 1}}
    result = _dual_emitted(task, original)
    assert result[key][0]["platform_id"] == result[key][0]["seller_id"] == "p1"
    assert result["extra"] == {"n": 1}
    assert original == {key: [returned], "extra": {"n": 1}}
    assert result is not original
    assert result[key] is not original[key]


@pytest.mark.parametrize(
    ("task", "result"),
    [
        ("get_products", {"audiences": [{"seller_id": "p1"}]}),
        ("create_media_buy", {"event_sources": [{"platform_id": "p1"}]}),
        ("sync_audiences", ["not", "a", "dict"]),
        ("sync_audiences", None),
        ("sync_audiences", "text"),
    ],
)
def test_dual_emitted_leaves_other_tasks_and_non_dicts_unchanged(task: str, result: Any) -> None:
    assert _dual_emitted(task, result) is result


@pytest.mark.parametrize(
    ("task", "key", "row_key"),
    [("sync_event_sources", "event_sources", "e1"), ("sync_audiences", "audiences", "a1")],
)
def test_dual_emitted_mismatch_raises_and_leaves_input_alone(
    task: str, key: str, row_key: str
) -> None:
    original = {key: [{"platform_id": "a", "seller_id": "b"}]}
    with pytest.raises(ValueError, match=f"/{key}/0"):
        _dual_emitted(task, original)
    assert original == {key: [{"platform_id": "a", "seller_id": "b"}]}
