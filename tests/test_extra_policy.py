"""Tests for AdCPBaseModel extra field policy.

Validates that:
- AdCPBaseModel defaults to extra='forbid'
- Generated types with additionalProperties: true override to extra='allow'
- Types without additionalProperties inherit forbid from base
- Consumer subclasses can override extra policy freely
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ConfigDict, ValidationError

from adcp.types.base import AdCPBaseModel

SCHEMAS_DIR = Path(__file__).parent.parent / "schemas" / "cache"
GENERATED_DIR = Path(__file__).parent.parent / "src" / "adcp" / "types" / "generated_poc"


def test_base_model_config_is_forbid() -> None:
    """Sanity check: AdCPBaseModel must have extra='forbid'."""
    assert AdCPBaseModel.model_config.get("extra") == "forbid", (
        f"AdCPBaseModel.model_config is {AdCPBaseModel.model_config!r} — "
        "is the package installed from the correct branch?"
    )


class TestBaseModelDefault:
    """AdCPBaseModel defaults to extra='forbid'."""

    def test_base_model_forbids_extra_fields(self) -> None:
        class StrictType(AdCPBaseModel):
            name: str

        with pytest.raises(ValidationError, match="extra_forbidden"):
            StrictType(name="test", unknown_field="oops")

    def test_base_model_accepts_known_fields(self) -> None:
        class StrictType(AdCPBaseModel):
            name: str

        obj = StrictType(name="test")
        assert obj.name == "test"


class TestGeneratedTypeOverrides:
    """Generated types with additionalProperties: true override to extra='allow'."""

    def test_allow_override_accepts_extra_fields(self) -> None:
        class ExtensibleType(AdCPBaseModel):
            model_config = ConfigDict(extra="allow")
            name: str

        obj = ExtensibleType(name="test", extra_field="allowed")
        assert obj.name == "test"
        assert obj.extra_field == "allowed"  # type: ignore[attr-defined]

    def test_forbid_inherited_without_explicit_config(self) -> None:
        """A subclass without its own model_config inherits forbid from base."""

        class InheritedType(AdCPBaseModel):
            name: str
            value: int

        with pytest.raises(ValidationError, match="extra_forbidden"):
            InheritedType(name="test", value=1, surprise="nope")


class TestConsumerSubclassing:
    """Consumers can override extra policy on subclasses."""

    def test_consumer_can_forbid_on_allow_parent(self) -> None:
        """Consumer subclass can tighten from allow to forbid."""

        class LibraryType(AdCPBaseModel):
            model_config = ConfigDict(extra="allow")
            name: str

        class ConsumerType(LibraryType):
            model_config = ConfigDict(extra="forbid")

        with pytest.raises(ValidationError, match="extra_forbidden"):
            ConsumerType(name="test", unknown="rejected")

    def test_consumer_can_ignore_on_allow_parent(self) -> None:
        """Consumer subclass can relax from allow to ignore."""

        class LibraryType(AdCPBaseModel):
            model_config = ConfigDict(extra="allow")
            name: str

        class ConsumerType(LibraryType):
            model_config = ConfigDict(extra="ignore")

        obj = ConsumerType(name="test", unknown="silently_dropped")
        assert obj.name == "test"
        assert not hasattr(obj, "unknown")

    def test_consumer_base_class_pattern(self) -> None:
        """Consumer can use a base class to batch-apply extra policy."""

        class LibraryType(AdCPBaseModel):
            model_config = ConfigDict(extra="allow")
            name: str

        class StrictBase(LibraryType):
            model_config = ConfigDict(extra="forbid")

        class ConsumerType(StrictBase):
            pass

        with pytest.raises(ValidationError, match="extra_forbidden"):
            ConsumerType(name="test", unknown="rejected")


class TestGeneratedCodeMatchesSchemas:
    """CI guard: generated extra='allow' must be backed by schema additionalProperties."""

    @staticmethod
    def _schema_allows_extra(obj: Any, all_schemas: dict[str, Any]) -> bool:
        """Check if a schema has additionalProperties: true, following $ref chains.

        Recursively walks the full schema tree. This is safe because
        non-structural keys (description, title, examples) contain strings
        or simple arrays, never dicts with additionalProperties.
        """
        if isinstance(obj, dict):
            if obj.get("additionalProperties") is True:
                return True
            # Follow $ref to check composed schemas
            if "$ref" in obj:
                ref_path = obj["$ref"]
                ref_normalized = ref_path.replace("-", "_").lstrip("./")
                for key in all_schemas:
                    if key == ref_normalized or key.endswith("/" + ref_normalized):
                        if TestGeneratedCodeMatchesSchemas._schema_allows_extra(
                            all_schemas[key], all_schemas
                        ):
                            return True
            return any(
                TestGeneratedCodeMatchesSchemas._schema_allows_extra(v, all_schemas)
                for k, v in obj.items()
                if k != "$schema"
            )
        if isinstance(obj, list):
            return any(
                TestGeneratedCodeMatchesSchemas._schema_allows_extra(item, all_schemas)
                for item in obj
            )
        return False

    @staticmethod
    def _load_schemas() -> dict[str, Any]:
        """Load all schemas with underscore-normalized keys for lookup."""
        all_schemas: dict[str, Any] = {}
        for schema_file in SCHEMAS_DIR.rglob("*.json"):
            if schema_file.name in (".hashes.json", "index.json"):
                continue
            with open(schema_file) as f:
                schema = json.load(f)
            rel = str(schema_file.relative_to(SCHEMAS_DIR))
            underscore_key = rel.replace("-", "_")
            all_schemas[underscore_key] = schema
        return all_schemas

    def test_no_spurious_extra_allow(self) -> None:
        """Generated types with extra='allow' must have schema additionalProperties: true."""
        all_schemas = self._load_schemas()
        schema_allows = {
            key: self._schema_allows_extra(schema, all_schemas)
            for key, schema in all_schemas.items()
        }

        spurious = []
        for py_file in sorted(GENERATED_DIR.rglob("*.py")):
            if py_file.name == "__init__.py":
                continue
            content = py_file.read_text()
            if "extra='allow'" not in content and 'extra="allow"' not in content:
                continue
            m = re.search(r"filename:\s+(.+)", content)
            if m:
                schema_name = m.group(1).strip()
                if schema_name in schema_allows and not schema_allows[schema_name]:
                    spurious.append(f"{py_file.name} <- {schema_name}")

        assert not spurious, (
            f"Generated files have extra='allow' without schema support:\n"
            + "\n".join(f"  {s}" for s in spurious)
        )
