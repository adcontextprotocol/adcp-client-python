"""Additive, page-local revision ownership without changing protocol schemas.

The extension is evidence, never authorization. A revision response can check
its own binding; only a complete periods walk can establish the named owner.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from adcp.reporting.evidence import reporting_identifier

_NAME = "reporting_revision_ownership"


class ReportingOwnershipError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid reporting revision ownership")


def page_revision_ownership(page: Mapping[str, Any]) -> dict[str, str] | None:
    """Read exactly one binding per returned revision, or the absent legacy mode.

    Duplicate bindings on one page are invalid, including identical duplicates.
    The full-walk accumulator may deduplicate identical records on later pages.
    Empty opted-in pages must contain an explicit empty bindings array.
    """
    if "ext" not in page:
        return None
    ext = page["ext"]
    if type(ext) is not dict:
        raise ReportingOwnershipError()
    if "adcp" not in ext:
        return None
    adcp = ext["adcp"]
    if type(adcp) is not dict:
        raise ReportingOwnershipError()
    if _NAME not in adcp:
        return None
    value = adcp[_NAME]
    if (
        type(value) is not dict
        or set(value) != {"version", "bindings"}
        # A2A's protobuf Struct represents every JSON number as a double.
        # JSON Schema's integer 1 includes 1.0, but never booleans or strings.
        or type(value["version"]) not in {int, float}
        or value["version"] != 1
        or type(value["bindings"]) is not list
    ):
        raise ReportingOwnershipError()
    bindings: dict[str, str] = {}
    for binding in value["bindings"]:
        if type(binding) is not dict or set(binding) != {
            "reporting_revision_id",
            "reporting_obligation_id",
        }:
            raise ReportingOwnershipError()
        revision, owner = binding["reporting_revision_id"], binding["reporting_obligation_id"]
        try:
            reporting_identifier(revision, maximum=255)
            reporting_identifier(owner, maximum=255)
        except (ValueError, TypeError):
            raise ReportingOwnershipError() from None
        if revision in bindings:
            raise ReportingOwnershipError()
        bindings[revision] = owner
    revisions = _page_revisions(page)
    if type(revisions) is not list or any(type(r) is not dict for r in revisions):
        raise ReportingOwnershipError()
    ids = [r.get("reporting_revision_id") for r in revisions]
    if (
        any(type(r) is not str for r in ids)
        or len(set(ids)) != len(ids)
        or set(ids) != set(bindings)
    ):
        raise ReportingOwnershipError()
    return bindings


def _page_revisions(page: Mapping[str, Any]) -> Any:
    names = {"revision", "reporting_revision", "revisions"}.intersection(page)
    if len(names) > 1:
        raise ReportingOwnershipError()
    for name in ("revision", "reporting_revision"):
        if name not in page:
            continue
        revision = page[name]
        if type(revision) is not dict:
            raise ReportingOwnershipError()
        if "reporting_revision_binding" in page:
            binding = page["reporting_revision_binding"]
            if type(binding) is not dict or binding.get("reporting_revision_id") != revision.get(
                "reporting_revision_id"
            ):
                raise ReportingOwnershipError()
        return [revision]
    return page.get("revisions", [])


def with_revision_ownership(page: Mapping[str, Any], bindings: Mapping[str, str]) -> dict[str, Any]:
    """Merge the reserved namespace, rejecting an existing conflicting claim.

    The input is not mutated. Callers supply the already frozen ownership map;
    only bindings for this page's revisions enter the response.
    """
    result = deepcopy(dict(page))
    previous = page_revision_ownership(result)
    revisions: Sequence[dict[str, Any]] = _page_revisions(result)
    try:
        local = {
            r["reporting_revision_id"]: bindings[r["reporting_revision_id"]] for r in revisions
        }
    except (KeyError, TypeError):
        raise ReportingOwnershipError() from None
    if previous is not None and previous != local:
        raise ReportingOwnershipError()
    ext = result.setdefault("ext", {})
    adcp = ext.setdefault("adcp", {})
    adcp[_NAME] = {
        "version": 1,
        "bindings": [
            {"reporting_revision_id": revision, "reporting_obligation_id": owner}
            for revision, owner in local.items()
        ],
    }
    page_revision_ownership(result)
    return result
