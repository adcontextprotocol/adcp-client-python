"""
Tests for code generation script.

Validates that the generator handles edge cases properly:
- Special characters in descriptions (quotes, newlines, unicode, backslashes)
- Field name collisions with Python keywords
- Generated code is valid Python (AST parse)
- Generated code is importable
- Edge cases (empty schemas, missing properties, etc.)
"""

import ast
import json
import sys
import tempfile
from pathlib import Path

# Add scripts directory to path
scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from generate_models_simple import (
    escape_string_for_python,
    generate_model_for_schema,
    sanitize_field_name,
    validate_python_syntax,
)


class TestStringEscaping:
    """Test proper escaping of special characters."""

    def test_escape_double_quotes(self):
        """Test that double quotes are properly escaped."""
        text = 'This is a "quoted" string'
        result = escape_string_for_python(text)
        assert '\\"' in result
        assert '"quoted"' not in result

    def test_escape_backslashes(self):
        """Test that backslashes are properly escaped before quotes."""
        text = r"Path: C:\Users\test"
        result = escape_string_for_python(text)
        assert "\\\\" in result
        # Backslashes should be escaped
        assert "C:\\\\Users\\\\test" in result

    def test_escape_backslash_before_quote(self):
        """Test the critical case: backslash before quote."""
        text = r'Path with quote: \"test'
        result = escape_string_for_python(text)
        # Should escape backslash first, then quote
        assert '\\\\"' in result

    def test_escape_newlines(self):
        """Test that newlines are replaced with spaces."""
        text = "Line 1\nLine 2\nLine 3"
        result = escape_string_for_python(text)
        assert "\n" not in result
        assert "Line 1 Line 2 Line 3" == result

    def test_escape_carriage_returns(self):
        """Test that carriage returns are removed."""
        text = "Line 1\r\nLine 2"
        result = escape_string_for_python(text)
        assert "\r" not in result

    def test_unicode_characters(self):
        """Test that unicode characters are preserved."""
        text = "Emoji: 🚀 Accented: café"
        result = escape_string_for_python(text)
        assert "🚀" in result
        assert "café" in result

    def test_multiple_spaces_collapsed(self):
        """Test that multiple spaces are collapsed to one."""
        text = "Too    many     spaces"
        result = escape_string_for_python(text)
        assert "Too many spaces" == result

    def test_tabs_converted_to_spaces(self):
        """Test that tabs are converted to spaces."""
        text = "Column1\tColumn2\tColumn3"
        result = escape_string_for_python(text)
        assert "\t" not in result
        assert "Column1 Column2 Column3" == result


class TestFieldNameSanitization:
    """Test field name collision detection and sanitization."""

    def test_python_keyword_collision(self):
        """Test that Python keywords are sanitized."""
        name, needs_alias = sanitize_field_name("class")
        assert name == "class_"
        assert needs_alias is True

        name, needs_alias = sanitize_field_name("def")
        assert name == "def_"
        assert needs_alias is True

        name, needs_alias = sanitize_field_name("return")
        assert name == "return_"
        assert needs_alias is True

    def test_pydantic_reserved_names(self):
        """Test that Pydantic reserved names are sanitized."""
        name, needs_alias = sanitize_field_name("model_config")
        assert name == "model_config_"
        assert needs_alias is True

        name, needs_alias = sanitize_field_name("model_fields")
        assert name == "model_fields_"
        assert needs_alias is True

    def test_normal_field_name(self):
        """Test that normal field names are unchanged."""
        name, needs_alias = sanitize_field_name("product_id")
        assert name == "product_id"
        assert needs_alias is False

        name, needs_alias = sanitize_field_name("description")
        assert name == "description"
        assert needs_alias is False


