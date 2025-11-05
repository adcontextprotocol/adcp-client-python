#!/usr/bin/env python3
"""
Conductor Environment Setup Script (Python version)

This script copies .env configuration from the parent repository to the current
Conductor worktree. Run this when starting a new worktree in Conductor.

Usage:
    python3 scripts/setup_conductor_env.py
"""

import json
import os
import shutil
import sys
from pathlib import Path


def main():
    """Main setup function."""
    script_dir = Path(__file__).parent.resolve()
    worktree_root = script_dir.parent
    repo_root = worktree_root.parent.parent

    print("=" * 60)
    print("Conductor Environment Setup")
    print("=" * 60)
    print()
    print(f"Worktree: {worktree_root}")
    print(f"Repository: {repo_root}")
    print()

    # Check if we're in a Conductor worktree
    if ".conductor" not in str(worktree_root):
        print("⚠️  Warning: This doesn't appear to be a Conductor worktree")
        print(f"   Current path: {worktree_root}")
        print()
        response = input("Continue anyway? (y/N) ")
        if response.lower() != "y":
            sys.exit(1)

    # Check if .env exists in repository root
    env_source = repo_root / ".env"
    env_dest = worktree_root / ".env"

    if not env_source.exists():
        print(f"❌ No .env file found in repository root: {env_source}")
        print()
        print("Please create one based on .env.example:")
        print(f"  cd {repo_root}")
        print("  cp .env.example .env")
        print("  # Edit .env with your configuration")
        print()
        sys.exit(1)

    # Copy .env to worktree
    print("📋 Copying .env to worktree...")
    try:
        shutil.copy2(env_source, env_dest)
        print("✅ Environment configuration copied successfully!")
        print()
        print(f"Configuration file: {env_dest}")
        print()

        # Show loaded agents
        print("📊 Checking agent configuration...")
        show_agent_config(env_dest)

        print()
        print("🎉 Setup complete! You can now run integration tests.")
        print()
        print("Next steps:")
        print("  1. Install dependencies: pip install -e .")
        print("  2. Run tests: python3 tests/integration/test_creative_agent.py")
        print()

    except Exception as e:
        print(f"❌ Failed to copy .env file: {e}")
        sys.exit(1)


def show_agent_config(env_path: Path):
    """Parse and display agent configuration from .env file."""
    try:
        agents_json = None

        # Parse .env file to extract ADCP_AGENTS (handle multiline)
        with open(env_path, "r") as f:
            content = f.read()

            # Find ADCP_AGENTS= and extract until the closing quote
            import re
            match = re.search(r"ADCP_AGENTS='([^']*(?:'[^']*)*)'", content, re.DOTALL)
            if not match:
                match = re.search(r'ADCP_AGENTS="([^"]*(?:"[^"]*)*)"', content, re.DOTALL)

            if match:
                agents_json = match.group(1)

        if agents_json:
            try:
                agents = json.loads(agents_json)
                print(f"   Found {len(agents)} agent(s):")
                for agent in agents:
                    auth_icon = "🔐" if agent.get("auth_token") else "🔓"
                    protocol = agent["protocol"].upper()
                    name = agent["name"]
                    print(f"   {auth_icon} {name} ({protocol})")
            except json.JSONDecodeError as e:
                print(f"   ⚠️  Could not parse ADCP_AGENTS JSON: {e}")
        else:
            print("   ⚠️  ADCP_AGENTS not found in .env")

    except Exception as e:
        print(f"   ⚠️  Error reading agent config: {e}")


if __name__ == "__main__":
    main()
