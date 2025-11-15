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
    """
    Convert snake_case to PascalCase.

    Raises:
        ValueError: If the result is not a valid Python identifier
    """
    pascal = "".join(word.capitalize() for word in name.split("-"))

    # Validate result is a valid Python identifier
    if not pascal.isidentifier():
        raise ValueError(
            f"Cannot convert '{name}' to valid Python identifier: '{pascal}'"
        )

    return pascal


def sanitize_field_name(name: str) -> str:
    """
    Sanitize field name to avoid Python keyword collisions and invalid identifiers.

    Returns tuple of (sanitized_name, needs_alias) where needs_alias indicates
    if the field needs a Field(alias=...) to preserve original JSON name.
    """
    # Handle fields starting with invalid characters (like $schema)
    if name.startswith("$"):
        return name.replace("$", "dollar_"), True

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


def generate_discriminated_union(schema: dict, base_name: str) -> str:
    """
    Generate Pydantic models for a discriminated union (oneOf with type discriminator).

    Creates a base model for each variant and a union type for the parent.
    For example, Destination = PlatformDestination | AgentDestination
    """
    lines = []

    # Add schema description as a comment
    if "description" in schema:
        desc = escape_string_for_python(schema["description"])
        lines.append(f"# {desc}")
        lines.append("")

    variant_names = []

    # Generate a model for each variant in oneOf
    for i, variant in enumerate(schema.get("oneOf", [])):
        # Try to get discriminator value for better naming
        # Check common discriminator fields: type, asset_kind, output_format, delivery_type
        discriminator_value = None
        if "properties" in variant:
            for disc_field in ["type", "asset_kind", "output_format", "delivery_type"]:
                if disc_field in variant["properties"]:
                    disc_prop = variant["properties"][disc_field]
                    if "const" in disc_prop:
                        discriminator_value = disc_prop["const"]
                        break
                    elif "enum" in disc_prop and len(disc_prop["enum"]) == 1:
                        # Single-value enum can also be a discriminator
                        discriminator_value = disc_prop["enum"][0]
                        break

        # Generate variant name
        if discriminator_value:
            # Capitalize discriminator value and append to base name
            # e.g., "media" + "SubAsset" = "MediaSubAsset"
            variant_name = f"{discriminator_value.capitalize()}{base_name}"
        else:
            variant_name = f"{base_name}Variant{i+1}"

        variant_names.append(variant_name)

        # Generate the variant model
        lines.append(f"class {variant_name}(BaseModel):")

        # Add description if available
        if "description" in variant:
            desc = variant["description"].replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
            desc = desc.replace("\n", " ").replace("\r", "")
            desc = re.sub(r"\s+", " ", desc).strip()
            lines.append(f'    """{desc}"""')
            lines.append("")

        # Add model_config with extra="forbid" if additionalProperties is false
        if variant.get("additionalProperties") is False:
            lines.append('    model_config = ConfigDict(extra="forbid")')
            lines.append("")

        # Add properties
        if "properties" in variant and variant["properties"]:
            for prop_name, prop_schema in variant["properties"].items():
                safe_name, needs_alias = sanitize_field_name(prop_name)
                prop_type = get_python_type(prop_schema)
                desc = prop_schema.get("description", "")
                if desc:
                    desc = escape_string_for_python(desc)

                is_required = prop_name in variant.get("required", [])

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
        else:
            lines.append("    pass")

        lines.append("")
        lines.append("")

    # Create union type
    union_type = " | ".join(variant_names)
    lines.append(f"# Union type for {schema.get('title', base_name)}")
    lines.append(f"{base_name} = {union_type}")

    return "\n".join(lines)


