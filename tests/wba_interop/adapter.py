"""Web Bot Auth interop adapter for the AdCP harness (``adcp/tests/wba-interop``).

The harness grades this SDK against Cloudflare's ``web-bot-auth`` reference. It
runs this script once per operation: one JSON operation on standard input, one
JSON result on standard output. Every operation goes through the SDK's public
``adcp.signing`` API, so the harness grades what an adopter ships.

Operations (``PROTOCOL.md`` in the harness):

``sign_request``
    ``key`` (private JWK), ``origin``, ``request`` (``method``, ``url``,
    ``headers``, ``body``), ``now``. Returns the signed request.
``sign_directory``
    ``keys`` (private JWKs), ``origin``, ``now``. Returns ``status``,
    ``headers``, and ``body``.
``verify_request``
    ``request``, ``directories`` (origin to the signed directory response its
    well-known path serves), ``now``, and ``prior`` (requests to verify first,
    against the same replay store). Returns ``ok``, ``identity``, and
    ``keyid``, or a failure. An origin absent from ``directories`` serves
    nothing, and nothing is fetched from the network.
``verify_directory``
    ``origin``, ``response``, ``now``. Returns ``ok`` and ``keyids``, or a
    failure.

A failure is ``{"ok": false, "status": ..., "rule": ..., "error": ...}``, with
the status the SDK answers with. Only the SDK's verification refusals are
failures: ``WbaVerificationError``. Any other exception, a ``ValueError``
included, crashes the adapter rather than reading as a refusal, as
``PROTOCOL.md`` requires.

Run it as ``uv run python tests/wba_interop/adapter.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from joserfc.jwk import JWKRegistry

from adcp.signing import (
    DirectoryResponse,
    DiscoveredSignerKeys,
    InMemoryReplayStore,
    WbaVerificationError,
    sign_key_directory,
    sign_wba_request,
    verify_directory_response,
    verify_wba_request,
)

DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"

Result = dict[str, Any]


def _private_key(jwk: Mapping[str, Any]) -> Any:
    return JWKRegistry.import_key(dict(jwk)).private_key


def _failure(exc: WbaVerificationError) -> Result:
    return {"ok": False, "status": exc.status, "rule": exc.reason, "error": str(exc)}


def _directory(response: Mapping[str, Any]) -> DirectoryResponse:
    return DirectoryResponse(
        body=response["body"].encode(),
        headers=dict(response["headers"]),
        status=response.get("status", 200),
    )


async def sign_request(op: Mapping[str, Any]) -> Result:
    request = op["request"]
    headers = sign_wba_request(
        method=request["method"],
        url=request["url"],
        headers=request["headers"],
        body=request.get("body", "").encode(),
        private_key=_private_key(op["key"]),
        agent_origin=op["origin"],
        created=op["now"],
    )
    return {**request, "headers": {**request["headers"], **headers}}


async def sign_directory(op: Mapping[str, Any]) -> Result:
    directory = sign_key_directory(
        agent_origin=op["origin"],
        private_keys=[_private_key(key) for key in op["keys"]],
        created=op["now"] - 60,
    )
    return {
        "url": op["origin"].rstrip("/") + DIRECTORY_PATH,
        "status": directory.status,
        "headers": dict(directory.headers),
        "body": directory.body.decode(),
    }


async def verify_request(op: Mapping[str, Any]) -> Result:
    served = {origin + DIRECTORY_PATH: response for origin, response in op["directories"].items()}

    async def fetch(url: str, *, allow_private: bool = False) -> DirectoryResponse:
        if url not in served:
            raise WbaVerificationError(f"no directory at {url}")
        return _directory(served[url])

    now = op["now"]
    signer_keys = DiscoveredSignerKeys(fetcher=fetch, clock=lambda: now)
    replay_store = InMemoryReplayStore()
    for message in [*op.get("prior", []), op["request"]]:
        (signer,) = await verify_wba_request(
            method=message["method"],
            url=message["url"],
            headers=message["headers"],
            body=message.get("body", "").encode(),
            signer_keys=signer_keys,
            replay_store=replay_store,
            now=now,
        )
    return {"ok": True, "identity": signer.agent_url, "keyid": signer.key_id}


async def verify_directory(op: Mapping[str, Any]) -> Result:
    keys = verify_directory_response(op["origin"], _directory(op["response"]), now=op["now"])
    return {"ok": True, "keyids": sorted(keys)}


OPERATIONS: dict[str, Callable[[Mapping[str, Any]], Awaitable[Result]]] = {
    "sign_request": sign_request,
    "sign_directory": sign_directory,
    "verify_request": verify_request,
    "verify_directory": verify_directory,
}


async def run(op: Mapping[str, Any]) -> Result:
    try:
        return await OPERATIONS[op["op"]](op)
    except WbaVerificationError as exc:
        # The harness grades the refusal itself, so a refusal is a result.
        return _failure(exc)


if __name__ == "__main__":
    sys.stdout.write(json.dumps(asyncio.run(run(json.loads(sys.stdin.read())))))
