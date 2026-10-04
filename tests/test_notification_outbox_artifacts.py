"""Installed sdist/wheel contain DDL and expose typed notification APIs."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, cwd: Path, timeout: int = 180) -> str:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    return result.stdout


@pytest.fixture(scope="module")
def installation(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    directory = tmp_path_factory.mktemp("notification-distribution")
    dist = directory / "dist"
    run([sys.executable, "-m", "build", "--outdir", str(dist)], cwd=ROOT)
    sdist = next(dist.glob("*.tar.gz"))
    wheel = next(dist.glob("*.whl"))
    with tarfile.open(sdist) as archive:
        assert any(
            name.endswith("/src/adcp/notification_outbox.sql") for name in archive.getnames()
        )
    with zipfile.ZipFile(wheel) as archive:
        assert "adcp/notification_outbox.sql" in archive.namelist()
        assert "adcp/py.typed" in archive.namelist()
        assert "adcp/types/v32.pyi" in archive.namelist()
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for isolated artifact installation"
    venv = directory / "installed"
    run([uv, "venv", "--python", sys.executable, str(venv)], cwd=directory)
    python = venv / "bin" / "python"
    run(
        [uv, "pip", "install", "--python", str(python), str(wheel) + "[pg]", "mypy==1.20.2"],
        cwd=directory,
    )
    return directory, python


def test_installed_public_import_schema_and_typing(installation: tuple[Path, Path]) -> None:
    directory, python = installation
    runtime = """
        from importlib.resources import files
        from pathlib import Path
        import adcp
        from adcp import PgNotificationOutbox, WebhookSender

        assert "site-packages" in adcp.__file__
        assert (
            "notification-outbox-v1"
            in files("adcp").joinpath("notification_outbox_pg.py").read_text()
        )
        assert (
            "CREATE TABLE"
            in files("adcp").joinpath("notification_outbox.sql").read_text()
        )
        prepared = WebhookSender.prepare_principal_changed(
            url="https://buyer.example/hooks",
            payload={
                "notification_id": "change_1",
                "subscriber_id": "buyer",
                "agent_url": "https://seller.example",
                "changed_at": "2026-10-02T12:00:00Z",
                "reason": "other",
            },
        )
        assert prepared.body and prepared.idempotency_key
        assert PgNotificationOutbox.__module__ == "adcp.notification_outbox_pg"
    """
    run([str(python), "-c", textwrap.dedent(runtime)], cwd=directory)
    fixture = directory / "adopter.py"
    fixture.write_text((ROOT / "tests/type_checks/notification_outbox.py").read_text())
    config = directory / "mypy.ini"
    config.write_text("[mypy]\nplugins = adcp.types.mypy_plugin\n")
    output = run(
        [
            str(python),
            "-m",
            "mypy",
            "--strict",
            "--follow-imports=silent",
            "--config-file",
            str(config),
            str(fixture),
        ],
        cwd=directory,
    )
    assert "Success" in output


@pytest.mark.skipif(
    not os.environ.get("ADCP_PG_TEST_URL"), reason="private PostgreSQL URL required"
)
def test_installed_database_publication_and_process_restart(
    installation: tuple[Path, Path],
) -> None:
    directory, python = installation
    # Each process imports only the installed artifact. Work survives its publisher's exit.
    publish = """
        import asyncio, json, os, secrets
        from pathlib import Path
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from psycopg_pool import AsyncConnectionPool
        from adcp import PgNotificationOutbox, WebhookSender


        async def main():
            sender = WebhookSender(
                private_key=Ed25519PrivateKey.generate(),
                key_id="process-key",
                alg="ed25519",
            )
            async with AsyncConnectionPool(
                os.environ["ADCP_PG_TEST_URL"], open=False
            ) as pool:
                table = "test_installed_notifications_" + secrets.token_hex(4)
                outbox = PgNotificationOutbox(
                    pool=pool,
                    sender=sender,
                    encryption_key=b"x" * 32,
                    delivery_retry_horizon_seconds=86400,
                    table=table,
                )
                await outbox.create_schema()
                prepared = WebhookSender.prepare_principal_changed(
                    url="https://buyer.example/hooks",
                    payload={
                        "notification_id": "change_1",
                        "subscriber_id": "buyer",
                        "agent_url": "https://seller.example",
                        "changed_at": "2026-10-02T12:00:00Z",
                        "reason": "other",
                    },
                )
                async with pool.connection() as conn:
                    async with conn.transaction():
                        row_id = await outbox.enqueue_prepared(
                            conn,
                            prepared,
                            notification_type="principal.changed",
                            caller_scope_id="caller-1",
                        )
                Path("publication.json").write_text(
                    json.dumps(
                        {
                            "table": table,
                            "row_id": row_id,
                            "body": prepared.body.hex(),
                            "key": prepared.idempotency_key,
                        }
                    )
                )
            await sender.aclose()


        asyncio.run(main())
    """
    deliver = """
        import asyncio, json, os
        from pathlib import Path
        import httpx
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from psycopg_pool import AsyncConnectionPool
        from adcp import PgNotificationOutbox, WebhookSender
        import adcp.webhook_sender as transport

        state = json.loads(Path("publication.json").read_text())
        accepted = []


        def receive(request):
            assert request.content.hex() == state["body"]
            assert json.loads(request.content)["idempotency_key"] == state["key"]
            accepted.append(request.content)
            return httpx.Response(200)


        transport.build_async_ip_pinned_transport = (
            lambda *a, **k: httpx.MockTransport(receive)
        )


        async def main():
            sender = WebhookSender(
                private_key=Ed25519PrivateKey.generate(),
                key_id="process-key",
                alg="ed25519",
            )
            async with AsyncConnectionPool(
                os.environ["ADCP_PG_TEST_URL"], open=False
            ) as pool:
                outbox = PgNotificationOutbox(
                    pool=pool,
                    sender=sender,
                    encryption_key=b"x" * 32,
                    delivery_retry_horizon_seconds=86400,
                    table=state["table"],
                )
                try:
                    assert await outbox.process_one()
                    assert not await outbox.process_one()
                    assert len(accepted) == 1
                    async with pool.connection() as conn:
                        row = await (
                            await conn.execute(
                                "SELECT state FROM "
                                + state["table"]
                                + " WHERE id=%s",
                                (state["row_id"],),
                            )
                        ).fetchone()
                        assert row[0] == "delivered"
                finally:
                    async with pool.connection() as conn:
                        await conn.execute("DROP TABLE " + state["table"])
            await sender.aclose()


        asyncio.run(main())
    """
    run([str(python), "-c", textwrap.dedent(publish)], cwd=directory)
    run([str(python), "-c", textwrap.dedent(deliver)], cwd=directory)
