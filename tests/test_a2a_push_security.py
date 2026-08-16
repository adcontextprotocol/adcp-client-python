"""A2A push destination policy and SQLAlchemy example parity tests."""

from __future__ import annotations

import socket

import pytest
from a2a.auth.user import UnauthenticatedUser, User
from a2a.server.context import ServerCallContext
from a2a.types import TaskPushNotificationConfig

from adcp.server.a2a_push_security import (
    normalize_allowed_push_hosts,
    resolve_push_destination_settings,
    validate_push_notification_url,
)


class _AuthenticatedUser(User):
    def __init__(self, user_name: str) -> None:
        self._user_name = user_name

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._user_name


@pytest.fixture(autouse=True)
def _resolve_example_hosts(monkeypatch: pytest.MonkeyPatch):
    original = socket.getaddrinfo

    def resolve(host: str, port: object, *args: object, **kwargs: object):
        if host.rstrip(".").endswith(".example") or host.rstrip(".") == "example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return original(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def test_push_url_canonicalizes_idna_case_and_trailing_dot() -> None:
    allowed = normalize_allowed_push_hosts(["BÜCHER.Example."])
    assert allowed == frozenset({"xn--bcher-kva.example"})
    validate_push_notification_url(
        "https://xn--bcher-kva.EXAMPLE./callback",
        allowed_hosts=frozenset({"BÜCHER.Example."}),
    )


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://user@example.com/hook", "userinfo"),
        ("https://user:password@example.com/hook", "userinfo"),
        ("https://example.com:not-a-port/hook", "Port could not be cast"),
        ("http://example.com/hook", "https"),
    ],
)
def test_push_url_rejects_unsafe_authority_forms(url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_push_notification_url(
            url,
            allowed_hosts=normalize_allowed_push_hosts(["example.com"]),
        )


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "::1", "fe80::1"])
def test_push_url_rejects_private_ipv4_and_ipv6_literals(host: str) -> None:
    rendered_host = f"[{host}]" if ":" in host else host
    with pytest.raises(ValueError, match="blocked|private|SSRF"):
        validate_push_notification_url(
            f"https://{rendered_host}/hook",
            allowed_hosts=normalize_allowed_push_hosts([host]),
        )


@pytest.mark.parametrize(
    "host",
    [
        "192.88.99.1",
        "192.31.196.1",
        "192.52.193.1",
        "192.175.48.1",
        "2001:20::1",
        "64:ff9b::7f00:1",
    ],
)
def test_push_url_rejects_globally_classified_reserved_literals(host: str) -> None:
    """Shared SSRF policy covers ranges ``ipaddress.is_global`` misses."""
    rendered_host = f"[{host}]" if ":" in host else host
    with pytest.raises(ValueError, match="blocked|reserved|SSRF"):
        validate_push_notification_url(
            f"https://{rendered_host}/hook",
            allowed_hosts=normalize_allowed_push_hosts([host]),
        )


def test_push_url_allows_nonstandard_tls_port_by_default() -> None:
    validate_push_notification_url(
        "https://example.com:9443/hook",
        allowed_hosts=frozenset({"example.com"}),
    )


def test_public_https_mode_accepts_unlisted_public_destination() -> None:
    validate_push_notification_url("https://buyer-callback.example/hook")


def test_public_https_mode_still_rejects_private_destination() -> None:
    with pytest.raises(ValueError, match="blocked|private|SSRF"):
        validate_push_notification_url("https://127.0.0.1/hook")


def test_explicit_empty_allowlist_denies_every_destination() -> None:
    with pytest.raises(ValueError, match="allowed_destination_hosts"):
        validate_push_notification_url(
            "https://buyer-callback.example/hook",
            allowed_hosts=frozenset(),
        )


@pytest.mark.parametrize(
    ("mode", "hosts", "enabled", "allowed"),
    [
        ("disabled", (), False, None),
        ("public_https", (), True, None),
        ("allowlist", ("CALLBACK.Example.",), True, frozenset({"callback.example"})),
    ],
)
def test_push_destination_modes(
    mode: str,
    hosts: tuple[str, ...],
    enabled: bool,
    allowed: frozenset[str] | None,
) -> None:
    settings = resolve_push_destination_settings(mode, hosts)
    assert settings.enabled is enabled
    assert settings.allowed_hosts == allowed


