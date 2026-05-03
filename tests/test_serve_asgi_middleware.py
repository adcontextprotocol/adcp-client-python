"""Tests for the ``serve(asgi_middleware=...)`` kwarg.

Operators wiring tenant routing, CORS, request-id propagation, and
custom auth use this kwarg to layer Starlette-style ASGI middleware
on the outer HTTP app before uvicorn binds. The kwarg accepts a
sequence of ``(MiddlewareClass, kwargs)`` tuples, callable factories,
or a mix of both, and composes outermost-first.
"""

from __future__ import annotations

import functools

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


def test_apply_asgi_middleware_callable_factory():
    """Callable factory form ``f(app) -> app`` is accepted."""
    app = _NoOpAsgi()

    def cors_factory(inner):
        return _TaggingMiddleware(inner, name="cors")

    wrapped = _apply_asgi_middleware(app, [cors_factory])
    assert isinstance(wrapped, _TaggingMiddleware)
    assert wrapped.name == "cors"
    assert wrapped.app is app


def test_apply_asgi_middleware_callable_factory_with_partial():
    """``functools.partial`` is a valid callable factory."""
    app = _NoOpAsgi()
    factory = functools.partial(_TaggingMiddleware, name="partial-cors")
    wrapped = _apply_asgi_middleware(app, [factory])
    assert isinstance(wrapped, _TaggingMiddleware)
    assert wrapped.name == "partial-cors"
    assert wrapped.app is app


def test_apply_asgi_middleware_mixed_tuple_and_callable_preserves_order():
    """Mixed list composes outermost-first regardless of entry type.

    Given ``[tuple_entry("outer"), callable("middle"), tuple_entry("inner")]``,
    the result must be outer → middle → inner → app, verified by walking
    the ``.app`` chain.
    """
    app = _NoOpAsgi()

    def middle_factory(inner):
        return _TaggingMiddleware(inner, name="middle")

    wrapped = _apply_asgi_middleware(
        app,
        [
            (_TaggingMiddleware, {"name": "outer"}),
            middle_factory,
            (_TaggingMiddleware, {"name": "inner"}),
        ],
    )
    assert isinstance(wrapped, _TaggingMiddleware)
    assert wrapped.name == "outer"
    assert isinstance(wrapped.app, _TaggingMiddleware)
    assert wrapped.app.name == "middle"
    assert isinstance(wrapped.app.app, _TaggingMiddleware)
    assert wrapped.app.app.name == "inner"
    assert wrapped.app.app.app is app
