from __future__ import annotations

"""Tests for response parsing utilities."""

import json

import pytest
from pydantic import BaseModel, Field

from adcp.utils.response_parser import parse_json_or_text, parse_mcp_content


class SampleResponse(BaseModel):
    """Sample response type for testing."""

    message: str
    count: int
    items: list[str] = Field(default_factory=list)


class TestParseMCPContent:
    """Tests for parse_mcp_content function."""

    def test_parse_text_content_with_json(self):
        """Test parsing MCP text content containing JSON."""
        content = [
            {
                "type": "text",
                "text": json.dumps({"message": "Hello", "count": 42, "items": ["a", "b"]}),
            }
        ]

        result = parse_mcp_content(content, SampleResponse)

        assert isinstance(result, SampleResponse)
        assert result.message == "Hello"
        assert result.count == 42
        assert result.items == ["a", "b"]

    def test_parse_multiple_content_items(self):
        """Test parsing MCP content with multiple items, returns first valid."""
        content = [
            {"type": "text", "text": "Not JSON"},
            {
                "type": "text",
                "text": json.dumps({"message": "Valid", "count": 10}),
            },
        ]

        result = parse_mcp_content(content, SampleResponse)

        assert result.message == "Valid"
        assert result.count == 10

    def test_empty_content_raises_error(self):
        """Test that empty content array raises ValueError."""
        with pytest.raises(ValueError, match="Empty MCP content array"):
            parse_mcp_content([], SampleResponse)

    def test_no_valid_content_raises_error(self):
        """Test that content with no valid data raises ValueError."""
        content = [
            {"type": "text", "text": "Not JSON"},
            {"type": "other", "data": "something"},
        ]

        with pytest.raises(ValueError, match="No valid SampleResponse data found"):
            parse_mcp_content(content, SampleResponse)

    def test_invalid_schema_raises_error(self):
        """Test that content not matching schema raises ValueError."""
        content = [
            {
                "type": "text",
                "text": json.dumps({"wrong_field": "value"}),
            }
        ]

        with pytest.raises(ValueError, match="doesn't match expected schema"):
            parse_mcp_content(content, SampleResponse)

    def test_empty_text_content_skipped(self):
        """Test that empty text content is skipped."""
        content = [
            {"type": "text", "text": ""},
            {
                "type": "text",
                "text": json.dumps({"message": "Found", "count": 5}),
            },
        ]

        result = parse_mcp_content(content, SampleResponse)
        assert result.message == "Found"


class TestParseJSONOrText:
    """Tests for parse_json_or_text function."""

    def test_parse_dict_directly(self):
        """Test parsing dict data directly."""
        data = {"message": "Hello", "count": 42}

        result = parse_json_or_text(data, SampleResponse)

        assert result.message == "Hello"
        assert result.count == 42

    def test_parse_json_string(self):
        """Test parsing JSON string."""
        data = json.dumps({"message": "World", "count": 100})

        result = parse_json_or_text(data, SampleResponse)

        assert result.message == "World"
        assert result.count == 100

    def test_invalid_json_string_raises_error(self):
        """Test that invalid JSON string raises ValueError."""
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_json_or_text("Not JSON at all", SampleResponse)

    def test_dict_not_matching_schema_raises_error(self):
        """Test that dict not matching schema raises ValueError."""
        with pytest.raises(ValueError, match="doesn't match expected schema"):
            parse_json_or_text({"wrong": "data"}, SampleResponse)

    def test_unsupported_type_raises_error(self):
        """Test that unsupported data type raises ValueError."""
        with pytest.raises(ValueError, match="Cannot parse response of type"):
            parse_json_or_text(12345, SampleResponse)  # type: ignore[arg-type]

    def test_json_string_not_matching_schema_raises_error(self):
        """Test that JSON string not matching schema raises ValueError."""
        data = json.dumps({"invalid": "structure"})

        with pytest.raises(ValueError, match="doesn't match expected schema"):
            parse_json_or_text(data, SampleResponse)
