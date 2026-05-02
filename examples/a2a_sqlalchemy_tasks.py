"""Example: A2A agent with SQLAlchemy-backed durable stores.

Companion to :file:`examples/a2a_db_tasks.py` (raw-SQLite reference).
Uses SQLAlchemy ORM so the same code runs against any backend SQLA
supports — SQLite for the demo, Postgres / MySQL in production.

**Why this exists.** Adopters with an existing SQLAlchemy schema
(salesagent, custom seller agents, anything Flask-SQLAlchemy-based)
don't need to write raw SQL — they wrap their existing models behind
the a2a-sdk Protocols. This example is the template:

* :class:`A2ATaskRow` — minimal ORM model for the task store.
* :class:`A2APushConfigRow` — minimal ORM model for the
  push-notif-config store.
* :class:`SqlAlchemyTaskStore` /
  :class:`SqlAlchemyPushNotificationConfigStore` — the Protocol
  implementations.

Adopters with richer existing schemas keep their seller-side columns
(salesagent's ``auth_blocked_at``, ``webhook_secret``, ``is_active``,
etc.) and the wrapper just maps those that the protocol cares about.

**Security model — same as the SQLite reference.** Tenant-scoped
lookups via ``ServerCallContext.user.user_name``; SSRF-vulnerable
``PushNotificationConfig.url`` MUST be validated by the seller before
persistence (this example does NOT validate — see the SQLite reference
docstring for the egress-allowlist pattern). Webhook secrets in
``authentication.credentials`` / ``token`` should be envelope-encrypted
or moved to a secrets backend in production; this example persists
them plaintext for runnability.

**Production switches:**

* Swap the engine URL: ``sqlite:///a2a.db`` →
  ``postgresql+asyncpg://...`` for Postgres.
* Use an ``AsyncSession`` + ``create_async_engine`` for end-to-end
  async persistence (this example uses sync sessions for clarity;
  the Protocol methods are async, sync-engine calls run in a default
  threadpool).
* Add row-level TTL / GC for completed tasks.
* Add a version column for optimistic concurrency on ``save()``.
* Replace plaintext ``credentials`` storage with an envelope-encrypted
  column or a secrets-backend pointer.

Run::

    uv run python examples/a2a_sqlalchemy_tasks.py

Then connect any A2A client to ``http://localhost:3001/`` —
``message/send`` carries a ``configuration.push_notification_config``
that lands in the ``a2a_push_configs`` SQLite table; ``tasks/get``
reads from ``a2a_tasks``. Tear down by deleting ``a2a_sqlalchemy.db``.
"""

from __future__ import annotations

import contextlib
import warnings
from contextvars import ContextVar
from datetime import datetime, timezone

UTC = timezone.utc
from pathlib import Path
from typing import Any

from a2a import types as pb
from a2a.server.context import ServerCallContext
from a2a.server.tasks.push_notification_config_store import (
    PushNotificationConfigStore,
)
from a2a.server.tasks.task_store import TaskStore

# 1.0 folded ``PushNotificationConfig`` into
# :class:`a2a.types.TaskPushNotificationConfig`; the protocol method
# signatures still reference the old name. Alias for clarity.
from a2a.types import TaskPushNotificationConfig as PushNotificationConfig
from google.protobuf.json_format import MessageToJson, Parse

try:
    from sqlalchemy import (
        Boolean,
        DateTime,
        ForeignKeyConstraint,
        Index,
        String,
        Text,
        create_engine,
        delete,
        select,
    )
    from sqlalchemy.orm import (
        DeclarativeBase,
        Mapped,
        Session,
        mapped_column,
        sessionmaker,
    )
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError(
        "examples/a2a_sqlalchemy_tasks.py needs SQLAlchemy. Install with "
        "`pip install 'sqlalchemy>=2.0'` or `uv add sqlalchemy` and re-run."
    ) from exc

from adcp.server import ADCPHandler, capabilities_response, products_response, serve

# ----------------------------------------------------------------------
# ORM models — minimal protocol-driven schema
# ----------------------------------------------------------------------


class _Base(DeclarativeBase):
    pass


