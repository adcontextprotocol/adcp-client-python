"""Pin the framework default for ``serve(validation=...)`` —
``ValidationHookConfig(requests="strict", responses="strict")``.

The framework promotes wire-conformance to the default to catch the
class of bug that shipped the ``pricing_options`` regression: a
Pydantic model with ``extra="allow"`` silently swallowed the unknown
shape past type-validation, and only an end-to-end schema check at the
wire boundary surfaced it. Strict-by-default puts that check on every
adopter's serve path.

These tests pin:

* ``inspect`` shows the default kwarg as ``DEFAULT_VALIDATION`` (signature
  contract — adopters/IDEs see strict in autocomplete).
* The default value is the canonical
  :data:`adcp.validation.client_hooks.SERVER_DEFAULT_VALIDATION` —
  same constant in serve, a2a_server, and the validation module.
* Adopters opt out via ``validation=None`` (off entirely) or
  ``ValidationHookConfig(responses="warn")`` (response warn-only).
"""

from __future__ import annotations

import inspect

from adcp.server.a2a_server import create_a2a_server
from adcp.server.serve import (
    DEFAULT_VALIDATION,
    create_mcp_server,
    serve,
)
from adcp.validation import ValidationHookConfig
from adcp.validation.client_hooks import SERVER_DEFAULT_VALIDATION


def test_default_validation_is_strict_strict() -> None:
    """The canonical default is strict requests and strict responses.

    The constant is the load-bearing source of truth; if this changes,
    every server-side call site that defaults to it changes posture in
    lockstep.
    """
    assert isinstance(DEFAULT_VALIDATION, ValidationHookConfig)
    assert DEFAULT_VALIDATION.requests == "strict"
    assert DEFAULT_VALIDATION.responses == "strict"


def test_default_validation_is_canonical() -> None:
    """``adcp.server.serve.DEFAULT_VALIDATION`` aliases the validation
    module's :data:`SERVER_DEFAULT_VALIDATION` — there is one source of
    truth, not two parallel constants drifting independently.
    """
    assert DEFAULT_VALIDATION is SERVER_DEFAULT_VALIDATION


def test_serve_signature_default_is_strict() -> None:
    """``serve(validation=...)`` advertises the strict default in its
    signature so IDEs and ``help(serve)`` show the right value.
    """
    sig = inspect.signature(serve)
    param = sig.parameters["validation"]
    assert param.default is DEFAULT_VALIDATION
    assert isinstance(param.default, ValidationHookConfig)
    assert param.default.requests == "strict"
    assert param.default.responses == "strict"


def test_create_mcp_server_signature_default_is_strict() -> None:
    """Same contract on ``create_mcp_server`` — the lower-level seam
    adopters compose with their own auth / ASGI middleware also
    defaults strict.
    """
    sig = inspect.signature(create_mcp_server)
    param = sig.parameters["validation"]
    assert param.default is DEFAULT_VALIDATION


def test_create_a2a_server_signature_default_is_strict() -> None:
    """Same contract on the A2A side — both transports default strict
    so an adopter calling ``serve(handler, transport="a2a")`` and an
    adopter calling ``create_a2a_server(handler)`` get identical
    posture without wiring it twice.
    """
    sig = inspect.signature(create_a2a_server)
    param = sig.parameters["validation"]
    assert param.default is SERVER_DEFAULT_VALIDATION
