"""Failure and check-mode safety for the type-generation pipeline."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts import generate_types


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
    output = repository / "src" / "adcp" / "types" / "generated_poc"
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

    def fake_package(staging_root: Path, _generated: Path) -> Path:
        source = staging_root / "source"
        types = source / "adcp" / "types"
        types.mkdir(parents=True)
        (types / "_generated.py").write_text("stale consolidated exports\n")
        (types / "_ergonomic.py").write_text("stale ergonomic module\n")
        return source

    monkeypatch.setattr(generate_types, "_copy_package_for_introspection", fake_package)

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
