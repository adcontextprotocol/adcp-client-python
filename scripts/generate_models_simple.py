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

    # Regular BaseModel class
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
    if not schema["properties"]:
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
        # Extract just the filename from paths like "/schemas/v1/core/format-id.json"
        ref = schema["$ref"]
        filename = ref.split("/")[-1].replace(".json", "")
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


def add_format_id_validation(code: str) -> str:
    """
    Add field validator to FormatId class for pattern enforcement.

    The format-id.json schema specifies a pattern, but Pydantic v2 requires
    explicit field_validator to enforce it.
    """
    # Find the FormatId class - match the class and find where to insert
    # Look for the pattern: class FormatId(...): ... id: str = Field(...)
    # Then add the validator after the last field

    lines = code.split("\n")
    result_lines = []
    in_format_id = False
    found_id_field = False
    indent = ""

    for i, line in enumerate(lines):
        result_lines.append(line)

        # Detect start of FormatId class
        if "class FormatId(BaseModel):" in line:
            in_format_id = True
            # Detect indent level (usually 4 spaces)
            if i + 1 < len(lines):
                next_line = lines[i + 1]
                indent = next_line[: len(next_line) - len(next_line.lstrip())]

        # Detect the id field in FormatId class
        if in_format_id and line.strip().startswith("id: str"):
            found_id_field = True

        # After the id field, add the validator
        if (
            in_format_id
            and found_id_field
            and (
                line.strip() == ""
                or (
                    i + 1 < len(lines)
                    and not lines[i + 1].strip().startswith(("agent_url:", "id:"))
                )
            )
        ):
            # Add validator here
            validator_lines = [
                "",
                f'{indent}@field_validator("id")',
                f"{indent}@classmethod",
                f"{indent}def validate_id_pattern(cls, v: str) -> str:",
                f'{indent}    """Validate format ID contains only alphanumeric characters, hyphens, and underscores."""',
                f'{indent}    if not re.match(r"^[a-zA-Z0-9_-]+$", v):',
                f"{indent}        raise ValueError(",
                f'{indent}            f"Invalid format ID: {{v!r}}. Must contain only alphanumeric characters, hyphens, and underscores"',
                f"{indent}        )",
                f"{indent}    return v",
            ]
            result_lines.extend(validator_lines)
            in_format_id = False
            found_id_field = False

    return "\n".join(result_lines)


def extract_type_names(code: str) -> list[str]:
    """
    Extract all type names (classes and type aliases) from generated code.

    Returns:
        List of type names sorted alphabetically
    """
    type_names = []

    # Parse the code to find class definitions and type aliases
    for line in code.split("\n"):
        stripped = line.strip()

        # Class definitions
        if stripped.startswith("class ") and "(BaseModel)" in stripped:
            # Extract class name: "class Foo(BaseModel):" -> "Foo"
            class_name = stripped.split("class ")[1].split("(")[0].strip()
            type_names.append(class_name)

        # Type aliases: "TypeName = ..." (not inside a class)
        elif "=" in stripped and not stripped.startswith(("#", "    ")):
            # Check if it looks like a type alias at module level
            parts = stripped.split("=", 1)
            if len(parts) == 2:
                name = parts[0].strip()
                # Valid identifier, not a dunder, not an import, starts with capital
                if (
                    name.replace("_", "").isalnum()
                    and not name.startswith("__")
                    and "import" not in stripped
                    and "Field(" not in stripped
                    and name[0].isupper()  # Type names start with capital letter
                ):
                    type_names.append(name)

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
# for complex schemas that use oneOf. We override them here with proper Pydantic classes
# to maintain type safety and enable batch API support.


class FormatId(BaseModel):
    """Structured format identifier with agent URL and format name"""

    agent_url: str = Field(description="URL of the agent that defines this format (e.g., 'https://creatives.adcontextprotocol.org' for standard formats, or 'https://publisher.com/.well-known/adcp/sales' for custom formats)")
    id: str = Field(description="Format identifier within the agent's namespace (e.g., 'display_300x250', 'video_standard_30s')")

    @field_validator("id")
    @classmethod
    def validate_id_pattern(cls, v: str) -> str:
        """Validate format ID contains only alphanumeric characters, hyphens, and underscores."""
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError(
                f"Invalid format ID: {v!r}. Must contain only alphanumeric characters, hyphens, and underscores"
            )
        return v


