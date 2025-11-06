#!/usr/bin/env python3
"""
Generate Pydantic models from AdCP task request/response schemas.

Simplified approach that handles the task schemas we need for type safety.
Core types are manually maintained in types/core.py.
"""

import ast
import json
import keyword
import re
import subprocess
import sys
from pathlib import Path

SCHEMAS_DIR = Path(__file__).parent.parent / "schemas" / "cache" / "latest"
OUTPUT_DIR = Path(__file__).parent.parent / "src" / "adcp" / "types"

# Python keywords and Pydantic reserved names that can't be used as field names
RESERVED_NAMES = set(keyword.kwlist) | {
    "model_config",
    "model_fields",
    "model_computed_fields",
    "model_extra",
    "model_fields_set",
}


def snake_to_pascal(name: str) -> str:
    """Convert snake_case to PascalCase."""
    return "".join(word.capitalize() for word in name.split("-"))


def sanitize_field_name(name: str) -> str:
    """
    Sanitize field name to avoid Python keyword collisions.

    Returns tuple of (sanitized_name, needs_alias) where needs_alias indicates
    if the field needs a Field(alias=...) to preserve original JSON name.
    """
    if name in RESERVED_NAMES:
        return f"{name}_", True
    return name, False


def escape_string_for_python(text: str) -> str:
    """
    Properly escape a string for use in Python source code.

    Handles:
    - Backslashes (must be escaped first!)
    - Double quotes
    - Newlines and carriage returns
    - Unicode characters (preserved as-is)
    """
    # Order matters: escape backslashes first
    text = text.replace("\\", "\\\\")
    text = text.replace('"', '\\"')
    text = text.replace("\n", " ")
    text = text.replace("\r", "")
    # Tab characters should be spaces in descriptions
    text = text.replace("\t", " ")
    # Collapse multiple spaces
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def generate_model_for_schema(schema_file: Path) -> str:
    """Generate Pydantic model code for a single schema inline."""
    with open(schema_file) as f:
        schema = json.load(f)

    # Start with model name
    model_name = snake_to_pascal(schema_file.stem)
    lines = [f"class {model_name}(BaseModel):"]

    # Add description if available
    if "description" in schema:
        # Escape description for docstring (triple quotes)
        desc = schema["description"].replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        desc = desc.replace("\n", " ").replace("\r", "")
        desc = re.sub(r"\s+", " ", desc).strip()
        lines.append(f'    """{desc}"""')
        lines.append("")

    # Add properties
    if "properties" not in schema or not schema["properties"]:
        lines.append("    pass")
        return "\n".join(lines)

    for prop_name, prop_schema in schema["properties"].items():
        # Sanitize field name to avoid keyword collisions
        safe_name, needs_alias = sanitize_field_name(prop_name)

        # Get type
        prop_type = get_python_type(prop_schema)

        # Get description and escape it properly
        desc = prop_schema.get("description", "")
        if desc:
            desc = escape_string_for_python(desc)

        # Check if required
        is_required = prop_name in schema.get("required", [])

        # Build field definition
        if is_required:
            if desc and needs_alias:
                lines.append(
                    f'    {safe_name}: {prop_type} = Field(alias="{prop_name}", description="{desc}")'
                )
            elif desc:
                lines.append(f'    {safe_name}: {prop_type} = Field(description="{desc}")')
            elif needs_alias:
                lines.append(f'    {safe_name}: {prop_type} = Field(alias="{prop_name}")')
            else:
                lines.append(f"    {safe_name}: {prop_type}")
        else:
            if desc and needs_alias:
                lines.append(
                    f'    {safe_name}: {prop_type} | None = Field(None, alias="{prop_name}", description="{desc}")'
                )
            elif desc:
                lines.append(
                    f'    {safe_name}: {prop_type} | None = Field(None, description="{desc}")'
                )
            elif needs_alias:
                lines.append(
                    f'    {safe_name}: {prop_type} | None = Field(None, alias="{prop_name}")'
                )
            else:
                lines.append(f"    {safe_name}: {prop_type} | None = None")

    return "\n".join(lines)


def get_python_type(schema: dict) -> str:
    """Convert JSON schema type to Python type hint."""
    if "$ref" in schema:
        # Reference to another model
        ref = schema["$ref"]
        return snake_to_pascal(ref.replace(".json", ""))

    schema_type = schema.get("type")

    if schema_type == "string":
        if "enum" in schema:
            # Literal type
            values = ", ".join(f'"{v}"' for v in schema["enum"])
            return f"Literal[{values}]"
        return "str"

    if schema_type == "number":
        return "float"

    if schema_type == "integer":
        return "int"

    if schema_type == "boolean":
        return "bool"

    if schema_type == "array":
        items = schema.get("items", {})
        item_type = get_python_type(items)
        return f"list[{item_type}]"

    if schema_type == "object":
        # Generic object
        return "dict[str, Any]"

    return "Any"


