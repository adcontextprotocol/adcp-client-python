"""Tests for ``python -m adcp.migrate v3-to-v4``.

The migration is one-shot tooling — once a codebase runs through it,
the output is reviewed in ``git diff`` and tests never run against
migrated code again. But the migration itself is code that rewrites
other people's code: bugs here corrupt their source tree. These tests
pin the behaviour tightly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adcp.migrate import v3_to_v4

# ---------------------------------------------------------------------------
# Dry-run scans — file contents unchanged, report lists findings
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def test_renames_all_nine_asset_content_types(tmp_path: Path) -> None:
    """All 9 ``<Type>Asset`` → ``<Type>Content`` names are detected."""
    source = "\n".join(
        [
            "from adcp.types import (",
            "    AudioAsset, CssAsset, HtmlAsset, ImageAsset, JavascriptAsset,",
            "    TextAsset, UrlAsset, VideoAsset, WebhookAsset,",
            ")",
            "x = AudioAsset(duration_seconds=30)",
        ]
    )
    _write(tmp_path, "user_code.py", source)

    report = v3_to_v4.run(tmp_path, apply_changes=False)

    assert report.scanned_files == 1
    # 9 import-line hits + 1 call-site hit for AudioAsset = 10 applied findings
    applied_names = {f.before for f in report.applied}
    assert applied_names == set(v3_to_v4.ASSET_CONTENT_RENAMES.keys())
    # Every applied rename carries the target name.
    for f in report.applied:
        assert f.after == v3_to_v4.ASSET_CONTENT_RENAMES[f.before]


def test_apply_rewrites_files_in_place(tmp_path: Path) -> None:
    """With ``--apply`` the file contents are rewritten."""
    path = _write(
        tmp_path,
        "code.py",
        "from adcp.types import AudioAsset, VideoAsset\n"
        "audio = AudioAsset(duration_seconds=30)\n"
        "video = VideoAsset(width=1920, height=1080)\n",
    )

    report = v3_to_v4.run(tmp_path, apply_changes=True)

    assert report.rewritten_files == 1
    rewritten = path.read_text()
    assert "AudioAsset" not in rewritten
    assert "VideoAsset" not in rewritten
    assert "AudioContent" in rewritten
    assert "VideoContent" in rewritten


def test_dry_run_does_not_modify_files(tmp_path: Path) -> None:
    """Default mode (no ``--apply``) leaves files untouched."""
    original = "from adcp.types import AudioAsset\n" "audio = AudioAsset(duration_seconds=30)\n"
    path = _write(tmp_path, "code.py", original)

    v3_to_v4.run(tmp_path, apply_changes=False)

    assert path.read_text() == original


def test_word_boundary_protects_against_partial_matches(tmp_path: Path) -> None:
    """``MyAudioAsset`` or ``AudioAssetExtra`` must NOT be rewritten —
    they're the seller's own types and coincidentally contain the
    renamed substring."""
    source = (
        "class MyAudioAsset: pass\n"
        "class AudioAssetExtra: pass\n"
        "class AudioAsset_Custom: pass\n"
        "from adcp.types import AudioAsset\n"
    )
    path = _write(tmp_path, "code.py", source)

    v3_to_v4.run(tmp_path, apply_changes=True)

    rewritten = path.read_text()
    assert "class MyAudioAsset: pass" in rewritten
    assert "class AudioAssetExtra: pass" in rewritten
    assert "class AudioAsset_Custom: pass" in rewritten
    # Only the bare import rewrote.
    assert "from adcp.types import AudioContent" in rewritten


# ---------------------------------------------------------------------------
# Flagged findings — reported with migration-guide anchor, not rewritten
# ---------------------------------------------------------------------------


def test_flags_removed_types_with_migration_anchor(tmp_path: Path) -> None:
    """Removed types (BrandManifest, DeliverTo, Pricing, etc.) are
    flagged — NOT rewritten, since replacement depends on context."""
    source = (
        "from adcp import BrandManifest, DeliverTo, Pricing\n"
        "manifest = BrandManifest(name='x')\n"
    )
    _write(tmp_path, "code.py", source)

    report = v3_to_v4.run(tmp_path, apply_changes=True)

    # Source is unchanged — removed types aren't auto-rewritten.
    rewritten = (tmp_path / "code.py").read_text()
    assert "BrandManifest" in rewritten
    assert "DeliverTo" in rewritten

    # Every flagged finding carries the migration anchor + hint.
    by_name = {f.before: f for f in report.flagged if f.kind == "flag_removed"}
    assert "BrandManifest" in by_name
    assert by_name["BrandManifest"].hint is not None
    assert "BrandReference" in by_name["BrandManifest"].hint
    assert by_name["BrandManifest"].migration_anchor == "brandmanifest--brandreference"


def test_flags_numbered_assets_imports(tmp_path: Path) -> None:
    """Direct ``Assets81`` imports are unstable across spec revisions —
    flag, don't rewrite (the semantic alias depends on what the caller
    was doing with the numbered class)."""
    _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.bundled.x import Assets81, Assets149\n",
    )

    report = v3_to_v4.run(tmp_path, apply_changes=False)

    numbered = [f for f in report.flagged if f.kind == "flag_numbered"]
    names = {f.before for f in numbered}
    assert names == {"Assets81", "Assets149"}


def test_bare_assets_is_not_flagged_as_numbered(tmp_path: Path) -> None:
    """``Assets`` (no digits) is the legitimate base alias. The numbered
    flag must not fire on it."""
    _write(tmp_path, "code.py", "from adcp.types import Assets\nx = Assets\n")

    report = v3_to_v4.run(tmp_path, apply_changes=False)

    numbered = [f for f in report.flagged if f.kind == "flag_numbered"]
    assert numbered == []


def test_flags_generated_poc_imports_unknown_symbol_falls_back_to_generic_hint(
    tmp_path: Path,
) -> None:
    """A ``generated_poc`` import for a symbol not in the per-symbol map
    falls back to the generic 'private module' flag — still surfaces the
    issue, adopter does the lookup."""
    _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.something import Unknown\n",
    )

    report = v3_to_v4.run(tmp_path, apply_changes=False)

    private = [f for f in report.flagged if f.kind == "flag_private"]
    assert len(private) == 1
    assert private[0].before == "adcp.types.generated_poc"


def test_flags_generated_poc_imports_per_symbol_mapping(tmp_path: Path) -> None:
    """Round-5 adopter feedback (salesagent v3→v4 experiment): the
    ``generated_poc`` flag-only output forced 82 of 156 findings into
    hand-grep territory. Each known reach-in now emits an explicit
    "Symbol → adcp.types.Symbol" replacement so adopters apply the fix
    without leaving the codemod report."""
    _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.context import ContextObject\n"
        "from adcp.types.generated_poc.core.brand_ref import BrandReference\n"
        "from adcp.types.generated_poc.enums.media_buy_status import MediaBuyStatus\n"
        "from adcp.types.generated_poc.core.error import Error as AdCPResponseError\n",
    )

    report = v3_to_v4.run(tmp_path, apply_changes=False)

    private = [f for f in report.flagged if f.kind == "flag_private"]
    by_symbol = {f.before: f for f in private}

    # Each known symbol gets a per-symbol replacement, NOT the generic
    # "adcp.types.generated_poc" flag.
    assert by_symbol["ContextObject"].after == "adcp.types.ContextObject"
    assert by_symbol["BrandReference"].after == "adcp.types.BrandReference"
    assert by_symbol["MediaBuyStatus"].after == "adcp.types.MediaBuyStatus"
    # ``import Error as AdCPResponseError`` — codemod keys off the LHS
    # canonical name, ignoring the local alias.
    assert by_symbol["Error"].after == "adcp.types.Error"
    # The generic private-module flag MUST NOT also fire when the
    # per-symbol mapping handled the line — would double-count and
    # confuse the report.
    assert "adcp.types.generated_poc" not in by_symbol


def test_flags_generated_poc_multiple_symbols_one_line(tmp_path: Path) -> None:
    """``from adcp.types.generated_poc.core.x import A, B, C`` emits
    one Finding per symbol so the report surfaces every replacement."""
    _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.x import "
        "BrandReference, ContextObject, MediaBuyStatus\n",
    )

    report = v3_to_v4.run(tmp_path, apply_changes=False)

    private = [f for f in report.flagged if f.kind == "flag_private"]
    by_symbol = {f.before: f.after for f in private}
    assert by_symbol == {
        "BrandReference": "adcp.types.BrandReference",
        "ContextObject": "adcp.types.ContextObject",
        "MediaBuyStatus": "adcp.types.MediaBuyStatus",
    }


def test_generated_poc_symbol_map_covers_publicly_exported_names() -> None:
    """Every entry in ``GENERATED_POC_SYMBOL_MAP`` MUST point at a real
    public-API symbol on ``adcp.types`` — otherwise the hint sends
    adopters to a NameError. Guards against drift between the codemod's
    map and the SDK's __all__."""
    import importlib

    types_module = importlib.import_module("adcp.types")
    for symbol, replacement in v3_to_v4.GENERATED_POC_SYMBOL_MAP.items():
        # Every replacement is exactly ``adcp.types.<symbol>``.
        assert replacement == f"adcp.types.{symbol}", (
            f"GENERATED_POC_SYMBOL_MAP[{symbol!r}] = {replacement!r} but "
            f"the convention is adcp.types.{symbol}"
        )
        assert hasattr(types_module, symbol), (
            f"GENERATED_POC_SYMBOL_MAP claims adcp.types.{symbol} exists "
            "but it's not on the public types module — drop the entry "
            "or add the public alias."
        )