def generate_inline_object_type(prop_name: str, prop_schema: dict, parent_name: str) -> tuple[str, str]:
    """
    Generate a Pydantic model for an inline object schema.

    Returns:
        Tuple of (generated_code, type_name)
    """
    # Generate type name from parent and property name
    # e.g., "publisher_properties" in Product -> PublisherProperty
    # Simple pluralization rules for English:
    # - words ending in "ies" -> "y" (properties -> property)
    # - words ending in "ses", "ches", "shes", "xes", "zes" -> remove "es"
    # - words ending in "s" -> remove "s"
    if prop_name.endswith('ies') and len(prop_name) > 3:
        singular = prop_name[:-3] + 'y'
    elif any(prop_name.endswith(suffix) for suffix in ['ses', 'ches', 'shes', 'xes', 'zes']) and len(prop_name) > 2:
        singular = prop_name[:-2]
    elif prop_name.endswith('s') and len(prop_name) > 1:
        singular = prop_name[:-1]
    else:
        singular = prop_name

    # Convert snake_case to PascalCase (split on underscores, not hyphens)
    pascal_singular = "".join(word.capitalize() for word in singular.split("_"))
    type_name = f"{parent_name}{pascal_singular}"

    lines = [f"class {type_name}(BaseModel):"]

    # Add description if available
    if "description" in prop_schema:
        desc = prop_schema["description"].replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        desc = desc.replace("\n", " ").replace("\r", "")
        desc = re.sub(r"\s+", " ", desc).strip()
        lines.append(f'    """{desc}"""')
        lines.append("")

    # Add model_config if additionalProperties is false
    if prop_schema.get("additionalProperties") is False:
        lines.append('    model_config = ConfigDict(extra="forbid")')
        lines.append("")

    # Add properties
    if "properties" in prop_schema and prop_schema["properties"]:
        for field_name, field_schema in prop_schema["properties"].items():
            safe_name, needs_alias = sanitize_field_name(field_name)
            field_type = get_python_type(field_schema)
            desc = field_schema.get("description", "")
            if desc:
                desc = escape_string_for_python(desc)

            is_required = field_name in prop_schema.get("required", [])

            # Check for pattern constraint
            pattern = field_schema.get("pattern")

            # Build field definition with pattern if present
            if is_required:
                if pattern and desc and needs_alias:
                    lines.append(
                        f'    {safe_name}: {field_type} = Field(alias="{field_name}", description="{desc}", pattern=r"{pattern}")'
                    )
                elif pattern and desc:
                    lines.append(f'    {safe_name}: {field_type} = Field(description="{desc}", pattern=r"{pattern}")')
                elif pattern and needs_alias:
                    lines.append(f'    {safe_name}: {field_type} = Field(alias="{field_name}", pattern=r"{pattern}")')
                elif pattern:
                    lines.append(f'    {safe_name}: {field_type} = Field(pattern=r"{pattern}")')
                elif desc and needs_alias:
                    lines.append(f'    {safe_name}: {field_type} = Field(alias="{field_name}", description="{desc}")')
                elif desc:
                    lines.append(f'    {safe_name}: {field_type} = Field(description="{desc}")')
                elif needs_alias:
                    lines.append(f'    {safe_name}: {field_type} = Field(alias="{field_name}")')
                else:
                    lines.append(f"    {safe_name}: {field_type}")
            else:
                if pattern and desc and needs_alias:
                    lines.append(
                        f'    {safe_name}: {field_type} | None = Field(None, alias="{field_name}", description="{desc}", pattern=r"{pattern}")'
                    )
                elif pattern and desc:
                    lines.append(
                        f'    {safe_name}: {field_type} | None = Field(None, description="{desc}", pattern=r"{pattern}")'
                    )
                elif pattern and needs_alias:
                    lines.append(
                        f'    {safe_name}: {field_type} | None = Field(None, alias="{field_name}", pattern=r"{pattern}")'
                    )
                elif pattern:
                    lines.append(f'    {safe_name}: {field_type} | None = Field(None, pattern=r"{pattern}")')
                elif desc and needs_alias:
                    lines.append(
                        f'    {safe_name}: {field_type} | None = Field(None, alias="{field_name}", description="{desc}")'
                    )
                elif desc:
                    lines.append(f'    {safe_name}: {field_type} | None = Field(None, description="{desc}")')
                elif needs_alias:
                    lines.append(f'    {safe_name}: {field_type} | None = Field(None, alias="{field_name}")')
                else:
                    lines.append(f"    {safe_name}: {field_type} | None = None")
    else:
        lines.append("    pass")

    return "\n".join(lines), type_name


