"""Failure and check-mode safety for the type-generation pipeline."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest

from scripts import generate_types


@pytest.mark.parametrize("present", [True, False])
def test_canonical_references_use_only_the_pinned_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, present: bool
) -> None:
    """Canonical refs work without a published CDN and fail closed on a cache miss."""
    cache = tmp_path / "cache"
    cache.mkdir()
    if present:
        (cache / "choice.json").write_text(json.dumps({"type": "string", "enum": ["local"]}))
    monkeypatch.setattr(generate_types, "SCHEMAS_DIR", cache)
    monkeypatch.setattr(generate_types, "_BUNDLE_KEY", "3.2.0-rc.6")
    source = tmp_path / "request.json"
    source.write_text(
        json.dumps(
            {
                "title": "Request",
                "type": "object",
                "properties": {
                    "choice": {
                        "$ref": "https://adcontextprotocol.org/schemas/3.2.0-rc.6/choice.json"
                    }
                },
            }
        )
    )
    output = tmp_path / "request.py"
    result = generate_types._run_datamodel_codegen(source, output)
    if present:
        assert result.returncode == 0, result.stderr
        assert "local" in output.read_text()
    else:
        assert result.returncode != 0
        assert "$ref local file not found" in result.stderr


def test_restore_unchanged_file_preserves_prior_generated_timestamp(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.py"
    candidate = tmp_path / "candidate.py"
    baseline.write_text("#   timestamp: yesterday\nGeneration date: yesterday UTC\nVALUE = 1\n")
    candidate.write_text("#   timestamp: today\nGeneration date: today UTC\nVALUE = 1\n")

    assert generate_types.restore_unchanged_file(candidate, baseline)
    assert candidate.read_bytes() == baseline.read_bytes()


def _configure_fake_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    fail_ergonomic: bool,
) -> tuple[Path, Path, Path, Path]:
    repository = tmp_path / "repository"
    output = repository / "src" / "adcp" / "types" / "domains"
    consolidated = output.parent / "_generated.py"
    ergonomic = output.parent / "_ergonomic.py"
    deltas = repository / "SCHEMA_DELTAS.md"
    output.mkdir(parents=True)
    scripts = repository / "scripts"
    scripts.mkdir()
    (scripts / "generate_ergonomic_coercion.py").write_text("")
    (output / "sentinel.py").write_text("old generated tree\n")
    consolidated.write_text("old consolidated exports\n")
    ergonomic.write_text("old ergonomic module\n")
    deltas.write_text("old delta report\n")

    monkeypatch.setattr(generate_types, "REPO_ROOT", repository)
    monkeypatch.setattr(generate_types, "OUTPUT_DIR", output)
    monkeypatch.setattr(generate_types, "DELTAS_FILE", deltas)
    monkeypatch.setattr(generate_types, "SCHEMAS_DIR", repository / "schemas")
    monkeypatch.setattr(generate_types.diff_generated_types, "snapshot", lambda _path: {})
    monkeypatch.setattr(generate_types, "flatten_schemas", lambda path: path)

    def fake_generate(_schemas: Path, destination: Path) -> bool:
        destination.mkdir()
        (destination / "sentinel.py").write_text("new generated tree\n")
        return True

    monkeypatch.setattr(generate_types, "generate_types", fake_generate)
    monkeypatch.setattr(generate_types, "generate_root_discovery_types", lambda *_args: True)
    monkeypatch.setattr(generate_types, "fix_forward_references", lambda *_args: None)
    monkeypatch.setattr(generate_types, "apply_post_generation_fixes", lambda *_args: True)
    monkeypatch.setattr(generate_types, "prune_unused_bundled_modules", lambda *_args: None)
    monkeypatch.setattr(generate_types, "restore_unchanged_files", lambda *_args: None)

    def fake_staged_source(staging_root: Path) -> Path:
        # Mirrors the real helper: the package copy, with the committed
        # generated tree dropped so generation creates it. The tree lives
        # INSIDE this copy now, which is what makes it one artifact.
        source = staging_root / "source"
        types = source / "adcp" / "types"
        types.mkdir(parents=True)
        (types / "_generated.py").write_text("stale consolidated exports\n")
        (types / "_ergonomic.py").write_text("stale ergonomic module\n")
        return source

    monkeypatch.setattr(generate_types, "_staged_source_tree", fake_staged_source)

    def fake_subprocess(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "consolidate_exports.py" in args[1]:
            destination = Path(args[args.index("--output-file") + 1])
            destination.write_text("new consolidated exports\n")
            return subprocess.CompletedProcess(args, 0, "", "")
        if fail_ergonomic:
            return subprocess.CompletedProcess(args, 1, "", "late ergonomic failure")
        destination = Path(args[args.index("--output-file") + 1])
        destination.write_text("new ergonomic module\n")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(generate_types.subprocess, "run", fake_subprocess)
    return output, consolidated, ergonomic, deltas


def test_late_generation_failure_leaves_checkout_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, consolidated, ergonomic, deltas = _configure_fake_pipeline(
        monkeypatch, tmp_path, fail_ergonomic=True
    )

    assert generate_types.main([]) == 1

    assert (output / "sentinel.py").read_text() == "old generated tree\n"
    assert consolidated.read_text() == "old consolidated exports\n"
    assert ergonomic.read_text() == "old ergonomic module\n"
    assert deltas.read_text() == "old delta report\n"


def test_check_mode_reports_drift_without_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output, consolidated, ergonomic, deltas = _configure_fake_pipeline(
        monkeypatch, tmp_path, fail_ergonomic=False
    )

    assert generate_types.main(["--check"]) == 1

    assert (output / "sentinel.py").read_text() == "old generated tree\n"
    assert consolidated.read_text() == "old consolidated exports\n"
    assert ergonomic.read_text() == "old ergonomic module\n"
    assert deltas.read_text() == "old delta report\n"


def test_artifact_install_rolls_back_partial_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    candidate_tree = staging / "candidate-tree"
    candidate_tree.mkdir()
    (candidate_tree / "value").write_text("new tree")
    candidate_file = staging / "candidate-file"
    candidate_file.write_text("new file")

    target_tree = tmp_path / "target-tree"
    target_tree.mkdir()
    (target_tree / "value").write_text("old tree")
    target_file = tmp_path / "target-file"
    target_file.write_text("old file")

    real_replace: Callable[[Path, Path], None] = os.replace

    def fail_second_install(source: Path, destination: Path) -> None:
        if Path(source) == candidate_file and Path(destination) == target_file:
            raise OSError("injected replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(generate_types.os, "replace", fail_second_install)

    with pytest.raises(OSError, match="injected replacement failure"):
        generate_types._install_generated_artifacts(
            staging,
            [(candidate_tree, target_tree), (candidate_file, target_file)],
        )

    assert (target_tree / "value").read_text() == "old tree"
    assert target_file.read_text() == "old file"
    assert (candidate_tree / "value").read_text() == "new tree"
    assert candidate_file.read_text() == "new file"


def _write_schema_tree(root: Path, schemas: dict[str, dict]) -> None:
    for rel_path, schema in schemas.items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(schema))


def _envelope_composition_tree() -> dict[str, dict]:
    """The adcp#8547 response layout, reduced to the shapes codegen has to handle."""
    url = "https://adcontextprotocol.org/schemas/3.3.0-beta.3/"
    envelope = {"$ref": f"{url}core/protocol-envelope.json"}
    submitted = {"$ref": f"{url}core/task-submitted.json"}
    return {
        "core/version-envelope.json": {
            "title": "AdCP Version Envelope",
            "type": "object",
            "properties": {"adcp_version": {"type": "string"}},
        },
        "enums/task-status.json": {
            "title": "Task Status",
            "type": "string",
            "enum": ["completed", "submitted"],
        },
        "core/context.json": {"title": "Context Object", "type": "object"},
        "core/protocol-envelope.json": {
            "title": "Protocol Envelope",
            "type": "object",
            "allOf": [{"$ref": f"{url}core/version-envelope.json"}],
            "properties": {
                "status": {"$ref": f"{url}enums/task-status.json"},
                "context": {"$ref": f"{url}core/context.json"},
                "task_id": {"type": "string"},
            },
            "required": ["status"],
        },
        "core/task-submitted.json": {
            "title": "Task Submitted",
            "type": "object",
            "properties": {
                "status": {"const": "submitted"},
                "task_id": {"type": "string", "minLength": 1},
            },
            "required": ["status", "task_id"],
        },
        # One directory deep, with the submitted arm as a oneOf branch.
        "media-buy/thing-response.json": {
            "title": "Thing Response",
            "type": "object",
            "allOf": [envelope],
            "oneOf": [
                {"type": "object", "properties": {"thing_id": {"type": "string"}}},
                {"type": "object", "allOf": [submitted], "properties": {"eta": {"type": "string"}}},
            ],
        },
        # Arms that disagree but compose nothing of their own: the generator's
        # flat merge loses nothing, and the class keeps that shape.
        "core/listing.json": {
            "title": "Listing",
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Listing name."}},
        },
        "core/enrichment.json": {
            "title": "Enrichment",
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Enriched name."},
                "score": {"type": "number"},
            },
        },
        "media-buy/enriched-listing.json": {
            "title": "Enriched Listing",
            "type": "object",
            "allOf": [
                {"$ref": f"{url}core/listing.json"},
                {"$ref": f"{url}core/enrichment.json"},
            ],
        },
        # Three directories deep, composing both at the root.
        "core/async-response-refs/media-buy/thing-async-response-submitted.json": {
            "title": "Thing Submitted",
            "type": "object",
            "allOf": [envelope, submitted],
            "properties": {"eta": {"type": "string"}},
        },
    }


