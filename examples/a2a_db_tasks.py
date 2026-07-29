"""Example: A2A agent with durable, scope-isolated SQLite-backed
``TaskStore`` + ``PushNotificationConfigStore``.

A2A's defaults (``InMemoryTaskStore`` + ``InMemoryPushNotificationConfigStore``)
are single-process and non-durable — fine for demos but tasks and
push-notif subscriptions vanish on restart. Production agents need
durable stores so long-running operations survive process restarts and
can be resumed by whichever worker picks up the request.

This example wires up a minimal SQLite-backed store that implements
``a2a.server.tasks.task_store.TaskStore``. SQLite is the right reference
target: it's in the stdlib, needs no infrastructure, and the SQL
pattern translates directly to Postgres / MySQL / etc. for production.

**Security model — tenant-scoped lookups.** The ``TaskStore`` ABC passes
a ``ServerCallContext`` carrying the authenticated user on every call.
**Ignoring it is a cross-tenant data leak**: any principal that learns
(or guesses) a task id owned by another tenant retrieves that tenant's
full task — including history, artifacts, and any caller-supplied PII
in ``Message.parts``. This store derives a ``scope`` column from
``context.user.user_name`` and filters every read/write by it, so a
request arriving with a different principal never sees another
tenant's task. Sellers with richer identity (a typed ``tenant_id``,
organization IDs, etc.) should override ``_scope_from_context`` to
return *their* scope key — the lookup filter then follows automatically.

**Security model — push-notification config store adds two threats
tenant-scoping alone does NOT address:**

1. **SSRF via webhook URLs.** Clients supply
   ``PushNotificationConfig.url`` when subscribing to task progress;
   a2a-sdk's push-notif sender POSTs the full task JSON to that URL
   with no built-in validation. An attacker can register
   ``url=http://169.254.169.254/…`` (cloud metadata),
   ``http://localhost:5432/`` (internal services), link-local IPs,
   etc. The store rejects URLs unless they use HTTPS and their canonical
   hostname is publicly routable. Closed deployments can additionally require
   it to appear in ``allowed_destination_hosts``. This storage-time gate does
   not replace sender-side DNS resolution checks and IP-pinned connections on
   every send; implement a custom ``PushNotificationSender`` for that
   production boundary.
2. **Webhook secrets stored plaintext.**
   ``PushNotificationConfig.authentication.credentials`` and
   ``PushNotificationConfig.token`` are bearer tokens / shared
   secrets clients pass for authenticated callbacks. The reference
   impl serialises them to JSON under chmod 0o600 — safe on a
   single-user host, but backups, Docker bind mounts with wrong
   umask, DB migrations, and shared-volume mounts all lose that
   guarantee. Production stores should envelope-encrypt or redact
   these fields, or move them to a secrets backend and persist only
   opaque references.

**Not production-ready.** Remaining gaps for real deployments:

- Postgres/MySQL + async driver (asyncpg / aiomysql).
- Transactional atomicity with the handler's business writes —
  same-engine transaction so a crash between "handler success" and
  "task save" doesn't duplicate side effects.
- Connection pooling.
- Row-level TTL / garbage collection for completed tasks.
- Optimistic concurrency: ``INSERT OR REPLACE`` below is
  last-writer-wins. Two in-flight ``save()`` calls on the same task
  interleave with no version check; a slow ``save(working)`` landing
  after a fast ``save(completed)`` will revert the state. Production
  stores need ``WHERE updated_at < ?`` guards or a version column.
- ``Task.model_dump_json`` includes ``history`` (buyer-supplied
  messages, artifact metadata). Persisting it makes plaintext
  conversation content land on disk — protect the database file
  (encryption at rest, backup access control) and consider
  field-level redaction before writing.
- Shared-host file permissions: this example sets the SQLite file
  mode to 0o600 on first creation so a co-tenant process on the same
  machine can't read it. A migration that recreates the file inherits
  that; replace or harden it if you need stricter access rules.

Run::

    A2A_PUSH_MODE=public_https \
      uv run python examples/a2a_db_tasks.py

    A2A_PUSH_MODE=allowlist \
      A2A_PUSH_ALLOWED_HOSTS=callback.example \
      uv run python examples/a2a_db_tasks.py

The default mode is ``disabled``: the example omits the push-config store and
does not advertise push support. ``public_https`` accepts any HTTPS callback
that passes DNS and reserved-range SSRF validation. ``allowlist`` adds an exact
hostname restriction using the comma-separated canonical hostnames.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import uuid
import warnings
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from a2a import types as pb
from a2a.server.context import ServerCallContext
from a2a.server.tasks.push_notification_config_store import (
    PushNotificationConfigStore,
)
from a2a.server.tasks.task_store import TaskStore

# 1.0 folded ``PushNotificationConfig`` into
# :class:`a2a.types.TaskPushNotificationConfig`; the example's
# :meth:`~SqlitePushNotificationConfigStore.set_info` signature still
# accepts a notification config object — the caller passes a
# :class:`TaskPushNotificationConfig` instance.
from a2a.types import Task
from a2a.types import TaskPushNotificationConfig as PushNotificationConfig
from google.protobuf.json_format import MessageToJson, Parse

from adcp.server import ADCPHandler, serve
from adcp.server.a2a_push_security import (
    normalize_allowed_push_hosts,
    resolve_push_destination_settings,
    scope_from_server_context,
    validate_a2a_push_notification_url,
)
from adcp.server.responses import capabilities_response, products_response

_ANONYMOUS_SCOPE = "__anonymous__"
"""Scope value used when a request arrives without an authenticated
principal. Unauthenticated tasks all share this scope — they can't
cross-contaminate with authenticated tasks because the scope column
is part of every WHERE clause."""


def _scope_from_server_context(context: ServerCallContext | None) -> str:
    """Derive a verified principal scope from an a2a-sdk call context."""
    return scope_from_server_context(context) or _ANONYMOUS_SCOPE


# ----------------------------------------------------------------------
# SQLite-backed TaskStore
# ----------------------------------------------------------------------


class SqliteTaskStore(TaskStore):
    """Durable A2A ``TaskStore`` backed by a single SQLite file.

    Tasks are serialised as JSON and scoped by an authenticated
    principal derived from ``ServerCallContext.user.user_name``. Every
    read and delete filters on that scope so a request for a task the
    current principal doesn't own returns ``None`` (not the task).

    SQLite connections are opened per-operation (not pool; not
    long-lived) because sqlite3 connections are not safe to share
    across threads. Fine for this reference impl; swap in an async
    pool (asyncpg, aiomysql) for multi-node production.
    """

    def __init__(self, db_path: str | Path = "a2a_tasks.db") -> None:
        self._db_path = str(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        path = Path(self._db_path)
        first_create = not path.exists()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS a2a_tasks (
                    scope      TEXT NOT NULL,
                    task_id    TEXT NOT NULL,
                    task_json  TEXT NOT NULL,
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                    PRIMARY KEY (scope, task_id)
                )
                """
            )
        # Only tighten permissions on first creation, so operators who
        # manage permissions via umask / ACLs externally aren't
        # clobbered on every process start.
        if first_create:
            with contextlib.suppress(OSError):
                os.chmod(self._db_path, 0o600)

    def _scope_from_context(self, context: ServerCallContext | None) -> str:
        """Derive the per-principal scope key from the request context.

        Defaults to ``user.user_name`` when an authenticated user is
        present, falling back to ``_ANONYMOUS_SCOPE`` for unauthenticated
        requests. Override for richer identity — e.g. return
        ``f"{tenant_id}:{principal_id}"`` when you carry an explicit
        tenant in ``context.state``. The scope is used as a partition
        key on every read/write; anything you don't include here
        *cannot* be enforced by the store.
        """
        return _scope_from_server_context(context)

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[sqlite3.Connection]:
        # SQLite connections aren't safe across threads. Open a fresh
        # connection per operation and commit-on-success / rollback-on-error
        # so a port to psycopg / aiomysql doesn't silently leak partial
        # writes — SQLite auto-rolls-back on close, but most other drivers
        # don't.
        conn = sqlite3.connect(self._db_path)
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        scope = self._scope_from_context(context)
        # Proto messages serialize via ``MessageToJson``; fields stay in
        # the canonical proto JSON shape so a different reader on the
        # same DB (gRPC bridge, future 1.x client) sees the same bytes.
        task_json = MessageToJson(task, preserving_proto_field_name=True)
        async with self._conn() as conn:
            # NOTE: ``INSERT OR REPLACE`` is last-writer-wins. Production
            # stores should guard with a version column or
            # ``WHERE updated_at < ?`` to prevent concurrent updates
            # silently reverting task state (e.g. 'completed' → 'working').
            conn.execute(
                "INSERT OR REPLACE INTO a2a_tasks "
                "(scope, task_id, task_json, updated_at) "
                "VALUES (?, ?, ?, strftime('%s','now'))",
                (scope, task.id, task_json),
            )

    async def get(self, task_id: str, context: ServerCallContext | None = None) -> Task | None:
        scope = self._scope_from_context(context)
        async with self._conn() as conn:
            row = conn.execute(
                "SELECT task_json FROM a2a_tasks WHERE scope = ? AND task_id = ?",
                (scope, task_id),
            ).fetchone()
        if row is None:
            return None
        return Parse(row[0], pb.Task())

    async def delete(self, task_id: str, context: ServerCallContext | None = None) -> None:
        scope = self._scope_from_context(context)
        async with self._conn() as conn:
            conn.execute(
                "DELETE FROM a2a_tasks WHERE scope = ? AND task_id = ?",
                (scope, task_id),
            )

    async def list(
        self,
        params: pb.ListTasksRequest | None = None,
        context: ServerCallContext | None = None,
    ) -> pb.ListTasksResponse:
        """Return tasks owned by the current scope.

        ``params.page_token`` / ``params.page_size`` support is left as
        an exercise for the seller — the reference impl returns every
        task in one response to keep the example compact. Real deployments
        should implement keyset pagination on ``(updated_at, task_id)``.
        """
        scope = self._scope_from_context(context)
        async with self._conn() as conn:
            rows = conn.execute(
                "SELECT task_json FROM a2a_tasks WHERE scope = ? ORDER BY updated_at DESC",
                (scope,),
            ).fetchall()
        tasks = [Parse(row[0], pb.Task()) for row in rows]
        return pb.ListTasksResponse(tasks=tasks)


