#!/usr/bin/env python3
"""Real-socket public webhook receiver with live revocation refresh state.

The receiver and revocation issuer bind distinct loopback sockets.  Verification
uses the installed SDK's public ``WebhookReceiver`` and
``CachingRevocationChecker``.  The HTTP fetcher below is a deterministic test
transport implementing the public ``RevocationListFetcher`` protocol; it still
performs an actual HTTP request to the issuer for every refresh attempt.
"""

from __future__ import annotations

import argparse
import base64
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ed25519

from adcp.server.idempotency import MemoryBackend, WebhookDedupStore
from adcp.signing import CachingRevocationChecker, StaticJwksResolver
from adcp.signing.revocation_fetcher import FetchResult, RevocationListFetchError
from adcp.signing.webhook_verifier import WebhookVerifyOptions
from adcp.webhooks import WebhookReceiver, WebhookReceiverConfig

REFERENCE_NOW = 1_776_520_800
ISSUER = "https://governance.example.test"
KID = "test-ed25519-webhook-2026"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receiver-port", required=True, type=int)
    parser.add_argument("--issuer-port", required=True, type=int)
    parser.add_argument("--vector-dir", required=True, type=Path)
    return parser.parse_args()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class State:
    def __init__(self, *, key: dict[str, Any]) -> None:
        private_bytes = base64.urlsafe_b64decode(
            key["_private_d_for_test_only"] + "=" * (-len(key["_private_d_for_test_only"]) % 4)
        )
        self.private_key = ed25519.Ed25519PrivateKey.from_private_bytes(private_bytes)
        self.lock = threading.Lock()
        self.mode = "fresh"
        self.wall_seconds = REFERENCE_NOW
        self.monotonic_seconds = 0.0
        self.fetch_attempts = 0
        self.successful_fetches = 0
        self.callbacks = 0

    def transition(self, mode: str) -> dict[str, Any]:
        with self.lock:
            if mode == "fresh":
                self.mode = mode
                self.wall_seconds = REFERENCE_NOW
                self.monotonic_seconds = 0.0
            elif mode == "outage_stale":
                self.mode = mode
                self.wall_seconds = REFERENCE_NOW + 500
                self.monotonic_seconds = 120.0
            elif mode == "refreshed":
                self.mode = mode
                self.wall_seconds = REFERENCE_NOW + 500
                self.monotonic_seconds = 240.0
            else:
                raise ValueError(f"unknown receiver state: {mode}")
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "wall_seconds": self.wall_seconds,
            "monotonic_seconds": self.monotonic_seconds,
            "fetch_attempts": self.fetch_attempts,
            "successful_fetches": self.successful_fetches,
            "callbacks": self.callbacks,
        }

    def revocation_document(self) -> str:
        with self.lock:
            if self.mode == "outage_stale":
                raise RevocationListFetchError("fixture issuer is unavailable")
            if self.mode == "refreshed":
                updated = REFERENCE_NOW + 480
                next_update = REFERENCE_NOW + 660
            else:
                updated = REFERENCE_NOW - 60
                next_update = REFERENCE_NOW + 60
            payload = {
                "version": 1,
                "issuer": ISSUER,
                "updated": datetime.fromtimestamp(updated, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "next_update": datetime.fromtimestamp(next_update, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "revoked_kids": [],
                "revoked_jtis": [],
            }
            protected = _b64url(
                json.dumps(
                    {"alg": "EdDSA", "kid": KID, "typ": "adcp-gov-revocation+jws"},
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            )
            encoded_payload = _b64url(
                json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
            )
            signature = _b64url(
                self.private_key.sign(f"{protected}.{encoded_payload}".encode("ascii"))
            )
            return f"{protected}.{encoded_payload}.{signature}"


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def main() -> None:
    args = _arguments()
    keys = json.loads((args.vector_dir / "keys.json").read_text(encoding="utf-8"))["keys"]
    key = next(item for item in keys if item["kid"] == KID)
    public_key = {name: value for name, value in key.items() if not name.startswith("_")}
    state = State(key=key)

    class IssuerHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/.well-known/governance-revocations.json":
                _json_response(self, 404, {"error": "not_found"})
                return
            with state.lock:
                state.fetch_attempts += 1
                outage = state.mode == "outage_stale"
            if outage:
                _json_response(self, 503, {"error": "temporarily_unavailable"})
                return
            document = state.revocation_document().encode()
            with state.lock:
                state.successful_fetches += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/jose")
            self.send_header("ETag", f'"{state.mode}"')
            self.send_header("Content-Length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    issuer = ThreadingHTTPServer(("127.0.0.1", args.issuer_port), IssuerHandler)
    issuer_thread = threading.Thread(target=issuer.serve_forever, daemon=True)
    issuer_thread.start()

    def fetcher(
        uri: str,
        *,
        if_none_match: str | None = None,
        if_modified_since: str | None = None,
    ) -> FetchResult:
        headers = {"Accept": "application/jose"}
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        if if_modified_since:
            headers["If-Modified-Since"] = if_modified_since
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed loopback fixture URI
                urllib.request.Request(uri, headers=headers), timeout=3
            ) as response:
                body = response.read().decode("utf-8")
                return FetchResult(
                    body=body,
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                )
        except (urllib.error.URLError, TimeoutError) as error:
            raise RevocationListFetchError(f"live fixture fetch failed: {error}") from error

    resolver = StaticJwksResolver({"keys": [public_key]})
    checker = CachingRevocationChecker(
        revocation_uri=(
            f"http://127.0.0.1:{args.issuer_port}/.well-known/governance-revocations.json"
        ),
        issuer=ISSUER,
        jwks_resolver=resolver,
        fetcher=fetcher,
        clock=lambda: state.monotonic_seconds,
        wall_clock=lambda: datetime.fromtimestamp(state.wall_seconds, timezone.utc),
    )
    receiver = WebhookReceiver(
        WebhookReceiverConfig(
            verify_options=WebhookVerifyOptions(
                jwks_resolver=resolver,
                replay_store=None,
                revocation_checker=checker,
                clock=lambda: float(REFERENCE_NOW),
            ),
            dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86_400),
            receiver_scope="reporting-interop-receiver",
            publisher_scope_for=lambda _signer: "test-publisher",
        )
    )

    class ReceiverHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            if self.path == "/control":
                try:
                    requested = json.loads(body)["state"]
                    snapshot = state.transition(requested)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    _json_response(self, 400, {"error": str(error)})
                    return
                _json_response(self, 200, snapshot)
                return
            if self.path != "/webhook":
                _json_response(self, 404, {"error": "not_found"})
                return
            with state.lock:
                state.callbacks += 1
            signed_url = f"http://127.0.0.1:{args.receiver_port}/webhook"
            try:
                outcome = receiver.receive_and_process_sync(
                    method="POST",
                    url=signed_url,
                    headers=dict(self.headers),
                    body=body,
                    handler=lambda _payload: None,
                )
            except Exception as error:  # noqa: BLE001 - retain public composition failures
                _json_response(
                    self,
                    503,
                    {
                        "accepted": False,
                        "operational_error": {
                            "name": type(error).__name__,
                            "message": str(error),
                        },
                        "state": state.snapshot(),
                    },
                )
                return
            status = outcome.http_status or 200
            payload = {
                "accepted": not outcome.rejected and not outcome.in_progress,
                "duplicate": outcome.duplicate,
                "handled": outcome.handled,
                "http_status": status,
                "rejection_reason": outcome.rejection_reason,
                "response_headers": dict(outcome.response_headers),
                "state": state.snapshot(),
            }
            response = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            self.send_response(status)
            for name, value in outcome.response_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", args.receiver_port), ReceiverHandler)
    print(
        json.dumps(
            {
                "kind": "reporting_interop_webhook_receiver_ready",
                "package": {"name": "adcp", "version": version("adcp")},
                "receiver_url": f"http://127.0.0.1:{args.receiver_port}/webhook",
                "issuer_url": (
                    f"http://127.0.0.1:{args.issuer_port}/.well-known/governance-revocations.json"
                ),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        issuer.shutdown()
        issuer.server_close()
        issuer_thread.join(timeout=3)


if __name__ == "__main__":
    main()
