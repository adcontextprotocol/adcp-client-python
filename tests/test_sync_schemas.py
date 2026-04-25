"""Tests for scripts/sync_schemas.py — skills sync functions."""

from __future__ import annotations

# Import the functions under test directly from the script module.
# sync_schemas.py lives under scripts/, which is not on sys.path by default,
# so we load it via importlib.
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "sync_schemas.py"
_spec = importlib.util.spec_from_file_location("sync_schemas", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["sync_schemas"] = _mod
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

replace_cache_from_bundle = _mod.replace_cache_from_bundle
sync_skills_from_bundle = _mod.sync_skills_from_bundle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bundle(
    tmp: Path,
    skills: dict[str, dict[str, str]] | None = None,
    manifest_skills: list[object] | None = None,
    include_manifest: bool = True,
    schemas_in_bundle: bool = False,
) -> Path:
    """Build a fake bundle_root directory for tests.

    Args:
        tmp: Parent temp directory.
        skills: Mapping of skill_name → {filename: content} for files to create.
        manifest_skills: List value for manifest["contents"]["skills"]. Defaults
            to the keys of *skills* when provided.
        include_manifest: Whether to write manifest.json at all.
        schemas_in_bundle: Whether to add a schemas/ subdir inside each skill.
    """
    bundle_root = tmp / "adcp-test"
    bundle_root.mkdir(exist_ok=True)

    if skills:
        skills_root = bundle_root / "skills"
        skills_root.mkdir(exist_ok=True)
        for skill_name, files in skills.items():
            skill_dir = skills_root / skill_name
            skill_dir.mkdir()
            for fname, content in files.items():
                (skill_dir / fname).write_text(content)
            if schemas_in_bundle:
                (skill_dir / "schemas").mkdir()
                (skill_dir / "schemas" / "schema.json").write_text("{}")

    if include_manifest:
        names: list[object]
        if manifest_skills is not None:
            names = manifest_skills
        elif skills:
            names = list(skills.keys())
        else:
            names = []
        manifest = {"contents": {"skills": names}}
        (bundle_root / "manifest.json").write_text(json.dumps(manifest))

    return bundle_root


# ---------------------------------------------------------------------------
# replace_cache_from_bundle
# ---------------------------------------------------------------------------


class TestReplaceCacheFromBundle:
    def test_copies_schemas_to_cache_dir(self, tmp_path: Path) -> None:
        bundle_root = tmp_path / "adcp-test"
        schemas_src = bundle_root / "schemas"
        schemas_src.mkdir(parents=True)
        (schemas_src / "request.json").write_text('{"type":"object"}')

        cache_dir = tmp_path / "cache"
        # Monkeypatch CACHE_DIR for this test
        original = _mod.CACHE_DIR
        _mod.CACHE_DIR = cache_dir
        try:
            count = replace_cache_from_bundle(bundle_root)
        finally:
            _mod.CACHE_DIR = original

        assert count == 1
        assert (cache_dir / "request.json").read_text() == '{"type":"object"}'

    def test_raises_if_schemas_dir_missing(self, tmp_path: Path) -> None:
        bundle_root = tmp_path / "adcp-test"
        bundle_root.mkdir()

        with pytest.raises(RuntimeError, match="Bundle missing expected directory"):
            replace_cache_from_bundle(bundle_root)


# ---------------------------------------------------------------------------
# sync_skills_from_bundle
# ---------------------------------------------------------------------------


class TestSyncSkillsFromBundle:
    def test_no_manifest_returns_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bundle_root = _make_bundle(tmp, include_manifest=False)
            skills_dir = tmp / "skills"

            result = sync_skills_from_bundle(bundle_root, skills_dir)

            assert result == 0
        assert "No manifest.json" in capsys.readouterr().out

    def test_empty_skills_list_returns_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bundle_root = _make_bundle(tmp, manifest_skills=[])
            skills_dir = tmp / "skills"

            result = sync_skills_from_bundle(bundle_root, skills_dir)

            assert result == 0
        assert "No skills listed" in capsys.readouterr().out

    def test_skill_copied_to_skills_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bundle_root = _make_bundle(
                tmp, skills={"call-adcp-agent": {"SKILL.md": "# Call AdCP Agent"}}
            )
            skills_dir = tmp / "skills"

            count = sync_skills_from_bundle(bundle_root, skills_dir)

            assert count == 1
            assert (skills_dir / "call-adcp-agent" / "SKILL.md").read_text() == (
                "# Call AdCP Agent"
            )

    def test_schemas_subdir_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bundle_root = _make_bundle(
                tmp,
                skills={"adcp-brand": {"SKILL.md": "# Brand"}},
                schemas_in_bundle=True,
            )
            skills_dir = tmp / "skills"

            sync_skills_from_bundle(bundle_root, skills_dir)

            # SKILL.md should be copied; schemas/ subdir must be excluded
            assert (skills_dir / "adcp-brand" / "SKILL.md").exists()
            assert not (skills_dir / "adcp-brand" / "schemas").exists()

    def test_previous_snapshot_created_on_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            skills_dir = tmp / "skills"
            skills_dir.mkdir()

            # Pre-populate an existing skill
            existing = skills_dir / "adcp-brand"
            existing.mkdir()
            (existing / "SKILL.md").write_text("# Old Brand")

            bundle_root = _make_bundle(
                tmp, skills={"adcp-brand": {"SKILL.md": "# New Brand"}}
            )
            sync_skills_from_bundle(bundle_root, skills_dir)

            assert (skills_dir / "adcp-brand" / "SKILL.md").read_text() == "# New Brand"
            assert (skills_dir / "adcp-brand.previous" / "SKILL.md").read_text() == (
                "# Old Brand"
            )

    def test_previous_snapshot_replaced_on_second_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            skills_dir = tmp / "skills"
            skills_dir.mkdir()

            # Set up an existing .previous dir from a prior run
            prev = skills_dir / "adcp-brand.previous"
            prev.mkdir()
            (prev / "SKILL.md").write_text("# Very Old Brand")

            existing = skills_dir / "adcp-brand"
            existing.mkdir()
            (existing / "SKILL.md").write_text("# Old Brand")

            bundle_root = _make_bundle(
                tmp, skills={"adcp-brand": {"SKILL.md": "# New Brand"}}
            )
            sync_skills_from_bundle(bundle_root, skills_dir)

            assert (skills_dir / "adcp-brand.previous" / "SKILL.md").read_text() == (
                "# Old Brand"
            )

    def test_local_only_skill_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            skills_dir = tmp / "skills"
            skills_dir.mkdir()

            # A locally-managed skill not in the manifest
            local = skills_dir / "build-seller-agent"
            local.mkdir()
            (local / "SKILL.md").write_text("# Seller Agent (SDK-local)")

            bundle_root = _make_bundle(
                tmp,
                skills={"call-adcp-agent": {"SKILL.md": "# Call"}},
                manifest_skills=["call-adcp-agent"],
            )
            sync_skills_from_bundle(bundle_root, skills_dir)

            assert (skills_dir / "build-seller-agent" / "SKILL.md").read_text() == (
                "# Seller Agent (SDK-local)"
            )

    def test_path_traversal_dotdot_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bundle_root = _make_bundle(
                tmp,
                skills={},
                manifest_skills=["../evil"],
                include_manifest=True,
            )
            skills_dir = tmp / "skills"

            with pytest.raises(RuntimeError, match="Unsafe skill name rejected"):
                sync_skills_from_bundle(bundle_root, skills_dir)

    def test_path_traversal_slash_in_name_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bundle_root = _make_bundle(
                tmp,
                skills={},
                manifest_skills=["good/../evil"],
                include_manifest=True,
            )
            skills_dir = tmp / "skills"

            with pytest.raises(RuntimeError, match="Unsafe skill name rejected"):
                sync_skills_from_bundle(bundle_root, skills_dir)

    def test_non_string_name_skipped(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bundle_root = _make_bundle(
                tmp,
                skills={"call-adcp-agent": {"SKILL.md": "# Call"}},
                manifest_skills=[42, "call-adcp-agent"],
            )
            skills_dir = tmp / "skills"

            count = sync_skills_from_bundle(bundle_root, skills_dir)

            assert count == 1  # only the valid string entry is synced
        assert "Skipping non-string" in capsys.readouterr().out

    def test_missing_bundle_skill_dir_skipped(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            # Manifest lists a skill that has no corresponding directory in the bundle
            bundle_root = _make_bundle(
                tmp,
                skills={},
                manifest_skills=["adcp-brand"],
                include_manifest=True,
            )
            skills_dir = tmp / "skills"

            count = sync_skills_from_bundle(bundle_root, skills_dir)

            assert count == 0
        assert "missing in bundle" in capsys.readouterr().out

    def test_missing_bundle_skill_preserves_existing_dst(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            skills_dir = tmp / "skills"
            skills_dir.mkdir()

            # Pre-existing skill in dst
            existing = skills_dir / "adcp-brand"
            existing.mkdir()
            (existing / "SKILL.md").write_text("# Existing Brand")

            # Manifest lists the skill but bundle has no source dir for it
            bundle_root = _make_bundle(
                tmp,
                skills={},
                manifest_skills=["adcp-brand"],
                include_manifest=True,
            )
            sync_skills_from_bundle(bundle_root, skills_dir)

            # dst must not be touched when src is absent
            assert (skills_dir / "adcp-brand" / "SKILL.md").read_text() == (
                "# Existing Brand"
            )
        assert "missing in bundle" in capsys.readouterr().out

    def test_multiple_skills_synced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bundle_root = _make_bundle(
                tmp,
                skills={
                    "adcp-brand": {"SKILL.md": "# Brand"},
                    "adcp-creative": {"SKILL.md": "# Creative"},
                    "call-adcp-agent": {"SKILL.md": "# Agent"},
                },
            )
            skills_dir = tmp / "skills"

            count = sync_skills_from_bundle(bundle_root, skills_dir)

            assert count == 3
            assert (skills_dir / "adcp-brand" / "SKILL.md").exists()
            assert (skills_dir / "adcp-creative" / "SKILL.md").exists()
            assert (skills_dir / "call-adcp-agent" / "SKILL.md").exists()
