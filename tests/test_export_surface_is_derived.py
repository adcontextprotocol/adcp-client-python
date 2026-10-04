"""The public type surface resolves, and no generated class is unreachable.

Three properties, each one a defect this suite caught in a shipped wheel:

* every name in ``adcp.types.__all__`` resolves, and no public binding in
  ``_generated`` reassigns a name the module imports — that reassignment needs
  ``# type: ignore[assignment]``, which left mypy holding the pre-reassignment
  declaration while the runtime held its neighbour (#1141). The static half of
  that contract is ``tests/type_checks/authorized_agents_variants.py``;
* every public class in a non-bundled generated module is importable from the
  public mirror of the schema that declares it (#911);
* ``adcp.types.error_details`` carries each ``error-details/*.json`` model
  together with the field types its annotations reference, so a seller
  constructs the payload with typed values (#1080).
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest

import adcp.types
import adcp.types._generated as generated
import adcp.types.domains as domains
import adcp.types.error_details as error_details
from scripts.consolidate_exports import (
    extract_exports_from_module,
    field_annotations,
    generated_models_in,
    scan_declared_names,
    schema_domain,
    unambiguous_domain_bindings,
)

# ---------------------------------------------------------------------------
# Every exported name resolves, and means one thing
# ---------------------------------------------------------------------------


def _domain_modules() -> list[object]:
    """Every generated domain module, imported."""
    return [importlib.import_module(f"adcp.types.domains.{d}") for d in domains.DOMAINS]


@pytest.mark.parametrize(
    "module",
    [adcp.types, generated, error_details],
    ids=["types", "_generated", "error_details"],
)
def test_every_name_in_all_resolves(module: object) -> None:
    """``__all__`` is a promise: a name listed there must be importable."""
    unresolved = [name for name in module.__all__ if not hasattr(module, name)]  # type: ignore[attr-defined]
    assert unresolved == []


def test_generated_module_rebinds_no_imported_name() -> None:
    """A public binding in ``_generated`` is a first binding, never a reassignment."""
    tree = ast.parse(Path(generated.__file__).read_text())
    imported = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    rebound = sorted(
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in imported
    )
    assert rebound == [], (
        "these names are imported and then reassigned, so mypy keeps the "
        "pre-reassignment type while the runtime holds the new object"
    )


# ---------------------------------------------------------------------------
# Every generated class is reachable
# ---------------------------------------------------------------------------


def test_no_generated_type_is_reachable_under_zero_names() -> None:
    """A model an adopter cannot import is a model an adopter cannot construct.

    Every ``(module, type)`` pair the tree declares is bound by the public
    mirror of the module that declares it. Checked against the built package,
    name by name, because a generated module binds several names to one class
    and not every export is a class at all.
    """
    unreachable: list[str] = []
    for type_name, modules in sorted(scan_declared_names().items()):
        for module_name in sorted(modules):
            mirror = importlib.import_module(f"adcp.types.domains.{module_name}")
            if type_name not in mirror.__all__ or not hasattr(mirror, type_name):
                unreachable.append(f"{module_name}.{type_name}")
    assert unreachable == []


def test_every_domain_root_binds_what_its_domain_declares_once() -> None:
    """A domain root carries the unambiguous names, and renames nothing.

    Checked in the forward direction — from the tree to the export — because not
    every export is a class. A schema whose root composes other schemas
    generates a ``typing.Annotated[...]`` alias, which has no ``__module__`` and
    no ``__name__`` to compare; skipping those would quietly stop grading them.
    """
    name_to_modules = scan_declared_names()
    expected = unambiguous_domain_bindings(name_to_modules)
    assert set(expected) == set(domains.DOMAINS)

    for domain, rows in sorted(expected.items()):
        module = importlib.import_module(f"adcp.types.domains.{domain}")
        for type_name, module_name in sorted(rows.items()):
            source = importlib.import_module(f"adcp.types.generated_poc.{module_name}")
            assert getattr(module, type_name) is getattr(source, type_name), f"{domain}.{type_name}"
            assert schema_domain(module_name) == domain
        # Every name a domain root binds keeps the name codegen gave it.
        assert all(name in rows or name in module.__all__ for name in rows)


def test_a_mirror_exports_its_schema_verbatim() -> None:
    """A mirror renames nothing and adds nothing: it is its module's own surface."""
    checked = 0
    for module_name in sorted({m for mods in scan_declared_names().values() for m in mods}):
        mirror = importlib.import_module(f"adcp.types.domains.{module_name}")
        source = importlib.import_module(f"adcp.types.generated_poc.{module_name}")
        for name in mirror.__all__:
            assert getattr(mirror, name) is getattr(source, name), f"{module_name}.{name}"
            checked += 1
    assert checked > 4000, f"only {checked} names checked — the mirror shrank"


