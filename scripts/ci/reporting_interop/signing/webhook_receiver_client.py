#!/usr/bin/env python3
"""Drive the real-socket stale revocation receiver through public signing."""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ed25519

from adcp.signing.webhook_signer import sign_webhook

REFERENCE_NOW = 1_776_520_800
KID = "test-ed25519-webhook-2026"
VECTOR_019_ERROR_CODE = "webhook_signature_revocation_stale"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receiver-url", required=True)
    parser.add_argument("--vector-dir", required=True, type=Path)
    parser.add_argument(
        "--allow-historical-red",
        action="store_true",
        help=(
            "Exit zero only when the known historical classification failure is reproduced. "
            "This mode is diagnostic and can never satisfy a blocking matrix cell."
        ),
    )
    return parser.parse_args()


def _post(
    url: str, body: bytes, headers: dict[str, str]
) -> tuple[int, dict[str, Any], dict[str, str]]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return (
                response.status,
                json.loads(response.read()),
                {name.lower(): value for name, value in response.headers.items()},
            )
    except urllib.error.HTTPError as error:
        return (
            error.code,
            json.loads(error.read()),
            {name.lower(): value for name, value in error.headers.items()},
        )


def _control(receiver_url: str, state: str) -> dict[str, Any]:
    url = receiver_url.rsplit("/", 1)[0] + "/control"
    status, response, _headers = _post(
        url,
        json.dumps({"state": state}, separators=(",", ":")).encode(),
        {"Content-Type": "application/json"},
    )
    if status != 200:
        raise RuntimeError(f"receiver control failed: {status} {response}")
    return response


def _disposition(
    *,
    normative_match: bool,
    receiver_http_mapping_match: bool,
    historical_red_reproduced: bool,
    allow_historical_red: bool,
) -> tuple[str, bool, int]:
    """Keep diagnostic completion distinct from blocking acceptance."""
    blocking_acceptance = normative_match and receiver_http_mapping_match
    if allow_historical_red:
        if historical_red_reproduced and not blocking_acceptance:
            return "historical_red_reproduced", False, 0
        return "historical_red_not_reproduced", False, 1
    if blocking_acceptance:
        return "passed", True, 0
    return "blocking_red", False, 1


