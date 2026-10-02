"""Canonical account keys shared by buyer tracking and seller resolution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def account_key_payload(ref: Any) -> dict[str, Any]:
    """Project the complete key, excluding mutable brand and unit metadata."""
    if hasattr(ref, "model_dump"):
        ref = ref.model_dump(mode="json", exclude_none=True)
    if not isinstance(ref, Mapping):
        raise ValueError("account reference must be an object")
    if ref.get("account_id"):
        return {"account_id": ref["account_id"]}
    brand = ref.get("brand")
    if not isinstance(brand, Mapping) or not brand.get("domain") or not ref.get("operator"):
        raise ValueError("natural account key requires brand.domain and operator")
    brand_key = {"domain": brand["domain"]}
    if brand.get("brand_id") is not None:
        brand_key["brand_id"] = brand["brand_id"]
    if brand.get("countries") is not None:
        brand_key["countries"] = sorted(set(brand["countries"]))
    key = {"brand": brand_key, "operator": ref["operator"], "sandbox": bool(ref.get("sandbox"))}
    unit = ref.get("operator_unit")
    if isinstance(unit, Mapping) and unit.get("id") is not None:
        key["operator_unit"] = {"id": unit["id"]}
    for name in ("currency", "timezone"):
        if ref.get(name) is not None:
            key[name] = ref[name]
    return key


def account_key(ref: Any) -> str:
    """Return a deterministic storage key for a complete account reference."""
    return json.dumps(account_key_payload(ref), sort_keys=True, separators=(",", ":"))


def is_provisioning_task(tool_name: str | None) -> bool:
    """Only spend commitments and account-owned resource creation provision."""
    return bool(
        tool_name
        and (
            tool_name.startswith("sync_")
            or tool_name
            in {"create_media_buy", "buy_products", "accept_proposal", "activate_signal"}
        )
    )