def generate_model_for_schema(schema_file: Path) -> str:
    """Generate Pydantic model code for a single schema inline."""
    with open(schema_file) as f:
        schema = json.load(f)

    # Start with model name
    model_name = snake_to_pascal(schema_file.stem)

    # Check if this is a oneOf discriminated union
    if "oneOf" in schema and "properties" not in schema:
        return generate_discriminated_union(schema, model_name)

    # Check if this is a simple type alias (enum or primitive type without properties)
    if "properties" not in schema:
        # This is a type alias, not a model class
        python_type = get_python_type(schema)
        lines = [f"# Type alias for {schema.get('title', model_name)}"]
        if "description" in schema:
            desc = escape_string_for_python(schema["description"])
            lines.append(f"# {desc}")
        lines.append(f"{model_name} = {python_type}")
        return "\n".join(lines)

    # Detect inline object types in array properties and generate them first
    inline_types = []
    inline_type_map = {}  # prop_name -> type_name mapping

    if "properties" in schema:
        for prop_name, prop_schema in schema["properties"].items():
            # Check if this is an array of inline objects
            if (prop_schema.get("type") == "array" and
                "items" in prop_schema and
                prop_schema["items"].get("type") == "object" and
                "properties" in prop_schema["items"] and
                "$ref" not in prop_schema["items"]):

                # Generate inline type
                inline_code, type_name = generate_inline_object_type(
                    prop_name,
                    prop_schema["items"],
                    model_name
                )
                inline_types.append(inline_code)
                inline_type_map[prop_name] = type_name

    # Regular BaseModel class
    lines = []

    # Add inline types first
    for inline_type in inline_types:
        lines.append(inline_type)
        lines.append("")
        lines.append("")

    lines.append(f"class {model_name}(BaseModel):")

    # Add description if available
    if "description" in schema:
        # Escape description for docstring (triple quotes)
        desc = schema["description"].replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        desc = desc.replace("\n", " ").replace("\r", "")
        desc = re.sub(r"\s+", " ", desc).strip()
        lines.append(f'    """{desc}"""')
        lines.append("")

    # Add properties
    if not schema["properties"]:
        lines.append("    pass")
        return "\n".join(lines)

    for prop_name, prop_schema in schema["properties"].items():
        # Sanitize field name to avoid keyword collisions
        safe_name, needs_alias = sanitize_field_name(prop_name)

        # Check if this property has an inline type we generated
        if prop_name in inline_type_map:
            prop_type = f"list[{inline_type_map[prop_name]}]"
        else:
            prop_type = get_python_type(prop_schema)

        # Get description and escape it properly
        desc = prop_schema.get("description", "")
        if desc:
            desc = escape_string_for_python(desc)

        # Check for pattern constraint
        pattern = prop_schema.get("pattern")

        # Check if required
        is_required = prop_name in schema.get("required", [])

        # Build field definition with pattern support
        if is_required:
            if pattern and desc and needs_alias:
                lines.append(
                    f'    {safe_name}: {prop_type} = Field(alias="{prop_name}", description="{desc}", pattern=r"{pattern}")'
                )
            elif pattern and desc:
                lines.append(f'    {safe_name}: {prop_type} = Field(description="{desc}", pattern=r"{pattern}")')
            elif pattern and needs_alias:
                lines.append(f'    {safe_name}: {prop_type} = Field(alias="{prop_name}", pattern=r"{pattern}")')
            elif pattern:
                lines.append(f'    {safe_name}: {prop_type} = Field(pattern=r"{pattern}")')
            elif desc and needs_alias:
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
            if pattern and desc and needs_alias:
                lines.append(
                    f'    {safe_name}: {prop_type} | None = Field(None, alias="{prop_name}", description="{desc}", pattern=r"{pattern}")'
                )
            elif pattern and desc:
                lines.append(
                    f'    {safe_name}: {prop_type} | None = Field(None, description="{desc}", pattern=r"{pattern}")'
                )
            elif pattern and needs_alias:
                lines.append(
                    f'    {safe_name}: {prop_type} | None = Field(None, alias="{prop_name}", pattern=r"{pattern}")'
                )
            elif pattern:
                lines.append(f'    {safe_name}: {prop_type} | None = Field(None, pattern=r"{pattern}")')
            elif desc and needs_alias:
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
        # Extract just the filename from paths like "/schemas/v1/core/format-id.json"
        # Handles: absolute paths, relative paths, fragment identifiers (#/definitions/Foo)
        ref = schema["$ref"]

        if not ref:
            raise ValueError("Empty $ref in schema")

        # Split on # to handle fragment identifiers, then get the path part
        path_part = ref.split("#")[0]

        if not path_part:
            # Pure fragment reference like "#/definitions/Foo" - not supported
            raise ValueError(f"Fragment-only $ref not supported: {ref}")

        # Extract filename from path (handles both / and \ separators)
        filename = path_part.replace("\\", "/").split("/")[-1].replace(".json", "")

        if not filename:
            raise ValueError(f"Could not extract filename from $ref: {ref}")

        return snake_to_pascal(filename)

    # Handle const (discriminator values)
    if "const" in schema:
        const_value = schema["const"]
        if isinstance(const_value, str):
            return f'Literal["{const_value}"]'
        return f"Literal[{const_value}]"

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