# ----------------------------------------------------------------------
# SQLite-backed PushNotificationConfigStore
# ----------------------------------------------------------------------

# Three things a durable push-notification config store MUST do in
# production — the reference impl below only enforces isolation.
# Sellers adapting this to their stack need to layer on the other two:
#
# 1. Validate the client-supplied ``url`` against an allowlist before
#    persisting. a2a-sdk's push-notif sender POSTs full task JSON to
#    whatever URL is stored — no built-in allowlist. An unvalidated
#    store is a cloud-metadata SSRF and a task-content exfiltration
#    primitive. Reject non-https, reject private/link-local IPs, reject
#    hosts outside your egress allowlist. ``ssrf_guard()``-style
#    helper below is a starting point.
# 2. Treat ``PushNotificationConfig.authentication.credentials`` and
#    ``PushNotificationConfig.token`` as secrets at rest. The reference
#    impl serialises them to plaintext JSON under chmod 0600 — safe on
#    a single-user host but loses that guarantee across backups,
#    Docker bind mounts with wrong umask, and DB migrations. Either
#    encrypt those fields or move them to a secrets backend.
# 3. Isolate by principal, not just by a coarse organization scope. The
#    normal SDK path below uses the authenticated ``user_name`` directly.
#    Adopters replacing it with a custom provider that groups principals
#    must widen that key or authorize each config row explicitly.
_current_push_config_scope: ContextVar[str | None] = ContextVar(
    "adcp_push_config_scope", default=None
)
"""Fallback ContextVar for context-less direct/background store calls.

Normal a2a-sdk handler calls carry ``ServerCallContext`` and do not consult
this value. Exposed so a custom sender can restore the owning scope when it
later reads configs without a request context.
"""


