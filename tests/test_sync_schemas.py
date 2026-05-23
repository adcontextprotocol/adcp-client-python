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
            count = replace_cache_from_bundle(bundle_root, "3.0")
        finally:
            _mod.CACHE_DIR = original

        assert count == 1
        # Output now lands in the per-bundle-key subdirectory.
        assert (cache_dir / "3.0" / "request.json").read_text() == '{"type":"object"}'

    def test_raises_if_schemas_dir_missing(self, tmp_path: Path) -> None:
        bundle_root = tmp_path / "adcp-test"
        bundle_root.mkdir()

        with pytest.raises(RuntimeError, match="Bundle missing expected directory"):
            replace_cache_from_bundle(bundle_root, "3.0")

    def test_caller_resolves_bundle_key_from_target_not_effective(self) -> None:
        """Latent-bug regression: when the pinned bundle isn't published
        and sync falls back to ``latest.tgz``, ``effective_version`` is
        the literal string ``"latest"``. ``resolve_bundle_key("latest")``
        rejects it. The script must compute the bundle key from
        ``target_version`` (the SDK pin) — which is what the loader
        looks up.

        This is a unit assertion on the helper; the integration check
        (sync_schemas main() uses target_version) is enforced by reading
        the relevant lines below.
        """
        from adcp.validation.version import resolve_bundle_key

        # ``effective_version`` after a 404-fallback:
        with pytest.raises(ValueError, match="not a valid version"):
            resolve_bundle_key("latest")

        # ``target_version`` (the SDK pin) always parses:
        assert resolve_bundle_key("3.0.7") == "3.0"

        # Sanity-check the call site uses target_version, not
        # effective_version. A regression that re-introduces
        # ``resolve_bundle_key(effective_version)`` here breaks fallback.
        src = _SCRIPT.read_text()
        assert "resolve_bundle_key(target_version)" in src, (
            "sync_schemas.py must derive bundle_key from target_version "
            "(the SDK pin), not effective_version (which can be 'latest')."
        )


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

    def test_empty_skills_list_returns_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
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

            bundle_root = _make_bundle(tmp, skills={"adcp-brand": {"SKILL.md": "# New Brand"}})
            sync_skills_from_bundle(bundle_root, skills_dir)

            assert (skills_dir / "adcp-brand" / "SKILL.md").read_text() == "# New Brand"
            assert (skills_dir / "adcp-brand.previous" / "SKILL.md").read_text() == ("# Old Brand")

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

            bundle_root = _make_bundle(tmp, skills={"adcp-brand": {"SKILL.md": "# New Brand"}})
            sync_skills_from_bundle(bundle_root, skills_dir)

            assert (skills_dir / "adcp-brand.previous" / "SKILL.md").read_text() == ("# Old Brand")

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

    def test_non_string_name_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
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

    def test_missing_bundle_skill_dir_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
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
            assert (skills_dir / "adcp-brand" / "SKILL.md").read_text() == ("# Existing Brand")
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


# ---------------------------------------------------------------------------
# BUNDLE_BASE_URL env override (ADCP_BASE_URL)
# ---------------------------------------------------------------------------


