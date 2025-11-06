#!/usr/bin/env python3
"""
Sync AdCP JSON schemas from adcontextprotocol.org.

This script downloads all AdCP schemas to schemas/cache/ for code generation.
Based on the JavaScript client's sync-schemas.ts.
"""

import json
import sys
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

ADCP_BASE_URL = "https://adcontextprotocol.org"
SCHEMA_INDEX_URL = f"{ADCP_BASE_URL}/schemas/v1/index.json"
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

    # Download referenced schemas
    refs = extract_refs(schema)
    for ref_url in refs:
        if ref_url.startswith(ADCP_BASE_URL):
            download_schema_file(ref_url, version)


def main():
    """Main entry point."""
    print("Syncing AdCP schemas from adcontextprotocol.org...")
    print(f"Cache directory: {CACHE_DIR}\n")

    try:
        # Download index
        print("Fetching schema index...")
        index = download_schema(SCHEMA_INDEX_URL)
        version = index.get("version", "unknown")
        print(f"Schema version: {version}\n")

        # Collect all schema URLs from index
        schema_urls = set()

        # Extract from schemas section
        if "schemas" in index:
            for section_name, section in index["schemas"].items():
                # Get schemas from schemas subsection
                if "schemas" in section:
                    for schema_name, schema_info in section["schemas"].items():
                        if "$ref" in schema_info:
                            ref_url = schema_info["$ref"]
                            # Convert relative URL to absolute
                            if not ref_url.startswith("http"):
                                ref_url = f"{ADCP_BASE_URL}{ref_url}"
                            schema_urls.add(ref_url)

                # Get schemas from tasks subsection (request/response)
                if "tasks" in section:
                    for task_name, task_info in section["tasks"].items():
                        for io_type in ["request", "response"]:
                            if io_type in task_info and "$ref" in task_info[io_type]:
                                ref_url = task_info[io_type]["$ref"]
                                # Convert relative URL to absolute
                                if not ref_url.startswith("http"):
                                    ref_url = f"{ADCP_BASE_URL}{ref_url}"
                                schema_urls.add(ref_url)

        print(f"Found {len(schema_urls)} schemas\n")

        # Download all schemas
        print("Downloading schemas:")
        for url in sorted(schema_urls):
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