def test_envelope_composition_generates_from_every_depth_with_inheritance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """adcp#8547's composed responses generate, and keep the envelope ancestry.

    Two generator behaviours meet here. Refs inside an ``allOf`` target are
    resolved against the caller's directory, so the envelope's own refs must
    name their files absolutely: from ``core/async-response-refs/<domain>/``
    the relative spelling failed with ``$ref file not found``. And an
    ``allOf`` whose arms disagree on a property (``task-submitted``'s
    ``status`` const against the envelope's enum) was merged into a flat
    class that inherited neither schema and dropped the version fields the
    envelope composes. An ``allOf`` whose arms disagree but compose nothing of
    their own keeps the flat merge it always had.
    """
    cache = tmp_path / "cache"
    _write_schema_tree(cache, _envelope_composition_tree())
    monkeypatch.setattr(generate_types, "SCHEMAS_DIR", cache)
    monkeypatch.setattr(generate_types, "TEMP_DIR", generate_types.TEMP_DIR)
    monkeypatch.setattr(generate_types, "GENERATED_SCHEMA_EXCLUDE_FILES", frozenset())
    monkeypatch.setattr(generate_types, "GENERATED_SCHEMA_EXCLUDE_DIRS", frozenset())

    prepared = generate_types.flatten_schemas(tmp_path / "prepared")
    package = tmp_path / "generated_envelope_tree"
    assert generate_types.generate_types(prepared, package)

    monkeypatch.syspath_prepend(str(tmp_path))
    envelope = importlib.import_module(f"{package.name}.core.protocol_envelope").ProtocolEnvelope
    task_submitted = importlib.import_module(f"{package.name}.core.task_submitted").TaskSubmitted
    deep = importlib.import_module(
        f"{package.name}.core.async_response_refs.media_buy.thing_async_response_submitted"
    ).ThingSubmitted
    response = importlib.import_module(f"{package.name}.media_buy.thing_response")
    branch = next(
        cls
        for cls in vars(response).values()
        if isinstance(cls, type)
        and cls.__module__ == response.__name__
        and "eta" in getattr(cls, "model_fields", {})
    )
    for cls in (deep, branch):
        assert issubclass(cls, envelope) and issubclass(cls, task_submitted), cls.__mro__
        fields = cls.model_fields
        assert fields["status"].annotation == Literal["submitted"]
        assert fields["status"].is_required()
        assert fields["task_id"].is_required()
        assert "adcp_version" in fields and "context" in fields

    # The merge names the class after the last arm's title, as the generator's does.
    flat_module = importlib.import_module(f"{package.name}.media_buy.enriched_listing")
    (flat,) = (
        cls
        for cls in vars(flat_module).values()
        if isinstance(cls, type) and cls.__module__ == flat_module.__name__
    )
    listing = importlib.import_module(f"{package.name}.core.listing").Listing
    enrichment = importlib.import_module(f"{package.name}.core.enrichment").Enrichment
    assert not issubclass(flat, (listing, enrichment)), flat.__mro__
    assert {"name", "score"} <= set(flat.model_fields)


