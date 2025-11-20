#!/usr/bin/env python3
"""
Sync AdCP JSON schemas from GitHub main branch.

This script downloads ALL AdCP schemas from the repository directory structure,
not just those listed in index.json. This ensures we get all schemas including
asset types (vast-asset.json, daast-asset.json) with discriminators from PR #189.

Features:
- Content-based change detection (only updates files when content changes)
- Automatic cleanup of orphaned schemas (files removed upstream)
- Preserves directory structure from upstream
- Symlink to latest version for convenience

Usage:
    python scripts/sync_schemas.py          # Normal sync (uses content hashing)
    python scripts/sync_schemas.py --force  # Force re-download all schemas
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

# Use GitHub API and raw content for complete schema discovery
GITHUB_API_BASE = "https://api.github.com/repos/adcontextprotocol/adcp/contents"
ADCP_BASE_URL = "https://raw.githubusercontent.com/adcontextprotocol/adcp/main"
SCHEMA_INDEX_URL = f"{ADCP_BASE_URL}/static/schemas/v1/index.json"
CACHE_DIR = Path(__file__).parent.parent / "schemas" / "cache"
HASH_CACHE_FILE = CACHE_DIR / ".hashes.json"


def compute_hash(content: str) -> str:
    """Compute SHA-256 hash of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def load_hash_cache() -> dict[str, str]:
    """Load cached hashes from disk."""
    if HASH_CACHE_FILE.exists():
        try:
            with open(HASH_CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_hash_cache(hash_cache: dict[str, str]) -> None:
    """Save content hashes to disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(HASH_CACHE_FILE, "w") as f:
        json.dump(hash_cache, f, indent=2)


def download_schema(url: str) -> dict:
    """
    Download a JSON schema from URL.

    Returns:
        Schema data as dict
    """
    try:
        req = Request(url)
        with urlopen(req) as response:
            data = json.loads(response.read().decode())
            return data

    except URLError as e:
        print(f"Error downloading {url}: {e}", file=sys.stderr)
        raise


def list_directory_contents(api_path: str) -> list[dict]:
    """List contents of a GitHub directory via API."""
    try:
        url = f"{GITHUB_API_BASE}/{api_path}"
        with urlopen(url) as response:
            return json.loads(response.read().decode())
    except URLError as e:
        print(f"Error listing {api_path}: {e}", file=sys.stderr)
        return []


def discover_all_schemas(api_path: str = "static/schemas/v1") -> list[str]:
    """
    Recursively discover all .json schema files in the GitHub repository.

    Returns:
        List of raw GitHub URLs for all schema files
    """
    schema_urls = []
    contents = list_directory_contents(api_path)

    for item in contents:
        if item["type"] == "file" and item["name"].endswith(".json"):
            # Convert download_url to raw GitHub URL for consistency
            raw_url = item["download_url"]
            schema_urls.append(raw_url)
        elif item["type"] == "dir":
            # Recursively explore subdirectories
            subdir_urls = discover_all_schemas(item["path"])
            schema_urls.extend(subdir_urls)

    return schema_urls


def download_schema_file(
    url: str, version: str, hash_cache: dict[str, str], force: bool = False
) -> tuple[bool, str | None]:
    """
    Download a schema and save it to cache, using content hashing for change detection.

    Returns:
        Tuple of (was_updated, new_hash)
    """
    # Extract filename from URL
    filename = url.split("/")[-1]
    if not filename.endswith(".json"):
        filename += ".json"

    # Create version directory
    version_dir = CACHE_DIR / version
    version_dir.mkdir(parents=True, exist_ok=True)

    output_path = version_dir / filename

    # Download schema
    try:
        schema = download_schema(url)

        # Normalize JSON output for consistent hashing
        content = json.dumps(schema, indent=2, sort_keys=True)
        new_hash = compute_hash(content)

        # Check if content changed
        if not force and output_path.exists():
            cached_hash = hash_cache.get(url)

            if cached_hash == new_hash:
                # Content unchanged
                print(f"  ✓ {filename} (unchanged)")
                return False, new_hash

            # Content changed
            with open(output_path, "w") as f:
                f.write(content)
            print(f"  ↻ {filename} (updated)")
            return True, new_hash

        # New file or forced download
        with open(output_path, "w") as f:
            f.write(content)

        status = "downloaded" if not output_path.exists() else "forced update"
        print(f"  ✓ {filename} ({status})")
        return True, new_hash

    except Exception as e:
        print(f"  ✗ {filename} (failed: {e})", file=sys.stderr)
        return False, None


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sync AdCP schemas from GitHub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download all schemas, ignoring content hashes",
    )
    args = parser.parse_args()

    print("Syncing AdCP schemas from GitHub main branch...")
    print(f"Cache directory: {CACHE_DIR}")
    print(f"Mode: {'FORCE' if args.force else 'NORMAL (using content hashing)'}\n")

    try:
        # Load hash cache
        hash_cache = {} if args.force else load_hash_cache()
        updated_hashes = {}

        # Download index to get version
        print("Fetching schema index for version info...")
        index_schema = download_schema(SCHEMA_INDEX_URL)
        version = index_schema.get("version", "unknown")

        # Compute hash for index
        index_content = json.dumps(index_schema, indent=2, sort_keys=True)
        index_hash = compute_hash(index_content)
        updated_hashes[SCHEMA_INDEX_URL] = index_hash

        print(f"Schema version: {version}\n")

        # Discover ALL schemas by crawling the directory structure
        print("Discovering all schemas in repository...")
        schema_urls = discover_all_schemas("static/schemas/v1")

        # Remove duplicates and sort
        schema_urls = sorted(set(schema_urls))

        print(f"Found {len(schema_urls)} schemas across all directories\n")

        # Download all schemas
        print("Downloading schemas:")
        updated_count = 0
        cached_count = 0
        removed_count = 0

        for url in schema_urls:
            was_updated, new_hash = download_schema_file(url, version, hash_cache, force=args.force)

            if was_updated:
                updated_count += 1
            else:
                cached_count += 1

            if new_hash:
                updated_hashes[url] = new_hash

        # Clean up orphaned schemas (files that exist locally but not upstream)
        version_dir = CACHE_DIR / version
        if version_dir.exists():
            # Get list of expected filenames from URLs
            expected_files = {url.split("/")[-1] for url in schema_urls}
            # Also allow the hash cache file
            expected_files.add(".hashes.json")

            # Find orphaned JSON files
            orphaned_files = []
            for json_file in version_dir.rglob("*.json"):
                if json_file.name not in expected_files and json_file.name != ".hashes.json":
                    orphaned_files.append(json_file)

            # Remove orphaned files
            if orphaned_files:
                print("\nCleaning up orphaned schemas:")
                for orphan in orphaned_files:
                    rel_path = orphan.relative_to(version_dir)
                    print(f"  ✗ {rel_path} (removed - no longer in upstream)")
                    orphan.unlink()
                    removed_count += 1

                    # Remove empty directories
                    parent = orphan.parent
                    try:
                        if parent != version_dir and not any(parent.iterdir()):
                            parent.rmdir()
                    except OSError:
                        pass

        # Save updated hash cache
        if updated_hashes:
            save_hash_cache(updated_hashes)

        # Create latest symlink
        latest_link = CACHE_DIR / "latest"
        version_dir = CACHE_DIR / version

        if latest_link.exists() or latest_link.is_symlink():
            latest_link.unlink()

        latest_link.symlink_to(version, target_is_directory=True)

        print(f"\n✓ Successfully synced {len(schema_urls)} schemas")
        print(f"  Version: {version}")
        print(f"  Location: {version_dir}")
        print(f"  Updated: {updated_count}")
        print(f"  Cached: {cached_count}")
        if removed_count > 0:
            print(f"  Removed: {removed_count} (orphaned)")

    except Exception as e:
        print(f"\n✗ Error syncing schemas: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
