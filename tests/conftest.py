from __future__ import annotations

import warnings as _warnings
from pathlib import Path
from types import UnionType
from typing import Any

import pytest
from pydantic import TypeAdapter

# Import the a2a-sdk 1.0 compat shim early so monkey-patches like
# ``Role.user = ROLE_USER`` and ``TaskStatus.__init__`` string coercion
# land before any test module constructs those proto types.
# Guard with try/except so a missing or wrong-version a2a-sdk doesn't
# break collection — only A2A tests that actually use the shim will fail.
try:
    from tests import a2a_compat_shim as _a2a_compat_shim
except (ImportError, AttributeError) as _shim_exc:
    _warnings.warn(
        f"a2a_compat_shim unavailable ({_shim_exc}); "
        "run: pip install 'a2a-sdk>=1.0.1,<1.0.2'. A2A tests may fail.",
        RuntimeWarning,
        stacklevel=1,
    )
    _a2a_compat_shim = None  # type: ignore[assignment]

_INTEGRATION_DIR = (Path(__file__).parent / "integration").resolve()


@pytest.fixture(autouse=True, scope="session")
def _ensure_pydantic_schemas() -> None:
    """Trigger lazy Pydantic schema init so ADCP_TOOL_DEFINITIONS has
    inputSchema/outputSchema populated for any test that reads them directly."""
    from adcp.server.mcp_tools import _ensure_pydantic_schemas_applied

    _ensure_pydantic_schemas_applied()


def _is_integration_test(request: pytest.FixtureRequest) -> bool:
    """Is this test under ``tests/integration/``?

    Uses a real path comparison rather than a ``"integration" in nodeid``
    substring check — the latter matches any file or classname that
    happens to contain the word, which is fragile if a future contributor
    names something like ``test_message_integration.py`` outside the
    integration dir.
    """
    try:
        node_path = Path(request.node.path).resolve()
    except (AttributeError, TypeError):
        return False
    try:
        node_path.relative_to(_INTEGRATION_DIR)
    except ValueError:
        return False
    return True


@pytest.fixture(autouse=True)
def _a2a_compat_send_and_aggregate(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch :meth:`A2AAdapter._send_and_aggregate` for unit tests only.

    The 1.0 ``Client.send_message`` is an async generator but the
    unit-test suite mocks it with ``AsyncMock(return_value=...)`` (the
    0.3 shape). The shim shortcuts the iterator drain so unit tests
    keep their original mock return values. Integration tests talk to
    a real a2a-sdk server and must NOT be shimmed — they rely on the
    genuine async-generator contract.
    """
    if _a2a_compat_shim is None or _is_integration_test(request):
        return
    _a2a_compat_shim.patch_send_and_aggregate(monkeypatch)


_adapter_cache: dict[type | UnionType, TypeAdapter[Any]] = {}


def validate_union(tp: type | UnionType, data: dict[str, Any]) -> Any:
    """Validate data against a type (handles both classes and union type aliases).

    Caches TypeAdapter instances to avoid repeated schema compilation.
    """
    try:
        adapter = _adapter_cache[tp]
    except KeyError:
        _adapter_cache[tp] = adapter = TypeAdapter(tp)
    return adapter.validate_python(data)