def fix_format_types(code: str) -> str:
    """
    No longer needed - formats now use proper schema references.

    This function is kept for backwards compatibility but does nothing.
    The inline schemas have been extracted to separate .json files:
    - dimensions.json
    - render.json
    - asset-required.json
    """
    return code


def extract_type_names(code: str) -> list[str]:
    """
    Extract all type names (classes and type aliases) from generated code using AST.

    This is more robust than string parsing as it handles:
    - Comments containing class-like patterns
    - Multiline docstrings
    - Complex type expressions

    Returns:
        List of type names sorted alphabetically
    """
    type_names = []

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        # If code has syntax errors, fall back to empty list
        # (validation will catch this later)
        return []

    for node in ast.walk(tree):
        # Class definitions (e.g., class Foo(BaseModel):)
        if isinstance(node, ast.ClassDef):
            type_names.append(node.name)

        # Type aliases at module level (e.g., TypeName = SomeType | OtherType)
        # These are Assign nodes at the module body level
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    # Only include type aliases that start with capital letter
                    # (convention for type names in Python)
                    if target.id[0].isupper():
                        type_names.append(target.id)

    return sorted(set(type_names))


def add_custom_implementations(code: str) -> str:
    """
    Add custom Pydantic class implementations that override type aliases.

    The simple code generator produces type aliases (e.g., PreviewCreativeRequest = Any)
    for complex schemas that use oneOf. We override them with proper Pydantic classes
    to maintain type safety and enable batch API support.
    """
    custom_code = '''

# ============================================================================
# CUSTOM IMPLEMENTATIONS (override type aliases from generator)
# ============================================================================
# The simple code generator produces type aliases (e.g., PreviewCreativeRequest = Any)
# for complex schemas that use oneOf. We override them with proper Pydantic classes
# to maintain type safety and enable batch API support.
# Note: All classes inherit from BaseModel (which is aliased to AdCPBaseModel for exclude_none).


# All custom implementations have been removed - types are now auto-generated from schemas
'''
    return code + custom_code


