"""Tests for FormatId validation."""

import pytest
from pydantic import ValidationError

from adcp.types._generated import FormatId


class TestFormatIdValidation:
    """Tests for FormatId type validation."""

    def test_valid_format_id(self):
        """Test that valid format IDs are accepted."""
        # Valid: alphanumeric, hyphens, underscores
        valid_ids = [
            "banner_300x250",
            "video-16x9",
            "native_feed_v2",
            "display123",
            "test-format_123",
            "a",  # Single character
            "a-b_c123",
        ]

        for format_id in valid_ids:
            fid = FormatId(agent_url="https://example.com", id=format_id)
            assert fid.id == format_id

    def test_invalid_format_id_with_spaces(self):
        """Test that format IDs with spaces are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            FormatId(agent_url="https://example.com", id="format with spaces")

        assert "pattern" in str(exc_info.value)

    def test_invalid_format_id_with_special_chars(self):
        """Test that format IDs with special characters are rejected."""
        invalid_ids = [
            "format@test",
            "format#123",
            "format$value",
            "format%percent",
            "format&ampersand",
            "format*asterisk",
            "format(parens)",
            "format[brackets]",
            "format{braces}",
            "format/slash",
            "format\\backslash",
            "format.dot",
            "format,comma",
            "format;semicolon",
            "format:colon",
            "format'quote",
            'format"doublequote',
        ]

        for invalid_id in invalid_ids:
            with pytest.raises(ValidationError) as exc_info:
                FormatId(agent_url="https://example.com", id=invalid_id)

            assert "pattern" in str(exc_info.value), f"Should reject: {invalid_id}"

    def test_empty_format_id(self):
        """Test that empty format ID is rejected."""
        with pytest.raises(ValidationError):
            FormatId(agent_url="https://example.com", id="")

    def test_format_id_with_unicode(self):
        """Test that format IDs with unicode characters are rejected."""
        invalid_ids = [
            "format_émoji",
            "format_中文",
            "format_🎨",
        ]

        for invalid_id in invalid_ids:
            with pytest.raises(ValidationError) as exc_info:
                FormatId(agent_url="https://example.com", id=invalid_id)

            assert "pattern" in str(exc_info.value)

    def test_format_id_case_sensitivity(self):
        """Test that format IDs are case-sensitive and accept both upper and lower case."""
        # Both uppercase and lowercase should be valid
        upper = FormatId(agent_url="https://example.com", id="BANNER_300X250")
        lower = FormatId(agent_url="https://example.com", id="banner_300x250")
        mixed = FormatId(agent_url="https://example.com", id="Banner_300x250")

        assert upper.id == "BANNER_300X250"
        assert lower.id == "banner_300x250"
        assert mixed.id == "Banner_300x250"

    def test_format_id_preserves_original(self):
        """Test that format ID is not modified during validation."""
        original = "Test-Format_123"
        fid = FormatId(agent_url="https://example.com", id=original)
        assert fid.id == original  # Should preserve exact case and format

    def test_format_id_dict_validation(self):
        """Test that FormatId can be validated from dict (e.g., API responses)."""
        data = {"agent_url": "https://creative.example.com", "id": "banner_300x250"}

        fid = FormatId.model_validate(data)
        assert str(fid.agent_url).rstrip("/") == "https://creative.example.com"
        assert fid.id == "banner_300x250"

    def test_format_id_invalid_dict_validation(self):
        """Test that invalid format IDs in dicts are rejected."""
        data = {"agent_url": "https://creative.example.com", "id": "invalid format with spaces"}

        with pytest.raises(ValidationError) as exc_info:
            FormatId.model_validate(data)

        assert "pattern" in str(exc_info.value)