class TestBundleBaseUrl:
    def test_default_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Fresh load with env var absent — guards against shell having ADCP_BASE_URL set.
        monkeypatch.delenv("ADCP_BASE_URL", raising=False)
        fresh_spec = importlib.util.spec_from_file_location("sync_schemas_default", _SCRIPT)
        assert fresh_spec is not None and fresh_spec.loader is not None
        fresh_mod = importlib.util.module_from_spec(fresh_spec)
        fresh_spec.loader.exec_module(fresh_mod)  # type: ignore[union-attr]
        assert fresh_mod.BUNDLE_BASE_URL == "https://adcontextprotocol.org/protocol"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Fresh module load with ADCP_BASE_URL set to verify override is applied.
        monkeypatch.setenv("ADCP_BASE_URL", "https://fixture.example.com")
        fresh_spec = importlib.util.spec_from_file_location("sync_schemas_fresh", _SCRIPT)
        assert fresh_spec is not None and fresh_spec.loader is not None
        fresh_mod = importlib.util.module_from_spec(fresh_spec)
        fresh_spec.loader.exec_module(fresh_mod)  # type: ignore[union-attr]
        assert fresh_mod.BUNDLE_BASE_URL == "https://fixture.example.com/protocol"

    def test_env_override_strips_trailing_slash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Trailing slash on ADCP_BASE_URL must not produce "//protocol".
        monkeypatch.setenv("ADCP_BASE_URL", "https://fixture.example.com/")
        fresh_spec = importlib.util.spec_from_file_location("sync_schemas_fresh2", _SCRIPT)
        assert fresh_spec is not None and fresh_spec.loader is not None
        fresh_mod = importlib.util.module_from_spec(fresh_spec)
        fresh_spec.loader.exec_module(fresh_mod)  # type: ignore[union-attr]
        assert fresh_mod.BUNDLE_BASE_URL == "https://fixture.example.com/protocol"
        assert "//protocol" not in fresh_mod.BUNDLE_BASE_URL

    def test_env_override_rejects_protocol_suffix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Override ending in /protocol would double-append. Fail loud at
        # import rather than silently 404-ing later.
        monkeypatch.setenv("ADCP_BASE_URL", "https://fixture.example.com/protocol")
        fresh_spec = importlib.util.spec_from_file_location("sync_schemas_protocol_suffix", _SCRIPT)
        assert fresh_spec is not None and fresh_spec.loader is not None
        fresh_mod = importlib.util.module_from_spec(fresh_spec)
        with pytest.raises(ValueError, match="ends with '/protocol'"):
            fresh_spec.loader.exec_module(fresh_mod)  # type: ignore[union-attr]

    def test_env_override_rejects_protocol_suffix_with_trailing_slash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same guard, but with a trailing slash on the override — rstrip
        # runs first, so the /protocol still trips the check.
        monkeypatch.setenv("ADCP_BASE_URL", "https://fixture.example.com/protocol/")
        fresh_spec = importlib.util.spec_from_file_location(
            "sync_schemas_protocol_trailing", _SCRIPT
        )
        assert fresh_spec is not None and fresh_spec.loader is not None
        fresh_mod = importlib.util.module_from_spec(fresh_spec)
        with pytest.raises(ValueError, match="ends with '/protocol'"):
            fresh_spec.loader.exec_module(fresh_mod)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Patches/ post-process — alive / dead / broken classification + apply
# ---------------------------------------------------------------------------


def _write_patch(patches_dir: Path, name: str, body: str) -> Path:
    """Helper: write a unified-diff patch file with a comment header."""
    path = patches_dir / name
    path.write_text(body, encoding="utf-8")
    return path