def test_scalar_bound_beside_a_shared_definition_survives_generation(tmp_path: Path) -> None:
    """A property that tightens a shared scalar keeps its own bound.

    ``core/task-id.json`` is the one definition of ``task_id``; the compliance controller
    adds ``maxLength: 128`` beside the reference. The generator drops a sibling constraint
    of a lone ``allOf`` reference, so the merge happens before it runs.
    """
    original = generate_types.SCHEMAS_DIR
    url = "https://adcontextprotocol.org/schemas/3.3.0-beta.3/"
    try:
        generate_types.SCHEMAS_DIR = tmp_path
        _write_schema_tree(
            tmp_path,
            {
                "core/task-id.json": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "title": "Task ID",
                    "type": "string",
                    "minLength": 1,
                },
                "core/closed.json": {"type": "string", "enum": ["a"]},
            },
        )
        bounded = {
            "properties": {
                "task_id": {
                    "description": "controller handle",
                    "allOf": [{"$ref": f"{url}core/task-id.json"}],
                    "maxLength": 128,
                },
                # No sibling bound: left as the reference.
                "plain": {"allOf": [{"$ref": f"{url}core/task-id.json"}]},
                # Not a plain scalar: left as the reference.
                "closed": {"allOf": [{"$ref": f"{url}core/closed.json"}], "maxLength": 1},
            }
        }
        merged = generate_types.merge_constrained_scalar_refs(
            bounded, Path("compliance/controller.json")
        )
    finally:
        generate_types.SCHEMAS_DIR = original

    task_id = merged["properties"]["task_id"]
    assert task_id["type"] == "string"
    assert (task_id["minLength"], task_id["maxLength"]) == (1, 128)
    assert task_id["description"] == "controller handle"
    assert "allOf" not in task_id and "title" not in task_id and "$schema" not in task_id
    assert "allOf" in merged["properties"]["plain"]
    assert "allOf" in merged["properties"]["closed"]