def test_a_domain_module_names_the_variant_the_flat_namespace_cannot() -> None:
    """The defect the domain modules answer, pinned as the behaviour it is.

    Three schemas declare ``QuerySummary``. The flat namespace binds one — the
    ``core`` one, which requires nothing — so an adopter reading
    ``ListCreativesResponse.query_summary`` and annotating with
    ``adcp.types.QuerySummary`` gets a model that accepts ``{}`` where the real
    field type requires two fields. Each domain module binds its own.
    """
    import adcp.types.domains.core as core_domain
    import adcp.types.domains.creative as creative_domain
    import adcp.types.domains.protocol as protocol_domain
    from adcp.types.generated_poc.creative.list_creatives_response import ListCreativesResponse

    assert ListCreativesResponse.model_fields["query_summary"].annotation is (
        creative_domain.QuerySummary
    )
    assert sorted(
        name
        for name, field in creative_domain.QuerySummary.model_fields.items()
        if field.is_required()
    ) == ["returned", "total_matching"]

    variants = {
        creative_domain.QuerySummary,
        core_domain.QuerySummary,
        protocol_domain.QuerySummary,
    }
    assert len(variants) == 3, "the three variants must be three distinct classes"
    assert adcp.types.QuerySummary is core_domain.QuerySummary
    assert not [
        name for name, field in adcp.types.QuerySummary.model_fields.items() if field.is_required()
    ], "the flat winner is the permissive variant — that is why the domain path exists"


def test_a_name_its_own_domain_declares_twice_is_reached_through_its_schema() -> None:
    """``creative`` declares ``Creative`` four times, so the domain cannot bind it."""
    import adcp.types.domains.creative as creative_domain
    from adcp.types.generated_poc.creative import list_creatives_response as lcr

    assert "Creative" not in creative_domain.__all__
    declaring = {
        module_name
        for module_name in scan_declared_names()["Creative"]
        if schema_domain(module_name) == "creative"
    }
    assert len(declaring) == 4, declaring

    resolved = {
        getattr(importlib.import_module(f"adcp.types.domains.{module_name}"), "Creative")
        for module_name in declaring
    }
    assert len(resolved) == 4, "each schema's Creative must be its own class"
    assert (
        importlib.import_module("adcp.types.domains.creative.list_creatives_response").Creative
        is lcr.Creative
    )


def test_nine_core_units_get_nine_public_paths() -> None:
    """The case a domain namespace cannot serve, and the reason for the depth."""
    import adcp.types.domains.core as core_domain

    declaring = {
        module_name
        for module_name in scan_declared_names()["Unit"]
        if schema_domain(module_name) == "core"
    }
    assert len(declaring) == 9, declaring
    assert "Unit" not in core_domain.__all__
    resolved = {
        getattr(importlib.import_module(f"adcp.types.domains.{module_name}"), "Unit")
        for module_name in declaring
    }
    assert len(resolved) == 9


# ---------------------------------------------------------------------------
# The generated modules re-export, they never rebuild
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [generated, error_details],
    ids=["_generated", "error_details"],
)
def test_generated_modules_define_and_build_no_class(module: object) -> None:
    """A derived export module binds the generated class, never a copy of it.

    Rebuilding a model with ``create_model`` and a copy of its ``model_fields``
    carries the fields and drops everything else attached to the class — model
    validators, field validators, custom methods. A copy like that validates
    documents the class it stands in for rejects, with no symptom until the data
    is wrong, so disambiguating a name by cloning is not an option here.
    """
    tree = ast.parse(Path(module.__file__).read_text())  # type: ignore[attr-defined]
    assert [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)] == []
    constructors = sorted(
        {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"create_model", "type", "ModelMetaclass", "new_class"}
        }
    )
    assert constructors == []


