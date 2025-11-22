#!/usr/bin/env python3
"""
Sync AdCP JSON schemas from the authoritative public website.

This script downloads AdCP schemas from https://adcontextprotocol.org/schemas/
which is the canonical, versioned source of truth for AdCP schemas.

Features:
- Downloads from versioned public API (e.g., v1, v2)
- Content-based change detection (only updates files when content changes)
- Automatic cleanup of orphaned schemas (files removed upstream)
- Preserves directory structure from upstream

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


def get_target_adcp_version() -> str:
    """
    Get the target AdCP version from ADCP_VERSION file.

    Returns:
        AdCP version string (e.g., "v1", "v2")
    """
    version_file = Path(__file__).parent.parent / "ADCP_VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    return "v1"  # Fallback


# Get target AdCP version
TARGET_ADCP_VERSION = get_target_adcp_version()

# Use authoritative public website as source
ADCP_BASE_URL = "https://adcontextprotocol.org/schemas"
SCHEMA_INDEX_URL = f"{ADCP_BASE_URL}/{TARGET_ADCP_VERSION}/index.json"
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


def discover_schemas_from_index(index_data: dict) -> list[str]:
    """
    Discover all schema URLs from index.json.

    The index contains references to all schemas organized by category.
    We recursively extract all $ref paths and convert them to full URLs.

    Returns:
        List of schema URLs
    """
    schema_urls = []

    def extract_refs(obj: dict | list | str) -> None:
        """Recursively extract all $ref values from nested structure."""
        if isinstance(obj, dict):
            if "$ref" in obj:
                # Found a schema reference
                # $ref paths are like "/schemas/2.4.0/core/package.json"
                # Convert to full URL by prepending domain only
                ref_path = obj["$ref"]
                schema_url = f"https://adcontextprotocol.org{ref_path}"
                schema_urls.append(schema_url)
            # Recurse into nested objects
            for value in obj.values():
                extract_refs(value)
        elif isinstance(obj, list):
            # Recurse into lists
            for item in obj:
                extract_refs(item)

    # Start extraction from schemas section
    schemas = index_data.get("schemas", {})
    extract_refs(schemas)

    return schema_urls


def download_schema_file(
    url: str, hash_cache: dict[str, str], force: bool = False
) -> tuple[bool, str | None]:
    """
    Download a schema and save it to cache, using content hashing for change detection.
    Preserves directory structure from URL path.

    Returns:
        Tuple of (was_updated, new_hash)
    """
    # Extract path from URL
    # URL format: https://adcontextprotocol.org/schemas/2.4.0/core/package.json
    # We want to extract: core/package.json
    # The URL from index has full path including version number
    url_path = url.replace(f"{ADCP_BASE_URL}/", "")
    # Split and skip the version part (e.g., "2.4.0" or "v1")
    parts = url_path.split("/")
    if len(parts) > 1:
        # Skip first part (version), keep rest
        url_parts = parts[1:]
    else:
        url_parts = parts

    # Create subdirectories if needed (directly in CACHE_DIR)
    if len(url_parts) > 1:
        subdir = CACHE_DIR / Path(*url_parts[:-1])
        subdir.mkdir(parents=True, exist_ok=True)
        output_path = subdir / url_parts[-1]
        display_name = "/".join(url_parts)
    else:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        output_path = CACHE_DIR / url_parts[0]
        display_name = url_parts[0]

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
                print(f"  ✓ {display_name} (unchanged)")
                return False, new_hash

            # Content changed
            with open(output_path, "w") as f:
                f.write(content)
            print(f"  ↻ {display_name} (updated)")
            return True, new_hash

        # New file or forced download
        with open(output_path, "w") as f:
            f.write(content)

        status = "downloaded" if not output_path.exists() else "forced update"
        print(f"  ✓ {display_name} ({status})")
        return True, new_hash

    except Exception as e:
        print(f"  ✗ {display_name} (failed: {e})", file=sys.stderr)
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

    print(f"Syncing AdCP schemas from {ADCP_BASE_URL}...")
    print(f"Target version: {TARGET_ADCP_VERSION}")
    print(f"Cache directory: {CACHE_DIR}")
    print(f"Mode: {'FORCE' if args.force else 'NORMAL (using content hashing)'}\n")

    try:
        # Load hash cache
        hash_cache = {} if args.force else load_hash_cache()
        updated_hashes = {}

        # Download index to get schema list
        print("Fetching schema index...")
        try:
            index_schema = download_schema(SCHEMA_INDEX_URL)

            # Compute hash for index
            index_content = json.dumps(index_schema, indent=2, sort_keys=True)
            index_hash = compute_hash(index_content)
            updated_hashes[SCHEMA_INDEX_URL] = index_hash

            print(f"Schema index retrieved\n")
        except Exception as e:
            print(f"Error: Could not fetch index.json from {SCHEMA_INDEX_URL}")
            print(f"Details: {e}\n")
            sys.exit(1)

        # Discover all schemas from index
        print(f"Discovering schemas from index...")
        schema_urls = discover_schemas_from_index(index_schema)

        # Remove duplicates and sort
        schema_urls = sorted(set(schema_urls))

        print(f"Found {len(schema_urls)} schemas across all directories\n")

        # Download all schemas
        print("Downloading schemas:")
        updated_count = 0
        cached_count = 0
        removed_count = 0

        for url in schema_urls:
            was_updated, new_hash = download_schema_file(url, hash_cache, force=args.force)

            if was_updated:
                updated_count += 1
            else:
                cached_count += 1

            if new_hash:
                updated_hashes[url] = new_hash

        # Clean up orphaned schemas (files that exist locally but not upstream)
        if CACHE_DIR.exists():
            # Get list of expected filenames from URLs
            expected_files = {url.split("/")[-1] for url in schema_urls}
            # Also allow the hash cache file
            expected_files.add(".hashes.json")

            # Find orphaned JSON files
            orphaned_files = []
            for json_file in CACHE_DIR.rglob("*.json"):
                if json_file.name not in expected_files and json_file.name != ".hashes.json":
                    orphaned_files.append(json_file)

            # Remove orphaned files
            if orphaned_files:
                print("\nCleaning up orphaned schemas:")
                for orphan in orphaned_files:
                    rel_path = orphan.relative_to(CACHE_DIR)
                    print(f"  ✗ {rel_path} (removed - no longer in upstream)")
                    orphan.unlink()
                    removed_count += 1

                    # Remove empty directories
                    parent = orphan.parent
                    try:
                        if parent != CACHE_DIR and not any(parent.iterdir()):
                            parent.rmdir()
                    except OSError:
                        pass

        # Save updated hash cache
        if updated_hashes:
            save_hash_cache(updated_hashes)

        print(f"\n✓ Successfully synced {len(schema_urls)} schemas")
        print(f"  Target AdCP version: {TARGET_ADCP_VERSION}")
        print(f"  Location: {CACHE_DIR}")
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