class PreviewCreativeRequest(BaseModel):
    """Request to generate a preview of a creative manifest. Supports single or batch mode."""

    # Single mode fields
    format_id: FormatId | None = Field(default=None, description="Format identifier for rendering the preview (single mode)")
    creative_manifest: CreativeManifest | None = Field(default=None, description="Complete creative manifest with all required assets (single mode)")
    inputs: list[dict[str, Any]] | None = Field(default=None, description="Array of input sets for generating multiple preview variants")
    template_id: str | None = Field(default=None, description="Specific template ID for custom format rendering")

    # Batch mode field
    requests: list[dict[str, Any]] | None = Field(default=None, description="Array of preview requests for batch processing (1-50 items)")

    # Output format (applies to both modes)
    output_format: Literal["url", "html"] | None = Field(default="url", description="Output format: 'url' for iframe URLs, 'html' for direct embedding")


class PreviewCreativeResponse(BaseModel):
    """Response containing preview links for one or more creatives. Format matches the request: single preview response for single requests, batch results for batch requests."""

    # Single mode fields
    previews: list[dict[str, Any]] | None = Field(default=None, description="Array of preview variants (single mode)")
    interactive_url: str | None = Field(default=None, description="Optional URL to interactive testing page (single mode)")
    expires_at: str | None = Field(default=None, description="ISO 8601 timestamp when preview links expire (single mode)")

    # Batch mode field
    results: list[dict[str, Any]] | None = Field(default=None, description="Array of preview results for batch processing")


# ============================================================================
# ONEOF DISCRIMINATED UNIONS FOR RESPONSE TYPES
# ============================================================================
# These response types use oneOf semantics: success XOR error, never both.
# Implemented as Union types with distinct Success/Error variants.


class ActivateSignalSuccess(BaseModel):
    """Successful signal activation response"""

    decisioning_platform_segment_id: str = Field(
        description="The platform-specific ID to use once activated"
    )
    estimated_activation_duration_minutes: float | None = None
    deployed_at: str | None = None


class ActivateSignalError(BaseModel):
    """Failed signal activation response"""

    errors: list[Error] = Field(description="Task-specific errors and warnings")


# Override the generated ActivateSignalResponse type alias
ActivateSignalResponse = ActivateSignalSuccess | ActivateSignalError


class CreateMediaBuySuccess(BaseModel):
    """Successful media buy creation response"""

    media_buy_id: str = Field(description="The unique ID for the media buy")
    buyer_ref: str = Field(description="The buyer's reference ID for this media buy")
    packages: list[Package] = Field(
        description="Array of approved packages. Each package is ready for creative assignment."
    )
    creative_deadline: str | None = Field(
        None,
        description="ISO 8601 date when creatives must be provided for launch",
    )


class CreateMediaBuyError(BaseModel):
    """Failed media buy creation response"""

    errors: list[Error] = Field(description="Task-specific errors and warnings")


# Override the generated CreateMediaBuyResponse type alias
CreateMediaBuyResponse = CreateMediaBuySuccess | CreateMediaBuyError


class UpdateMediaBuySuccess(BaseModel):
    """Successful media buy update response"""

    media_buy_id: str = Field(description="The unique ID for the media buy")
    buyer_ref: str = Field(description="The buyer's reference ID for this media buy")
    packages: list[Package] = Field(
        description="Array of updated packages reflecting the changes"
    )


class UpdateMediaBuyError(BaseModel):
    """Failed media buy update response"""

    errors: list[Error] = Field(description="Task-specific errors and warnings")


# Override the generated UpdateMediaBuyResponse type alias
UpdateMediaBuyResponse = UpdateMediaBuySuccess | UpdateMediaBuyError


class SyncCreativesSuccess(BaseModel):
    """Successful creative sync response"""

    assignments: list[CreativeAssignment] = Field(
        description="Array of creative assignments with updated status"
    )


class SyncCreativesError(BaseModel):
    """Failed creative sync response"""

    errors: list[Error] = Field(description="Task-specific errors and warnings")


# Override the generated SyncCreativesResponse type alias
SyncCreativesResponse = SyncCreativesSuccess | SyncCreativesError
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
        "from pydantic import BaseModel, ConfigDict, Field, field_validator",
        "",
        "",
        "",
        "# ============================================================================",
        "# CORE DOMAIN TYPES",
        "# ============================================================================",
        "",
    ]

    # Skip core types that have custom implementations
    skip_core_types = {"format-id"}

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
    # Skip types that have custom implementations
    skip_types = {
        "preview-creative-request",
        "preview-creative-response",
        "activate-signal-response",
        "create-media-buy-response",
        "update-media-buy-response",
        "sync-creatives-response",
    }
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

    # Add custom implementations (FormatId, PreviewCreativeRequest, PreviewCreativeResponse)
    generated_code = add_custom_implementations(generated_code)

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