def test_every_exported_class_is_the_class_its_module_defines() -> None:
    """Identity, not shape: the exported name IS the generated class object.

    ``__module__`` must point into ``generated_poc`` and the class must be the
    object that module holds under its own name. Both halves matter:
    ``create_model`` stamps the calling module onto the class it builds, so a
    copy would satisfy an identity check that trusted ``__module__``.
    """
    checked = 0
    for module in (*_domain_modules(), error_details):
        for name in module.__all__:  # type: ignore[attr-defined]
            bound = getattr(module, name)
            if not inspect.isclass(bound):
                continue
            assert bound.__module__.startswith("adcp.types.generated_poc."), (
                f"{name} resolves to {bound.__module__}.{bound.__name__}, which codegen "
                "did not define — a derived export re-exports, it does not rebuild"
            )
            source = importlib.import_module(bound.__module__)
            assert getattr(source, bound.__name__) is bound, name
            checked += 1
    assert checked > 3000, f"only {checked} classes checked — the surface shrank"


# ---------------------------------------------------------------------------
# The error-details family
# ---------------------------------------------------------------------------


def _error_details_models() -> dict[str, object]:
    """Every public top-level ``*Details`` name an ``error-details/*`` module declares.

    Read from the AST rather than filtered on ``inspect.isclass``: a composing
    root generates a ``typing.Annotated[...]`` alias, and an alias is a model an
    adopter imports like any other. The ``isclass`` filter dropped two of them
    the moment codegen started emitting that shape.
    """
    package = importlib.import_module("adcp.types.generated_poc.error_details")
    package_dir = Path(package.__path__[0])
    models: dict[str, object] = {}
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for name in extract_exports_from_module(package_dir / f"{info.name}.py"):
            if name.endswith("Details") and hasattr(module, name):
                models[name] = getattr(module, name)
    return models


def test_every_error_details_model_is_exported() -> None:
    """Derived from the schema file list, so a new schema arrives exported."""
    models = _error_details_models()
    assert len(models) >= 24, "the error-details family shrank unexpectedly"
    missing = sorted(name for name in models if not hasattr(error_details, name))
    assert missing == []
    for name, cls in models.items():
        assert getattr(error_details, name) is cls


def test_every_error_details_field_type_is_exported() -> None:
    """Exporting a model without its field types leaves it unconstructable.

    Both halves go through ``field_annotations`` and ``generated_models_in``: a
    root may be an alias with no ``model_fields``, and a field type may be a
    union rather than a class. Walking either by hand is what broke this guard
    three times.
    """
    exported = {
        obj
        for name in error_details.__all__
        if inspect.isclass(obj := getattr(error_details, name))
    }
    unreachable = sorted(
        {
            f"{model_name}: {cls.__name__}"
            for model_name, model in _error_details_models().items()
            for annotation in field_annotations(model)
            for cls in generated_models_in(annotation)
            if cls not in exported
        }
    )
    assert unreachable == []


def test_error_details_payload_constructs_with_typed_values() -> None:
    """The point of exporting the family: no dict at the raise site."""
    details = error_details.VersionUnsupportedDetails(
        adcp_version="3.2",
        supported_versions=[
            error_details.SupportedVersion("3.1"),
            error_details.SupportedVersion("3.2"),
        ],
        supported_majors=[error_details.SupportedMajor(3)],
    )
    assert details.model_dump(exclude_none=True) == {
        "adcp_version": "3.2",
        "supported_versions": ["3.1", "3.2"],
        "supported_majors": [3],
    }
    with pytest.raises(ValueError):
        error_details.VersionUnsupportedDetails(adcp_version="3.2")  # type: ignore[call-arg]
