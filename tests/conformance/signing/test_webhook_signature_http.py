"""Signed requests cross an actual socket into a separate public receiver."""

import asyncio
import base64
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization

from adcp.signing import private_key_from_jwk
from adcp.webhooks import WebhookSender, create_mcp_webhook_payload, sign_webhook, to_wire_dict


@asynccontextmanager
async def receiver(tmp_path):
    fixtures = Path(__file__).resolve().parents[3]
    launcher = (
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        "from tests.conformance.signing._webhook_http_server import main; main()"
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        url = f"http://127.0.0.1:{sock.getsockname()[1]}/webhook"
        with (
            (tmp_path / "server.stdout").open("xb") as out,
            (tmp_path / "server.stderr").open("xb") as err,
        ):
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    launcher,
                    str(fixtures),
                    str(tmp_path),
                    str(sock.fileno()),
                ],
                cwd=tmp_path,
                pass_fds=(sock.fileno(),),
                start_new_session=True,
                stdout=out,
                stderr=err,
            )
            try:
                for _ in range(1200):
                    if (tmp_path / "ready").exists():
                        break
                    assert child.poll() is None, (tmp_path / "server.stderr").read_text()
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("webhook receiver startup exceeded 60 seconds")
                yield url
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        await asyncio.to_thread(child.wait, 15)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        await asyncio.to_thread(child.wait)
            assert (tmp_path / "stopped").is_file()


def keys():
    return json.loads(
        files("adcp")
        .joinpath("_compliance/3.2.0-rc.4/test-vectors/webhook-signing/keys.json")
        .read_text()
    )["keys"]


async def test_malformed_then_valid_signatures_over_http(tmp_path):
    key = next(row for row in keys() if row["kid"] == "test-ed25519-webhook-2026")
    async with receiver(tmp_path) as url, httpx.AsyncClient(timeout=30) as client:
        for index, encoding in enumerate(("legacy_standard", "url")):
            body = json.dumps(
                to_wire_dict(
                    create_mcp_webhook_payload(
                        task_id=f"wire-signature-{index}",
                        task_type="create_media_buy",
                        operation_id=f"operation-{index}",
                        status="completed",
                        idempotency_key=f"webhook-boundary-{index}-unique-event",
                    )
                )
            ).encode()
            signed = sign_webhook(
                method="POST",
                url=url,
                headers={"Content-Type": "application/json"},
                body=body,
                private_key=private_key_from_jwk(key, d_field="_private_d_for_test_only"),
                key_id=key["kid"],
                alg="ed25519",
            )
            headers = {"Content-Type": "application/json", **signed.as_dict()}
            if encoding == "legacy_standard":
                # Retained decoder tolerance is separate from webhook-v1 emission.
                token = headers["Signature"].split(":")[1]
                raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
                headers["Signature"] = f"sig1=:{base64.b64encode(raw).decode()}:"
            malformed = {
                **headers,
                "Signature": (
                    "sig1=:A+B-CDEFGHIJKLMNOPQRSTUVWXYZ" "abcdefghijklmnopqrstuvwxyz0123456789AB:"
                ),
            }
            denied = await client.post(url, content=body, headers=malformed)
            assert denied.status_code == 401
            assert (
                'Signature error="webhook_signature_header_malformed"'
                in denied.headers["www-authenticate"]
            )
            assert denied.json() == {
                "key_lookups": index,
                "processed": index,
                "rejected": True,
                "reason": "signature_invalid",
            }
            assert "A+B-CDE" not in denied.text
            # The malformed request consumed neither nonce nor event.
            accepted = await client.post(url, content=body, headers=headers)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json() == {
                "key_lookups": index + 1,
                "processed": index + 1,
                "rejected": False,
                "reason": None,
            }


async def test_public_sender_paths_emit_webhook_profile_over_http(tmp_path):
    async with receiver(tmp_path) as url, httpx.AsyncClient(timeout=30) as client:
        count = 0
        for alg, kid in (
            ("ed25519", "test-ed25519-webhook-2026"),
            ("ecdsa-p256-sha256", "test-es256-webhook-2026"),
        ):
            key = next(row for row in keys() if row["kid"] == kid)
            private_key = private_key_from_jwk(key, d_field="_private_d_for_test_only")
            for entry in ("constructor", "jwk", "pem"):
                if entry == "constructor":
                    sender = WebhookSender(
                        private_key=private_key, key_id=kid, alg=alg, client=client
                    )
                elif entry == "jwk":
                    sender = WebhookSender.from_jwk(
                        key, d_field="_private_d_for_test_only", client=client
                    )
                else:
                    pem = private_key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                    sender = WebhookSender.from_pem(pem, key_id=kid, alg=alg, client=client)
                async with sender:
                    parameters = dict(
                        url=url,
                        task_id=f"sender-{count}",
                        task_type="create_media_buy",
                        operation_id=f"operation-{count}",
                        status="completed",
                    )
                    if entry == "constructor":
                        result = await sender.send_raw(
                            url=url,
                            idempotency_key=f"sender-emission-{count}",
                            payload=to_wire_dict(
                                create_mcp_webhook_payload(
                                    task_id=f"sender-{count}",
                                    task_type="create_media_buy",
                                    operation_id=f"operation-{count}",
                                    status="completed",
                                )
                            ),
                        )
                    elif entry == "jwk":
                        result = await sender.send_mcp(**parameters)
                    else:
                        result = await sender.send_prepared(sender.prepare_mcp(**parameters))
                count += 1
                assert result.ok and result.status_code == 200, result.response_body
                records = [
                    json.loads(row)
                    for row in (tmp_path / "received.jsonl").read_text().splitlines()
                ]
                assert len(records) == count
                assert records[-1]["body_sha256"] == hashlib.sha256(result.sent_body).hexdigest()
                assert re.fullmatch(r"sig1=:[A-Za-z0-9_-]+:", records[-1]["signature"])
