"""``spec_compat_hooks()`` emits a DeprecationWarning pointing at the
new ``adcp.compat.legacy`` adapter layer.

Behavior is otherwise unchanged from the original
``spec_compat_hooks_impl`` — the existing test suite at
``tests/test_spec_compat_hooks.py`` covers all the hook semantics via
the internal implementation entry point so DeprecationWarning noise
doesn't drown those out.
"""

from __future__ import annotations

import pytest

from adcp.server.spec_compat import spec_compat_hooks


def test_spec_compat_hooks_emits_deprecation_warning() -> None:
    with pytest.deprecated_call() as record:
        spec_compat_hooks()
    # Warning text mentions the migration path so adopters reading
    # logs know where to go.
    assert any("adcp.compat.legacy" in str(w.message) for w in record)
    assert any("6.0" in str(w.message) for w in record)


def test_spec_compat_hooks_still_returns_working_hooks() -> None:
    """Deprecation doesn't break the existing call shape — the legacy
    adopter path continues to function until 6.0."""
    with pytest.warns(DeprecationWarning):
        hooks = spec_compat_hooks()
    assert "get_products" in hooks
    assert "sync_creatives" in hooks
    assert callable(hooks["get_products"])
    assert callable(hooks["sync_creatives"])