def test_flags_removed_attribute_accesses(tmp_path: Path) -> None:
    """``.brand_manifest`` on ResolvedBrand was removed — flag the
    attribute access with a hint."""
    _write(
        tmp_path,
        "code.py",
        "result = await registry.lookup_brand('x')\n" "manifest = result.brand_manifest\n",
    )

    report = v3_to_v4.run(tmp_path, apply_changes=False)

    attr = [f for f in report.flagged if f.kind == "flag_attribute"]
    assert len(attr) == 1
    assert attr[0].before == ".brand_manifest"


def test_flags_removed_enum_values(tmp_path: Path) -> None:
    """`MediaBuyStatus.pending_activation` references are flagged with
    a hint describing both replacement values and a runtime check."""
    _write(
        tmp_path,
        "code.py",
        "if status == MediaBuyStatus.pending_activation:\n"
        "    handle_pending()\n"
        "# also in a comparison\n"
        "is_pending = mb.status is MediaBuyStatus.pending_activation\n",
    )

    report = v3_to_v4.run(tmp_path, apply_changes=False)

    enum_flags = [f for f in report.flagged if f.kind == "flag_enum_value"]
    assert len(enum_flags) == 2
    for finding in enum_flags:
        assert finding.before == "MediaBuyStatus.pending_activation"
        assert finding.hint is not None
        assert "pending_start" in finding.hint
        assert "pending_creatives" in finding.hint
        assert "valid_actions" in finding.hint


