"""The 8.x audit's remaining RootModel map keys retain their scalar constraints."""

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from adcp.types.generated_poc.manifest_schema import AdcpManifest, TaskResultResolution

_TOOL = {
    "protocol": "protocol",
    "mutating": False,
    "request_schema": "protocol/get-adcp-capabilities-request.json",
    "response_schema": "protocol/get-adcp-capabilities-response.json",
    "async_response_schemas": [],
}


def test_manifest_tool_keys_round_trip() -> None:
    adapter = TypeAdapter(AdcpManifest.model_fields["tools"].rebuild_annotation())
    data = {"get_adcp_capabilities": _TOOL}
    result = adapter.validate_json(json.dumps(data))
    assert type(next(iter(result))) is str
    assert list(json.loads(adapter.dump_json(result))) == list(data)
    with pytest.raises(ValidationError):
        adapter.validate_python({})


def test_terminal_schema_override_keys_round_trip() -> None:
    data = {"terminal_schema_overrides": {"media_buy_delivery": "media-buy/delivery.json"}}
    result = TaskResultResolution.model_validate_json(json.dumps(data))
    assert type(next(iter(result.terminal_schema_overrides))) is str
    assert (
        json.loads(result.model_dump_json())["terminal_schema_overrides"]
        == data["terminal_schema_overrides"]
    )


@pytest.mark.parametrize("key", ["BadName", "bad/name", ""])
def test_tool_name_constraints_are_preserved(key: str) -> None:
    adapter = TypeAdapter(AdcpManifest.model_fields["tools"].rebuild_annotation())
    with pytest.raises(ValidationError):
        adapter.validate_python({key: _TOOL})
    with pytest.raises(ValidationError):
        TaskResultResolution.model_validate(
            {"terminal_schema_overrides": {key: "media-buy/delivery.json"}}
        )


def test_manifest_tool_key_rewrite_is_idempotent(tmp_path, monkeypatch) -> None:
    from scripts import post_generate_fixes

    target = tmp_path / "manifest_schema.py"
    target.write_text(
        "class ToolName(RootModel[str]):\n"
        "    root: Annotated[str, Field(pattern='^[a-z][a-z0-9_]*$')]\n"
        "class TaskResultResolution(AdCPBaseModel):\n"
        "    terminal_schema_overrides: dict[ToolName, SchemaPath]\n"
        "class AdcpManifest(AdCPBaseModel):\n"
        "    tools: Annotated[dict[ToolName, Tools], Field(min_length=1)]\n"
    )
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", tmp_path)
    post_generate_fixes.preserve_manifest_tool_map_keys()
    before = target.read_text()
    assert "dict[ToolName," not in before
    assert before.count("pattern='^[a-z][a-z0-9_]*$'") == 3
    assert "Field(min_length=1)" in before
    post_generate_fixes.preserve_manifest_tool_map_keys()
    assert target.read_text() == before
