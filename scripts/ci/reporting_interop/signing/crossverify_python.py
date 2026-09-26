#!/usr/bin/env python3
"""Verify a synthetic public-test-key webhook sample with the Python API."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from adcp.signing.errors import SignatureVerificationError
from adcp.signing.webhook_verifier import WebhookVerifyOptions, verify_webhook_signature


def main() -> None:
    sample = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    vector_dir = Path(sys.argv[2])
    keys = {
        key["kid"]: {name: value for name, value in key.items() if not name.startswith("_")}
        for key in json.loads((vector_dir / "keys.json").read_text(encoding="utf-8"))["keys"]
    }
    request = sample["request"]
    try:
        verify_webhook_signature(
            method=request["method"],
            url=request["url"],
            headers=request["headers"],
            body=request["body"].encode(),
            options=WebhookVerifyOptions(
                jwks_resolver=lambda kid: keys.get(kid),
                clock=lambda: 1776520800.0,
            ),
        )
        result = {"accepted": True}
    except SignatureVerificationError as error:
        result = {"accepted": False, "code": error.code, "step": error.step}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
