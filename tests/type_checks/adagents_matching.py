"""The public listing helper retains typed JSON entries for adopter code."""

from typing import Any

from adcp import find_authorized_agent_entries


def is_listed(document: dict[str, Any], agent_url: str) -> bool:
    entries: list[dict[str, Any]] = find_authorized_agent_entries(document, agent_url)
    return bool(entries)