def test_enum_value_word_boundary_no_false_positive(tmp_path: Path) -> None:
    """`MediaBuyStatus.pending_activation_v2` must NOT be flagged —
    the trailing `_v2` is a word character so the word boundary fires
    before the suffix, not after."""
    _write(
        tmp_path,
        "code.py",
        "x = MediaBuyStatus.pending_activation_v2\n"
        "y = MediaBuyStatus.pending_activation_custom()\n",
    )

    report = v3_to_v4.run(tmp_path, apply_changes=False)

    enum_flags = [f for f in report.flagged if f.kind == "flag_enum_value"]
    assert enum_flags == [], f"false-positive on pending_activation_* suffixes: {enum_flags}"


def test_brand_manifest_word_boundary_no_false_positive(tmp_path: Path) -> None:
    """``.brand_manifest_v2`` / ``.brand_manifest_override`` are
    seller-specific extensions that happen to share a prefix. They
    MUST NOT be flagged — the regex requires a trailing word boundary."""
    _write(
        tmp_path,
        "code.py",
        "x = seller.brand_manifest_v2\n"
        "y = obj.brand_manifest_override = True\n"
        "z = other.brand_manifest_custom()\n",
    )

    report = v3_to_v4.run(tmp_path, apply_changes=False)

    flagged = [f for f in report.flagged if f.kind == "flag_attribute"]
    assert flagged == [], f"false-positive on brand_manifest_* suffixes: {flagged}"


# ---------------------------------------------------------------------------
# Skips + file-iteration safety
# ---------------------------------------------------------------------------


def test_skips_common_build_and_dep_dirs(tmp_path: Path) -> None:
    """.venv, .git, node_modules etc. MUST be skipped — scanning
    dependency code would generate thousands of false-positive hits."""
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "bad.py").write_text("from adcp.types import AudioAsset\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "bad.py").write_text("AudioAsset\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "bad.py").write_text("AudioAsset\n")
    (tmp_path / "user.py").write_text("from adcp.types import AudioAsset\n")

    report = v3_to_v4.run(tmp_path, apply_changes=False)

    # Only user.py scanned.
    assert report.scanned_files == 1
    assert {f.path for f in report.applied} == {str(tmp_path / "user.py")}


