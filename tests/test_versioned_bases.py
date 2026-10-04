"""Pinned named bases agree with their static declarations and wire validators."""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import Field, ValidationError

from adcp.types.versioned import make_versioned_base
from adcp.types.versioned_bases.v31 import ListCreativesRequestBase, PackageRequestBase
from adcp.types.versioned_bases.v32 import PackageRequestBase as PackageRequest32Base
from adcp.validation.schema_loader import get_portable_schema, get_validator


class SellerListCreatives(ListCreativesRequestBase):
    tenant_id: str | None = Field(default=None, exclude=True)


class SellerPackage(PackageRequestBase):
    inventory_key: str | None = Field(default=None, exclude=True)


def test_imported_base_subclass_preserves_protocol_fields_and_excludes_internal_fields():
    request = SellerListCreatives(
        include_assignments=True,
        tenant_id="tenant-1",
        pagination={"max_results": 10},
        account={"account_id": "account-1"},
    )
    assert request.include_assignments is True
    assert request.pagination == {"max_results": 10}
    wire = request.model_dump(mode="json")
    assert "tenant_id" not in wire
    assert json.loads(request.model_dump_json()) == wire
    validator = get_validator("list_creatives", "request", version="3.1")
    assert validator is not None
    assert validator.is_valid(wire)
    assert request.model_json_schema() == get_portable_schema(
        "list_creatives", "request", version="3.1"
    )
    assert SellerListCreatives.model_validate(wire).include_assignments is True
    schema = get_portable_schema("list_creatives", "request", version="3.1")
    assert (
        SellerListCreatives().include_assignments
        == schema["properties"]["include_assignments"]["default"]
    )


def test_nested_wire_values_are_dicts_and_preserve_extensions_for_schema_validation():
    request = SellerListCreatives(context={"trace_id": "t", "future": {"n": 1}})
    assert request.context == {"trace_id": "t", "future": {"n": 1}}
    assert request.model_dump(mode="json")["context"] == request.context
    with pytest.raises(ValidationError):
        SellerListCreatives(pagination={"max_results": 0})
    with pytest.raises(ValidationError):
        SellerListCreatives(account={"account_id": "account-1", "brand": {"domain": "example.com"}})
    with pytest.raises(ValidationError):
        SellerListCreatives(assignment_projection="matching")
    with pytest.raises(ValidationError):
        SellerListCreatives(unknown_internal="secret")


def test_package_version_requiredness_and_dynamic_helper_compatibility():
    package = SellerPackage(
        product_id="p1", pricing_option_id="fixed", budget=100, inventory_key="internal"
    )
    assert package.budget == 100.0
    assert "inventory_key" not in package.model_dump(mode="json")
    with pytest.raises(ValidationError):
        SellerPackage(product_id="p1", pricing_option_id="fixed")
    package32 = PackageRequest32Base(product_id="p1", pricing_option_id="fixed")
    assert package32.budget is None
    assert "budget" not in package32.model_dump(mode="json")
    dynamic = make_versioned_base("3.1", "ListCreativesRequest")
    assert dynamic is make_versioned_base("3.1", "ListCreativesRequest")
    assert dynamic(include_assignments=True).include_assignments is True


@pytest.mark.parametrize("version", ["v30", "v31", "v32"])
def test_stub_runtime_exports_and_annotations_match(version):
    module = importlib.import_module(f"adcp.types.versioned_bases.{version}")
    definitions = importlib.import_module(f"adcp.types.versioned_bases._{version}")
    tree = ast.parse(Path(module.__file__).with_suffix(".pyi").read_text())
    declarations = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    assert set(module.__all__) == set(definitions.FIELDS) == set(declarations)
    for name, fields in definitions.FIELDS.items():
        declared = {
            node.target.id for node in declarations[name].body if isinstance(node, ast.AnnAssign)
        }
        # JSON metadata like $schema has no legal Python attribute spelling;
        # runtime validation still retains it via model_validate / **dict input.
        assert declared == {field for field in fields if field.isidentifier()}
    model = getattr(module, "ListCreativesRequestBase")
    assert model is getattr(module, "ListCreativesRequestBase")
    assert model.__module__ == module.__name__
    assert model.model_fields["include_assignments"].annotation is bool
    schema = model.model_json_schema()
    assert (
        model.model_fields["include_assignments"].default
        == schema["properties"]["include_assignments"]["default"]
    )


def test_importing_version_namespace_does_not_build_bases_or_current_models():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import adcp.types.versioned_bases.v31 as bases; assert 'adcp.types._eager' not in sys.modules; assert 'adcp.types.versioned_bases._v31' not in sys.modules; assert not any(name.endswith('Base') for name in vars(bases)); from adcp.types.versioned_bases.v31 import ListCreativesRequestBase; assert 'adcp.types._eager' not in sys.modules; assert not hasattr(bases, 'TypoRequestBase'); assert not any(name.endswith('Base') and name != 'ListCreativesRequestBase' for name in vars(bases)); print('ok')",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


@pytest.mark.parametrize("checker", ["mypy", "pyright"])
def test_named_adopter_with_static_checkers(checker, tmp_path):
    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests/type_checks/versioned_bases.py"
    if checker == "mypy":
        config = tmp_path / "mypy.ini"
        config.write_text("[mypy]\nplugins = pydantic.mypy\nstrict = True\n")
        command = [sys.executable, "-m", "mypy", "--config-file", str(config), str(fixture)]
    else:
        command = [sys.executable, "-m", "pyright", "--pythonpath", sys.executable, str(fixture)]
    environment = dict(os.environ, MYPYPATH=str(root / "src"))
    result = subprocess.run(
        command, cwd=root, env=environment, capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stdout + result.stderr
