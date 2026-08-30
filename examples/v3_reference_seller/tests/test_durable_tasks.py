"""Configuration tests for the production task/outbox example."""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from src.durable_tasks import DurableTaskWiring  # noqa: E402
from src.workflow_queue import PgWorkflowQueue  # noqa: E402


def _write_signing_key(path: Path) -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _environment(key_path: Path) -> dict[str, str]:
    return {
        "ADCP_ENV": "production",
        "ADCP_TASK_DATABASE_URL": "postgresql://postgres@localhost/adcp",
        "ADCP_TASK_WEBHOOK_ENCRYPTION_KEY": base64.b64encode(b"k" * 32).decode(),
        "ADCP_WEBHOOK_SIGNING_KEY_PATH": str(key_path),
        "ADCP_WEBHOOK_SIGNING_KEY_ID": "reference-webhook-key",
        "ADCP_WEBHOOK_SIGNING_ALG": "ed25519",
        "ADCP_TASK_WEBHOOK_RETRY_HORIZON_SECONDS": "172800",
    }


def test_local_development_may_omit_durable_bundle() -> None:
    assert DurableTaskWiring.from_env({"ADCP_ENV": "development"}) is None


def test_production_fails_before_boot_when_bundle_is_missing() -> None:
    with pytest.raises(ValueError, match="ADCP_TASK_DATABASE_URL"):
        DurableTaskWiring.from_env({"ADCP_ENV": "production"})


def test_partial_bundle_lists_every_missing_field() -> None:
    with pytest.raises(ValueError) as exc_info:
        DurableTaskWiring.from_env(
            {
                "ADCP_ENV": "development",
                "ADCP_TASK_DATABASE_URL": "postgresql://postgres@localhost/adcp",
            }
        )
    message = str(exc_info.value)
    assert "ADCP_TASK_WEBHOOK_ENCRYPTION_KEY" in message
    assert "ADCP_WEBHOOK_SIGNING_KEY_PATH" in message
    assert "ADCP_WEBHOOK_SIGNING_KEY_ID" in message


def test_encryption_key_must_decode_to_32_bytes(tmp_path: Path) -> None:
    key_path = tmp_path / "webhook-signing.pem"
    _write_signing_key(key_path)
    environ = _environment(key_path)
    environ["ADCP_TASK_WEBHOOK_ENCRYPTION_KEY"] = base64.b64encode(b"short").decode()

    with pytest.raises(ValueError, match="exactly 32 bytes"):
        DurableTaskWiring.from_env(environ)


def test_retry_horizon_is_validated_before_boot(tmp_path: Path) -> None:
    key_path = tmp_path / "webhook-signing.pem"
    _write_signing_key(key_path)
    environ = _environment(key_path)
    environ["ADCP_TASK_WEBHOOK_RETRY_HORIZON_SECONDS"] = "60"

    with pytest.raises(ValueError, match="between 86400 and 604800"):
        DurableTaskWiring.from_env(environ)