class A2ATaskRow(_Base):
    """One row per A2A task. Scope-isolated by ``scope``.

    Adopters with existing seller-side task tables wrap their model
    instead — this is the *minimum* the protocol needs. The
    ``payload`` column carries the full ``Task.model_dump_json()``
    blob; partial-update queries (e.g., status-only) use SQLAlchemy
    update statements over that column.
    """

    __tablename__ = "a2a_tasks"

    scope: Mapped[str] = mapped_column(String(255), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (Index("idx_a2a_tasks_scope", "scope"),)


class A2APushConfigRow(_Base):
    """One row per registered push-notification config.

    Mirror of salesagent's ``push_notification_configs`` minus the
    seller-specific columns (``auth_blocked_at``, ``is_active``,
    ``webhook_secret``). Adopters extending this row keep their
    existing columns and the ``A2APushConfigRow`` wrapper just maps the
    Protocol-required subset.
    """

    __tablename__ = "a2a_push_configs"

    scope: Mapped[str] = mapped_column(String(255), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    config_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["scope", "task_id"],
            ["a2a_tasks.scope", "a2a_tasks.task_id"],
            ondelete="CASCADE",
        ),
        Index("idx_a2a_push_configs_task", "scope", "task_id"),
    )


# ----------------------------------------------------------------------
# Scope resolution — derives tenant key from ServerCallContext
# ----------------------------------------------------------------------


_NO_AUTH_SCOPE = "anonymous"


def _scope_from_context(context: ServerCallContext | None) -> str:
    """Pull the tenant scope key from the call context.

    Adopters with richer identity (typed ``tenant_id`` columns,
    organization IDs) override this — the protocol only requires that
    every read/write be filtered by the same scope value.

    Returns ``_NO_AUTH_SCOPE`` for unauthenticated calls so a missing
    context never falls through to a "no filter" query that would leak
    other tenants' tasks.
    """
    if context is None or context.user is None:
        return _NO_AUTH_SCOPE
    return context.user.user_name or _NO_AUTH_SCOPE


# Adopter-side push-notif scope hook. The
# ``PushNotificationConfigStore`` Protocol does NOT receive the
# ``ServerCallContext`` (a2a-sdk caveat — see the SQLite reference's
# docstring); adopters compose with their tenant-scoped ``TaskStore``
# to derive scope by walking from ``task_id`` to the owning row. This
# example uses a contextvar that the surrounding handler populates
# from its own auth middleware.
_push_config_scope: ContextVar[str | None] = ContextVar("_push_config_scope", default=None)


# ----------------------------------------------------------------------
# TaskStore — the protocol implementation
# ----------------------------------------------------------------------