def test_skip_dirs_are_relative_to_root_not_absolute(tmp_path: Path) -> None:
    """Repos frequently sit under ancestor directories named ``build``,
    ``dist``, etc. (common CI path: ``/home/ci/build/repo/src``). A
    too-eager absolute-path check would skip the whole project. The
    skip list must apply only to components *below* the scan root."""
    # Simulate running against a tree mounted under an ancestor named
    # "build". Create an actual directory on disk to exercise this.
    ancestor_dir = tmp_path / "build" / "myrepo"
    ancestor_dir.mkdir(parents=True)
    user_code = ancestor_dir / "app.py"
    user_code.write_text("from adcp.types import AudioAsset\n")

    # Scan from the repo root (inside the ancestor named "build"). The
    # skip list should NOT match "build" because it is not below the
    # scan root.
    report = v3_to_v4.run(ancestor_dir, apply_changes=False)

    assert report.scanned_files == 1, (
        "Scan was skipped when repo sits under an ancestor named like a "
        "skip-dir (e.g. /home/ci/build/repo). Skip-dirs must be relative "
        "to the scan root, not the absolute path."
    )
    assert len(report.applied) == 1


def test_empty_directory_yields_empty_report(tmp_path: Path) -> None:
    report = v3_to_v4.run(tmp_path, apply_changes=False)
    assert report.scanned_files == 0
    assert report.applied == []
    assert report.flagged == []


def test_non_python_files_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "AudioAsset is mentioned here\n")
    _write(tmp_path, "config.yaml", "key: AudioAsset\n")

    report = v3_to_v4.run(tmp_path, apply_changes=False)

    assert report.scanned_files == 0


def test_single_file_path_scans_one_file(tmp_path: Path) -> None:
    """Running against a single .py file scans just that file."""
    path = _write(tmp_path, "user.py", "from adcp.types import AudioAsset\n")

    report = v3_to_v4.run(path, apply_changes=False)

    assert report.scanned_files == 1
    assert len(report.applied) == 1


def test_crlf_line_endings_preserved(tmp_path: Path) -> None:
    """Windows sellers commit CRLF-terminated Python source. The
    migration MUST preserve CRLF on read+write, otherwise every line
    flips to LF and ``git diff`` is polluted with thousands of
    whitespace-only lines."""
    path = tmp_path / "code.py"
    path.write_bytes(b"from adcp.types import AudioAsset\r\nx = AudioAsset()\r\n")

    v3_to_v4.run(tmp_path, apply_changes=True)

    # Read raw bytes to check line endings preserved.
    rewritten = path.read_bytes()
    assert b"\r\n" in rewritten, f"CRLF line endings lost during rewrite. Got: {rewritten!r}"
    # LF-only mixed in would indicate a split/join bug.
    assert b"\n" not in rewritten.replace(
        b"\r\n", b""
    ), f"Mixed line endings after rewrite: {rewritten!r}"
    assert b"AudioContent" in rewritten


def test_utf8_bom_source_migrates(tmp_path: Path) -> None:
    """UTF-8 BOM is legal at the start of Python source (Windows
    editors sometimes add it). The codemod must read and rewrite it
    correctly rather than silently skipping the file as 'binary'."""
    path = tmp_path / "code.py"
    path.write_bytes(b"\xef\xbb\xbffrom adcp.types import AudioAsset\n")

    report = v3_to_v4.run(tmp_path, apply_changes=True)

    assert report.scanned_files == 1
    assert len(report.applied) == 1
    rewritten = path.read_text(encoding="utf-8-sig")
    assert "AudioContent" in rewritten
    assert "AudioAsset" not in rewritten


def test_multiline_import_rewrites_correctly(tmp_path: Path) -> None:
    """Most real codebases write parenthesised multi-line imports. The
    rewrite MUST handle a name mid-parenthesis."""
    path = _write(
        tmp_path,
        "code.py",
        "from adcp.types import (\n"
        "    AudioAsset,\n"
        "    VideoAsset,\n"
        "    BuyingMode,\n"
        ")\n"
        "x = AudioAsset()\n"
        "y = VideoAsset()\n",
    )

    v3_to_v4.run(tmp_path, apply_changes=True)

    rewritten = path.read_text()
    assert "AudioAsset" not in rewritten
    assert "VideoAsset" not in rewritten
    assert "AudioContent" in rewritten
    assert "VideoContent" in rewritten
    # BuyingMode is unrelated — must stay untouched.
    assert "BuyingMode" in rewritten


def test_idempotent(tmp_path: Path) -> None:
    """Running the migration twice must leave the file identical to
    running it once — no double-rewrite, no double-flag, nothing
    drifts between runs. Pins the contract for sellers who may re-run
    the codemod after a partial apply."""
    path = _write(tmp_path, "code.py", "from adcp.types import AudioAsset\n")

    v3_to_v4.run(tmp_path, apply_changes=True)
    after_first = path.read_text()

    report = v3_to_v4.run(tmp_path, apply_changes=True)
    after_second = path.read_text()

    assert after_first == after_second
    assert report.applied == []  # second run has nothing to rewrite
    assert report.rewritten_files == 0


