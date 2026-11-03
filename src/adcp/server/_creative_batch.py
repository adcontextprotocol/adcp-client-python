"""Partial admission for explicitly lenient legacy creative uploads.

The dispatcher owns format projection, not the seller's creative library.
Never reconcile a destructive library replacement from a filtered request.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from adcp.canonical_formats.projection import (
    LegacyCreativeProjectionError,
    LegacyFormatConverter,
    normalize_legacy_creative_request,
)


@dataclass(frozen=True)
class PartialCreativeBatch:
    submitted_ids: tuple[str, ...]
    failures: tuple[dict[str, Any], ...]

    def merge(self, result: Any) -> None:
        """Overlay once per terminal result, before persistence or delivery.

        Idempotency replays may already contain our rejection rows. Replacing
        them rather than appending makes repeated overlays idempotent too.
        Handler request-level errors retain their own terminal error branch.
        """
        payload = (
            result.model_dump(mode="json", exclude_none=True)
            if hasattr(result, "model_dump")
            else result
        )
        if not isinstance(payload, dict) or "creatives" not in payload:
            return
        rows = payload["creatives"]
        if not isinstance(rows, list):
            raise ValueError("sync_creatives completion must contain a creatives array")
        by_id = {row["creative_id"]: row for row in rows}
        if len(by_id) != len(rows):
            raise ValueError("sync_creatives completion contains duplicate creative IDs")
        by_id.update((row["creative_id"], deepcopy(row)) for row in self.failures)
        if any(creative_id not in by_id for creative_id in self.submitted_ids):
            raise ValueError("sync_creatives completion omits an admitted creative")
        ordered = [by_id[creative_id] for creative_id in self.submitted_ids]
        submitted = set(self.submitted_ids)
        ordered.extend(row for row in rows if row["creative_id"] not in submitted)
        if isinstance(result, dict):
            result["creatives"] = ordered
        else:
            from adcp.types import SyncCreativeResult

            result.creatives = [SyncCreativeResult.model_validate(row) for row in ordered]


partial_creative_batch: ContextVar[PartialCreativeBatch | None] = ContextVar(
    "adcp_partial_creative_batch", default=None
)


def normalize_lenient_creative_batch(
    value: Mapping[str, Any],
    *,
    legacy_format_converter: LegacyFormatConverter | None,
    projection_sources: list[Any],
) -> tuple[dict[str, Any], PartialCreativeBatch | None]:
    """Validate the whole envelope before isolating unprojectable formats."""
    from adcp.types.legacy import LegacySyncCreativesRequest

    try:
        LegacySyncCreativesRequest.model_validate(value)
    except ValidationError as exc:
        first = exc.errors(include_input=False, include_context=False, include_url=False)[0]
        path = ".".join(str(part) for part in first["loc"])
        raise LegacyCreativeProjectionError(
            f"Invalid sync_creatives request at {path or 'request'}: {first['msg']}"
        ) from exc

    creatives = value.get("creatives")
    if not creatives:
        return (
            normalize_legacy_creative_request(
                value,
                legacy_format_converter=legacy_format_converter,
                projection_sources=projection_sources,
            ),
            None,
        )
    submitted_ids = tuple(row["creative_id"] for row in creatives)
    if len(set(submitted_ids)) != len(submitted_ids):
        raise LegacyCreativeProjectionError("sync_creatives requires unique creative IDs")

    normalized = normalize_legacy_creative_request(
        {key: child for key, child in value.items() if key != "creatives"},
        legacy_format_converter=legacy_format_converter,
        projection_sources=projection_sources,
    )
    admitted: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, creative in enumerate(creatives):
        creative_sources: list[Any] = []
        try:
            projected = normalize_legacy_creative_request(
                creative,
                legacy_format_converter=legacy_format_converter,
                projection_sources=creative_sources,
                field_path=f"request.creatives[{index}]",
            )
        except LegacyCreativeProjectionError as exc:
            message = str(exc)
            failures.append(
                {
                    "creative_id": creative["creative_id"],
                    "action": "failed",
                    "errors": [
                        {
                            "code": "UNSUPPORTED_FEATURE",
                            "message": message,
                            "field": message.split(" cannot be projected", 1)[0].removeprefix(
                                "request."
                            ),
                        }
                    ],
                }
            )
        else:
            admitted.append(projected)
            projection_sources.extend(creative_sources)
    normalized["creatives"] = admitted
    if not failures:
        return normalized, None
    if value.get("delete_missing"):
        raise LegacyCreativeProjectionError(
            "delete_missing cannot reconcile a partially projected creative batch; "
            "retry the uploads without delete_missing, then reconcile the complete library"
        )
    if value.get("creative_ids"):
        raise LegacyCreativeProjectionError(
            "creative_ids filtering cannot be combined with a partially projected batch; "
            "submit only the selected creatives instead"
        )

    rejected = {row["creative_id"]: row for row in failures}
    assignments = []
    for assignment in normalized.get("assignments", []):
        failure = rejected.get(assignment["creative_id"])
        if failure is None:
            assignments.append(assignment)
        else:
            failure.setdefault("assignment_errors", {})[
                assignment["package_id"]
            ] = "Assignment refused because this submitted creative's format cannot be projected"
    if assignments:
        if not admitted:
            raise LegacyCreativeProjectionError(
                "A batch with no projectable creatives cannot also apply unrelated assignments; "
                "retry the assignments separately"
            )
        normalized["assignments"] = assignments
    else:
        normalized.pop("assignments", None)
    return normalized, PartialCreativeBatch(submitted_ids, tuple(failures))
