#!/usr/bin/env python3
"""Installed Python typed-client and semantic Core reconciliation probe."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from importlib.metadata import version

from adcp import ADCPClient, AgentConfig
from adcp.exceptions import ConfigurationError
from adcp.reporting import ExpectedReportingPeriod, reconcile_reporting_core
from adcp.types import GetReportingStatusRequest, Protocol


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--auth-env", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--adcp-version", required=True)
    parser.add_argument("--expect-unsupported", action="store_true")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict[str, object]:
    token = os.environ.get(args.auth_env)
    if not token:
        raise RuntimeError(f"authentication environment variable is unset: {args.auth_env}")
    client = ADCPClient(
        AgentConfig(
            id="interop-seller",
            name="interop-seller",
            agent_uri=args.url,
            protocol=Protocol.MCP,
            auth_token=token,
            auth_header="Authorization",
            auth_type="bearer",
        ),
        adcp_version=args.adcp_version,
    )
    request = GetReportingStatusRequest.model_validate(
        {
            "account": {"account_id": args.account},
            "view": "periods",
            "period": {
                "start": "2026-08-01T00:00:00Z",
                "end": "2026-08-02T00:00:00Z",
            },
        }
    )
    async with client:
        typed = await client.get_reporting_status(request)
        result = await reconcile_reporting_core(
            client,
            request,
            expected_periods=[
                ExpectedReportingPeriod(
                    "shared-daily-key",
                    1,
                    "interop-core-v1",
                    "analytics",
                    "interop-core-v1",
                    (f"media-buy-{args.account[-1]}",),
                    "2026-08-01T00:00:00Z",
                    "2026-08-02T00:00:00Z",
                )
            ],
            now=datetime(2026, 9, 3, tzinfo=timezone.utc),
        )
    return {
        "kind": "python_core_semantic_probe",
        "package": {"name": "adcp", "version": version("adcp")},
        "protocol": args.adcp_version,
        "typed_status": typed.status.value,
        "definitive": result.definitive,
        "obligations": [
            {
                "definitive": item.definitive,
                "reasons": list(item.reasons),
                "reporting_revision_id": item.reporting_revision_id,
            }
            for item in result.obligations
        ],
        "submitted_receipts": len(result.submitted_receipts),
    }


def main() -> None:
    args = _arguments()
    try:
        result = asyncio.run(run(args))
    except ConfigurationError as error:
        if not args.expect_unsupported:
            raise
        message = str(error)
        if args.adcp_version not in message or "not advertised by this SDK" not in message:
            raise RuntimeError("unsupported-version failure was not actionable") from error
        result = {
            "kind": "python_core_semantic_probe",
            "package": {"name": "adcp", "version": version("adcp")},
            "protocol": args.adcp_version,
            "status": "actionable_unsupported",
            "phase": "client_construction",
            "error": {
                "name": type(error).__name__,
                "message": message,
            },
            "mutation_attempted": False,
        }
    else:
        if args.expect_unsupported:
            raise RuntimeError("expected an unsupported-version failure, but the request succeeded")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