class TestModelGeneration:
    """Test complete model generation from schemas."""

    def test_empty_schema(self):
        """Test generation with minimal schema."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(
                {"type": "object", "properties": {}}, f
            )
            temp_path = Path(f.name)

        try:
            result = generate_model_for_schema(temp_path)
            assert "class" in result
            assert "pass" in result

            # Validate syntax
            is_valid, _ = validate_python_syntax(
                "from pydantic import BaseModel\n" + result, "test"
            )
            assert is_valid
        finally:
            temp_path.unlink()

    def test_schema_with_special_characters_in_description(self):
        """Test schema with quotes, backslashes, and unicode in description."""
        schema = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": r'Windows path: C:\Users\test with "quotes"',
                },
                "emoji": {
                    "type": "string",
                    "description": "Unicode emoji: 🚀 and accented: café",
                },
            },
            "required": ["path"],
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(schema, f)
            temp_path = Path(f.name)

        try:
            result = generate_model_for_schema(temp_path)

            # Validate syntax with proper imports
            full_code = (
                "from pydantic import BaseModel, Field\n"
                "from typing import Any\n" + result
            )
            is_valid, error_msg = validate_python_syntax(full_code, "test")
            assert is_valid, f"Generated code has syntax errors: {error_msg}"

            # Validate the model can be parsed
            ast.parse(full_code)

            # Check that special characters are properly handled
            assert "path:" in result
            assert "emoji:" in result
        finally:
            temp_path.unlink()

    def test_schema_with_keyword_field_name(self):
        """Test schema with Python keyword as field name."""
        schema = {
            "type": "object",
            "properties": {
                "class": {"type": "string", "description": "CSS class name"},
                "return": {"type": "boolean"},
            },
            "required": ["class"],
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(schema, f)
            temp_path = Path(f.name)

        try:
            result = generate_model_for_schema(temp_path)

            # Should use sanitized names
            assert "class_:" in result
            assert "return_:" in result

            # Should have aliases
            assert 'alias="class"' in result
            assert 'alias="return"' in result

            # Validate syntax
            full_code = (
                "from pydantic import BaseModel, Field\n"
                "from typing import Any\n" + result
            )
            is_valid, error_msg = validate_python_syntax(full_code, "test")
            assert is_valid, f"Generated code has syntax errors: {error_msg}"
        finally:
            temp_path.unlink()

    def test_schema_with_complex_types(self):
        """Test schema with arrays, objects, and refs."""
        schema = {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Array of tags",
                },
                "metadata": {
                    "type": "object",
                    "description": "Generic metadata object",
                },
                "enum_field": {
                    "type": "string",
                    "enum": ["active", "inactive", "pending"],
                },
            },
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(schema, f)
            temp_path = Path(f.name)

        try:
            result = generate_model_for_schema(temp_path)

            # Check type mappings
            assert "list[str]" in result
            assert "dict[str, Any]" in result
            assert "Literal[" in result

            # Validate syntax
            full_code = (
                "from pydantic import BaseModel, Field\n"
                "from typing import Any, Literal\n" + result
            )
            is_valid, error_msg = validate_python_syntax(full_code, "test")
            assert is_valid, f"Generated code has syntax errors: {error_msg}"
        finally:
            temp_path.unlink()

    def test_generated_code_is_parseable(self):
        """Test that generated code can be parsed by Python AST."""
        schema = {
            "type": "object",
            "description": "Test model with various fields",
            "properties": {
                "id": {"type": "string"},
                "count": {"type": "integer"},
                "active": {"type": "boolean"},
            },
            "required": ["id"],
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(schema, f)
            temp_path = Path(f.name)

        try:
            result = generate_model_for_schema(temp_path)

            # Should be parseable as Python
            full_code = (
                "from pydantic import BaseModel, Field\n"
                "from typing import Any\n" + result
            )
            ast.parse(full_code)  # Will raise SyntaxError if invalid
        finally:
            temp_path.unlink()


class TestValidation:
    """Test validation functions."""

    def test_validate_valid_syntax(self):
        """Test that valid Python passes validation."""
        code = """
class TestModel(BaseModel):
    name: str
    count: int
"""
        is_valid, error = validate_python_syntax(code, "test.py")
        assert is_valid
        assert error == ""

    def test_validate_invalid_syntax(self):
        """Test that invalid Python fails validation."""
        code = """
class TestModel(BaseModel)
    name: str  # Missing colon after class definition
"""
        is_valid, error = validate_python_syntax(code, "test.py")
        assert not is_valid
        assert "Syntax error" in error

    def test_validate_unclosed_string(self):
        """Test that unclosed strings are caught."""
        code = '''
class TestModel(BaseModel):
    name: str = Field(description="unclosed string)
'''
        is_valid, error = validate_python_syntax(code, "test.py")
        assert not is_valid

    def test_validate_unescaped_backslash(self):
        """Test that unescaped backslashes in strings are caught."""
        # This is actually valid Python with a raw string, but let's test
        # that our escaping prevents issues
        code = r'''
class TestModel(BaseModel):
    path: str = Field(description="C:\Users\test")
'''
        # This should actually fail without proper escaping
        # because \U and \t are escape sequences
        is_valid, error = validate_python_syntax(code, "test.py")
        assert not is_valid  # \U starts unicode escape


class TestEdgeCases:
    """Test edge cases in code generation."""

    def test_missing_properties(self):
        """Test schema without properties key."""
        schema = {"type": "object", "description": "Empty model"}

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(schema, f)
            temp_path = Path(f.name)

        try:
            result = generate_model_for_schema(temp_path)
            assert "pass" in result
        finally:
            temp_path.unlink()

    def test_missing_description(self):
        """Test that missing descriptions don't break generation."""
        schema = {
            "type": "object",
            "properties": {"field1": {"type": "string"}},
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(schema, f)
            temp_path = Path(f.name)

        try:
            result = generate_model_for_schema(temp_path)
            assert "field1:" in result
        finally:
            temp_path.unlink()

    def test_optional_fields(self):
        """Test that optional fields (not in required) get None default."""
        schema = {
            "type": "object",
            "properties": {
                "required_field": {"type": "string"},
                "optional_field": {"type": "string"},
            },
            "required": ["required_field"],
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(schema, f)
            temp_path = Path(f.name)

        try:
            result = generate_model_for_schema(temp_path)
            # Required field should not have None default
            assert "required_field: str" in result
            # Optional field should have | None and = None
            assert "optional_field: str | None = None" in result
        finally:
            temp_path.unlink()