# ---------------------------------------------------------------------------
# CLI entry + JSON report
# ---------------------------------------------------------------------------


def test_cli_exits_nonzero_on_flagged_findings(tmp_path: Path) -> None:
    """CI gate: any flagged finding (manual review required) → exit 1."""
    _write(tmp_path, "code.py", "from adcp import BrandManifest\n")

    rc = v3_to_v4.main([str(tmp_path)])

    assert rc == 1  # flagged finding


def test_cli_exits_zero_on_renames_only(tmp_path: Path) -> None:
    """Mechanical renames alone don't gate CI — they're a clean apply."""
    _write(tmp_path, "code.py", "from adcp.types import AudioAsset\n")

    rc = v3_to_v4.main([str(tmp_path)])

    assert rc == 0


def test_cli_exits_zero_on_empty_tree(tmp_path: Path) -> None:
    rc = v3_to_v4.main([str(tmp_path)])
    assert rc == 0


def test_cli_exits_nonzero_on_missing_path() -> None:
    rc = v3_to_v4.main(["/nonexistent/path-that-does-not-exist-xyz"])
    assert rc == 2


def test_cli_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--json`` emits a structured report parseable by CI / editors."""
    _write(
        tmp_path,
        "code.py",
        "from adcp.types import AudioAsset\n" "from adcp import BrandManifest\n",
    )

    v3_to_v4.main([str(tmp_path), "--json"])
    out = capsys.readouterr().out

    payload = json.loads(out)
    assert payload["schema_version"] == v3_to_v4.REPORT_SCHEMA_VERSION
    assert payload["scanned_files"] == 1
    assert payload["rewritten_files"] == 0
    assert len(payload["applied"]) == 1
    assert payload["applied"][0]["before"] == "AudioAsset"
    assert payload["applied"][0]["after"] == "AudioContent"
    removed = [f for f in payload["flagged"] if f["kind"] == "flag_removed"]
    assert any(f["before"] == "BrandManifest" for f in removed)


def test_json_report_schema_version_is_declared() -> None:
    """The v1 JSON shape is a wire contract with CI scripts and
    editors. A non-additive change (renaming a field, removing one)
    MUST bump ``REPORT_SCHEMA_VERSION`` AND the SDK minor version —
    this test pins the current version so a change is a deliberate
    choice, not an accident."""
    assert v3_to_v4.REPORT_SCHEMA_VERSION == 1


def test_cli_apply_rewrites_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The happy end-to-end path: scan + rewrite + human-readable summary."""
    path = _write(tmp_path, "code.py", "from adcp.types import AudioAsset\n")

    v3_to_v4.main([str(tmp_path), "--apply"])
    out = capsys.readouterr().out

    assert "Rewrote 1 files" in out or "Rewrote 1 file" in out
    assert path.read_text() == "from adcp.types import AudioContent\n"


def test_unreadable_file_does_not_crash(tmp_path: Path) -> None:
    """Non-UTF-8 binary files in the tree are skipped silently —
    they're obviously not Python source the migration cares about."""
    path = tmp_path / "binary.py"
    path.write_bytes(b"\xff\xfe\x00 not valid utf-8")

    # Doesn't raise.
    report = v3_to_v4.run(tmp_path, apply_changes=False)
    assert report.scanned_files == 1
    assert report.applied == []


# ---------------------------------------------------------------------------
# --apply safety: refuse on dirty git tree, allow with --allow-dirty
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo at ``path`` and commit one file so the
    default branch exists."""
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    (path / ".gitkeep").write_text("")
    subprocess.run(["git", "add", ".gitkeep"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


def test_apply_refuses_on_dirty_git_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--apply`` MUST refuse when the working tree is dirty. Otherwise
    the seller's in-progress work gets mixed into the codemod's rewrite
    diff and ``git diff`` review stops being useful."""
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git not available")

    _init_git_repo(tmp_path)
    # Create an uncommitted file — this makes the tree dirty.
    _write(tmp_path, "code.py", "from adcp.types import AudioAsset\n")

    rc = v3_to_v4.main([str(tmp_path), "--apply"])
    err = capsys.readouterr().err

    assert rc == 2
    assert "dirty git working tree" in err
    assert "--allow-dirty" in err
    # File NOT rewritten (the guard short-circuits before `run`).
    assert (tmp_path / "code.py").read_text() == "from adcp.types import AudioAsset\n"


