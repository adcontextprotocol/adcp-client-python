#!/usr/bin/env python3
"""Bind installed dependency floors and preserve historical reporting model reds."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
from decimal import Decimal
from pathlib import Path

from adcp import ADCPClient
from adcp.reporting import (
    load_reporting_ledger,
    plan_consumer_statuses,
    post_consumer_statuses,
    reconcile_reporting,
    reconcile_reporting_core,
)
from adcp.reporting.feed.request import FeedRequest, transport_parameters
from adcp.types import (
    GetMediaBuyDeliveryRequest,
    GetReportingStatusRequest,
    ReportingDeliveryConfiguration,
    SyncAccountsRequest,
    SyncReportingReceiptsRequest,
    SyncReportingStatusRequest,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--expected-pydantic", required=True)
    parser.add_argument("--expected-mcp", required=True)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    artifact = args.artifact.resolve()
    pydantic = importlib.metadata.version("pydantic")
    mcp = importlib.metadata.version("mcp")
    if (pydantic, mcp) != (args.expected_pydantic, args.expected_mcp):
        raise SystemExit(f"unexpected dependency versions: pydantic={pydantic}, mcp={mcp}")
    distribution = importlib.metadata.distribution("adcp")
    direct_url = json.loads(distribution.read_text("direct_url.json") or "null")
    if direct_url is None or direct_url.get("url") != artifact.as_uri():
        raise SystemExit("adcp was not installed from the supplied exact local artifact")
    installed_members = {
        item.as_posix(): hashlib.sha256(distribution.locate_file(item).read_bytes()).hexdigest()
        for item in (distribution.files or [])
        if item.as_posix().startswith("adcp/") and distribution.locate_file(item).is_file()
    }

    scope_type = ReportingDeliveryConfiguration.model_fields["scope"].annotation
    explicit_scope = scope_type.model_validate({"media_buy_ids": ["shared-media-buy"]})
    encoded_scope = explicit_scope.model_dump(mode="json", exclude_none=True)
    exact_request = GetMediaBuyDeliveryRequest.model_validate(
        {
            "account": {"account_id": "usd"},
            "reporting_revision_id": "rpr_f1f83c2f27f7c8fe5fac9bb94abe3d8551e07ef5",
            "pagination": {"max_results": 100},
        }
    )
    encoded_exact = exact_request.model_dump(mode="json", exclude_none=True)
    numeric_requests = []
    for value in ("9007199254740993.0", "9007199254740992.0"):
        decoded = json.loads(
            '{"view":"periods","account":{"account_id":"usd"},"ext":{"v":' + value + "}}",
            parse_float=Decimal,
        )
        numeric_requests.append(FeedRequest.parse(transport_parameters(decoded)))

    public_imports = {
        item.__name__: True
        for item in [
            ADCPClient,
            SyncAccountsRequest,
            GetReportingStatusRequest,
            GetMediaBuyDeliveryRequest,
            SyncReportingReceiptsRequest,
            SyncReportingStatusRequest,
            load_reporting_ledger,
            reconcile_reporting,
            reconcile_reporting_core,
            plan_consumer_statuses,
            post_consumer_statuses,
        ]
    }
    report = {
        "kind": "reporting_dependency_floor_probe",
        "python": sys.version.split()[0],
        "artifact": {
            "path": str(artifact),
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "direct_url": direct_url,
        },
        "installed_sdk_members": {
            "count": len(installed_members),
            "manifest_sha256": hashlib.sha256(
                json.dumps(installed_members, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        },
        "dependencies": {"pydantic": pydantic, "mcp": mcp},
        "requires_dist_pydantic": sorted(
            item for item in (distribution.requires or []) if item.lower().startswith("pydantic")
        ),
        "public_imports": public_imports,
        "reporting_model_controls": {
            "py_public_001": {
                "raw": {"media_buy_ids": ["shared-media-buy"]},
                "encoded": encoded_scope,
                "required_correct": encoded_scope == {"media_buy_ids": ["shared-media-buy"]},
            },
            "py_public_002": {
                "raw_has_breakdown_flags": False,
                "encoded": encoded_exact,
                "required_correct": "include_package_daily_breakdown" not in encoded_exact
                and "include_window_breakdown" not in encoded_exact,
            },
            "rlx_py_002": {
                "bindings": [request.filters_json.decode() for request in numeric_requests],
                "required_correct": numeric_requests[0].filters_json
                != numeric_requests[1].filters_json,
            },
        },
        "limitations": [
            "pr1208_artifact_metadata_still_predates_merged_main_1190_floor",
            "model_serialization_and_import_control_only",
            "not_production_or_quadrant_acceptance",
            "does_not_transfer_to_integrated_main",
        ],
    }
    if not all(item["required_correct"] for item in report["reporting_model_controls"].values()):
        raise SystemExit("reporting model required-correct control failed")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
