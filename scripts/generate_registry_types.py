#!/usr/bin/env python3
"""Generate Pydantic models from the AAO registry OpenAPI spec.

Usage:
    python scripts/generate_registry_types.py

Reads:  schemas/registry-openapi.yaml
Writes: src/adcp/types/registry.py

Uses datamodel-code-generator to produce Pydantic v2 models, then applies
post-processing fixes for name collisions and formatting.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPENAPI_PATH = ROOT / "schemas" / "registry-openapi.yaml"
OUTPUT_PATH = ROOT / "src" / "adcp" / "types" / "registry.py"

# Rename colliding enum classes. datamodel-code-generator appends numbers
# when the same enum name appears with different values.
ENUM_RENAMES: dict[str, str] = {
    "Source": "BrandSource",
    "Source1": "BrandRegistrySource",
    "Source2": "PropertySource",
    "Source3": "PropertyRegistrySource",
    "Source4": "AgentSource",
    "Type": "AgentType",
    "Protocol": "AgentProtocol",
    "Status": "ComplianceStatus",
    "LifecycleStage": "AgentLifecycleStage",
    "Category": "PolicyCategory",
    "Enforcement": "PolicyEnforcement",
    "SourceType": "PolicySourceType",
    "ReviewStatus": "PolicyReviewStatus",
    "Contact": "AgentContact",
    "Contact1": "AgentDetailedContact",
    # Avoid collision with core.Error
    "Error": "RegistryApiError",
}

# Classes to rename for clarity (inline response/request schemas)
CLASS_RENAMES: dict[str, str] = {
    "AuthorizedAgent1": "DomainAuthorizedAgent",
    "SalesAgentsClaimingItem": "SalesAgentClaim",
    "Pas": "PolicyExemplarPass",
    "FailItem": "PolicyExemplarFail",
    "Exemplars": "PolicyExemplars",
    "Revision": "ActivityRevision",
    "Revision2": "PolicyRevision",
    "DiscoveredFrom": "AgentDiscoveredFrom",
    "DiscoveredFrom1": "PublisherDiscoveredFrom",
    "Member": "AgentMember",
    "Property": "ResolvedPropertyEntry",
    "Tool": "AgentTool",
    "StandardOperations": "AgentStandardOperations",
    "CreativeCapabilities": "AgentCreativeCapabilities",
}


def run_codegen() -> str:
    """Run datamodel-code-generator and return raw output."""
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tmp:
        tmp_path = tmp.name

    cmd = [
        sys.executable,
        "-m",
        "datamodel_code_generator",
        "--input",
        str(OPENAPI_PATH),
        "--input-file-type",
        "openapi",
        "--output",
        tmp_path,
        "--output-model-type",
        "pydantic_v2.BaseModel",
        "--base-class",
        "adcp.types.base.RegistryBaseModel",
        "--use-standard-collections",
        "--use-union-operator",
        "--target-python-version",
        "3.10",
        "--use-annotated",
        "--field-constraints",
        "--collapse-root-models",
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    content = Path(tmp_path).read_text()
    Path(tmp_path).unlink()
    return content


def apply_renames(content: str) -> str:
    """Apply class and enum renames to avoid collisions."""
    all_renames = {**ENUM_RENAMES, **CLASS_RENAMES}

    # Sort by length descending to avoid partial matches
    for old_name, new_name in sorted(all_renames.items(), key=lambda x: len(x[0]), reverse=True):
        # Rename class definitions
        content = re.sub(
            rf"^(class ){old_name}(\()",
            rf"\g<1>{new_name}\g<2>",
            content,
            flags=re.MULTILINE,
        )
        # Rename type references (word boundary)
        content = re.sub(
            rf"\b{old_name}\b",
            new_name,
            content,
        )

    return content


def fix_spec_reality_gaps(content: str) -> str:
    """Fix cases where the OpenAPI spec doesn't match the actual API."""
    # ValidationResult.errors/warnings: spec says list[str] but API
    # returns list[dict] with structured error objects
    content = content.replace(
        "errors: list[str] | None = None\n" "    warnings: list[str] | None = None",
        "errors: list[str | dict[str, Any]] | None = None\n"
        "    warnings: list[str | dict[str, Any]] | None = None",
    )
    # Class renames intentionally rewrite type names, but can also touch
    # prose emitted from schema descriptions.
    content = content.replace("Founding AgentMember", "Founding Member")
    # The registry feed endpoint now declares its response inline, so
    # datamodel-code-generator no longer emits the named models imported by
    # RegistryClient/RegistrySync. Keep the stable SDK surface.
    if "class FeedEvent(" not in content:
        content += (
            "\n\nclass FeedEvent(RegistryBaseModel):\n"
            "    event_id: str\n"
            "    event_type: str\n"
            "    entity_type: str\n"
            "    entity_id: str\n"
            "    payload: dict[str, Any]\n"
            "    actor: str\n"
            "    created_at: str\n"
            "\n\nclass FeedPage(RegistryBaseModel):\n"
            "    events: list[FeedEvent]\n"
            "    cursor: str | None\n"
            "    has_more: bool\n"
        )
    return content


