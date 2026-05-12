#!/usr/bin/env python3
"""Fetch legacy AdCP schemas from pinned upstream commits.

Unlike the current-version ``sync_schemas.py`` (which pulls signed
tarballs from ``adcontextprotocol.org/protocol/``), legacy versions are
pinned to specific commit SHAs of the ``adcontextprotocol/adcp`` repo
because:

* The upstream CDN doesn't ship legacy tarballs (only the latest major
  is hosted).
* Legacy branches receive maintenance patches; the SHA freezes the
  exact tree we ship in the wheel.

The SHA itself is the integrity check — GitHub guarantees the same SHA
yields the same tree. No checksum sidecar needed.

Adding a new legacy version: append to ``_LEGACY_BUNDLES``. The script
fetches each bundle's ``static/schemas/source/`` subtree and writes it
into ``schemas/cache/{bundle_key}/`` (the same per-version layout
``sync_schemas.py`` uses for the current version).

Usage::

    python scripts/sync_legacy_schemas.py             # all legacy versions
    python scripts/sync_legacy_schemas.py 2.5         # just one
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).parent.parent
CACHE_DIR = REPO_ROOT / "schemas" / "cache"

# Pinned legacy bundles. Each entry maps a wire-version → (commit SHA,
# subtree path within the upstream repo). SHA is the integrity gate.
_LEGACY_BUNDLES: dict[str, tuple[str, str]] = {
    "2.5": (
        "4e553ad955f83b49c7d221ab5c3ff78237ad02e3",
        "static/schemas/source",
    ),
}

_UPSTREAM_REPO = "adcontextprotocol/adcp"
_USER_AGENT = "adcp-python-sdk/legacy-schema-sync"


def _load_resolve_bundle_key():
    """Load ``resolve_bundle_key`` from its source file (importlib).

    Mirrors the pattern in ``scripts/sync_schemas.py``: bypass
    ``adcp/__init__`` to avoid pulling generated Pydantic models that
    may be mid-regeneration when this script runs.
    """
    src = REPO_ROOT / "src" / "adcp" / "validation" / "version.py"
    spec = importlib.util.spec_from_file_location("_adcp_bundle_key", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.resolve_bundle_key


resolve_bundle_key = _load_resolve_bundle_key()


def fetch_zipball(sha: str) -> bytes:
    """Download the upstream zipball at ``sha``."""
    url = f"https://api.github.com/repos/{_UPSTREAM_REPO}/zipball/{sha}"
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(req) as response:
        return response.read()


def extract_subtree(zip_bytes: bytes, subtree: str, dest: Path) -> int:
    """Extract every file under ``subtree`` from the zipball to ``dest``.

    The zipball's top-level dir name is ``{owner}-{repo}-{short_sha}``
    (variable). We discover it by reading the archive's first entry,
    then strip that prefix when computing destination paths.

    Returns the number of files written.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        if not names:
            raise RuntimeError("Empty zipball")
        # GitHub-zipball top dir always ends in a slash.
        top_prefix = names[0].split("/", 1)[0] + "/"
        subtree_prefix = f"{top_prefix}{subtree.rstrip('/')}/"

        count = 0
        # Wipe the destination first — a partial prior run leaves stale
        # files that shadow upstream removals.
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)

        for name in names:
            if not name.startswith(subtree_prefix) or name.endswith("/"):
                continue
            rel = name[len(subtree_prefix) :]
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(name))
            count += 1
        return count


def synthesize_bundled_dir(root: Path) -> int:
    """Build a flat ``bundled/`` mirror of the per-tool request/response
    schemas under each top-level domain directory.

    The 3.0 upstream layout ships both forms; older versions (2.5) ship
    only the top-level form. The :mod:`adcp.validation.schema_loader`
    indexes ``bundled/`` first for ``(tool, request)`` and
    ``(tool, sync)`` pairs (the top-level dirs are walked separately
    for the ``async-response-*`` variants). Synthesizing the directory
    keeps the loader's expectations uniform across versions.

    Only ``*-request.json`` and ``*-response.json`` files are copied —
    async-variant schemas stay in their top-level domain dir where the
    loader's variant walk picks them up.
    """
    bundled = root / "bundled"
    if bundled.exists():
        return 0
    count = 0
    bundled.mkdir()
    for child in root.iterdir():
        if not child.is_dir() or child.name in {"bundled", "core", "enums"}:
            continue
        for schema_file in child.glob("*.json"):
            stem = schema_file.stem
            # ``-async-response-*`` variants stay in the top-level dir.
            if "-async-response-" in stem:
                continue
            if not (stem.endswith("-request") or stem.endswith("-response")):
                continue
            target_dir = bundled / child.name
            target_dir.mkdir(exist_ok=True)
            (target_dir / schema_file.name).write_bytes(schema_file.read_bytes())
            count += 1
    return count


def rewrite_refs_in_tree(root: Path) -> int:
    """Run :func:`fix_schema_refs.fix_refs` over every JSON file in ``root``.

    Upstream legacy schemas use absolute ``$ref`` paths like
    ``/schemas/core/foo.json`` that the validator's
    :class:`jsonschema.RefResolver` would try to fetch as
    ``file:///schemas/...`` (and fail). Rewriting to relative paths
    keeps the loader's local-file resolution happy.
    """
    # Load via importlib to avoid the package init (same pattern as
    # the bundle-key loader above).
    spec = importlib.util.spec_from_file_location(
        "_fix_schema_refs", REPO_ROOT / "scripts" / "fix_schema_refs.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # ``fix_refs`` reads ``mod.SCHEMAS_DIR`` for the resolution base —
    # point it at our legacy root.
    mod.SCHEMAS_DIR = root

    count = 0
    for schema_file in root.rglob("*.json"):
        with open(schema_file) as f:
            schema = json.load(f)
        mod.fix_refs(schema, schema_file)
        with open(schema_file, "w") as f:
            json.dump(schema, f, indent=2)
        count += 1
    return count


def sync_version(version: str, sha: str, subtree: str) -> int:
    """Fetch + extract one legacy bundle. Returns file count."""
    bundle_key = resolve_bundle_key(version)
    dest = CACHE_DIR / bundle_key
    print(f"Fetching adcp@{sha[:8]} → schemas/cache/{bundle_key}/ ...")
    zip_bytes = fetch_zipball(sha)
    count = extract_subtree(zip_bytes, subtree, dest)
    synthesized = synthesize_bundled_dir(dest)
    rewritten = rewrite_refs_in_tree(dest)
    print(
        f"  ✓ Wrote {count} files (+ {synthesized} synthesized in bundled/, "
        f"{rewritten} $refs rewritten)"
    )
    return count + synthesized


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "version",
        nargs="?",
        help="Specific legacy version to sync (default: all pinned versions)",
    )
    args = parser.parse_args()

    if args.version is not None:
        if args.version not in _LEGACY_BUNDLES:
            available = ", ".join(sorted(_LEGACY_BUNDLES))
            print(
                f"error: version {args.version!r} not in _LEGACY_BUNDLES "
                f"(available: {available})",
                file=sys.stderr,
            )
            return 1
        targets = [(args.version, *_LEGACY_BUNDLES[args.version])]
    else:
        targets = [(v, sha, sub) for v, (sha, sub) in _LEGACY_BUNDLES.items()]

    total = 0
    for version, sha, subtree in targets:
        try:
            total += sync_version(version, sha, subtree)
        except Exception as exc:
            print(f"\n✗ Failed to sync {version}: {exc}", file=sys.stderr)
            return 1

    print(f"\n✓ Synced {total} legacy schema files across {len(targets)} version(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