def main():
    """Generate models for core types and task request/response schemas."""
    if not SCHEMAS_DIR.exists():
        print("Error: Schemas not found. Run scripts/sync_schemas.py first.", file=sys.stderr)
        sys.exit(1)

    print(f"Generating models from {SCHEMAS_DIR}...")

    # Core domain types that are referenced by task schemas
    core_types = [
        "format-id.json",  # Must come before format.json (which references it)
        "dimensions.json",  # Must come before render.json and format.json
        "render.json",  # Must come before format.json
        "asset-required.json",  # Must come before format.json
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
        "destination.json",
        "deployment.json",
        "activation-key.json",
        "push-notification-config.json",
        "reporting-capabilities.json",
        # Enum types (need type aliases)
        "channels.json",
        "delivery-type.json",
        "pacing.json",
        "package-status.json",
        "media-buy-status.json",
        "task-type.json",
        "task-status.json",
        "pricing-model.json",
        "pricing-option.json",
        "standard-format-ids.json",
        # Asset types with discriminators (from ADCP PR #189)
        "vast-asset.json",
        "daast-asset.json",
        "preview-render.json",
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
        "import re",
        "from typing import Any, Literal",
        "",
        "from pydantic import ConfigDict, Field, field_validator",
        "",
        "from adcp.types.base import AdCPBaseModel as BaseModel",
        "",
        "",
        "",
        "# ============================================================================",
        "# CORE DOMAIN TYPES",
        "# ============================================================================",
        "",
    ]

    # Skip core types that have custom implementations
    skip_core_types: set[str] = set()  # All core types now auto-generated

    # Generate core types first
    for schema_file in core_schemas:
        if schema_file.stem in skip_core_types:
            print(f"  Skipping {schema_file.stem} (custom implementation)...")
            continue
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
    # All task types now auto-generated from schemas
    skip_types: set[str] = set()
    for schema_file in task_schemas:
        if schema_file.stem in skip_types:
            print(f"  Skipping {schema_file.stem} (custom implementation)...")
            continue
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

    # Add custom implementations (FormatId, PreviewCreativeRequest, PreviewCreativeResponse, etc.)
    generated_code = add_custom_implementations(generated_code)

    # Fix Format class to use proper types instead of dict[str, Any]
    generated_code = fix_format_types(generated_code)

    # Extract all type names for __all__ export
    type_names = extract_type_names(generated_code)
    all_exports = "\n\n# Explicit exports for module interface\n__all__ = [\n"
    for name in type_names:
        all_exports += f'    "{name}",\n'
    all_exports += "]\n"
    generated_code += all_exports

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

    # Validate no schemas are missing from core_types list
    print("\nValidating schema coverage...")
    all_schemas = sorted(SCHEMAS_DIR.glob("*.json"))
    all_schema_names = {s.name for s in all_schemas if s.name != "index.json"}

    # Schemas we explicitly process
    core_type_names = set(core_types)
    task_patterns = {"*-request.json", "*-response.json"}

    # Schemas we intentionally skip with custom implementations or reasons
    skip_with_reason = {
        # Custom implementations in types/core.py or types/tasks.py
        "format-id.json": "custom FormatId type alias",
        "preview-creative-request.json": "custom implementation",
        "preview-creative-response.json": "custom implementation with preview variants",
        "activate-signal-response.json": "custom discriminated union",
        "create-media-buy-response.json": "custom discriminated union",
        "sync-creatives-response.json": "custom discriminated union",
        "update-media-buy-response.json": "custom discriminated union",
        # Standalone configuration files (not used in Python SDK)
        "adagents.json": "standalone config file for /.well-known/adagents.json",
        "promoted-offerings.json": "standalone brand manifest structure",
        # Asset type schemas (referenced within creative-asset.json's oneOf)
        "audio-asset.json": "sub-schema of creative-asset oneOf",
        "css-asset.json": "sub-schema of creative-asset oneOf",
        "html-asset.json": "sub-schema of creative-asset oneOf",
        "image-asset.json": "sub-schema of creative-asset oneOf",
        "javascript-asset.json": "sub-schema of creative-asset oneOf",
        "markdown-asset.json": "sub-schema of creative-asset oneOf",
        "text-asset.json": "sub-schema of creative-asset oneOf",
        "url-asset.json": "sub-schema of creative-asset oneOf",
        "video-asset.json": "sub-schema of creative-asset oneOf",
        "webhook-asset.json": "sub-schema of creative-asset oneOf",
        # Pricing option schemas (referenced within pricing-option.json's oneOf)
        "cpc-option.json": "sub-schema of pricing-option oneOf",
        "cpcv-option.json": "sub-schema of pricing-option oneOf",
        "cpm-auction-option.json": "sub-schema of pricing-option oneOf",
        "cpm-fixed-option.json": "sub-schema of pricing-option oneOf",
        "cpp-option.json": "sub-schema of pricing-option oneOf",
        "cpv-option.json": "sub-schema of pricing-option oneOf",
        "flat-rate-option.json": "sub-schema of pricing-option oneOf",
        "vcpm-auction-option.json": "sub-schema of pricing-option oneOf",
        "vcpm-fixed-option.json": "sub-schema of pricing-option oneOf",
        # Enum/type schemas (used inline, not as standalone types)
        "asset-type.json": "enum used inline in sub-asset",
        "creative-status.json": "enum used inline in creative-asset",
        "frequency-cap-scope.json": "enum used inline in frequency-cap",
        "identifier-types.json": "enum used inline in targeting",
        "publisher-identifier-types.json": "enum used inline in property",
    }

    # Find schemas that aren't covered
    processed_schemas = core_type_names.copy()
    for pattern in task_patterns:
        task_files = SCHEMAS_DIR.glob(pattern)
        processed_schemas.update(f.name for f in task_files)

    unprocessed = all_schema_names - processed_schemas - set(skip_with_reason.keys())

    if unprocessed:
        print(f"\n✗ ERROR: Found {len(unprocessed)} schema(s) not processed by generator:", file=sys.stderr)
        for schema in sorted(unprocessed):
            print(f"  - {schema}", file=sys.stderr)
        print(f"\nThese schemas should either be:", file=sys.stderr)
        print(f"  1. Added to core_types list in generate_models_simple.py", file=sys.stderr)
        print(f"  2. Added to skip_with_reason with explanation", file=sys.stderr)
        print(f"\nThis prevents accidentally missing schema types!", file=sys.stderr)
        sys.exit(1)

    print(f"  ✓ All {len(all_schema_names)} schemas accounted for")
    print(f"    - {len(core_type_names)} core types")
    print(f"    - {len([s for s in all_schema_names if s.endswith('-request.json') or s.endswith('-response.json')])} task types")
    print(f"    - {len(skip_with_reason)} intentionally skipped")

    print(f"\n✓ Successfully generated and validated models")
    print(f"  Output: {output_file}")
    print(f"  Core types: {len(core_schemas)}")
    print(f"  Task types: {len(task_schemas)}")
    print(f"  Total models: {len(core_schemas) + len(task_schemas)}")


if __name__ == "__main__":
    main()
