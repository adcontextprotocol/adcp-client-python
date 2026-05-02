"""PostgreSQL-backed implementations for the decisioning module.

Ships durable backends behind the ``[pg]`` optional extra so the
base ``adcp.decisioning`` import path stays free of SQL dependencies
for adopters who only need the in-memory primitives.

Available when ``adcp[pg]`` is installed:

* :class:`PgBuyerAgentRegistry` — durable Tier 2 commercial-identity
  layer for v3 sellers. The framework calls the registry on every
  request to gate dispatch on the seller's commercial relationship
  with the buyer agent (allowlist + onboarding state + billing
  capabilities).
* :class:`PgTaskRegistry` — durable
  :class:`~adcp.decisioning.TaskRegistry` for HITL task state. Survives
  process restarts and is safe for multi-worker deployments sharing a
  single Postgres database. Drop-in replacement for
  :class:`~adcp.decisioning.InMemoryTaskRegistry` that satisfies the
  production-mode durability gate. (``PostgresTaskRegistry`` is the
  pre-4.4 name and remains as a deprecated alias through 4.4.x.)

The schema DDL ships alongside the Python code (e.g.
``adcp/decisioning/pg/buyer_agent_registry.sql``,
``adcp/decisioning/pg/decisioning_tasks.sql``) so adopters can run it
through whatever migration tool they use (Alembic, Flyway, psql).
"""

from __future__ import annotations

from adcp.decisioning.pg.buyer_agent_registry import (
    DEFAULT_TABLE_NAME,
    PG_AVAILABLE,
    PgBuyerAgentRegistry,
)
from adcp.decisioning.pg.task_registry import PgTaskRegistry, PostgresTaskRegistry

__all__ = [
    "DEFAULT_TABLE_NAME",
    "PG_AVAILABLE",
    "PgBuyerAgentRegistry",
    "PgTaskRegistry",
    "PostgresTaskRegistry",
]
