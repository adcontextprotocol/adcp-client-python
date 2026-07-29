"""Drive every AdCP request-signing vector through `verify_request_signature`.

Each vector carries an `expected_outcome` — either success (positive vectors)
or a specific error code (negative vectors). Conformance requires byte-for-byte
match on the error code; `failed_step` is informational.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from adcp.signing import (
    InMemoryReplayStore,
    RevocationList,
    SignatureVerificationError,
    VerifierCapability,
    VerifyOptions,
    verify_request_signature,
)
from tests.conformance.signing.vectors import VECTORS_DIR, load_vector_set

KEYS_BY_KID = {k["kid"]: k for k in json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]}

# Negative vectors the verifier does not yet satisfy, each mapped to the gap it
# tracks. Every one is the same shape: the verifier has no step-1 strict-parse
# stage, so malformed or ambiguous input flows on to a later check and is
# rejected under the wrong error code. Conformance grades the code
# byte-for-byte, so "rejected anyway" is not a pass.
#
# Marked strict: when the verifier is fixed the vector XPASSes and this module
# goes red, which is the signal to delete the entry rather than leave a stale
# exemption behind.
KNOWN_VERIFIER_GAPS: dict[str, str] = {}


def _operation_from_url(url: str) -> str:
    path = urlsplit(url).path
    return path.rstrip("/").rsplit("/", 1)[-1]


def _build_jwks_resolver(vector: dict):
    if "jwks_override" in vector:
        entries = {k["kid"]: k for k in vector["jwks_override"]["keys"]}
    else:
        entries = {kid: KEYS_BY_KID[kid] for kid in vector.get("jwks_ref", [])}

    def resolve(keyid: str) -> dict | None:
        return entries.get(keyid)

    return resolve


def _build_options(vector: dict) -> tuple[VerifyOptions, InMemoryReplayStore]:
    cap_data = vector["verifier_capability"]
    capability = VerifierCapability(
        supported=cap_data.get("supported", True),
        covers_content_digest=cap_data.get("covers_content_digest", "either"),
        required_for=frozenset(cap_data.get("required_for", ())),
        supported_for=frozenset(cap_data.get("supported_for", ())),
    )

    replay_store = InMemoryReplayStore()
    revocation: RevocationList | None = None
    state = vector.get("test_harness_state") or {}
    for entry in state.get("replay_cache_entries", []):
        replay_store.remember(entry["keyid"], entry["nonce"], entry["ttl_seconds"])
    if "replay_cache_per_keyid_cap_hit" in state:
        replay_store.mark_cap_hit(state["replay_cache_per_keyid_cap_hit"]["keyid"])
    if "revocation_list" in state:
        revocation = RevocationList.from_dict(state["revocation_list"])

    options = VerifyOptions(
        now=float(vector["reference_now"]),
        capability=capability,
        operation=_operation_from_url(vector["request"]["url"]),
        jwks_resolver=_build_jwks_resolver(vector),
        replay_store=replay_store,
        revocation_checker=(revocation.is_revoked if revocation is not None else None),
    )
    return options, replay_store


_vector_id = lambda v: v if isinstance(v, str) else v.name  # noqa: E731


def _negative_params() -> list[Any]:
    """Negative vectors, with known verifier gaps marked strict-xfail."""
    params = []
    for name, path in load_vector_set("negative"):
        marks = []
        if name in KNOWN_VERIFIER_GAPS:
            marks.append(pytest.mark.xfail(strict=True, reason=KNOWN_VERIFIER_GAPS[name]))
        params.append(pytest.param(name, path, marks=marks))
    return params


@pytest.mark.parametrize(("name", "path"), load_vector_set("positive"), ids=_vector_id)
def test_positive_vector(name: str, path: Path) -> None:
    vector = json.loads(path.read_text())
    options, _ = _build_options(vector)
    request = vector["request"]
    signer = verify_request_signature(
        method=request["method"],
        url=request["url"],
        headers=request["headers"],
        body=request.get("body", "").encode("utf-8"),
        options=options,
    )
    assert signer.label == vector["expected_outcome"].get("verified_label", "sig1")


@pytest.mark.parametrize(("name", "path"), _negative_params(), ids=_vector_id)
def test_negative_vector(name: str, path: Path) -> None:
    vector = json.loads(path.read_text())
    options, _ = _build_options(vector)
    request = vector["request"]
    expected_code = vector["expected_outcome"]["error_code"]

    with pytest.raises(SignatureVerificationError) as exc_info:
        verify_request_signature(
            method=request["method"],
            url=request["url"],
            headers=request["headers"],
            body=request.get("body", "").encode("utf-8"),
            options=options,
        )
    assert exc_info.value.code == expected_code, (
        f"{name}: error_code mismatch\n"
        f"  expected: {expected_code}\n"
        f"  actual:   {exc_info.value.code}\n"
        f"  step:     {exc_info.value.step}\n"
        f"  message:  {exc_info.value}"
    )
