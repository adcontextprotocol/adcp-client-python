"""Test helpers for the v6 DecisioningPlatform framework.

Two adopter-facing helpers that close gaps surfaced by the salesagent
v3.12 → 4.x migration:

* :func:`make_request_context` — build a
  :class:`adcp.decisioning.RequestContext` for unit tests with sane
  defaults. The dataclass has a dozen fields with factory defaults; this
  helper documents what tests should reach for so adopters don't guess
  whether ``state`` / ``resolve`` / ``now`` factory defaults are safe.

* :func:`build_asgi_app` — build a Starlette ASGI app from a
  :class:`adcp.decisioning.DecisioningPlatform` without binding a port.
  Useful for in-process integration tests via ``httpx.AsyncClient``,
  ``starlette.testclient.TestClient``, or direct ASGI invocation. The
  default ``auto_emit_completion_webhooks=False`` skips the F12 boot
  gate that otherwise refuses to start a sales platform without a
  webhook sender wired.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from adcp.decisioning.context import RequestContext
from adcp.decisioning.types import Account

if TYPE_CHECKING:
    from datetime import datetime

    from adcp.decisioning import (
        AuthInfo,
        BuyerAgent,
        DecisioningPlatform,
        ResourceResolver,
        StateReader,
    )


def make_request_context(
    *,
    account: Account[Any] | str | None = None,
    auth_info: AuthInfo | None = None,
    auth_principal: str | None = None,
    buyer_agent: BuyerAgent | None = None,
    now: datetime | None = None,
    state: StateReader | None = None,
    resolve: ResourceResolver | None = None,
    request_id: str | None = None,
    tenant_id: str | None = None,
    caller_identity: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RequestContext[Any]:
    """Build a :class:`RequestContext` for unit tests.

    All parameters are optional. The defaults are stable test contract:
    an empty ``Account(id="test-account")``, no auth, framework-default
    ``state`` / ``resolve`` (the v6.0 stub readers), ``now`` set to wall
    clock, and an empty metadata dict.

    Pass ``account=`` as either an :class:`Account` instance (full
    control) or a string (shorthand for ``Account(id=<string>)``) — the
    common test case.

    :param account: Resolved account. ``None`` → ``Account(id="test-account")``.
        ``str`` → ``Account(id=<string>)``.
    :param auth_info: Verified principal info. ``None`` for unauthenticated
        / ``'derived'`` test fixtures.
    :param auth_principal: Convenience field for tests that read
        ``ctx.auth_principal`` without constructing an ``AuthInfo``.
    :param buyer_agent: Resolved commercial buyer agent. ``None`` for
        tests not exercising the registry path.
    :param now: Request timestamp. ``None`` → wall clock at construction.
    :param state: Workflow-state reader. ``None`` → framework default
        (v6.0 stub returning empty values).
    :param resolve: Async resource resolver. ``None`` → framework default
        (v6.0 stub raising ``NotImplementedError``).
    :param request_id: Inherited from :class:`adcp.server.ToolContext`.
    :param tenant_id: Inherited from :class:`adcp.server.ToolContext`.
    :param caller_identity: Inherited from :class:`adcp.server.ToolContext`.
        The framework's idempotency middleware reads this; tests that
        exercise idempotency paths should set it explicitly.
    :param metadata: Inherited from :class:`adcp.server.ToolContext`.
        ``None`` → empty dict.

    :returns: A populated :class:`RequestContext[Any]`.
    """
    resolved_account: Account[Any]
    if account is None:
        resolved_account = Account(id="test-account")
    elif isinstance(account, str):
        resolved_account = Account(id=account)
    else:
        resolved_account = account

    kwargs: dict[str, Any] = {"account": resolved_account}
    if auth_info is not None:
        kwargs["auth_info"] = auth_info
    if auth_principal is not None:
        kwargs["auth_principal"] = auth_principal
    if buyer_agent is not None:
        kwargs["buyer_agent"] = buyer_agent
    if now is not None:
        kwargs["now"] = now
    if state is not None:
        kwargs["state"] = state
    if resolve is not None:
        kwargs["resolve"] = resolve
    if request_id is not None:
        kwargs["request_id"] = request_id
    if tenant_id is not None:
        kwargs["tenant_id"] = tenant_id
    if caller_identity is not None:
        kwargs["caller_identity"] = caller_identity
    if metadata is not None:
        kwargs["metadata"] = metadata

    return RequestContext(**kwargs)


def build_asgi_app(
    platform: DecisioningPlatform,
    *,
    name: str | None = None,
    advertise_all: bool = False,
    auto_emit_completion_webhooks: bool = False,
    **factory_kwargs: Any,
) -> Any:
    """Build a Starlette ASGI app for in-process integration tests.

    Composes :func:`adcp.decisioning.create_adcp_server_from_platform`
    with :func:`adcp.server.create_mcp_server` and returns the
    streamable-HTTP ASGI app — the same surface
    :func:`adcp.decisioning.serve` would mount, minus the network bind.

    Defaults differ from the production :func:`serve` wrapper in one
    place: ``auto_emit_completion_webhooks`` is ``False`` so tests don't
    need to wire a :class:`adcp.webhook_sender.WebhookSender` just to
    instantiate a sales platform. Override to ``True`` if your test
    explicitly exercises the F12 auto-emit path.

    :param platform: The :class:`DecisioningPlatform` instance under
        test.
    :param name: Server name on the AdCP capabilities envelope. Defaults
        to ``type(platform).__name__``.
    :param advertise_all: Forwarded to
        :func:`create_adcp_server_from_platform` and
        :func:`create_mcp_server`. Default ``False`` (override-detection
        filter on; matches :func:`serve`).
    :param auto_emit_completion_webhooks: Forwarded to
        :func:`create_adcp_server_from_platform`. Default ``False`` for
        test ergonomics — production :func:`serve` defaults to ``True``.
    :param factory_kwargs: Forwarded to
        :func:`create_adcp_server_from_platform` (executor, registry,
        webhook_sender, etc.).

    :returns: A Starlette ASGI application. Usable with
        ``starlette.testclient.TestClient``,
        ``httpx.AsyncClient(app=app, ...)``, or any ASGI test harness.
    """
    from adcp.decisioning.serve import create_adcp_server_from_platform
    from adcp.server.serve import create_mcp_server

    handler, _executor, _registry = create_adcp_server_from_platform(
        platform,
        advertise_all=advertise_all,
        auto_emit_completion_webhooks=auto_emit_completion_webhooks,
        **factory_kwargs,
    )
    server_name = name or type(platform).__name__
    mcp = create_mcp_server(handler, name=server_name, advertise_all=advertise_all)
    return mcp.streamable_http_app()


__all__ = ["build_asgi_app", "make_request_context"]
