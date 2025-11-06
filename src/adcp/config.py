from __future__ import annotations

"""Configuration management for AdCP CLI."""

import json
from pathlib import Path
from typing import Any, cast

CONFIG_DIR = Path.home() / ".adcp"
CONFIG_FILE = CONFIG_DIR / "config.json"


def ensure_config_dir() -> None:
    """Ensure config directory exists."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    """Load configuration file."""
    if not CONFIG_FILE.exists():
        return {"agents": {}}

    with open(CONFIG_FILE) as f:
        return cast(dict[str, Any], json.load(f))


def save_config(config: dict[str, Any]) -> None:
    """Save configuration file with atomic write."""
    ensure_config_dir()

    # Write to temporary file first
    temp_file = CONFIG_FILE.with_suffix(".tmp")
    with open(temp_file, "w") as f:
        json.dump(config, f, indent=2)

    # Atomic rename
    temp_file.replace(CONFIG_FILE)


def save_agent(
    alias: str, url: str, protocol: str | None = None, auth_token: str | None = None
) -> None:
    """Save agent configuration."""
    config = load_config()

    if "agents" not in config:
        config["agents"] = {}

    config["agents"][alias] = {
        "agent_uri": url,
        "protocol": protocol or "mcp",
    }

    if auth_token:
        config["agents"][alias]["auth_token"] = auth_token

    save_config(config)


def get_agent(alias: str) -> dict[str, Any] | None:
    """Get agent configuration by alias."""
    config = load_config()
    result = config.get("agents", {}).get(alias)
    return cast(dict[str, Any], result) if result is not None else None


def list_agents() -> dict[str, Any]:
    """List all saved agents."""
    config = load_config()
    return cast(dict[str, Any], config.get("agents", {}))


def remove_agent(alias: str) -> bool:
    """Remove agent configuration."""
    config = load_config()

    if alias in config.get("agents", {}):
        del config["agents"][alias]
        save_config(config)
        return True

    return False
