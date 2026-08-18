"""Full-wire end-to-end: signed HTTP request → Starlette verifier → PgReplayStore.

This is the shape an actual integrator ships: a FastAPI/Starlette
server running ``verify_starlette_request`` with a ``PgReplayStore``,
receiving signed requests from a buyer-side client. Single-process
here (via ``httpx.ASGITransport``), but the wire-level contract is
identical to what a load-balanced multi-worker deployment sees —
including the cross-instance replay defense the Postgres store
exists to provide.

Scenarios covered:

1. **Happy path** — signed request with fresh nonce → 200.
2. **Replay** — same signed headers sent again → 401 with
   ``WWW-Authenticate: Signature error="request_signature_replayed"``.
3. **Fresh nonce after replay** — different signed request → 200.
4. **Simulated second worker** — second ``PgReplayStore`` instance
   on the same pool sees the first instance's ``remember``, rejects
   a replay that landed on the "other" worker.

Requires ``ADCP_PG_TEST_URL``; skipped otherwise (same gate as the
rest of the pg suite).
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

import httpx
import pytest

psycopg = pytest.importorskip("psycopg")
psycopg_pool = pytest.importorskip("psycopg_pool")

TEST_URL = os.environ.get("ADCP_PG_TEST_URL")
if not TEST_URL:
    pytest.skip(
        "ADCP_PG_TEST_URL not set — skipping PgReplayStore e2e tests",
        allow_module_level=True,
    )

from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from adcp.signing import (  # noqa: E402
    PgReplayStore,
    SignatureVerificationError,
    StaticJwksResolver,
    VerifierCapability,
    VerifyOptions,
    b64url_encode,
    sign_request,
    unauthorized_response_headers,
    verify_starlette_request,
)

# -- fixtures ---------------------------------------------------------


@pytest.fixture()
def isolated_table() -> str:
    """Unique per-test table so parallel runs and reruns don't collide."""
    table = f"test_e2e_replay_{secrets.token_hex(6)}"
    with psycopg_pool.ConnectionPool(TEST_URL, min_size=1, max_size=2) as pool:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE {table} (
                    keyid      TEXT        COLLATE "C" NOT NULL,
                    nonce      TEXT        COLLATE "C" NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (keyid, nonce)
                )
                """
            )
        yield table
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")


@pytest.fixture()
def signing_keypair() -> tuple[ed25519.Ed25519PrivateKey, dict[str, Any]]:
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "use": "sig",
        "key_ops": ["verify"],
        "adcp_use": "request-signing",
        "kid": "e2e-buyer",
        "x": b64url_encode(private.public_key().public_bytes_raw()),
    }
    return private, jwk


# -- helpers ----------------------------------------------------------


def _build_app(*, pool: psycopg_pool.ConnectionPool, table: str, jwk: dict[str, Any]) -> Starlette:
    """Build a Starlette app that runs the verifier with PgReplayStore.

    Identical to the shape an integrator writes — no special-case code
    beyond wiring ``replay_store=PgReplayStore(...)`` into
    ``VerifyOptions``.
    """
    replay_store = PgReplayStore(pool=pool, table_name=table)
    jwks_resolver = StaticJwksResolver({"keys": [jwk]})

    async def create_media_buy(request: Request) -> JSONResponse:
        options = VerifyOptions(
            now=float(int(time.time())),
            capability=VerifierCapability(
                covers_content_digest="either",
                required_for=frozenset({"create_media_buy"}),
            ),
            operation="create_media_buy",
            jwks_resolver=jwks_resolver,
            replay_store=replay_store,
        )
        try:
            signer = await verify_starlette_request(request, options=options)
        except SignatureVerificationError as exc:
            return JSONResponse(
                {"error": exc.code, "step": exc.step, "message": str(exc)},
                status_code=401,
                headers=unauthorized_response_headers(exc),
            )
        return JSONResponse({"verified_key_id": signer.key_id, "status": "accepted"})

    return Starlette(routes=[Route("/adcp/create_media_buy", create_media_buy, methods=["POST"])])


# -- tests ------------------------------------------------------------


async def test_signed_request_verifies_end_to_end(
    isolated_table: str,
    signing_keypair: tuple[ed25519.Ed25519PrivateKey, dict[str, Any]],
) -> None:
    """Happy path: sign → POST → Starlette verifies → 200."""
    private_key, jwk = signing_keypair

    with psycopg_pool.ConnectionPool(TEST_URL, min_size=1, max_size=4) as pool:
        app = _build_app(pool=pool, table=isolated_table, jwk=jwk)
        body = b'{"plan_id":"p1"}'
        signed = sign_request(
            method="POST",
            url="http://test/adcp/create_media_buy",
            headers={"Content-Type": "application/json"},
            body=body,
            private_key=private_key,
            key_id="e2e-buyer",
            alg="ed25519",
            signing_profile_version="3.2",
        )
        headers = {"Content-Type": "application/json", **signed.as_dict()}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/adcp/create_media_buy", content=body, headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["verified_key_id"] == "e2e-buyer"


async def test_replay_rejected_with_request_signature_replayed(
    isolated_table: str,
    signing_keypair: tuple[ed25519.Ed25519PrivateKey, dict[str, Any]],
) -> None:
    """The load-bearing property: the second identical request → 401 replayed.

    Without the replay store this would succeed twice; with it the
    second attempt must return the spec's ``request_signature_replayed``
    code and the WWW-Authenticate header.
    """
    private_key, jwk = signing_keypair

    with psycopg_pool.ConnectionPool(TEST_URL, min_size=1, max_size=4) as pool:
        app = _build_app(pool=pool, table=isolated_table, jwk=jwk)
        body = b'{"plan_id":"p1"}'
        signed = sign_request(
            method="POST",
            url="http://test/adcp/create_media_buy",
            headers={"Content-Type": "application/json"},
            body=body,
            private_key=private_key,
            key_id="e2e-buyer",
            alg="ed25519",
            signing_profile_version="3.2",
        )
        headers = {"Content-Type": "application/json", **signed.as_dict()}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # First pass: accepted.
            r1 = await client.post("/adcp/create_media_buy", content=body, headers=headers)
            assert r1.status_code == 200, r1.text

            # Replay the same headers → the spec says reject with 401 +
            # WWW-Authenticate: Signature error="request_signature_replayed".
            r2 = await client.post("/adcp/create_media_buy", content=body, headers=headers)
            assert r2.status_code == 401, r2.text
            assert r2.json()["error"] == "request_signature_replayed"
            www_auth = r2.headers.get("www-authenticate", "")
            assert 'Signature error="request_signature_replayed"' in www_auth


async def test_fresh_nonce_after_replay_accepted(
    isolated_table: str,
    signing_keypair: tuple[ed25519.Ed25519PrivateKey, dict[str, Any]],
) -> None:
    """After a replay rejection, a newly-signed request MUST be accepted —
    the replay store locks one (keyid, nonce), not the whole keyid.
    """
    private_key, jwk = signing_keypair

    with psycopg_pool.ConnectionPool(TEST_URL, min_size=1, max_size=4) as pool:
        app = _build_app(pool=pool, table=isolated_table, jwk=jwk)
        body = b'{"plan_id":"p1"}'

        def _sign() -> dict[str, str]:
            signed = sign_request(
                method="POST",
                url="http://test/adcp/create_media_buy",
                headers={"Content-Type": "application/json"},
                body=body,
                private_key=private_key,
                key_id="e2e-buyer",
                alg="ed25519",
                signing_profile_version="3.2",
            )
            return {"Content-Type": "application/json", **signed.as_dict()}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            h1 = _sign()
            r1 = await client.post("/adcp/create_media_buy", content=body, headers=h1)
            assert r1.status_code == 200

            # Replay of h1 rejected.
            r2 = await client.post("/adcp/create_media_buy", content=body, headers=h1)
            assert r2.status_code == 401

            # Fresh signature (new nonce under the hood) accepted.
            h2 = _sign()
            assert h2["Signature-Input"] != h1["Signature-Input"]  # sanity
            r3 = await client.post("/adcp/create_media_buy", content=body, headers=h2)
            assert r3.status_code == 200, r3.text


async def test_cross_instance_replay_rejection(
    isolated_table: str,
    signing_keypair: tuple[ed25519.Ed25519PrivateKey, dict[str, Any]],
) -> None:
    """Sim two workers sharing a pool: worker A accepts, worker B rejects the replay.

    This is the core reason Postgres exists in this module — the
    in-memory store can't enforce this. Worker B holds a SEPARATE
    ``PgReplayStore`` instance backed by the same pool, and still sees
    worker A's ``remember`` via the shared table.
    """
    private_key, jwk = signing_keypair

    with psycopg_pool.ConnectionPool(TEST_URL, min_size=2, max_size=6) as pool:
        # Two independent Starlette apps, each with its own
        # PgReplayStore instance but sharing the DB-side table.
        app_a = _build_app(pool=pool, table=isolated_table, jwk=jwk)
        app_b = _build_app(pool=pool, table=isolated_table, jwk=jwk)

        body = b'{"plan_id":"cross"}'
        signed = sign_request(
            method="POST",
            url="http://test/adcp/create_media_buy",
            headers={"Content-Type": "application/json"},
            body=body,
            private_key=private_key,
            key_id="e2e-buyer",
            alg="ed25519",
            signing_profile_version="3.2",
        )
        headers = {"Content-Type": "application/json", **signed.as_dict()}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="http://test"
        ) as client_a:
            r_a = await client_a.post("/adcp/create_media_buy", content=body, headers=headers)
        assert r_a.status_code == 200, r_a.text

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="http://test"
        ) as client_b:
            # Worker B receives the replay. Its own PgReplayStore instance
            # has never called remember(), but the DB-side row from
            # worker A is visible, so seen() returns True → 401.
            r_b = await client_b.post("/adcp/create_media_buy", content=body, headers=headers)
        assert r_b.status_code == 401, r_b.text
        assert r_b.json()["error"] == "request_signature_replayed"
