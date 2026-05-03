"""Tests for the ``serve(asgi_middleware=...)`` kwarg.

Operators wiring tenant routing, CORS, request-id propagation, and
custom auth use this kwarg to layer Starlette-style ASGI middleware
on the outer HTTP app before uvicorn binds. The kwarg accepts a
sequence of ``(MiddlewareClass, kwargs)`` tuples and composes
outermost-first.
"""

from __future__ import annotations

from adcp.server.serve import _apply_asgi_middleware


class _NoOpAsgi:
    """Bare ASGI app stub — never actually invoked in these tests."""

    async def __call__(self, scope, receive, send):  # pragma: no cover
        return None


class _TaggingMiddleware:
    """Records its name onto the outer app's call list when constructed."""

    def __init__(self, app, *, name):
        self.app = app
        self.name = name


def test_apply_asgi_middleware_no_op_for_empty_sequence():
    app = _NoOpAsgi()
    out = _apply_asgi_middleware(app, None)
    assert out is app
    out2 = _apply_asgi_middleware(app, [])
    assert out2 is app


def test_apply_asgi_middleware_wraps_in_outermost_first_order():
    """First entry in the sequence is the outermost wrapper.

    Adopters reading ``[(A, {}), (B, {})]`` expect A to see every
    request before B — same semantics as Starlette's
    ``add_middleware``. The implementation reverses the iteration
    order so the first entry ends up wrapping the result of the
    later entries.
    """
    app = _NoOpAsgi()
    wrapped = _apply_asgi_middleware(
        app,
        [
            (_TaggingMiddleware, {"name": "outer"}),
            (_TaggingMiddleware, {"name": "inner"}),
        ],
    )
    assert isinstance(wrapped, _TaggingMiddleware)
    assert wrapped.name == "outer"
    assert isinstance(wrapped.app, _TaggingMiddleware)
    assert wrapped.app.name == "inner"
    assert wrapped.app.app is app


def test_apply_asgi_middleware_passes_kwargs_through():
    app = _NoOpAsgi()
    wrapped = _apply_asgi_middleware(app, [(_TaggingMiddleware, {"name": "audit"})])
    assert isinstance(wrapped, _TaggingMiddleware)
    assert wrapped.name == "audit"
    assert wrapped.app is app
