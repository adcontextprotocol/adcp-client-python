"""Tests for AdCPBaseModel extra field policy.

Validates that:
- AdCPBaseModel defaults to extra='ignore' (forward-compatible)
- Generated types with open or typed additionalProperties override to extra='allow'
- Generated types with x-adcp-open-payload: true preserve open payload fields
- Types without additionalProperties inherit ignore from base
- Consumer subclasses can override extra policy freely
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ConfigDict, ValidationError

from adcp._version import _read_packaged_version
from adcp.types.base import AdCPBaseModel
from adcp.types.domains.core.media_buy_features import MediaBuyFeatures
from adcp.types.domains.governance.check_governance_request import CheckGovernanceRequest
from adcp.validation.version import resolve_bundle_key
from scripts.post_generate_fixes import (
    _ensure_configdict_import,
    _first_generated_class_name,
    _inline_typed_extra_annotations,
    _open_payload_class_names,
    _set_class_extra_allow,
)

_BUNDLE_KEY = resolve_bundle_key(_read_packaged_version())
SCHEMAS_DIR = Path(__file__).parent.parent / "schemas" / "cache" / _BUNDLE_KEY
GENERATED_DIR = Path(__file__).parent.parent / "src" / "adcp" / "types" / "domains"


def test_base_model_config_is_ignore() -> None:
    """Sanity check: AdCPBaseModel must have extra='ignore'."""
    assert AdCPBaseModel.model_config.get("extra") == "ignore", (
        f"AdCPBaseModel.model_config is {AdCPBaseModel.model_config!r} — "
        "is the package installed from the correct branch?"
    )


class TestBaseModelDefault:
    """AdCPBaseModel defaults to extra='ignore'."""

    def test_base_model_drops_extra_fields(self) -> None:
        """Unknown fields are silently dropped, not stored or rejected."""

        class DefaultType(AdCPBaseModel):
            name: str

        obj = DefaultType(name="test", unknown_field="dropped")
        assert obj.name == "test"
        assert not hasattr(obj, "unknown_field")

    def test_base_model_accepts_known_fields(self) -> None:
        class DefaultType(AdCPBaseModel):
            name: str

        obj = DefaultType(name="test")
        assert obj.name == "test"


class TestGeneratedTypeOverrides:
    """Generated types with additionalProperties override to extra='allow'."""

    def test_allow_override_stores_extra_fields(self) -> None:
        class ExtensibleType(AdCPBaseModel):
            model_config = ConfigDict(extra="allow")
            name: str

        obj = ExtensibleType(name="test", extra_field="stored")
        assert obj.name == "test"
        assert obj.extra_field == "stored"  # type: ignore[attr-defined]

    def test_ignore_inherited_without_explicit_config(self) -> None:
        """A subclass without its own model_config inherits ignore from base."""

        class InheritedType(AdCPBaseModel):
            name: str
            value: int

        obj = InheritedType(name="test", value=1, surprise="dropped")
        assert obj.name == "test"
        assert not hasattr(obj, "surprise")

    def test_open_payload_field_preserves_extension_data(self) -> None:
        """x-adcp-open-payload fields keep arbitrary structured payload data."""
        payload = {
            "package_id": "pkg_1",
            "custom_targeting": {"segments": ["sports_fans"], "score": 0.92},
        }

        # A payload makes this an intent check, and governance/check-governance-request.json
        # states that an intent check names the tool and the agent it targets.
        obj = CheckGovernanceRequest(
            plan_id="plan_1",
            caller="https://buyer.example",
            tool="create_media_buy",
            target_agent="https://seller.example",
            payload=payload,
        )

        assert obj.payload == payload

    def test_typed_additional_properties_are_preserved_and_validated(self) -> None:
        """Schema-valued additionalProperties retain their generated value type."""
        features = MediaBuyFeatures(seller_extension=True)

        assert features.seller_extension is True
        with pytest.raises(ValidationError, match="bool_type"):
            MediaBuyFeatures(seller_extension=[])

    def test_named_open_payload_schema_injects_extra_allow(self) -> None:
        """Named x-adcp-open-payload schemas get explicit generated model_config."""
        schema = {
            "title": "Partner Payload",
            "type": "object",
            "x-adcp-open-payload": True,
            "properties": {"name": {"type": "string"}},
        }
        class_names, anonymous_count = _open_payload_class_names(schema)
        assert class_names == ["PartnerPayload"]
        assert anonymous_count == 0

        content = (
            "from __future__ import annotations\n\n"
            "from pydantic import Field\n\n\n"
            "class PartnerPayload(AdCPBaseModel):\n"
            "    name: str\n"
        )
        updated, status = _set_class_extra_allow(content, "PartnerPayload")
        updated = _ensure_configdict_import(updated)

        assert status == "updated"
        assert "from pydantic import ConfigDict, Field" in updated
        assert "model_config = ConfigDict(\n        extra='allow',\n    )" in updated

    def test_compact_configdict_forbid_rewritten_in_place(self) -> None:
        """Compact single-line ConfigDict(extra='forbid') is rewritten, not duplicated.

        The compact form `model_config = ConfigDict(extra='forbid')` must be
        flipped to extra='allow' in place. Prepending a second model_config
        block instead produces two declarations and breaks Pydantic at import.
        """
        content = (
            "from __future__ import annotations\n\n"
            "from pydantic import ConfigDict, Field\n\n\n"
            "class PartnerPayload(AdCPBaseModel):\n"
            "    model_config = ConfigDict(extra='forbid')\n"
            "    name: str\n"
        )
        updated, status = _set_class_extra_allow(content, "PartnerPayload")

        assert status == "updated"
        assert "model_config = ConfigDict(extra='allow')" in updated
        assert "extra='forbid'" not in updated
        # Exactly one model_config declaration — no duplicate prepended block.
        assert updated.count("model_config") == 1

    def test_configdict_import_added_when_no_pydantic_import_exists(self) -> None:
        content = (
            "from __future__ import annotations\n\n"
            "class PartnerPayload(AdCPBaseModel):\n"
            "    model_config = ConfigDict(extra='allow')\n"
        )

        updated = _ensure_configdict_import(content)

        assert (
            "from __future__ import annotations\n\nfrom pydantic import ConfigDict\n\n" in updated
        )
        compile(updated, "<generated>", "exec")

    def test_typed_extra_annotation_is_moved_into_model_class(self) -> None:
        content = (
            "class FeatureFlags(AdCPBaseModel):\n"
            "    model_config = ConfigDict(extra='allow')\n\n\n"
            "FeatureFlags.__annotations__['__pydantic_extra__'] = Dict[str, bool]\n"
            "FeatureFlags.model_rebuild(force=True)\n"
        )

        updated, fixed = _inline_typed_extra_annotations(content)

        assert fixed == 1
        assert "    __pydantic_extra__: Dict[str, bool]\n" in updated
        assert "FeatureFlags.__annotations__" not in updated
        assert "FeatureFlags.model_rebuild" not in updated

    def test_titleless_root_fallback_skips_leading_enum(self) -> None:
        schema = {"type": "object", "x-adcp-open-payload": True}
        class_names, anonymous_count = _open_payload_class_names(schema)
        content = (
            "from enum import Enum\n\n"
            "class Kind(Enum):\n"
            "    one = 'one'\n\n"
            "class PartnerPayload(AdCPBaseModel):\n"
            "    name: str\n"
        )

        assert class_names == [None]
        assert anonymous_count == 0
        assert _first_generated_class_name(content) == "PartnerPayload"


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

    def test_consumer_can_forbid_on_ignore_parent(self) -> None:
        """Consumer subclass can tighten from ignore to forbid (dev/CI pattern)."""

        class LibraryType(AdCPBaseModel):
            name: str

        class ConsumerType(LibraryType):
            model_config = ConfigDict(extra="forbid")

        with pytest.raises(ValidationError, match="extra_forbidden"):
            ConsumerType(name="test", unknown="rejected")

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
    """CI guard: generated extra='allow' must be backed by schema open-payload policy."""

    @staticmethod
    def _schema_allows_extra(
        obj: Any, all_schemas: dict[str, Any], active: frozenset[int] = frozenset()
    ) -> bool:
        """Check if a schema allows extra fields, following $ref chains.

        Recursively walks the full schema tree, stopping only reference cycles
        on the current path so an open sibling remains discoverable.
        Non-structural keys are safe because
        non-structural keys (description, title, examples) contain strings
        or simple arrays, never dicts with additionalProperties.
        """
        if isinstance(obj, (dict, list)):
            if id(obj) in active:
                return False
            active = active | {id(obj)}
        if isinstance(obj, dict):
            open_payload = obj.get("x-adcp-open-payload")
            if open_payload is False:
                return False
            if open_payload is True:
                return True
            additional_properties = obj.get("additionalProperties")
            if additional_properties is True or isinstance(additional_properties, dict):
                return True
            # Follow $ref to check composed schemas
            if "$ref" in obj:
                ref_path = obj["$ref"]
                if ref_path.startswith(("http://", "https://")):
                    from urllib.parse import urlsplit

                    ref_path = urlsplit(ref_path).path
                ref_normalized = ref_path.replace("-", "_").lstrip("./")
                # Strip versioned prefix like /schemas/3.0.0_rc.2/ to get relative key
                ref_stripped = re.sub(r"^/?schemas/[^/]+/", "", ref_normalized)
                for key in all_schemas:
                    if (
                        key in (ref_normalized, ref_stripped)
                        or key.endswith("/" + ref_normalized)
                        or key.endswith("/" + ref_stripped)
                    ):
                        if TestGeneratedCodeMatchesSchemas._schema_allows_extra(
                            all_schemas[key], all_schemas, active
                        ):
                            return True
            return any(
                TestGeneratedCodeMatchesSchemas._schema_allows_extra(v, all_schemas, active)
                for k, v in obj.items()
                if k != "$schema"
            )
        if isinstance(obj, list):
            return any(
                TestGeneratedCodeMatchesSchemas._schema_allows_extra(item, all_schemas, active)
                for item in obj
            )
        return False

    @staticmethod
    def _load_schemas() -> dict[str, Any]:
        """Load all schemas with underscore-normalized keys for lookup."""
        all_schemas: dict[str, Any] = {}
        for schema_file in SCHEMAS_DIR.rglob("*.json"):
            if schema_file.name == "index.json":
                continue
            with open(schema_file) as f:
                schema = json.load(f)
            rel = str(schema_file.relative_to(SCHEMAS_DIR))
            underscore_key = rel.replace("-", "_")
            all_schemas[underscore_key] = schema
        return all_schemas

    @staticmethod
    def _declares_extra_allow(source: str) -> bool:
        """Whether this module DECLARES ``extra='allow'``, by parsing it.

        A substring search over the file text cannot tell a declaration from a
        mention, and the branch emitter writes the string in prose: every
        generated branch class carries a comment explaining why its
        ``mode="before"`` validator is needed, and the explanation names
        ``extra='allow'``. Three generated modules mention it that way while
        declaring ``extra='forbid'``, so a text match asked the wrong question
        of all three -- flagging one against its closed schema, and passing the
        other two only because their schemas happen to be open.
        """
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - generated code parses
            return False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", "")) == "ConfigDict"
            ):
                for keyword in node.keywords:
                    if (
                        keyword.arg == "extra"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value == "allow"
                    ):
                        return True
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=False):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "extra"
                        and isinstance(value, ast.Constant)
                        and value.value == "allow"
                    ):
                        return True
        return False

    def test_a_mention_of_extra_allow_is_not_a_declaration(self) -> None:
        """The detector reads the config, not the file's prose.

        Both halves asserted: the generated comment that made this necessary
        must not count, and a real ``ConfigDict`` must.
        """
        mention = (
            "class X:\n"
            "    model_config = ConfigDict(extra='forbid')\n"
            "    # a raw document still needs this: extra='allow' files an\n"
            "    # undeclared key under extras and accepts\n"
        )
        assert not self._declares_extra_allow(mention)
        assert self._declares_extra_allow("model_config = ConfigDict(extra='allow')")
        assert self._declares_extra_allow("model_config = {'extra': 'allow'}")

    def test_no_spurious_extra_allow(self) -> None:
        """Generated types with extra='allow' must have schema support for open extras."""
        all_schemas = self._load_schemas()
        schema_allows = {
            key: self._schema_allows_extra(schema, all_schemas)
            for key, schema in all_schemas.items()
        }

        spurious = []
        graded = 0
        for py_file in sorted(GENERATED_DIR.rglob("*.py")):
            if py_file.name == "__init__.py":
                continue
            content = py_file.read_text()
            if not self._declares_extra_allow(content):
                continue
            m = re.search(r"filename:\s+(.+)", content)
            if m:
                schema_name = m.group(1).strip()
                if schema_name in schema_allows:
                    graded += 1
                    if not schema_allows[schema_name]:
                        spurious.append(f"{py_file.name} <- {schema_name}")

        # A detector that matches nothing grades nothing and reports a clean
        # pass, which is how this check would fail silently if the AST walk
        # above ever stopped recognising the generated form.
        assert graded > 400, f"only {graded} generated modules were graded; the detector is broken"
        assert (
            not spurious
        ), "Generated files have extra='allow' without schema support:\n" + "\n".join(
            f"  {s}" for s in spurious
        )

    def test_open_payload_true_allows_extra_even_without_additional_properties(self) -> None:
        """x-adcp-open-payload is an authoritative open-payload signal."""
        assert self._schema_allows_extra({"x-adcp-open-payload": True}, {})

    def test_closed_recursive_references_do_not_allow_extras(self) -> None:
        schemas = {
            "first.json": {"$ref": "second.json", "additionalProperties": False},
            "second.json": {"items": {"$ref": "first.json"}},
        }
        assert not self._schema_allows_extra(schemas["first.json"], schemas)

    def test_recursive_reference_does_not_hide_a_reachable_open_sibling(self) -> None:
        schemas = {
            "first.json": {"allOf": [{"$ref": "second.json"}, {"$ref": "open.json"}]},
            "second.json": {"$ref": "first.json"},
            "open.json": {"x-adcp-open-payload": True},
        }
        assert self._schema_allows_extra(schemas["first.json"], schemas)
        assert self._schema_allows_extra(schemas["second.json"], schemas)

    def test_recursive_references_do_not_hide_reachable_open_payloads(self) -> None:
        schemas = {
            "a.json": {"$ref": "b.json"},
            "b.json": {"$ref": "a.json", "properties": {"extension": {"type": "string"}}},
        }
        assert not self._schema_allows_extra(schemas["a.json"], schemas)
        schemas["b.json"]["properties"]["extension"] = {"additionalProperties": True}
        assert self._schema_allows_extra(schemas["a.json"], schemas)

    def test_typed_additional_properties_allows_extra(self) -> None:
        """A schema-valued additionalProperties keyword is an open object contract."""
        assert self._schema_allows_extra(
            {"additionalProperties": {"type": "boolean"}},
            {},
        )

    def test_open_payload_false_wins_over_additional_properties(self) -> None:
        """x-adcp-open-payload false prevents accidental widening of structured objects."""
        assert not self._schema_allows_extra(
            {"x-adcp-open-payload": False, "additionalProperties": True},
            {},
        )