def main() -> None:
    args = _arguments()
    keys = json.loads((args.vector_dir / "keys.json").read_text(encoding="utf-8"))["keys"]
    key = next(item for item in keys if item["kid"] == KID)
    private_bytes = base64.urlsafe_b64decode(
        key["_private_d_for_test_only"] + "=" * (-len(key["_private_d_for_test_only"]) % 4)
    )
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(private_bytes)
    rows: list[dict[str, Any]] = []
    states = ["fresh", "outage_stale", "refreshed"]
    for index, state in enumerate(states, start=1):
        control = _control(args.receiver_url, state)
        body = json.dumps(
            {
                "idempotency_key": f"whk_RLXRECEIVER000000000000{index}",
                "task_id": f"task-receiver-{index}",
                "task_type": "sync_reporting_status",
                "protocol": "media-buy",
                "operation_id": "op-receiver",
                "status": "completed",
                "timestamp": "2026-04-18T14:00:00Z",
            },
            separators=(",", ":"),
        ).encode()
        signed = sign_webhook(
            method="POST",
            url=args.receiver_url,
            headers={"Content-Type": "application/json"},
            body=body,
            private_key=private_key,
            key_id=KID,
            alg="ed25519",
            created=REFERENCE_NOW,
            nonce=f"RLXreceiverNonce{index:02d}",
        )
        headers = {"Content-Type": "application/json", **dict(signed.as_dict())}
        status, observed, response_headers = _post(args.receiver_url, body, headers)
        rows.append(
            {
                "state": state,
                "control": control,
                "http_status": status,
                "http_response_headers": response_headers,
                "observed": observed,
            }
        )

    fresh, stale, recovered = rows
    if fresh["http_status"] != 200 or fresh["observed"].get("accepted") is not True:
        raise RuntimeError(f"fresh receiver control failed: {fresh}")
    expected_authenticate = f'Signature error="{VECTOR_019_ERROR_CODE}"'
    observed_authenticate = stale["http_response_headers"].get("www-authenticate")
    outcome_authenticate = stale["observed"].get("response_headers", {}).get("WWW-Authenticate")
    prefix = 'Signature error="'
    observed_signature_code = (
        observed_authenticate[len(prefix) : -1]
        if isinstance(observed_authenticate, str)
        and observed_authenticate.startswith(prefix)
        and observed_authenticate.endswith('"')
        else None
    )
    stale_vector_match = observed_signature_code == VECTOR_019_ERROR_CODE
    stale_receiver_http_mapping_match = (
        stale_vector_match
        and stale["http_status"] == 401
        and outcome_authenticate == observed_authenticate
    )
    operational_error = stale["observed"].get("operational_error", {})
    operational_message = operational_error.get("message")
    stale_historical_red = (
        stale["http_status"] == 503
        and operational_error.get("name") == "RevocationListFreshnessError"
        and isinstance(operational_message, str)
        and "past next_update" in operational_message
        and "last refresh error" in operational_message
        and "code" not in operational_error
        and "step" not in operational_error
        and observed_authenticate is None
        and outcome_authenticate is None
    )
    if not stale_vector_match and not stale_historical_red:
        raise RuntimeError(f"stale revocation did not fail closed: {stale}")
    if recovered["http_status"] != 200 or recovered["observed"].get("accepted") is not True:
        raise RuntimeError(f"receiver did not recover after a fresh fetch: {recovered}")
    final_state = recovered["observed"]["state"]
    if final_state["fetch_attempts"] != 3 or final_state["successful_fetches"] != 2:
        raise RuntimeError(f"unexpected live fetch accounting: {final_state}")
    status, blocking_acceptance, exit_code = _disposition(
        normative_match=stale_vector_match,
        receiver_http_mapping_match=stale_receiver_http_mapping_match,
        historical_red_reproduced=stale_historical_red,
        allow_historical_red=args.allow_historical_red,
    )
    print(
        json.dumps(
            {
                "kind": "reporting_interop_webhook_receiver_refresh",
                "status": status,
                "execution_completed": True,
                "blocking_acceptance": blocking_acceptance,
                "historical_reproducer_mode": args.allow_historical_red,
                "historical_red_reproduced": stale_historical_red,
                "normative_019_match": stale_vector_match,
                "vector_019": {
                    "expected_error_code": VECTOR_019_ERROR_CODE,
                    "observed_error_code": observed_signature_code,
                    "error_code_match": stale_vector_match,
                    "expected_failed_step": 9,
                    "observed_failed_step": None,
                    "failed_step_note": "not exposed by the receiver HTTP contract",
                    "receiver_http_mapping_match": stale_receiver_http_mapping_match,
                    "observed_http_status": stale["http_status"],
                    "observed_www_authenticate": observed_authenticate,
                    "outcome_www_authenticate": outcome_authenticate,
                    "expected_www_authenticate": expected_authenticate,
                },
                "stale_failure_classification": (
                    VECTOR_019_ERROR_CODE
                    if stale_vector_match
                    else "operational_RevocationListFreshnessError"
                ),
                "transport": "real_loopback_http_raw_body",
                "replay_persistence_exercised": False,
                "public_composition": [
                    "WebhookReceiver",
                    "WebhookVerifyOptions",
                    "CachingRevocationChecker",
                    "RevocationListFetcher",
                ],
                "rows": rows,
                "limitations": [
                    "deterministic_public_test_key",
                    "loopback_http_test_transport",
                    "historical_python_signer_profile_does_not_close_cross_language_interop",
                    "not_reporting_delivery_retry_or_account_activity_evidence",
                ],
            },
            sort_keys=True,
        )
    )
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