def test_workflow_retry_backoff_is_exponential_and_capped() -> None:
    queue = PgWorkflowQueue(
        pool=MagicMock(),
        registry=MagicMock(),
        max_attempts=5,
        retry_base_seconds=2,
        max_retry_seconds=5,
    )

    assert queue._retry_delay(1) == 2  # noqa: SLF001
    assert queue._retry_delay(2) == 4  # noqa: SLF001
    assert queue._retry_delay(3) == 5  # noqa: SLF001


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_attempts": 0}, "max_attempts"),
        ({"retry_base_seconds": 0}, "retry_base_seconds"),
        (
            {"retry_base_seconds": 2, "max_retry_seconds": 1},
            "max_retry_seconds",
        ),
    ],
)
def test_workflow_retry_configuration_is_validated(
    kwargs: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        PgWorkflowQueue(pool=MagicMock(), registry=MagicMock(), **kwargs)


def test_complete_bundle_builds_atomic_registry_outbox_pair(tmp_path: Path) -> None:
    key_path = tmp_path / "webhook-signing.pem"
    _write_signing_key(key_path)

    wiring = DurableTaskWiring.from_env(_environment(key_path))

    assert wiring is not None
    try:
        assert wiring.registry.task_webhook_outbox is wiring.outbox
        assert wiring.workflow_queue._registry is wiring.registry  # noqa: SLF001
        assert wiring.registry.atomic_task_webhook_outbox is True
        assert wiring.idempotency_backend is not None
        assert wiring.idempotency_backend._lock_pool is wiring.lock_pool  # noqa: SLF001
        assert wiring.idempotency is not None
        assert wiring.idempotency.raise_on_persist_error is True
        assert wiring.retry_horizon_seconds == 172800
        assert wiring.signing_algorithm == "ed25519"
    finally:
        asyncio.run(wiring.shutdown())


def test_worker_bundle_omits_unused_idempotency_pool(tmp_path: Path) -> None:
    key_path = tmp_path / "webhook-signing.pem"
    _write_signing_key(key_path)

    wiring = DurableTaskWiring.from_env(
        _environment(key_path),
        include_idempotency=False,
    )

    assert wiring is not None
    try:
        assert wiring.lock_pool is None
        assert wiring.idempotency_backend is None
        assert wiring.idempotency is None
    finally:
        asyncio.run(wiring.shutdown())


def test_complete_bundle_passes_capability_wiring_preflight(tmp_path: Path) -> None:
    from src.platform import V3ReferenceSeller

    from adcp.decisioning import create_adcp_server_from_platform

    key_path = tmp_path / "webhook-signing.pem"
    _write_signing_key(key_path)
    wiring = DurableTaskWiring.from_env(_environment(key_path))
    assert wiring is not None
    assert wiring.idempotency is not None
    executor = None
    try:
        seller = V3ReferenceSeller(
            sessionmaker=lambda: None,  # type: ignore[arg-type]
            upstream_api_key="test-key",
            mock_upstream_url=None,
            webhook_signing_alg=wiring.signing_algorithm,
            webhook_retry_horizon_seconds=wiring.retry_horizon_seconds,
            idempotency=wiring.idempotency,
        )

        handler, executor, registry = create_adcp_server_from_platform(
            seller,
            registry=wiring.registry,
        )
        assert handler.get_advertised_tools()
        assert registry is wiring.registry
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
        asyncio.run(wiring.shutdown())


@pytest.mark.asyncio
async def test_startup_failure_closes_every_resource() -> None:
    pool = MagicMock(open=AsyncMock(), close=AsyncMock())
    lock_pool = MagicMock(open=AsyncMock(), close=AsyncMock())
    sender = MagicMock(aclose=AsyncMock())
    registry = MagicMock(create_schema=AsyncMock(side_effect=RuntimeError("DDL failed")))
    outbox = MagicMock(create_schema=AsyncMock())
    workflow_queue = MagicMock(create_schema=AsyncMock())
    backend = MagicMock(create_schema=AsyncMock())
    wiring = DurableTaskWiring(
        pool=pool,
        lock_pool=lock_pool,
        sender=sender,
        outbox=outbox,
        registry=registry,
        workflow_queue=workflow_queue,
        idempotency_backend=backend,
        idempotency=None,
        retry_horizon_seconds=86400,
        signing_algorithm="ed25519",
    )

    with pytest.raises(RuntimeError, match="DDL failed"):
        await wiring.startup()

    sender.aclose.assert_awaited_once()
    lock_pool.close.assert_awaited_once()
    pool.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_cancellation_closes_every_resource_and_reraises() -> None:
    pool = MagicMock(open=AsyncMock(), close=AsyncMock())
    lock_pool = MagicMock(open=AsyncMock(), close=AsyncMock())
    sender_close_started = asyncio.Event()
    release_sender_close = asyncio.Event()

    async def close_sender() -> None:
        sender_close_started.set()
        await release_sender_close.wait()

    sender = MagicMock(aclose=AsyncMock(side_effect=close_sender))
    schema_started = asyncio.Event()

    async def wait_forever() -> None:
        schema_started.set()
        await asyncio.Event().wait()

    registry = MagicMock(create_schema=AsyncMock(side_effect=wait_forever))
    wiring = DurableTaskWiring(
        pool=pool,
        lock_pool=lock_pool,
        sender=sender,
        outbox=MagicMock(create_schema=AsyncMock()),
        registry=registry,
        workflow_queue=MagicMock(create_schema=AsyncMock()),
        idempotency_backend=MagicMock(create_schema=AsyncMock()),
        idempotency=None,
        retry_horizon_seconds=86400,
        signing_algorithm="ed25519",
    )

    startup = asyncio.create_task(wiring.startup())
    await schema_started.wait()
    startup.cancel()
    await sender_close_started.wait()
    startup.cancel()
    release_sender_close.set()

    done, pending = await asyncio.wait({startup})
    assert done == {startup}
    assert not pending
    assert startup.cancelled()

    sender.aclose.assert_awaited_once()
    lock_pool.close.assert_awaited_once()
    pool.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_continues_after_close_failure() -> None:
    pool = MagicMock(close=AsyncMock())
    lock_pool = MagicMock(close=AsyncMock())
    sender = MagicMock(aclose=AsyncMock(side_effect=RuntimeError("sender close failed")))
    wiring = DurableTaskWiring(
        pool=pool,
        lock_pool=lock_pool,
        sender=sender,
        outbox=MagicMock(),
        registry=MagicMock(),
        workflow_queue=MagicMock(),
        idempotency_backend=None,
        idempotency=None,
        retry_horizon_seconds=86400,
        signing_algorithm="ed25519",
    )

    with pytest.raises(RuntimeError, match="sender close failed"):
        await wiring.shutdown()

    lock_pool.close.assert_awaited_once()
    pool.close.assert_awaited_once()
