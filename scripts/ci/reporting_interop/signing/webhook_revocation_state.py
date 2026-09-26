#!/usr/bin/env python3
"""Exercise rc.4 vector 019 with the public revocation-list receiver state."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adcp.signing.errors import SignatureVerificationError
from adcp.signing.revocation import RevocationList
from adcp.signing.webhook_verifier import WebhookVerifyOptions, verify_webhook_signature


def _options(vector: dict, keys: dict[str, dict], *, stale: bool) -> WebhookVerifyOptions:
    now = datetime.fromtimestamp(vector["reference_now"], tz=timezone.utc)
    next_update = now - timedelta(seconds=10800) if stale else now + timedelta(seconds=3600)
    revocations = RevocationList(
        issuer="https://receiver.example.test",
        updated=(now - timedelta(seconds=14400)).isoformat(),
        next_update=next_update.isoformat(),
    )
    return WebhookVerifyOptions(
        jwks_resolver=lambda kid: keys.get(kid),
        revocation_list=revocations,
        clock=lambda: float(vector["reference_now"]),
    )


def _run(vector: dict, keys: dict[str, dict], *, stale: bool) -> dict:
    request = vector["request"]
    try:
        verify_webhook_signature(
            method=request["method"],
            url=request["url"],
            headers=request["headers"],
            body=request["body"].encode(),
            options=_options(vector, keys, stale=stale),
        )
        return {"success": True}
    except SignatureVerificationError as error:
        return {
            "success": False,
            "error_code": error.code,
            "step": error.step,
        }


def main() -> None:
    vector_dir = Path(sys.argv[1])
    vector = json.loads(
        (vector_dir / "negative" / "019-revocation-stale.json").read_text(encoding="utf-8")
    )
    keys = {
        key["kid"]: {name: value for name, value in key.items() if not name.startswith("_")}
        for key in json.loads((vector_dir / "keys.json").read_text(encoding="utf-8"))["keys"]
    }
    report = {
        "kind": "protocol_rc4_webhook_revocation_state_control",
        "vector": "negative/019-revocation-stale.json",
        "requires_contract": vector["requires_contract"],
        "mode": "configured_in_process_public_revocation_list",
        "fresh": _run(vector, keys, stale=False),
        "stale": _run(vector, keys, stale=True),
        "limitations": [
            "not_real_stale_fetch",
            "not_live_polling_or_refresh_recovery",
            "not_socket_callback_evidence",
        ],
    }
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