def fix_imports(content: str) -> str:
    """Fix import statements for the registry module."""
    # Replace the default BaseModel import with our base
    content = content.replace(
        "from pydantic import BaseModel",
        "from adcp.types.base import RegistryBaseModel",
    )
    # Remove any duplicate base imports
    content = content.replace(
        "from adcp.types.base import RegistryBaseModel\n"
        "from adcp.types.base import RegistryBaseModel\n",
        "from adcp.types.base import RegistryBaseModel\n",
    )
    return content


def add_header(content: str) -> str:
    """Add generation header."""
    header = (
        '"""Registry API types generated from OpenAPI spec.\n'
        "\n"
        "DO NOT EDIT — regenerate with:\n"
        "    python scripts/generate_registry_types.py\n"
        "\n"
        "Source: schemas/registry-openapi.yaml\n"
        '"""\n\n'
    )
    # Replace the datamodel-code-generator header
    content = re.sub(
        r"# generated by datamodel-codegen:.*?\n\n",
        header,
        content,
        flags=re.DOTALL,
    )
    return content


def build_all_list(content: str) -> str:
    """Build __all__ from class definitions."""
    classes = re.findall(r"^class (\w+)\(", content, re.MULTILINE)
    if not classes:
        return content

    all_str = "__all__ = [\n"
    for cls in classes:
        all_str += f'    "{cls}",\n'
    all_str += "]\n\n"

    # Insert after imports, before first class
    first_class = re.search(r"^class ", content, re.MULTILINE)
    if first_class:
        insert_pos = first_class.start()
        content = content[:insert_pos] + all_str + content[insert_pos:]

    return content


def format_output(path: Path) -> None:
    """Format with ruff."""
    subprocess.run(
        ["ruff", "format", str(path)],
        check=False,
        capture_output=True,
    )
    subprocess.run(
        ["ruff", "check", "--fix", str(path)],
        check=False,
        capture_output=True,
    )


def main() -> None:
    if not OPENAPI_PATH.exists():
        print(f"ERROR: {OPENAPI_PATH} not found", file=sys.stderr)
        print(
            "Run: curl -o schemas/registry-openapi.yaml "
            "https://agenticadvertising.org/openapi/registry.yaml",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Generating from {OPENAPI_PATH}...")
    content = run_codegen()

    print("Applying renames...")
    content = apply_renames(content)

    print("Fixing spec/reality gaps...")
    content = fix_spec_reality_gaps(content)

    print("Fixing imports...")
    content = fix_imports(content)

    print("Adding header...")
    content = add_header(content)

    print("Building __all__...")
    content = build_all_list(content)

    print(f"Writing {OUTPUT_PATH}...")
    OUTPUT_PATH.write_text(content)

    print("Formatting...")
    format_output(OUTPUT_PATH)

    # Count classes
    classes = re.findall(r"^class (\w+)\(", OUTPUT_PATH.read_text(), re.MULTILINE)
    print(f"Generated {len(classes)} types in {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