@pytest.mark.parametrize(
    ("mode", "hosts", "message"),
    [
        ("allowlist", (), "requires at least one"),
        ("public_https", ("callback.example",), "require A2A_PUSH_MODE=allowlist"),
        ("disabled", ("callback.example",), "set while A2A_PUSH_MODE=disabled"),
        ("anything", (), "must be one of"),
    ],
)
def test_invalid_push_destination_mode_configuration(
    mode: str,
    hosts: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_push_destination_settings(mode, hosts)


def test_sqlite_example_disabled_mode_omits_push_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import examples.a2a_db_tasks as example

    captured: dict[str, object] = {}
    monkeypatch.delenv("A2A_PUSH_MODE", raising=False)
    monkeypatch.delenv("A2A_PUSH_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(example, "SqliteTaskStore", lambda **_kwargs: object())
    monkeypatch.setattr(
        example,
        "SqlitePushNotificationConfigStore",
        lambda **_kwargs: pytest.fail("disabled mode must not construct a push store"),
    )
    monkeypatch.setattr(
        example,
        "serve",
        lambda *_args, **kwargs: captured.update(kwargs),
    )

    example.main()

    assert captured["push_config_store"] is None


@pytest.mark.asyncio
async def test_sqlite_push_store_uses_a2a_context_for_tenant_isolation(tmp_path) -> None:
    import examples.a2a_db_tasks as example

    store = example.SqlitePushNotificationConfigStore(
        tmp_path / "push.db",
        allowed_destination_hosts=frozenset({"callback.example"}),
    )
    tenant_a = ServerCallContext(user=_AuthenticatedUser("tenant-a"))
    tenant_b = ServerCallContext(user=_AuthenticatedUser("tenant-b"))
    config = TaskPushNotificationConfig(
        id="cfg-1",
        task_id="task-shared",
        url="https://callback.example/hook",
    )

    await store.set_info("task-shared", config, tenant_a)
    assert [item.id for item in await store.get_info("task-shared", tenant_a)] == ["cfg-1"]
    assert await store.get_info("task-shared", tenant_b) == []

    await store.delete_info("task-shared", tenant_b)
    assert [item.id for item in await store.get_info("task-shared", tenant_a)] == ["cfg-1"]


@pytest.mark.asyncio
async def test_sqlite_explicit_unauthenticated_context_cannot_inherit_ambient_scope(
    tmp_path,
) -> None:
    import examples.a2a_db_tasks as example

    store = example.SqlitePushNotificationConfigStore(
        tmp_path / "push.db",
        scope_provider=lambda: "tenant-a",
        allowed_destination_hosts=frozenset({"callback.example"}),
    )
    tenant_a = ServerCallContext(user=_AuthenticatedUser("tenant-a"))
    unauthenticated = ServerCallContext(user=UnauthenticatedUser())
    config = TaskPushNotificationConfig(
        id="cfg-1",
        task_id="task-shared",
        url="https://callback.example/hook",
    )

    await store.set_info("task-shared", config, tenant_a)
    assert await store.get_info("task-shared", unauthenticated) == []


@pytest.mark.asyncio
async def test_sqlalchemy_push_store_matches_a2a_v1_set_get_delete_contract() -> None:
    import examples.a2a_sqlalchemy_tasks as example

    session_factory = example.build_engine_and_sessions(database_url="sqlite:///:memory:")
    store = example.SqlAlchemyPushNotificationConfigStore(
        session_factory,
        allowed_destination_hosts=frozenset({"callback.example"}),
    )
    tenant_a = ServerCallContext(user=_AuthenticatedUser("tenant-a"))
    tenant_b = ServerCallContext(user=_AuthenticatedUser("tenant-b"))
    first = TaskPushNotificationConfig(
        id="cfg-1",
        task_id="task-1",
        url="https://callback.example/first",
    )
    second = TaskPushNotificationConfig(
        id="cfg-2",
        task_id="task-1",
        url="https://callback.example/second",
    )
    await store.set_info("task-1", first, tenant_a)
    await store.set_info("task-1", second, tenant_a)

    stored = await store.get_info("task-1", tenant_a)
    assert {config.id for config in stored} == {"cfg-1", "cfg-2"}
    assert {config.url for config in stored} == {
        "https://callback.example/first",
        "https://callback.example/second",
    }
    assert await store.get_info("task-1", tenant_b) == []

    await store.delete_info("task-1", tenant_b)
    assert len(await store.get_info("task-1", tenant_a)) == 2

    await store.delete_info("task-1", tenant_a, "cfg-1")
    remaining = await store.get_info("task-1", tenant_a)
    assert [config.id for config in remaining] == ["cfg-2"]

    await store.delete_info("task-1", tenant_a)
    assert await store.get_info("task-1", tenant_a) == []


@pytest.mark.asyncio
async def test_sqlalchemy_explicit_unauthenticated_context_cannot_inherit_ambient_scope() -> None:
    import examples.a2a_sqlalchemy_tasks as example

    session_factory = example.build_engine_and_sessions(database_url="sqlite:///:memory:")
    store = example.SqlAlchemyPushNotificationConfigStore(
        session_factory,
        allowed_destination_hosts=frozenset({"callback.example"}),
    )
    tenant_a = ServerCallContext(user=_AuthenticatedUser("tenant-a"))
    unauthenticated = ServerCallContext(user=UnauthenticatedUser())
    config = TaskPushNotificationConfig(
        id="cfg-1",
        task_id="task-shared",
        url="https://callback.example/hook",
    )
    token = example._push_config_scope.set("tenant-a")
    try:
        await store.set_info("task-shared", config, tenant_a)
        assert [item.id for item in await store.get_info("task-shared", None)] == ["cfg-1"]
        assert await store.get_info("task-shared", unauthenticated) == []
    finally:
        example._push_config_scope.reset(token)


@pytest.mark.asyncio
async def test_sqlalchemy_push_store_defaults_to_public_https_destinations() -> None:
    import examples.a2a_sqlalchemy_tasks as example

    session_factory = example.build_engine_and_sessions(database_url="sqlite:///:memory:")
    store = example.SqlAlchemyPushNotificationConfigStore(session_factory)
    scope_token = example._push_config_scope.set("tenant-a")
    try:
        await store.set_info(
            "task-1",
            TaskPushNotificationConfig(
                task_id="task-1",
                url="https://callback.example/hook",
            ),
            ServerCallContext(user=UnauthenticatedUser()),
        )
        stored = await store.get_info("task-1", ServerCallContext(user=UnauthenticatedUser()))
        assert [item.url for item in stored] == ["https://callback.example/hook"]
    finally:
        example._push_config_scope.reset(scope_token)
