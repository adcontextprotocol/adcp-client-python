"""The rc.4 webhook-v1 corpus, unchanged in the shipped current fixtures."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import datetime, timezone
from importlib.resources import files

import pytest

from adcp.signing import InMemoryReplayStore, SignatureVerificationError
from adcp.signing.revocation import RevocationList
from adcp.webhooks import WebhookVerifyOptions, verify_webhook_signature

VECTORS = files("adcp").joinpath("_compliance/3.2.0-rc.6/test-vectors/webhook-signing")
KEYS = {
    row["kid"]: {name: value for name, value in row.items() if not name.startswith("_")}
    for row in json.loads(VECTORS.joinpath("keys.json").read_text())["keys"]
}
NAMES = sorted(
    f"{kind}/{path.name}"
    for kind in ("positive", "negative")
    for path in VECTORS.joinpath(kind).iterdir()
    if path.name.endswith(".json")
)


def vector_options(vector):
    state = vector.get("test_harness_state", {})
    cap_for = state.get("per_keyid_cap_filled_for")
    replay = InMemoryReplayStore(per_keyid_cap=1 if cap_for else 1_000_000)
    if cap_for:
        assert replay.remember(cap_for, "test-capacity-prefill", 3600)
    for entry in state.get("replay_cache_entries", []):
        assert replay.remember(entry["keyid"], entry["nonce"], 3600)
    keys = {kid: KEYS[kid] for kid in vector["jwks_ref"]}
    keys.update(vector.get("jwks_override", {}))
    revoked = set(state.get("revoked_kids", []))
    revocation = None
    if "revocation_list_stale_seconds" in state:
        # The corpus permits public in-process state installation. This is
        # distinct from its live stale-fetch receiver-runner composition.
        stale_at = vector["reference_now"] - state["revocation_list_stale_seconds"]
        revocation = RevocationList(
            issuer="https://test-agent.example.test",
            updated=datetime.fromtimestamp(stale_at - 1, timezone.utc).isoformat(),
            next_update=datetime.fromtimestamp(stale_at, timezone.utc).isoformat(),
        )
    return WebhookVerifyOptions(
        jwks_resolver=keys.get,
        replay_store=replay,
        revocation_checker=revoked.__contains__,
        revocation_list=revocation,
        clock=lambda: vector["reference_now"],
    )


@pytest.mark.parametrize("name", NAMES)
def test_protocol_owned_webhook_vector(name):
    vector = json.loads(VECTORS.joinpath(name).read_text())
    if name == "negative/019-revocation-stale.json":
        assert vector["requires_contract"] == "webhook_receiver_runner"
        assert vector["black_box_behavior"] == "simulate_stale_revocation_fetch"
        assert vector_options(vector).revocation_list is not None
    request = vector["request"]
    arguments = {
        "method": request["method"],
        "url": request["url"],
        "headers": request["headers"],
        "body": request["body"].encode("utf-8"),
        "options": vector_options(vector),
    }
    expected = vector["expected_outcome"]
    if expected["success"]:
        assert verify_webhook_signature(**arguments).label == "sig1"
    else:
        with pytest.raises(SignatureVerificationError) as raised:
            verify_webhook_signature(**arguments)
        assert raised.value.code == expected["error_code"]
        if name.split("/")[1][:3] in {"006", "009", "015", "019", "021"}:
            assert raised.value.step == expected["failed_step"]


@pytest.mark.parametrize(
    "signature",
    [
        "sig1=:A+B-CDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789AB:",
        "sig1=:A/B_CDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789AB:",
    ],
)
def test_mixed_alphabet_stops_before_lookup_crypto_or_replay(signature, monkeypatch):
    vector = json.loads(VECTORS.joinpath("positive/001-basic-post.json").read_text())
    request = vector["request"]
    options = vector_options(vector)

    def forbidden(*args, **kwargs):
        pytest.fail("malformed signature reached key lookup, crypto or replay state")

    monkeypatch.setattr("adcp.signing.verifier.verify_signature", forbidden)
    for method in ("seen", "remember", "claim", "at_capacity"):
        monkeypatch.setattr(options.replay_store, method, forbidden)
    options = replace(options, jwks_resolver=forbidden)
    with pytest.raises(SignatureVerificationError) as raised:
        verify_webhook_signature(
            method=request["method"],
            url=request["url"],
            headers={**request["headers"], "Signature": signature},
            body=request["body"].encode(),
            options=options,
        )
    assert (raised.value.code, raised.value.step) == ("webhook_signature_header_malformed", 1)
    assert str(raised.value) == "webhook Signature must not mix Base64 alphabets"


@pytest.mark.parametrize("encoding", ["standard_padded", "url_unpadded"])
def test_legacy_decoder_tolerance_and_unselected_labels(encoding):
    vector = json.loads(VECTORS.joinpath("positive/001-basic-post.json").read_text())
    request = vector["request"]
    token = request["headers"]["Signature"].split(":")[1]
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    token = (
        base64.b64encode(raw).decode()
        if encoding == "standard_padded"
        else base64.urlsafe_b64encode(raw).decode().rstrip("=")
    )
    # Standard Base64 remains decoder tolerance, not conformant webhook-v1
    # emission. Only the selected label supplies the verified signature.
    header = f"unused=:A+B-CDEF:, sig1=:{token}:"
    assert (
        verify_webhook_signature(
            method=request["method"],
            url=request["url"],
            headers={**request["headers"], "Signature": header},
            body=request["body"].encode(),
            options=vector_options(vector),
        ).label
        == "sig1"
    )


@pytest.mark.parametrize("header", ['sig1="not-binary"', "sig1=:abcde:", "sig1=:abcde=:"])
def test_malformed_binary_shape_and_padding_stay_step_one(header):
    vector = json.loads(VECTORS.joinpath("positive/001-basic-post.json").read_text())
    request = vector["request"]
    with pytest.raises(SignatureVerificationError) as raised:
        verify_webhook_signature(
            method=request["method"],
            url=request["url"],
            headers={**request["headers"], "Signature": header},
            body=request["body"].encode(),
            options=vector_options(vector),
        )
    assert (raised.value.code, raised.value.step) == ("webhook_signature_header_malformed", 1)