class TestApplyTrackedPatches:
    """Patch state machine — verifies the alive/dead/broken classifier
    fails loudly on dead and broken patches, and applies alive ones
    cleanly. The cache directory is monkey-patched per test so we never
    touch the real schemas/cache/."""

    def _wire_isolated_dirs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> tuple[Path, Path]:
        """Point CACHE_DIR, PATCHES_DIR, and REPO_ROOT at a temp workspace.

        ``apply_tracked_patches`` shells out to ``patch -p1`` from
        ``REPO_ROOT`` and reads from ``PATCHES_DIR``; redirecting all
        three keeps the test hermetic.
        """
        repo = tmp_path / "repo"
        cache = repo / "schemas" / "cache"
        patches = repo / "schemas" / "patches"
        cache.mkdir(parents=True)
        patches.mkdir(parents=True)
        monkeypatch.setattr(_mod, "REPO_ROOT", repo)
        monkeypatch.setattr(_mod, "CACHE_DIR", cache)
        monkeypatch.setattr(_mod, "PATCHES_DIR", patches)
        return cache, patches

    def test_empty_patches_dir_is_noop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Initial state immediately after the infrastructure lands —
        # patches/ exists with only a README, no .patch files. Must
        # return 0 cleanly so the sync script doesn't fail-loud on the
        # back-compat path.
        _cache, patches = self._wire_isolated_dirs(monkeypatch, tmp_path)
        # README.md is fine; only .patch files are picked up.
        (patches / "README.md").write_text("# notes\n", encoding="utf-8")
        assert _mod.apply_tracked_patches() == 0

    def test_missing_patches_dir_is_noop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # patches/ doesn't exist at all (fresh checkout before the
        # directory is created). Same back-compat path — return 0.
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(_mod, "REPO_ROOT", repo)
        monkeypatch.setattr(_mod, "PATCHES_DIR", repo / "schemas" / "patches")
        assert _mod.apply_tracked_patches() == 0

    def test_alive_patch_applies_cleanly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Build a real target file and a unified diff that adds a field
        # to it. The classifier should report "alive" and the apply
        # step should write the new bytes.
        cache, patches = self._wire_isolated_dirs(monkeypatch, tmp_path)
        target_rel = "schemas/cache/3.0/test.json"
        target = cache / "3.0" / "test.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"a": 1}\n', encoding="utf-8")

        _write_patch(
            patches,
            "01-add-b.patch",
            f"""# Patch: add field b
# Reason: test fixture
# Drop when: never (test-only)
--- a/{target_rel}
+++ b/{target_rel}
@@ -1 +1 @@
-{{"a": 1}}
+{{"a": 1, "b": 2}}
""",
        )

        assert _mod.apply_tracked_patches() == 1
        assert '"b": 2' in target.read_text(encoding="utf-8")

    def test_dead_patch_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Simulate upstream landing the patch: the target file already
        # has the patched shape. Forward-apply fails; reverse-apply
        # succeeds → classified as "dead". The script must exit
        # non-zero and the stderr must name the patch so the operator
        # knows which file to delete.
        cache, patches = self._wire_isolated_dirs(monkeypatch, tmp_path)
        target_rel = "schemas/cache/3.0/test.json"
        target = cache / "3.0" / "test.json"
        target.parent.mkdir(parents=True)
        # File is already in the post-patch state.
        target.write_text('{"a": 1, "b": 2}\n', encoding="utf-8")

        _write_patch(
            patches,
            "01-add-b.patch",
            f"""# Patch: add field b
# Reason: test fixture
# Drop when: never (test-only)
--- a/{target_rel}
+++ b/{target_rel}
@@ -1 +1 @@
-{{"a": 1}}
+{{"a": 1, "b": 2}}
""",
        )

        with pytest.raises(SystemExit) as exc_info:
            _mod.apply_tracked_patches()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "DEAD" in captured.err
        assert "01-add-b.patch" in captured.err

    def test_broken_patch_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Upstream restructured the file so neither forward- nor
        # reverse-apply works. Must exit non-zero with the patch name
        # surfaced so the operator can update or remove it.
        cache, patches = self._wire_isolated_dirs(monkeypatch, tmp_path)
        target_rel = "schemas/cache/3.0/test.json"
        target = cache / "3.0" / "test.json"
        target.parent.mkdir(parents=True)
        # File contents differ from both the patch's pre- and post-state.
        target.write_text('{"completely": "different"}\n', encoding="utf-8")

        _write_patch(
            patches,
            "01-add-b.patch",
            f"""# Patch: add field b
# Reason: test fixture
# Drop when: never (test-only)
--- a/{target_rel}
+++ b/{target_rel}
@@ -1 +1 @@
-{{"a": 1}}
+{{"a": 1, "b": 2}}
""",
        )

        with pytest.raises(SystemExit) as exc_info:
            _mod.apply_tracked_patches()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "BROKEN" in captured.err
        assert "01-add-b.patch" in captured.err

    def test_patches_apply_in_lex_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Two patches against the same file; the second one depends on
        # the line layout the first creates. Iff applied in lex order,
        # both succeed. Pins the ordering convention so a future
        # refactor (e.g. os.listdir order) can't break it.
        cache, patches = self._wire_isolated_dirs(monkeypatch, tmp_path)
        target_rel = "schemas/cache/3.0/test.json"
        target = cache / "3.0" / "test.json"
        target.parent.mkdir(parents=True)
        target.write_text("a\n", encoding="utf-8")

        _write_patch(
            patches,
            "01-first.patch",
            f"""# Patch: first
# Reason: test fixture
# Drop when: never
--- a/{target_rel}
+++ b/{target_rel}
@@ -1 +1,2 @@
 a
+b
""",
        )
        _write_patch(
            patches,
            "02-second.patch",
            f"""# Patch: second (depends on 01-first having added 'b')
# Reason: test fixture
# Drop when: never
--- a/{target_rel}
+++ b/{target_rel}
@@ -1,2 +1,3 @@
 a
 b
+c
""",
        )

        assert _mod.apply_tracked_patches() == 2
        assert target.read_text(encoding="utf-8") == "a\nb\nc\n"
