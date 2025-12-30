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


class ProductResponse(BaseModel):
    """Response type without protocol fields for testing protocol field stripping."""

    products: list[str]
    total: int = 0


class TestProtocolFieldExtraction:
    """Tests for protocol field extraction from A2A responses.

    A2A servers may include protocol-level fields (message, context_id, data)
    that are not part of task-specific response schemas. These are separated
    for task data validation, but preserved at the TaskResult level.

    See: https://github.com/adcontextprotocol/adcp-client-python/issues/109
    """

    def test_response_with_message_field_separated(self):
        """Test that protocol 'message' field is separated before validation."""
        # A2A server returns task data with protocol message mixed in
        data = {
            "message": "No products matched your requirements.",
            "products": ["product-1", "product-2"],
            "total": 2,
        }

        result = parse_json_or_text(data, ProductResponse)

        assert isinstance(result, ProductResponse)
        assert result.products == ["product-1", "product-2"]
        assert result.total == 2

    def test_response_with_context_id_separated(self):
        """Test that protocol 'context_id' field is separated before validation."""
        data = {
            "context_id": "session-123",
            "products": ["product-1"],
            "total": 1,
        }

        result = parse_json_or_text(data, ProductResponse)

        assert isinstance(result, ProductResponse)
        assert result.products == ["product-1"]

    def test_response_with_multiple_protocol_fields_separated(self):
        """Test that multiple protocol fields are separated."""
        data = {
            "message": "Found products",
            "context_id": "session-456",
            "products": ["a", "b", "c"],
            "total": 3,
        }

        result = parse_json_or_text(data, ProductResponse)

        assert isinstance(result, ProductResponse)
        assert result.products == ["a", "b", "c"]
        assert result.total == 3

    def test_response_with_data_wrapper_extracted(self):
        """Test that ProtocolResponse 'data' wrapper is extracted."""
        # Full ProtocolResponse format: {"message": "...", "data": {...task_data...}}
        data = {
            "message": "Operation completed",
            "context_id": "ctx-789",
            "data": {
                "products": ["wrapped-product"],
                "total": 1,
            },
        }

        result = parse_json_or_text(data, ProductResponse)

        assert isinstance(result, ProductResponse)
        assert result.products == ["wrapped-product"]
        assert result.total == 1

    def test_response_with_payload_wrapper_extracted(self):
        """Test that ProtocolEnvelope 'payload' wrapper is extracted."""
        # Full ProtocolEnvelope format
        data = {
            "message": "Operation completed",
            "status": "completed",
            "task_id": "task-123",
            "timestamp": "2025-01-01T00:00:00Z",
            "payload": {
                "products": ["envelope-product"],
                "total": 1,
            },
        }

        result = parse_json_or_text(data, ProductResponse)

        assert isinstance(result, ProductResponse)
        assert result.products == ["envelope-product"]
        assert result.total == 1

    def test_exact_match_still_works(self):
        """Test that responses exactly matching schema still work."""
        data = {
            "products": ["exact-match"],
            "total": 1,
        }

        result = parse_json_or_text(data, ProductResponse)

        assert result.products == ["exact-match"]
        assert result.total == 1

    def test_json_string_with_protocol_fields(self):
        """Test JSON string with protocol fields is parsed correctly."""
        data = json.dumps(
            {
                "message": "Success",
                "products": ["from-json-string"],
                "total": 1,
            }
        )

        result = parse_json_or_text(data, ProductResponse)

        assert result.products == ["from-json-string"]

    def test_invalid_data_after_separation_raises_error(self):
        """Test that invalid data still raises error after separation."""
        data = {
            "message": "Some message",
            "wrong_field": "value",
        }

        with pytest.raises(ValueError, match="doesn't match expected schema"):
            parse_json_or_text(data, ProductResponse)

    def test_model_with_message_field_validates_directly(self):
        """Test that models containing 'message' field validate without separation."""
        # SampleResponse has a 'message' field, so it should validate directly
        data = {
            "message": "Hello",
            "count": 42,
        }

        result = parse_json_or_text(data, SampleResponse)

        assert result.message == "Hello"
        assert result.count == 42