class SqlAlchemyTaskStore(TaskStore):
    """Tenant-scoped, SQLAlchemy-backed :class:`TaskStore`.

    All four protocol methods filter by the scope derived from
    ``ServerCallContext`` so cross-tenant id-collision (or guessing) can
    never leak another principal's task.

    ``save`` uses ORM ``merge`` (last-writer-wins). Production stores
    that need optimistic concurrency add a ``version`` column +
    ``WHERE version = ?`` predicate.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    async def save(
        self,
        task: pb.Task,
        context: ServerCallContext | None = None,
    ) -> None:
        scope = _scope_from_context(context)
        # a2a-sdk Tasks are protobuf messages — serialize via the proto
        # JSON form so a different reader on the same DB (gRPC bridge,
        # future client) sees canonical bytes.
        with self._session_factory() as session:
            row = A2ATaskRow(
                scope=scope,
                task_id=task.id,
                payload=MessageToJson(task, preserving_proto_field_name=True),
                updated_at=datetime.now(UTC),
            )
            session.merge(row)
            session.commit()

    async def get(
        self,
        task_id: str,
        context: ServerCallContext | None = None,
    ) -> pb.Task | None:
        scope = _scope_from_context(context)
        with self._session_factory() as session:
            row = session.execute(
                select(A2ATaskRow).where(A2ATaskRow.scope == scope, A2ATaskRow.task_id == task_id)
            ).scalar_one_or_none()
            if row is None:
                return None
            return Parse(row.payload, pb.Task())

    async def delete(
        self,
        task_id: str,
        context: ServerCallContext | None = None,
    ) -> None:
        scope = _scope_from_context(context)
        with self._session_factory() as session:
            session.execute(
                delete(A2ATaskRow).where(A2ATaskRow.scope == scope, A2ATaskRow.task_id == task_id)
            )
            session.commit()

    async def list(
        self,
        params: pb.ListTasksRequest | None = None,
        context: ServerCallContext | None = None,
    ) -> pb.ListTasksResponse:
        """Return all tasks owned by the current scope.

        Pagination (``params.page_token`` / ``page_size``) is left as a
        seller exercise; production deployments add keyset pagination on
        ``(updated_at, task_id)``.
        """
        scope = _scope_from_context(context)
        with self._session_factory() as session:
            rows = session.execute(
                select(A2ATaskRow)
                .where(A2ATaskRow.scope == scope)
                .order_by(A2ATaskRow.updated_at.desc())
            ).scalars()
            tasks = [Parse(row.payload, pb.Task()) for row in rows]
        return pb.ListTasksResponse(tasks=tasks)


# ----------------------------------------------------------------------
# PushNotificationConfigStore — the protocol implementation
# ----------------------------------------------------------------------


class SqlAlchemyPushNotificationConfigStore(PushNotificationConfigStore):
    """Tenant-scoped, SQLAlchemy-backed push-notification config store.

    URL validation is the seller's responsibility. This example does
    NOT validate ``config.push_notification_config.url`` before
    persisting — production deployments MUST reject non-https,
    RFC-1918, link-local IPv6, and the cloud metadata service URL
    before this method runs. See the SQLite reference's module
    docstring for the SSRF threat model.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _scope() -> str:
        scope = _push_config_scope.get()
        if scope is None:
            warnings.warn(
                "PushNotificationConfigStore scope contextvar unset — "
                "falling back to anonymous scope. Wire the contextvar "
                "from your auth middleware so per-tenant config is "
                "isolated.",
                RuntimeWarning,
                stacklevel=2,
            )
            return _NO_AUTH_SCOPE
        return scope

    async def set_info(
        self,
        task_id: str,
        notification_config: PushNotificationConfig,
    ) -> None:
        scope = self._scope()
        config_id = (
            notification_config.push_notification_config.id
            or notification_config.push_notification_config.url
        )
        with self._session_factory() as session:
            row = A2APushConfigRow(
                scope=scope,
                task_id=task_id,
                config_id=config_id,
                payload=MessageToJson(notification_config, preserving_proto_field_name=True),
                created_at=datetime.now(UTC),
                is_active=True,
            )
            session.merge(row)
            session.commit()

    async def get_info(self, task_id: str) -> list[PushNotificationConfig]:
        scope = self._scope()
        with self._session_factory() as session:
            rows = session.execute(
                select(A2APushConfigRow).where(
                    A2APushConfigRow.scope == scope,
                    A2APushConfigRow.task_id == task_id,
                    A2APushConfigRow.is_active.is_(True),
                )
            ).scalars()
            return [Parse(row.payload, PushNotificationConfig()) for row in rows]

    async def delete_info(self, task_id: str, config_id: str | None = None) -> None:
        scope = self._scope()
        with self._session_factory() as session:
            stmt = delete(A2APushConfigRow).where(
                A2APushConfigRow.scope == scope,
                A2APushConfigRow.task_id == task_id,
            )
            if config_id is not None:
                stmt = stmt.where(A2APushConfigRow.config_id == config_id)
            session.execute(stmt)
            session.commit()


# ----------------------------------------------------------------------
# Engine + session factory builders
# ----------------------------------------------------------------------


def build_engine_and_sessions(
    *, database_url: str = "sqlite:///a2a_sqlalchemy.db"
) -> sessionmaker[Session]:
    """Build a :class:`sessionmaker` for a chosen backend.

    SQLite for the demo. Switch to ``postgresql+psycopg://...`` or
    ``mysql+pymysql://...`` for production — the rest of this file
    is unchanged.
    """
    if database_url.startswith("sqlite:///") and database_url != "sqlite:///:memory:":
        # Restrict file mode on first creation so a co-tenant process
        # on the same host can't read it. Production deployments use
        # OS-level access control on the data directory; this match
        # the SQLite reference's posture.
        path = Path(database_url.removeprefix("sqlite:///")).expanduser()
        existed = path.exists()
        engine = create_engine(database_url, future=True)
        _Base.metadata.create_all(engine)
        if not existed:
            with contextlib.suppress(OSError):
                path.chmod(0o600)
    else:
        engine = create_engine(database_url, future=True)
        _Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


# ----------------------------------------------------------------------
# Minimal handler so the example runs end-to-end
# ----------------------------------------------------------------------


class DemoAgent(ADCPHandler):
    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return capabilities_response(["media_buy"])

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        return products_response([{"product_id": "demo_display", "name": "Demo display placement"}])


# ----------------------------------------------------------------------
# Wiring — pass the stores through ``serve()``.
# ----------------------------------------------------------------------


def main() -> None:
    session_factory = build_engine_and_sessions()
    task_store = SqlAlchemyTaskStore(session_factory)
    push_store = SqlAlchemyPushNotificationConfigStore(session_factory)
    serve(
        DemoAgent(),
        name="a2a-sqlalchemy-demo",
        transport="a2a",
        task_store=task_store,
        push_config_store=push_store,
    )


if __name__ == "__main__":
    main()
