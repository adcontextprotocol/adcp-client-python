"""Generate experimental identity types from pinned draft schemas, offline.

The stable AdCP wire pin and generated domain tree are intentionally unchanged.
The two onboarding slots are an explicit composition of independent P5/P6 drafts.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREVIEW = ROOT / "src/adcp/signing/_identity_schemas"
STABLE = ROOT / "schemas/cache/3.2"
ENTRIES = (
    "core/agent-onboarding.json",
    "core/buyer-onboarding-posture.json",
    "core/principal-relationship.json",
    "trust/v1/trust.json",
)


def build_bundle() -> dict:
    definitions = {}
    visited = set()

    def key(path):
        return path.removesuffix(".json").replace("/", "_").replace("-", "_")

    def load(path):
        if path in visited:
            return
        visited.add(path)
        source = PREVIEW / path
        if not source.exists():
            source = STABLE / path
        schema = json.loads(source.read_text())
        # Compose the independently proposed buyer posture slots without editing
        # the immutable source schemas or claiming the composition is a release.
        if path == "trust/v1/trust.json":
            for name in ("agent", "record_profiles"):
                profiles = (
                    schema["definitions"][name]["properties"]["profiles"]
                    if name == "agent"
                    else schema["definitions"][name]
                )
                profiles["allOf"][1]["properties"]["adcp"]["properties"]["onboarding"] = {
                    "$ref": "/schemas/core/buyer-onboarding-posture.json"
                }

        def walk(value):
            if isinstance(value, dict):
                value.pop("$id", None)
                if "$ref" in value:
                    reference = value["$ref"]
                    if reference.startswith("#"):
                        value["$ref"] = "#/definitions/" + key(path) + reference[1:]
                    else:
                        target, _, fragment = reference.partition("#")
                        target = target.split("/schemas/", 1)[1]
                        if target.startswith("v3/") or target.split("/", 1)[0][0].isdigit():
                            target = target.split("/", 1)[1]
                        load(target)
                        value["$ref"] = (
                            "#/definitions/"
                            + key(target)
                            + ("/" + fragment.lstrip("/") if fragment else "")
                        )
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(schema)
        definitions[key(path)] = schema

    for path in ENTRIES:
        load(path)
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "IdentityPreview",
        "definitions": definitions,
        "anyOf": [{"$ref": "#/definitions/" + key(path)} for path in ENTRIES],
    }


def main():
    bundle = build_bundle()
    (PREVIEW / "bundle.json").write_text(json.dumps(bundle, indent=2) + "\n")
    target = ROOT / "src/adcp/types/_identity_preview.py"
    with tempfile.TemporaryDirectory() as directory:
        schema = Path(directory) / "identity-preview.json"
        schema.write_text(json.dumps(bundle))
        subprocess.run(
            [
                sys.executable,
                "-m",
                "datamodel_code_generator",
                "--input",
                str(schema),
                "--input-file-type",
                "jsonschema",
                "--output",
                str(target),
                "--output-model-type",
                "pydantic_v2.BaseModel",
                "--target-python-version",
                "3.10",
                "--use-union-operator",
                "--use-standard-collections",
                "--disable-timestamp",
                "--use-schema-description",
                "--use-subclass-enum",
                "--use-annotated",
                "--field-constraints",
            ],
            check=True,
        )
    # Codegen still emits a constrained type call for propertyNames used as
    # dict keys. Annotated preserves the runtime constraint and is valid for
    # static type checkers (including inside a RootModel generic base).
    text = target.read_text().replace(
        "constr(pattern=r'^[a-z0-9-]+$')",
        "Annotated[str, Field(pattern=r'^[a-z0-9-]+$')]",
    )
    # Readers retain future requirement kinds so they can show the posture's
    # human fallback instead of discarding the whole capability response.
    # The immutable schema bundle remains strict for authoring validation.
    text = text.replace(
        "kind: EnumsAgentOnboardingRequirementKind",
        "kind: Annotated[EnumsAgentOnboardingRequirementKind | str, "
        "Field(union_mode='left_to_right')]",
    )
    text = text.replace("    constr,\n", "")
    target.write_text(text)
    subprocess.run([sys.executable, "-m", "black", str(target)], check=True)
    print(f"Generated {target.relative_to(ROOT)} from pinned drafts")


if __name__ == "__main__":
    main()
