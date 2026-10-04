"""Configuration errors fail before SQL or network side effects."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from adcp import PgNotificationOutbox
from adcp.webhook_supervisor import RetryPolicy
from adcp.webhooks import WebhookSender


def options() -> dict[str, Any]:
    sender = MagicMock()
    sender._owns_client = True
    sender._allow_private_destinations = False
    sender._timeout = 10.0
    sender.signs_with_rfc9421 = True
    return {
        "pool": MagicMock(),
        "sender": sender,
        "encryption_key": b"x" * 32,
        "delivery_retry_horizon_seconds": 86400,
    }


@pytest.fixture
def pg_constructor() -> None:
    """Constructor configuration checks need the optional implementation dependency."""
    pytest.importorskip("psycopg_pool", reason="constructor checks require adcp[pg]")


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"sender": None}, "exactly one"),
        ({"encryption_key": b"bad"}, "32 bytes"),
        ({"delivery_retry_horizon_seconds": 86399}, "86400"),
        ({"delivery_retry_horizon_seconds": 604801}, "604800"),
        ({"delivery_retry_horizon_seconds": True}, "integer"),
        ({"lease_seconds": 1}, "greater than 1"),
        ({"lease_seconds": True}, "integer"),
        ({"lease_seconds": 14}, "timeout"),
        ({"table": "x;DROP TABLE users"}, "table identifier"),
        ({"encryption_key_id": ""}, "encryption_key_id"),
        ({"decryption_keys": {"default": b"z" * 32}}, "conflicts"),
        ({"retry": RetryPolicy(base_delay_seconds=float("nan"))}, "finite"),
        ({"retry": RetryPolicy(max_delay_seconds=float("inf"))}, "finite"),
        ({"retry": RetryPolicy(base_delay_seconds=10, max_delay_seconds=1)}, "ordered"),
    ],
)
@pytest.mark.usefixtures("pg_constructor")
def test_constructor_validation(changes: dict[str, Any], match: str) -> None:
    config = {**options(), **changes}
    with pytest.raises(ValueError, match=match):
        PgNotificationOutbox(**config)
    config["pool"].connection.assert_not_called()


@pytest.mark.parametrize(
    "name,value",
    [("_owns_client", False), ("_allow_private_destinations", True), ("signs_with_rfc9421", False)],
)
@pytest.mark.usefixtures("pg_constructor")
def test_delivery_sender_contract(name: str, value: bool) -> None:
    config = options()
    setattr(config["sender"], name, value)
    with pytest.raises(ValueError, match="RFC 9421"):
        PgNotificationOutbox(**config)


@pytest.mark.parametrize(
    "headers",
    [
        {"Signature": "override"},
        {"Host": "evil"},
        {"X-Token": "line\r\nbreak"},
        {"Bad Header": "value"},
    ],
)
def test_headers_validated_before_encryption(headers: dict[str, str]) -> None:
    prepared = WebhookSender.prepare_raw(
        url="https://buyer.example/hooks",
        payload={"notification_type": "extension.changed"},
        extra_headers=headers,
    )
    with pytest.raises(ValueError):
        PgNotificationOutbox._protected(prepared)


def test_public_export_and_no_task_api() -> None:
    from adcp.notification_outbox_pg import PgNotificationOutbox as Direct

    assert PgNotificationOutbox is Direct
    assert not hasattr(PgNotificationOutbox, "enqueue_terminal")


def test_constructor_without_pg_preserves_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing PG remains an ImportError, including in a PG-enabled test runner."""
    config = options()
    monkeypatch.setitem(sys.modules, "psycopg_pool", None)
    with pytest.raises(
        ImportError,
        match=r"pip install 'adcp\[pg\]'",
    ) as error:
        PgNotificationOutbox(**config)
    assert isinstance(error.value.__cause__, ImportError)
    config["pool"].connection.assert_not_called()