def test_apply_allow_dirty_overrides_guard(tmp_path: Path) -> None:
    """``--allow-dirty`` lets sellers deliberately run the codemod on
    top of staged changes (e.g. batched with a related refactor)."""
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git not available")

    _init_git_repo(tmp_path)
    path = _write(tmp_path, "code.py", "from adcp.types import AudioAsset\n")

    rc = v3_to_v4.main([str(tmp_path), "--apply", "--allow-dirty"])

    # Renames applied; exit code reflects no flagged findings.
    assert rc == 0
    assert "AudioContent" in path.read_text()


def test_apply_proceeds_when_not_in_git_repo(tmp_path: Path) -> None:
    """Running in a non-git directory (CI sandbox, scratch env) must
    not block --apply. The guard fails-safe: if git can't verify
    dirty state, we proceed. This is already implicitly tested by
    other --apply tests (they use tmp_path which isn't a repo),
    but pin it explicitly so a future tightening of the guard breaks
    here, not silently in seller CI environments."""
    path = _write(tmp_path, "code.py", "from adcp.types import AudioAsset\n")

    rc = v3_to_v4.main([str(tmp_path), "--apply"])

    assert rc == 0
    assert "AudioContent" in path.read_text()


# ---------------------------------------------------------------------------
# --auto-apply: flag_private (all-known import lines)
# ---------------------------------------------------------------------------


def test_auto_apply_rewrites_all_known_private_import(tmp_path: Path) -> None:
    """When every symbol on a generated_poc import line is in
    GENERATED_POC_SYMBOL_MAP, --auto-apply rewrites it to adcp.types."""
    path = _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.context import ContextObject\n"
        "x = ContextObject()\n",
    )
    report = v3_to_v4.run(tmp_path, apply_changes=True, auto_apply=True)

    rewritten = path.read_text()
    assert "adcp.types.generated_poc" not in rewritten
    assert "from adcp.types import ContextObject" in rewritten
    assert "x = ContextObject()" in rewritten

    auto_applied = [f for f in report.auto_applied if f.before == "ContextObject"]
    assert len(auto_applied) >= 1
    assert auto_applied[0].after == "adcp.types.ContextObject"


def test_auto_apply_rewrites_multi_symbol_all_known_line(tmp_path: Path) -> None:
    """A single import line with multiple known symbols is rewritten in one shot."""
    path = _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.x import "
        "BrandReference, ContextObject, MediaBuyStatus\n",
    )
    v3_to_v4.run(tmp_path, apply_changes=True, auto_apply=True)

    rewritten = path.read_text()
    assert "adcp.types.generated_poc" not in rewritten
    assert "from adcp.types import BrandReference, ContextObject, MediaBuyStatus" in rewritten


def test_auto_apply_preserves_as_alias(tmp_path: Path) -> None:
    """``import Error as AdCPError`` — the local alias must survive the
    module-path rewrite."""
    path = _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.error import Error as AdCPError\n",
    )
    v3_to_v4.run(tmp_path, apply_changes=True, auto_apply=True)

    rewritten = path.read_text()
    assert "adcp.types.generated_poc" not in rewritten
    assert "from adcp.types import Error as AdCPError" in rewritten


def test_auto_apply_mixed_line_not_rewritten(tmp_path: Path) -> None:
    """A line with at least one unknown symbol MUST NOT be auto-applied.
    The known symbol is still flagged (not silently dropped)."""
    path = _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.x import BrandReference, Unknown\n",
    )
    report = v3_to_v4.run(tmp_path, apply_changes=True, auto_apply=True)

    rewritten = path.read_text()
    assert "adcp.types.generated_poc" in rewritten, "mixed line must NOT be rewritten"

    # Known symbol must still appear in flagged (not silently dropped).
    flagged_symbols = {f.before for f in report.flagged if f.kind == "flag_private"}
    assert "BrandReference" in flagged_symbols

    # Unknown symbol gets a generic flag too (silent-drop bug is fixed).
    generic_flags = [
        f for f in report.flagged
        if f.kind == "flag_private" and f.before == "adcp.types.generated_poc"
    ]
    assert len(generic_flags) >= 1

    # Nothing should be in auto_applied.
    assert report.auto_applied == []


# ---------------------------------------------------------------------------
# --auto-apply: flag_numbered (numbered Assets classes)
# ---------------------------------------------------------------------------


def test_auto_apply_rewrites_numbered_asset_usage_sites(tmp_path: Path) -> None:
    """Assets81 → VideoFormatAsset everywhere in the file."""
    path = _write(
        tmp_path,
        "code.py",
        "from adcp.types import Assets81\nx: Assets81 = Assets81(asset_type='video')\n",
    )
    v3_to_v4.run(tmp_path, apply_changes=True, auto_apply=True)

    rewritten = path.read_text()
    assert "Assets81" not in rewritten
    assert "VideoFormatAsset" in rewritten