def _default_push_config_scope_provider() -> str | None:
    """Read the current push-config scope from the module-level
    ``ContextVar``. Returned ``None`` = "unauthenticated request" per
    ``_ANONYMOUS_SCOPE`` below."""
    return _current_push_config_scope.get()


class SqlitePushNotificationConfigStore(PushNotificationConfigStore):
    """Durable A2A ``PushNotificationConfigStore`` backed by a single
    SQLite file, scoped by an authenticated principal resolved at
    set/get/delete time from the a2a-sdk ``ServerCallContext``.

    a2a-sdk 1.0 passes ``ServerCallContext`` to all three store methods;
    normal handler calls therefore bind directly to the authenticated
    ``user.user_name`` just like :class:`SqliteTaskStore`. A ContextVar
    ``scope_provider`` remains as a fallback for background sender and direct
    calls that genuinely lack a context.

    Example — wiring the default ContextVar from auth middleware::

        from contextvars import ContextVar

        from examples.a2a_db_tasks import (
            SqlitePushNotificationConfigStore,
            _current_push_config_scope,
        )

        class AuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                scope = _resolve_tenant_scope(request)  # your auth logic
                token = _current_push_config_scope.set(scope)
                try:
                    return await call_next(request)
                finally:
                    _current_push_config_scope.reset(token)

        serve(
            agent,
            transport="a2a",
            push_config_store=SqlitePushNotificationConfigStore(
                db_path="a2a_push_configs.db",
            ),
        )

    Example — injecting a custom provider (different ContextVar, or
    pulling from your own auth layer)::

        my_scope: ContextVar[str | None] = ContextVar("my_scope")
        store = SqlitePushNotificationConfigStore(
            db_path="a2a_push_configs.db",
            scope_provider=lambda: my_scope.get(default=None),
        )

    **Fails loudly on anonymous fallback.** If a context-less call's provider returns
    ``None``, a ``UserWarning`` is emitted once per store instance and
    the store falls through to ``__anonymous__`` — unauthenticated
    requests end up sharing one giant scope. Operators should reject
    unauthenticated push-notif-config requests at the auth layer
    before the store is touched; the warning is the signal they
    forgot to.

    ``allowed_destination_hosts=None`` accepts any public HTTPS destination
    that passes the shared DNS/SSRF checks. Pass a concrete ``frozenset`` for
    an additional exact-host allowlist; an explicitly empty set denies all.

    **Background-task caveat — sender path.** a2a-sdk's push-notif
    sender may call ``get_info()`` from a background ``asyncio.Task``
    without a ``ServerCallContext``. That task inherits the
    ContextVar snapshot captured at task-creation time; if the
    seller's auth middleware has already reset the ContextVar before
    the background task reads it, ``get_info()`` will return an empty
    list and notifications silently drop. Sellers running non-blocking
    push-notifs MUST propagate scope into the sender path explicitly —
    either by capturing the scope into a closure at ``set_info()`` time
    and stashing it alongside the config, or by overriding
    a2a-sdk's ``BasePushNotificationSender`` to re-set the ContextVar
    before calling ``get_info``. Not yet addressed by the SDK;
    tracked separately.
    """

    def __init__(
        self,
        db_path: str | Path = "a2a_push_configs.db",
        *,
        scope_provider: Callable[[], str | None] | None = None,
        allowed_destination_hosts: frozenset[str] | None = None,
    ) -> None:
        self._db_path = str(db_path)
        self._scope_provider = scope_provider or _default_push_config_scope_provider
        self._allowed_destination_hosts = (
            normalize_allowed_push_hosts(allowed_destination_hosts)
            if allowed_destination_hosts is not None
            else None
        )
        self._init_schema()
        self._warned_anonymous = False

    def _init_schema(self) -> None:
        path = Path(self._db_path)
        first_create = not path.exists()
        with sqlite3.connect(self._db_path) as conn:
            # One task can have multiple push-notif configs; the config
            # id is the secondary key. scope isolates across tenants.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS a2a_push_configs (
                    scope      TEXT NOT NULL,
                    task_id    TEXT NOT NULL,
                    config_id  TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                    PRIMARY KEY (scope, task_id, config_id)
                )
                """
            )
        if first_create:
            with contextlib.suppress(OSError):
                os.chmod(self._db_path, 0o600)

    def _scope(self, context: ServerCallContext | None) -> str:
        # An explicit context is authoritative. Never let an unauthenticated
        # request inherit an ambient tenant from a ContextVar/provider.
        scope = (
            scope_from_server_context(context) if context is not None else self._scope_provider()
        )
        if not scope:
            if not self._warned_anonymous:
                self._warned_anonymous = True
                warnings.warn(
                    "SqlitePushNotificationConfigStore: scope_provider "
                    "returned None — operating in the __anonymous__ scope. "
                    "All unauthenticated requests will share a single "
                    "push-notif bucket, which is unsafe in multi-tenant "
                    "deployments. Wire your auth middleware to populate "
                    "the ContextVar (or reject unauthenticated push-notif-"
                    "config requests at the auth layer) before production.",
                    UserWarning,
                    stacklevel=3,
                )
            return _ANONYMOUS_SCOPE
        return scope

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    async def set_info(
        self,
        task_id: str,
        notification_config: PushNotificationConfig,
        context: ServerCallContext | None = None,
    ) -> None:
        scope = self._scope(context)
        validate_a2a_push_notification_url(
            str(notification_config.url),
            allowed_hosts=self._allowed_destination_hosts,
        )
        # PushNotificationConfig.id is optional on the wire; when the
        # client didn't supply one we synthesise a UUID so two clients
        # registering on the same task without explicit ids don't
        # silently overwrite each other. Production stores should
        # consider requiring an explicit config_id and rejecting
        # None outright — the client loses the ability to delete the
        # config they just created unless they round-trip the
        # server-assigned id.
        config_id = notification_config.id or f"auto-{uuid.uuid4()}"
        config_json = MessageToJson(notification_config, preserving_proto_field_name=True)
        async with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO a2a_push_configs "
                "(scope, task_id, config_id, config_json, updated_at) "
                "VALUES (?, ?, ?, ?, strftime('%s','now'))",
                (scope, task_id, config_id, config_json),
            )

    async def get_info(
        self,
        task_id: str,
        context: ServerCallContext | None = None,
    ) -> list[PushNotificationConfig]:
        scope = self._scope(context)
        async with self._conn() as conn:
            rows = conn.execute(
                "SELECT config_json FROM a2a_push_configs WHERE scope = ? AND task_id = ?",
                (scope, task_id),
            ).fetchall()
        return [Parse(r[0], PushNotificationConfig()) for r in rows]

    async def delete_info(
        self,
        task_id: str,
        context: ServerCallContext | None = None,
        config_id: str | None = None,
    ) -> None:
        scope = self._scope(context)
        async with self._conn() as conn:
            if config_id is None:
                # a2a-sdk's ABC semantic: ``config_id=None`` removes every
                # config for the task. Within a scope
                # with multiple principals, this lets any principal
                # wipe every other principal's subscriptions — a
                # tenant-local DoS. Production stores that admit
                # multi-principal-per-scope should reject
                # ``config_id=None`` or authorise the caller against
                # each config row. The reference impl honours the ABC
                # semantic as-is.
                conn.execute(
                    "DELETE FROM a2a_push_configs WHERE scope = ? AND task_id = ?",
                    (scope, task_id),
                )
            else:
                conn.execute(
                    "DELETE FROM a2a_push_configs "
                    "WHERE scope = ? AND task_id = ? AND config_id = ?",
                    (scope, task_id, config_id),
                )


# ----------------------------------------------------------------------
# Minimal handler so the example runs end-to-end
# ----------------------------------------------------------------------


class DemoAgent(ADCPHandler[Any]):
    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return capabilities_response(["media_buy"])

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        return products_response([{"product_id": "demo_display", "name": "Demo display placement"}])


# ----------------------------------------------------------------------
# Wiring — pass the store through ``serve()``.
# ----------------------------------------------------------------------


def main() -> None:
    task_store = SqliteTaskStore(db_path="a2a_tasks.db")
    configured_push_hosts = frozenset(
        host for host in os.environ.get("A2A_PUSH_ALLOWED_HOSTS", "").split(",") if host
    )
    push_settings = resolve_push_destination_settings(
        os.environ.get("A2A_PUSH_MODE", "disabled"),
        configured_push_hosts,
    )
    push_store = (
        SqlitePushNotificationConfigStore(
            db_path="a2a_push_configs.db",
            allowed_destination_hosts=push_settings.allowed_hosts,
        )
        if push_settings.enabled
        else None
    )
    serve(
        DemoAgent(),
        name="a2a-db-tasks-demo",
        transport="a2a",
        task_store=task_store,
        push_config_store=push_store,
    )


if __name__ == "__main__":
    main()