def validate_python_syntax(code: str, filename: str) -> tuple[bool, str]:
    """
    Validate that generated code is syntactically valid Python.

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        ast.parse(code)
        return True, ""
    except SyntaxError as e:
        return False, f"Syntax error in {filename} at line {e.lineno}: {e.msg}"


def validate_imports(output_file: Path) -> tuple[bool, str]:
    """
    Validate that the generated module can be imported.

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        # Try to compile the module
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(output_file)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False, f"Import validation failed:\n{result.stderr}"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Import validation timed out"
    except Exception as e:
        return False, f"Import validation error: {e}"


def main():
    """Generate models for core types and task request/response schemas."""
    if not SCHEMAS_DIR.exists():
        print("Error: Schemas not found. Run scripts/sync_schemas.py first.", file=sys.stderr)
        sys.exit(1)

    print(f"Generating models from {SCHEMAS_DIR}...")

    # Core domain types that are referenced by task schemas
    core_types = [
        "product.json",
        "media-buy.json",
        "package.json",
        "creative-asset.json",
        "creative-manifest.json",
        "brand-manifest.json",
        "brand-manifest-ref.json",
        "format.json",
        "targeting.json",
        "frequency-cap.json",
        "measurement.json",
        "delivery-metrics.json",
        "error.json",
        "property.json",
        "placement.json",
        "creative-policy.json",
        "creative-assignment.json",
        "performance-feedback.json",
        "start-timing.json",
        "sub-asset.json",
        "webhook-payload.json",
        "protocol-envelope.json",
        "response.json",
        "promoted-products.json",
    ]

    # Find all schemas
    core_schemas = [SCHEMAS_DIR / name for name in core_types if (SCHEMAS_DIR / name).exists()]
    task_schemas = sorted(SCHEMAS_DIR.glob("*-request.json")) + sorted(
        SCHEMAS_DIR.glob("*-response.json")
    )

    print(f"Found {len(core_schemas)} core schemas")
    print(f"Found {len(task_schemas)} task schemas\n")

    # Generate header
    output_lines = [
        '"""',
        "Auto-generated Pydantic models from AdCP JSON schemas.",
        "",
        "DO NOT EDIT THIS FILE MANUALLY.",
        "Generated from: https://adcontextprotocol.org/schemas/v1/",
        "To regenerate:",
        "  python scripts/sync_schemas.py",
        "  python scripts/fix_schema_refs.py",
        "  python scripts/generate_models_simple.py",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any, Literal",
        "",
        "from pydantic import BaseModel, Field",
        "",
        "",
        "# ============================================================================",
        "# CORE DOMAIN TYPES",
        "# ============================================================================",
        "",
    ]

    # Generate core types first
    for schema_file in core_schemas:
        print(f"  Generating core type: {schema_file.stem}...")
        try:
            model_code = generate_model_for_schema(schema_file)
            output_lines.append(model_code)
            output_lines.append("")
            output_lines.append("")
        except Exception as e:
            print(f"    Warning: Could not generate model: {e}")

    # Add separator for task types
    output_lines.extend(
        [
            "",
            "# ============================================================================",
            "# TASK REQUEST/RESPONSE TYPES",
            "# ============================================================================",
            "",
        ]
    )

    # Generate task models
    for schema_file in task_schemas:
        print(f"  Generating task type: {schema_file.stem}...")
        try:
            model_code = generate_model_for_schema(schema_file)
            output_lines.append(model_code)
            output_lines.append("")
            output_lines.append("")
        except Exception as e:
            print(f"    Warning: Could not generate model: {e}")

    # Join all lines into final code
    generated_code = "\n".join(output_lines)

    # Validate syntax before writing
    print("\nValidating generated code...")
    is_valid, error_msg = validate_python_syntax(generated_code, "generated.py")
    if not is_valid:
        print(f"✗ Syntax validation failed:", file=sys.stderr)
        print(f"  {error_msg}", file=sys.stderr)
        sys.exit(1)
    print("  ✓ Syntax validation passed")

    # Write output
    output_file = OUTPUT_DIR / "generated.py"
    output_file.write_text(generated_code)

    # Validate imports
    is_valid, error_msg = validate_imports(output_file)
    if not is_valid:
        print(f"✗ Import validation failed:", file=sys.stderr)
        print(f"  {error_msg}", file=sys.stderr)
        sys.exit(1)
    print("  ✓ Import validation passed")

    print(f"\n✓ Successfully generated and validated models")
    print(f"  Output: {output_file}")
    print(f"  Core types: {len(core_schemas)}")
    print(f"  Task types: {len(task_schemas)}")
    print(f"  Total models: {len(core_schemas) + len(task_schemas)}")


if __name__ == "__main__":
    main()
