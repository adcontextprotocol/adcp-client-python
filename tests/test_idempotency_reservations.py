"""Request validation for explicit PostgreSQL reservations (no database needed)."""

from unittest.mock import MagicMock, patch

import pytest

from adcp.exceptions import IdempotencyScopeError
from adcp.server.idempotency import IdempotencyStore, MemoryBackend, PgBackend


def pg_store():
    with patch("adcp.server.idempotency.backends._PG_AVAILABLE", True):
        backend = PgBackend(pool=MagicMock(), lock_pool=MagicMock())
    return IdempotencyStore(backend)


def test_memory_backend_does_not_claim_transactional_support():
    store = IdempotencyStore(MemoryBackend())
    with pytest.raises(TypeError, match="direct PgBackend"):
        store.reserve({"idempotency_key": "key"}, {"caller_identity": "buyer"})


def test_missing_key_is_rejected_before_business_execution():
    with pytest.raises(ValueError, match="idempotency_key"):
        pg_store().reserve({}, {"caller_identity": "buyer"})


def test_missing_authenticated_scope_is_rejected():
    with pytest.warns(UserWarning, match="no caller_identity"):
        with pytest.raises(IdempotencyScopeError):
            pg_store().reserve({"idempotency_key": "key"}, {})