def test_auto_apply_rewrites_numbered_asset_and_fixes_import_path(tmp_path: Path) -> None:
    """When a numbered asset is imported from generated_poc, the module
    path must also be corrected — leaving
    ``from adcp.types.generated_poc.core.format import VideoFormatAsset``
    would be a broken import."""
    path = _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.format import Assets81\n"
        "slot: Assets81\n",
    )
    v3_to_v4.run(tmp_path, apply_changes=True, auto_apply=True)

    rewritten = path.read_text()
    assert "Assets81" not in rewritten
    assert "adcp.types.generated_poc" not in rewritten, (
        "generated_poc import path must be fixed after numbered rename"
    )
    assert "from adcp.types import VideoFormatAsset" in rewritten
    assert "slot: VideoFormatAsset" in rewritten


def test_auto_apply_unknown_numbered_stays_flagged(tmp_path: Path) -> None:
    """A numbered asset not in NUMBERED_ASSETS_RENAMES (e.g. Assets149)
    is not auto-applied and remains in report.flagged."""
    path = _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.bundled.x import Assets149\n",
    )
    original = path.read_text()
    report = v3_to_v4.run(tmp_path, apply_changes=True, auto_apply=True)

    assert path.read_text() == original, "unmapped numbered asset must not be rewritten"
    numbered = [f for f in report.flagged if f.kind == "flag_numbered"]
    assert any(f.before == "Assets149" for f in numbered)
    assert report.auto_applied == []


def test_auto_apply_numbered_plus_known_symbol_same_line(tmp_path: Path) -> None:
    """A line mixing a numbered asset (Assets81) with a known symbol
    (ContextObject) must be fully auto-applied: both symbols resolved,
    import path corrected, nothing left in flagged."""
    path = _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.x import Assets81, ContextObject\n"
        "slot: Assets81\n",
    )
    report = v3_to_v4.run(tmp_path, apply_changes=True, auto_apply=True)

    rewritten = path.read_text()
    assert "adcp.types.generated_poc" not in rewritten
    assert "from adcp.types import VideoFormatAsset, ContextObject" in rewritten
    assert "slot: VideoFormatAsset" in rewritten

    assert any(
        f.before == "Assets81" and f.after == "VideoFormatAsset" for f in report.auto_applied
    )
    assert any(f.before == "ContextObject" for f in report.auto_applied)
    assert not any(f.kind == "flag_private" for f in report.flagged)


# ---------------------------------------------------------------------------
# --auto-apply: combined behaviour + idempotency
# ---------------------------------------------------------------------------


def test_auto_apply_implies_apply(tmp_path: Path) -> None:
    """--auto-apply must also apply the Asset→Content renames (implies --apply)."""
    path = _write(
        tmp_path,
        "code.py",
        "from adcp.types import AudioAsset\n"
        "from adcp.types.generated_poc.core.x import ContextObject\n",
    )
    v3_to_v4.main([str(tmp_path), "--auto-apply"])

    rewritten = path.read_text()
    assert "AudioAsset" not in rewritten, "--auto-apply must imply --apply for Asset renames"
    assert "AudioContent" in rewritten
    assert "adcp.types.generated_poc" not in rewritten


def test_dry_run_with_auto_apply_does_not_write_files(tmp_path: Path) -> None:
    """run(apply_changes=False, auto_apply=True) must NOT write files.
    The public run() API must honour apply_changes=False even when
    auto_apply=True is set (regression guard for the missing apply_changes
    guard on the auto-apply rewrite block)."""
    path = _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.x import ContextObject\n"
        "from adcp.types.generated_poc.core.format import Assets81\n",
    )
    original = path.read_text()
    report = v3_to_v4.run(tmp_path, apply_changes=False, auto_apply=True)

    assert path.read_text() == original, "dry-run must not write files"
    assert report.rewritten_files == 0


def test_auto_apply_leaves_flag_removed_flagged(tmp_path: Path) -> None:
    """flag_removed findings (BrandManifest, DeliverTo, etc.) always
    require human review and must NOT be auto-applied."""
    path = _write(
        tmp_path,
        "code.py",
        "from adcp import BrandManifest\nmanifest = BrandManifest(name='x')\n",
    )
    original = path.read_text()
    report = v3_to_v4.run(tmp_path, apply_changes=True, auto_apply=True)

    assert path.read_text() == original
    removed = [f for f in report.flagged if f.kind == "flag_removed"]
    assert any(f.before == "BrandManifest" for f in removed)
    assert report.auto_applied == []


