#!/usr/bin/env python3
"""
Sync AdCP JSON schemas from GitHub main branch.

This script downloads ALL AdCP schemas from the repository directory structure,
not just those listed in index.json. This ensures we get all schemas including
asset types (vast-asset.json, daast-asset.json) with discriminators from PR #189.
"""

import json
import sys
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

# Use GitHub API and raw content for complete schema discovery
GITHUB_API_BASE = "https://api.github.com/repos/adcontextprotocol/adcp/contents"
ADCP_BASE_URL = "https://raw.githubusercontent.com/adcontextprotocol/adcp/main"
SCHEMA_INDEX_URL = f"{ADCP_BASE_URL}/static/schemas/v1/index.json"
CACHE_DIR = Path(__file__).parent.parent / "schemas" / "cache"


def download_schema(url: str) -> dict:
    """Download a JSON schema from URL."""
    try:
        with urlopen(url) as response:
            return json.loads(response.read().decode())
    except URLError as e:
        print(f"Error downloading {url}: {e}", file=sys.stderr)
        raise


def extract_refs(schema: dict) -> set[str]:
    """Extract all $ref URLs from a schema recursively."""
    refs = set()

    def walk(obj):
        if isinstance(obj, dict):
            if "$ref" in obj:
                ref = obj["$ref"]
                if ref.startswith("http"):
                    refs.add(ref)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(schema)
    return refs


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


def download_schema_file(url: str, version: str) -> None:
    """Download a schema and save it to cache."""
    # Extract filename from URL
    filename = url.split("/")[-1]
    if not filename.endswith(".json"):
        filename += ".json"

    # Create version directory
    version_dir = CACHE_DIR / version
    version_dir.mkdir(parents=True, exist_ok=True)

    output_path = version_dir / filename

    # Skip if already exists
    if output_path.exists():
        print(f"  ✓ {filename} (cached)")
        return

    print(f"  Downloading {filename}...")
    schema = download_schema(url)

    # Save schema
    with open(output_path, "w") as f:
        json.dump(schema, f, indent=2)

    print(f"  ✓ {filename}")


def main():
    """Main entry point."""
    print("Syncing AdCP schemas from GitHub main branch...")
    print(f"Cache directory: {CACHE_DIR}\n")

    try:
        # Download index to get version
        print("Fetching schema index for version info...")
        index = download_schema(SCHEMA_INDEX_URL)
        version = index.get("version", "unknown")
        print(f"Schema version: {version}\n")

        # Discover ALL schemas by crawling the directory structure
        print("Discovering all schemas in repository...")
        schema_urls = discover_all_schemas("static/schemas/v1")

        # Remove duplicates and sort
        schema_urls = sorted(set(schema_urls))

        print(f"Found {len(schema_urls)} schemas across all directories\n")

        # Download all schemas
        print("Downloading schemas:")
        for url in schema_urls:
            download_schema_file(url, version)

        # Create latest symlink
        latest_link = CACHE_DIR / "latest"
        version_dir = CACHE_DIR / version

        if latest_link.exists() or latest_link.is_symlink():
            latest_link.unlink()

        latest_link.symlink_to(version, target_is_directory=True)

        print(f"\n✓ Successfully synced {len(schema_urls)} schemas")
        print(f"  Version: {version}")
        print(f"  Location: {version_dir}")

    except Exception as e:
        print(f"\n✗ Error syncing schemas: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
