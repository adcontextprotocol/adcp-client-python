#!/usr/bin/env python3
"""
Sync AdCP JSON schemas from GitHub main branch.

This script downloads ALL AdCP schemas from the repository directory structure,
not just those listed in index.json. This ensures we get all schemas including
asset types (vast-asset.json, daast-asset.json) with discriminators from PR #189.

Usage:
    python scripts/sync_schemas.py          # Normal sync (uses ETags)
    python scripts/sync_schemas.py --force  # Force re-download all schemas
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

# Use GitHub API and raw content for complete schema discovery
GITHUB_API_BASE = "https://api.github.com/repos/adcontextprotocol/adcp/contents"
ADCP_BASE_URL = "https://raw.githubusercontent.com/adcontextprotocol/adcp/main"
SCHEMA_INDEX_URL = f"{ADCP_BASE_URL}/static/schemas/v1/index.json"
CACHE_DIR = Path(__file__).parent.parent / "schemas" / "cache"
ETAG_CACHE_FILE = CACHE_DIR / ".etags.json"


def load_etag_cache() -> dict[str, str]:
    """Load cached ETags from disk."""
    if ETAG_CACHE_FILE.exists():
        try:
            with open(ETAG_CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_etag_cache(etag_cache: dict[str, str]) -> None:
    """Save ETags to disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(ETAG_CACHE_FILE, "w") as f:
        json.dump(etag_cache, f, indent=2)


def download_schema(url: str, etag: str | None = None) -> tuple[dict, str | None]:
    """
    Download a JSON schema from URL with ETag support.

    Returns:
        Tuple of (schema_data, new_etag)
        If not modified (304), returns (None, cached_etag)
    """
    try:
        req = Request(url)
        if etag:
            req.add_header("If-None-Match", etag)

        with urlopen(req) as response:
            new_etag = response.headers.get("ETag")
            data = json.loads(response.read().decode())
            return data, new_etag

    except URLError as e:
        # Check if it's a 304 Not Modified
        if hasattr(e, "code") and e.code == 304:
            return None, etag
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
    url: str, version: str, etag_cache: dict[str, str], force: bool = False
) -> tuple[bool, str | None]:
    """
    Download a schema and save it to cache.

    Returns:
        Tuple of (was_updated, new_etag)
    """
    # Extract filename from URL
    filename = url.split("/")[-1]
    if not filename.endswith(".json"):
        filename += ".json"

    # Create version directory
    version_dir = CACHE_DIR / version
    version_dir.mkdir(parents=True, exist_ok=True)

    output_path = version_dir / filename

    # Check ETag if not forcing and file exists
    if not force and output_path.exists():
        cached_etag = etag_cache.get(url)

        if cached_etag:
            # Try to download with ETag check
            try:
                schema, new_etag = download_schema(url, cached_etag)

                if schema is None:
                    # 304 Not Modified
                    print(f"  ✓ {filename} (not modified)")
                    return False, cached_etag

                # Content changed - save it
                with open(output_path, "w") as f:
                    json.dump(schema, f, indent=2)
                print(f"  ↻ {filename} (updated)")
                return True, new_etag

            except Exception as e:
                # Fall back to simple download
                print(f"  ⚠ {filename} (ETag check failed: {e})")
        else:
            # No ETag cached, but file exists - skip
            print(f"  ✓ {filename} (cached, no ETag)")
            return False, None

    # Force download or file doesn't exist
    try:
        schema, new_etag = download_schema(url)

        with open(output_path, "w") as f:
            json.dump(schema, f, indent=2)

        status = "downloaded" if not output_path.exists() else "forced update"
        print(f"  ✓ {filename} ({status})")
        return True, new_etag

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
        help="Force re-download all schemas, ignoring ETags",
    )
    args = parser.parse_args()

    print("Syncing AdCP schemas from GitHub main branch...")
    print(f"Cache directory: {CACHE_DIR}")
    print(f"Mode: {'FORCE' if args.force else 'NORMAL (using ETags)'}\n")

    try:
        # Load ETag cache
        etag_cache = {} if args.force else load_etag_cache()
        updated_etags = {}

        # Download index to get version
        print("Fetching schema index for version info...")
        index_schema, index_etag = download_schema(SCHEMA_INDEX_URL)
        version = index_schema.get("version", "unknown")
        if index_etag:
            updated_etags[SCHEMA_INDEX_URL] = index_etag
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

        for url in schema_urls:
            was_updated, new_etag = download_schema_file(
                url, version, etag_cache, force=args.force
            )

            if was_updated:
                updated_count += 1
            else:
                cached_count += 1

            if new_etag:
                updated_etags[url] = new_etag

        # Save updated ETag cache
        if updated_etags:
            save_etag_cache(updated_etags)

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

    except Exception as e:
        print(f"\n✗ Error syncing schemas: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