def test_auto_apply_idempotent(tmp_path: Path) -> None:
    """Running --auto-apply twice must leave the file identical and produce
    zero auto_applied findings on the second pass."""
    path = _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.x import ContextObject\n"
        "from adcp.types.generated_poc.core.format import Assets81\n",
    )

    v3_to_v4.run(tmp_path, apply_changes=True, auto_apply=True)
    after_first = path.read_text()

    report2 = v3_to_v4.run(tmp_path, apply_changes=True, auto_apply=True)
    after_second = path.read_text()

    assert after_first == after_second
    assert report2.auto_applied == []
    assert report2.rewritten_files == 0


def test_auto_apply_exits_nonzero_when_flag_removed_remain(tmp_path: Path) -> None:
    """Even after auto-apply resolves all safe findings, exit code is 1
    when flag_removed findings remain — CI must still gate on them."""
    _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.x import ContextObject\n"
        "from adcp import BrandManifest\n",
    )
    rc = v3_to_v4.main([str(tmp_path), "--auto-apply"])
    assert rc == 1


def test_auto_apply_exits_zero_when_only_safe_findings(tmp_path: Path) -> None:
    """When all findings are auto-applicable, exit code is 0."""
    _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.x import ContextObject\n"
        "from adcp.types.generated_poc.core.format import Assets81\n",
    )
    rc = v3_to_v4.main([str(tmp_path), "--auto-apply"])
    assert rc == 0


# ---------------------------------------------------------------------------
# --auto-apply: text + JSON report
# ---------------------------------------------------------------------------


def test_auto_apply_text_report_has_safe_rewrites_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The human-readable report includes a 'Safe rewrites applied' section."""
    _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.x import ContextObject\n",
    )
    v3_to_v4.main([str(tmp_path), "--auto-apply"])
    out = capsys.readouterr().out
    assert "Safe rewrites applied" in out


def test_auto_apply_json_report_has_auto_applied_array(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json output includes an ``auto_applied`` array (additive, v1 schema)."""
    _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.x import ContextObject\n",
    )
    v3_to_v4.main([str(tmp_path), "--auto-apply", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert "auto_applied" in payload
    assert payload["schema_version"] == 1  # no version bump for additive field
    assert len(payload["auto_applied"]) >= 1
    assert payload["auto_applied"][0]["kind"] == "auto_applied"
    assert payload["auto_applied"][0]["before"] == "ContextObject"


def test_auto_apply_json_auto_applied_empty_without_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --auto-apply the ``auto_applied`` array is always present
    but empty — existing parsers can safely ignore it."""
    _write(tmp_path, "code.py", "from adcp.types import AudioAsset\n")
    v3_to_v4.main([str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "auto_applied" in payload
    assert payload["auto_applied"] == []


# ---------------------------------------------------------------------------
# Discoverability: Tip line and dirty-tree error flag name
# ---------------------------------------------------------------------------


def test_text_report_shows_tip_when_safe_findings_remain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --auto-apply the text report hints at --auto-apply when
    flag_private or flag_numbered findings are present."""
    _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.x import ContextObject\n",
    )
    v3_to_v4.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert "--auto-apply" in out
    assert "Tip:" in out


def test_text_report_no_tip_when_auto_apply_active(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The tip is suppressed when --auto-apply is already active."""
    _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.x import ContextObject\n",
    )
    v3_to_v4.main([str(tmp_path), "--auto-apply"])
    out = capsys.readouterr().out
    assert "Tip:" not in out


# ---------------------------------------------------------------------------
# Pre-existing silent-drop bug fix: mixed known/unknown lines
# ---------------------------------------------------------------------------


def test_mixed_line_unknown_symbol_not_silently_dropped(tmp_path: Path) -> None:
    """When a generated_poc import line mixes known and unknown symbols,
    the unknown symbol must still produce a flag_private finding (bug fix:
    previously it was silently dropped from the report)."""
    _write(
        tmp_path,
        "code.py",
        "from adcp.types.generated_poc.core.x import BrandReference, Unknown\n",
    )
    report = v3_to_v4.run(tmp_path, apply_changes=False)

    all_private = [f for f in report.flagged if f.kind == "flag_private"]
    # BrandReference: known symbol → per-symbol flag with after
    known = [f for f in all_private if f.before == "BrandReference"]
    assert len(known) == 1
    assert known[0].after == "adcp.types.BrandReference"

    # Unknown: unknown symbol → generic private-module flag (was silently
    # dropped before this fix).
    generic = [f for f in all_private if f.before == "adcp.types.generated_poc"]
    assert len(generic) >= 1
