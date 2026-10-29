"""Version-gated wire projection for the fields adcp#8585 added after 3.3.0-beta.1.

adcontextprotocol/adcp#8585 adds ``standard_dimensions`` and
``unmapped_dimensions`` on offerings, ``inactive_intervals`` on the
subscription readback, and ``previous_timezone`` and ``offering_id`` on
``timezone_cutovers[]`` and ``gaps[]``. The pinned 3.3.0-beta.1 schemas close
those objects, and the server validates responses against the release it
serves, so these fields reach only callers served 3.3.0-beta.2 or later (the
same gate the TypeScript SDK applies). For older callers:

* the new fields are dropped;
* per-offering ``gaps[]`` collapse into one account-wide gap spanning them
  (it removes at least the periods any offering's gap removes);
* per-offering ``timezone_cutovers[]`` for one change collapse into one entry
  at the latest ``effective_at``.

TODO(3.3.0-beta.2): once the SDK pins a bundle containing adcp#8585 these
projections only matter for callers pinned to 3.3.0-beta.1.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

_VERSION = re.compile(r"^(\d+)\.(\d+)(?:\.\d+)?(?:-([A-Za-z]+)\.?(\d+)?)?$")
_OFFERING_FIELDS = ("standard_dimensions", "unmapped_dimensions")


def serves_settled_subscription_fields(adcp_version: str | None) -> bool:
    """Whether a response served at ``adcp_version`` may carry the adcp#8585 fields.

    True for 3.3.0-beta.2 and later prereleases, any 3.3 release candidate or
    final release, and anything later. An absent or unparseable version is
    treated as 3.3.0-beta.1, the pinned default.
    """
    if not adcp_version:
        return False
    match = _VERSION.match(adcp_version.strip())
    if match is None:
        return False
    major, minor, tag, number = match.groups()
    release = (int(major), int(minor))
    if release != (3, 3):
        return release > (3, 3)
    if tag is None:
        return True
    tag = tag.lower()
    if tag == "beta":
        return int(number or 0) >= 2
    return tag == "rc"


def project_offering(offering: Mapping[str, Any], adcp_version: str | None) -> dict[str, Any]:
    """A reporting-delivery offering as served at ``adcp_version``."""
    payload = dict(offering)
    if not serves_settled_subscription_fields(adcp_version):
        for name in _OFFERING_FIELDS:
            payload.pop(name, None)
    return payload


def project_reporting_delivery(
    block: Mapping[str, Any], adcp_version: str | None
) -> dict[str, Any]:
    """A ``reporting_delivery`` capability block as served at ``adcp_version``."""
    payload = dict(block)
    if "offerings" in payload:
        payload["offerings"] = [
            project_offering(item, adcp_version) for item in payload["offerings"]
        ]
    return payload


def project_subscription_state(
    state: Mapping[str, Any], adcp_version: str | None
) -> dict[str, Any]:
    """A ``core/reporting-subscription-state.json`` readback as served."""
    payload = dict(state)
    if not serves_settled_subscription_fields(adcp_version):
        payload.pop("inactive_intervals", None)
    return payload


def project_subscription_coverage(
    coverage: Mapping[str, Any], adcp_version: str | None
) -> dict[str, Any]:
    """A ``subscription_coverage[]`` entry as served at ``adcp_version``."""
    payload = dict(coverage)
    if serves_settled_subscription_fields(adcp_version):
        return payload
    if payload.get("gaps"):
        payload["gaps"] = _account_wide_gaps(payload["gaps"])
    if payload.get("timezone_cutovers"):
        payload["timezone_cutovers"] = _account_wide_cutovers(payload["timezone_cutovers"])
    return payload


def _instant(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _account_wide_gaps(gaps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Drop ``offering_id``; merge overlapping per-offering gaps into their span."""
    plain = [dict(gap) for gap in gaps if "offering_id" not in gap]
    scoped = sorted(
        (
            {k: v for k, v in gap.items() if k != "offering_id"}
            for gap in gaps
            if "offering_id" in gap
        ),
        key=lambda item: _instant(item["from"]),
    )
    merged: list[dict[str, Any]] = []
    for entry in scoped:
        last = merged[-1] if merged else None
        if last is not None and _instant(entry["from"]) <= _instant(last["until"]):
            if _instant(entry["until"]) > _instant(last["until"]):
                last["until"] = entry["until"]
            continue
        merged.append(entry)
    result = plain + [entry for entry in merged if entry not in plain]
    return sorted(result, key=lambda item: _instant(item["from"]))


def _account_wide_cutovers(cutovers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_change: dict[tuple[Any, Any], dict[str, Any]] = {}
    for cutover in cutovers:
        key = (cutover.get("timezone"), cutover.get("previous_timezone"))
        entry = {"effective_at": cutover["effective_at"], "timezone": cutover["timezone"]}
        current = by_change.get(key)
        if current is None or _instant(entry["effective_at"]) > _instant(current["effective_at"]):
            by_change[key] = entry
    return sorted(by_change.values(), key=lambda item: _instant(item["effective_at"]))


__all__ = [
    "project_offering",
    "project_reporting_delivery",
    "project_subscription_coverage",
    "project_subscription_state",
    "serves_settled_subscription_fields",
]
