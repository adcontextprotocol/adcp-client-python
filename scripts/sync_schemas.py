#!/usr/bin/env python3
"""Sync AdCP JSON schemas and agent skills from the authoritative protocol bundle.

Downloads a single gzipped tarball at
https://adcontextprotocol.org/protocol/{version}.tgz, verifies the bundle
against its SHA-256 sidecar, and extracts the `schemas/` tree into
`schemas/cache/` and protocol-managed skills into `skills/`. Replaces the
prior per-file sync.

Pinned releases additionally ship Sigstore keyless sidecars (`.tgz.sig` +
`.tgz.crt`). When present, this script shells out to `cosign verify-blob`
to prove the bundle was built by the adcontextprotocol/adcp release
workflow. Missing sidecars (e.g., `latest.tgz`, or releases that predate
signing) fall back to checksum-only trust per the upstream client contract.

The target version comes from `src/adcp/ADCP_VERSION`. If that version's
bundle is not published, sync falls back to `latest.tgz` (the dev snapshot).

Environment variables:
  ADCP_BASE_URL       Override the protocol host (default: https://adcontextprotocol.org).
                      Set to point at a fixture CDN for cross-SDK CI or pre-release testing.
                      Trailing slashes are stripped automatically. Do NOT include "/protocol".
  ADCP_SKIP_SIGNATURE Set to "1" to skip Sigstore verification and trust the SHA-256 only.

Usage:
    python scripts/sync_schemas.py              # sync schemas + skills
    python scripts/sync_schemas.py --no-skills  # schemas only (e.g. drift checks)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).parent.parent
CACHE_DIR = REPO_ROOT / "schemas" / "cache"
SKILLS_DIR = REPO_ROOT / "skills"
VERSION_FILE = REPO_ROOT / "src" / "adcp" / "ADCP_VERSION"

# Import the bundle-key helper without forcing a full ``adcp`` package
# import (sync runs against fresh clones where dependencies may not be
# installed yet).
sys.path.insert(0, str(REPO_ROOT / "src"))
from adcp.validation.version import resolve_bundle_key  # noqa: E402

_ADCP_BASE = os.environ.get("ADCP_BASE_URL", "https://adcontextprotocol.org").rstrip("/")
# Reject overrides ending in /protocol — appending our own /protocol below
# would silently produce //protocol and 404 against any sensible CDN. Fail
# loud at module import so the typo surfaces immediately.
if _ADCP_BASE.endswith("/protocol"):
    raise ValueError(
        f"ADCP_BASE_URL={_ADCP_BASE!r} ends with '/protocol'. The script "
        "appends '/protocol' itself — pass only the protocol host "
        "(e.g. https://adcontextprotocol.org)."
    )
BUNDLE_BASE_URL = _ADCP_BASE + "/protocol"
USER_AGENT = "adcp-python-sdk/3.0"

# Sigstore keyless verification identity. Must match the upstream release
# workflow — see adcontextprotocol/adcp#2273. Accepts any branch or tag ref;
# the trust gate is upstream `release.yml`'s `on.push.branches` allowlist
# (currently main, 3.0.x, 2.6.x), which is what determines which refs can
# produce a signature in the first place. `refs/tags/*` is forward-compat
# for any future post-tag re-signing flow. Aligned with adcp-client (TS) and
# adcp-go, which both use the same `refs/(heads|tags)/.*` pattern.
COSIGN_IDENTITY_REGEX = (
    r"^https://github\.com/adcontextprotocol/adcp/"
    r"\.github/workflows/release\.yml@refs/(heads|tags)/.*$"
)
COSIGN_OIDC_ISSUER = "https://token.actions.githubusercontent.com"


def get_target_adcp_version() -> str:
    """Read target AdCP version from ADCP_VERSION file (e.g. "3.0.0-rc.3")."""
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text().strip()
    return "latest"


def _http_get(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req) as response:
        return response.read()


def _http_get_optional(url: str) -> bytes | None:
    """GET returning None on 404 instead of raising."""
    try:
        return _http_get(url)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def fetch_bundle(version: str) -> tuple[bytes, str]:
    """Fetch the bundle and expected checksum for a version.

    Returns:
        (tgz_bytes, expected_sha256_hex)
    """
    tgz_bytes = _http_get(f"{BUNDLE_BASE_URL}/{version}.tgz")
    sha_text = _http_get(f"{BUNDLE_BASE_URL}/{version}.tgz.sha256").decode()
    expected = sha_text.split()[0].strip().lower()
    return tgz_bytes, expected


def fetch_bundle_with_fallback(version: str) -> tuple[bytes, str, str]:
    """Fetch a pinned bundle; fall back to `latest` if not published yet.

    Returns:
        (tgz_bytes, expected_sha256_hex, effective_version)
    """
    try:
        data, sha = fetch_bundle(version)
        return data, sha, version
    except HTTPError as exc:
        if exc.code != 404 or version == "latest":
            raise
        print(f"  ! {version}.tgz not published yet; falling back to latest.tgz")
        data, sha = fetch_bundle("latest")
        return data, sha, "latest"


def fetch_signature_sidecars(version: str) -> tuple[bytes | None, bytes | None]:
    """Fetch Sigstore `.sig` and `.crt` sidecars for a version.

    Returns:
        (sig_bytes, crt_bytes), or (None, None) if either sidecar is missing.
    """
    sig = _http_get_optional(f"{BUNDLE_BASE_URL}/{version}.tgz.sig")
    crt = _http_get_optional(f"{BUNDLE_BASE_URL}/{version}.tgz.crt")
    if sig is None or crt is None:
        return None, None
    return sig, crt


def verify_cosign_signature(tgz_bytes: bytes, sig_bytes: bytes, crt_bytes: bytes) -> None:
    """Verify the bundle with `cosign verify-blob`.

    Raises RuntimeError if cosign is not installed or verification fails.
    """
    if shutil.which("cosign") is None:
        raise RuntimeError(
            "Bundle has Sigstore signature sidecars but `cosign` is not installed.\n"
            "  Install: https://docs.sigstore.dev/cosign/installation/\n"
            "  Or set ADCP_SKIP_SIGNATURE=1 to trust the SHA-256 checksum only."
        )

    with tempfile.TemporaryDirectory() as tmp:
        tgz_path = Path(tmp) / "bundle.tgz"
        sig_path = Path(tmp) / "bundle.tgz.sig"
        crt_path = Path(tmp) / "bundle.tgz.crt"
        tgz_path.write_bytes(tgz_bytes)
        sig_path.write_bytes(sig_bytes)
        crt_path.write_bytes(crt_bytes)

        result = subprocess.run(
            [
                "cosign",
                "verify-blob",
                "--signature",
                str(sig_path),
                "--certificate",
                str(crt_path),
                "--certificate-identity-regexp",
                COSIGN_IDENTITY_REGEX,
                "--certificate-oidc-issuer",
                COSIGN_OIDC_ISSUER,
                str(tgz_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    if result.returncode != 0:
        raise RuntimeError(
            "Cosign signature verification failed.\n"
            f"  stdout: {result.stdout.strip()}\n"
            f"  stderr: {result.stderr.strip()}"
        )


def _extract_bundle(
    tgz_bytes: bytes, effective_version: str
) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    """Extract the bundle to a temporary directory and return the bundle root.

    The caller is responsible for closing the returned TemporaryDirectory.
    Use as a context manager: ``with tmpdir: ...``
    """
    tmpdir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
    try:
        with tarfile.open(fileobj=io.BytesIO(tgz_bytes), mode="r:gz") as tf:
            tf.extractall(tmpdir.name, filter="data")
    except Exception:
        tmpdir.cleanup()
        raise
    bundle_root = Path(tmpdir.name) / f"adcp-{effective_version}"
    return bundle_root, tmpdir


def replace_cache_from_bundle(bundle_root: Path, bundle_key: str) -> int:
    """Extract the bundle's ``schemas/`` tree into ``CACHE_DIR/{bundle_key}/``.

    Per-version layout: ``schemas/cache/3.0/``, ``schemas/cache/2.5/``,
    ``schemas/cache/3.1.0-beta.1/``. Replaces only the target bundle key's
    subtree, leaving sibling versions intact.

    Returns the number of files written.
    """
    schemas_src = bundle_root / "schemas"
    if not schemas_src.is_dir():
        raise RuntimeError(f"Bundle missing expected directory: {bundle_root.name}/schemas/")

    dest = CACHE_DIR / bundle_key
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(schemas_src, dest)

    return sum(1 for _ in dest.rglob("*") if _.is_file())


def sync_skills_from_bundle(bundle_root: Path, skills_dir: Path) -> int:
    """Sync protocol-managed skills from the bundle into skills_dir.

    Reads manifest.json to enumerate canonical skills, then copies each
    skill directory, excluding nested schemas/ subdirs (the SDK already has
    those in schemas/cache/). SDK-local skills not in the manifest are left
    untouched. Previous versions are snapshotted as <name>.previous siblings.

    Returns the number of skill files written.
    """
    manifest_path = bundle_root / "manifest.json"
    if not manifest_path.exists():
        print("  ! No manifest.json in bundle — skipping skill sync")
        return 0

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    skill_names = manifest.get("contents", {}).get("skills", [])
    if not isinstance(skill_names, list) or not skill_names:
        print("  ! No skills listed in bundle manifest — skipping skill sync")
        return 0

    skills_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    for name in skill_names:
        if not isinstance(name, str):
            print(f"  ! Skipping non-string skill entry: {name!r}")
            continue

        # Guard against path traversal: reject names containing "/" or that
        # resolve to a different basename (e.g. "good/../evil" or "../evil").
        if "/" in name or name != Path(name).name:
            raise RuntimeError(f"Unsafe skill name rejected: {name!r}")

        src = bundle_root / "skills" / name
        if not src.is_dir():
            print(f"  ! Skill directory missing in bundle: skills/{name}/ — skipping")
            continue

        dst = skills_dir / name
        prev = skills_dir / f"{name}.previous"
        if dst.exists():
            if prev.is_dir():
                shutil.rmtree(prev)
            elif prev.exists():
                prev.unlink()
            shutil.copytree(dst, prev)
            shutil.rmtree(dst)

        # Copy the skill tree, excluding embedded schemas/ subdirs —
        # those duplicate the canonical schemas/cache/ tree the SDK already has.
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("schemas"))
        count += sum(1 for _ in dst.rglob("*") if _.is_file())

    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync AdCP schemas and skills from the protocol bundle."
    )
    parser.add_argument(
        "--no-skills",
        action="store_true",
        help="Skip skill sync (useful for schema-only drift checks).",
    )
    args = parser.parse_args()

    target_version = get_target_adcp_version()
    if "ADCP_BASE_URL" in os.environ:
        print(f"  ! ADCP_BASE_URL override active: {_ADCP_BASE}")
    print(f"Syncing AdCP protocol bundle from {BUNDLE_BASE_URL}...")
    print(f"Target version: {target_version}")
    print(f"Schema cache: {CACHE_DIR}")
    if not args.no_skills:
        print(f"Skills dir:   {SKILLS_DIR}")
    print()

    try:
        print(f"Fetching {target_version}.tgz + checksum...")
        tgz_bytes, expected_sha, effective_version = fetch_bundle_with_fallback(target_version)
    except (HTTPError, URLError) as exc:
        print(f"\n✗ Failed to download bundle: {exc}", file=sys.stderr)
        sys.exit(1)

    actual_sha = hashlib.sha256(tgz_bytes).hexdigest()
    if actual_sha != expected_sha:
        print(
            f"\n✗ Checksum mismatch for adcp-{effective_version}.tgz:\n"
            f"  expected: {expected_sha}\n"
            f"  actual:   {actual_sha}",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"  ✓ Checksum verified ({actual_sha[:12]}…, {len(tgz_bytes):,} bytes)")

    if os.environ.get("ADCP_SKIP_SIGNATURE") == "1":
        print("  ! Skipping Sigstore verification (ADCP_SKIP_SIGNATURE=1)")
    else:
        try:
            sig_bytes, crt_bytes = fetch_signature_sidecars(effective_version)
        except (HTTPError, URLError) as exc:
            print(f"\n✗ Failed to fetch signature sidecars: {exc}", file=sys.stderr)
            sys.exit(1)

        if sig_bytes is None or crt_bytes is None:
            print(f"  ! No Sigstore sidecars for adcp-{effective_version} " "(checksum-only trust)")
        else:
            try:
                verify_cosign_signature(tgz_bytes, sig_bytes, crt_bytes)
            except RuntimeError as exc:
                print(f"\n✗ {exc}", file=sys.stderr)
                sys.exit(1)
            print(
                "  ✓ Sigstore signature verified "
                "(issued to adcontextprotocol/adcp release workflow)"
            )

    try:
        bundle_root, tmpdir = _extract_bundle(tgz_bytes, effective_version)
    except (tarfile.TarError, RuntimeError) as exc:
        print(f"\n✗ Failed to extract bundle: {exc}", file=sys.stderr)
        sys.exit(1)

    bundle_key = resolve_bundle_key(effective_version)

    with tmpdir:
        try:
            schema_count = replace_cache_from_bundle(bundle_root, bundle_key)
        except (OSError, shutil.Error, RuntimeError) as exc:
            print(f"\n✗ Failed to extract schemas: {exc}", file=sys.stderr)
            sys.exit(1)

        skill_count = 0
        if not args.no_skills:
            try:
                skill_count = sync_skills_from_bundle(bundle_root, SKILLS_DIR)
            except (OSError, shutil.Error, RuntimeError) as exc:
                print(f"\n✗ Failed to sync skills: {exc}", file=sys.stderr)
                sys.exit(1)

    print(f"\n✓ Successfully synced {schema_count} schema files")
    if not args.no_skills:
        print(f"✓ Successfully synced {skill_count} skill files")
    print(f"  Effective version: adcp-{effective_version}")
    print(f"  Bundle key:       {bundle_key}")
    print(f"  Schema location:  {CACHE_DIR / bundle_key}")
    if not args.no_skills:
        print(f"  Skills location:  {SKILLS_DIR}")


if __name__ == "__main__":
    main()
