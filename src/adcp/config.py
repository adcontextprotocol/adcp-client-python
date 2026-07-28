from __future__ import annotations

"""Configuration management for AdCP CLI."""

import json
import os
from pathlib import Path
from typing import Any, cast

CONFIG_DIR = Path.home() / ".adcp"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _chmod_private(path: Path, mode: int) -> None:
    if os.name != "posix":
        return
    try:
        path.chmod(mode)
    except OSError as exc:
        raise PermissionError(
            f"Refusing to read insecure AdCP config path {path}; "
            f"could not set permissions to {mode:o}"
        ) from exc


def ensure_config_dir() -> None:
    """Ensure config directory exists."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    _chmod_private(CONFIG_DIR, 0o700)


def load_config() -> dict[str, Any]:
    """Load configuration file."""
    if not CONFIG_FILE.exists():
        return {"agents": {}}

    _chmod_private(CONFIG_DIR, 0o700)
    _chmod_private(CONFIG_FILE, 0o600)

    with open(CONFIG_FILE) as f:
        return cast(dict[str, Any], json.load(f))


def save_config(config: dict[str, Any]) -> None:
    """Save configuration file with atomic write."""
    ensure_config_dir()

    # Write to temporary file first, with restrictive permissions before
    # credentials hit disk.
    temp_file = CONFIG_FILE.with_suffix(".tmp")
    try:
        temp_file.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(temp_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)

    # Atomic rename
    temp_file.replace(CONFIG_FILE)
    _chmod_private(CONFIG_FILE, 0o600)


def save_agent(
    alias: str,
    url: str,
    protocol: str | None = None,
    auth_token: str | None = None,
    extra_headers: dict[str, str] | None = None,
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

    if extra_headers:
        config["agents"][alias]["extra_headers"] = dict(extra_headers)

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
