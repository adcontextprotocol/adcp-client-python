"""Tests for :mod:`adcp.decisioning.compose`.

Covers:

* ``before`` returning ``None`` falls through to the inner method.
* ``before`` returning :class:`ShortCircuit` short-circuits; inner
  is not called.
* ``after`` runs on the inner result.
* ``after`` runs on the short-circuit result with original
  ``params`` + ``ctx`` available.
* ``after`` wraps both paths uniformly.
* :class:`TypeError` when ``inner`` is not callable at wrap time.
* :class:`TypeError` when ``before`` returns a bare non-
  :class:`ShortCircuit` value.
* :func:`require_account_match` raises :class:`AdcpError`
  ``PERMISSION_DENIED`` on mismatch, falls through on match.
* :func:`require_advertiser_match` similar.
* :func:`require_org_scope` similar.
* Composing three security hooks chains correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from adcp.decisioning.compose import (
    ShortCircuit,
    compose_method,
    require_account_match,
    require_advertiser_match,
    require_org_scope,
)
from adcp.decisioning.context import RequestContext
from adcp.decisioning.types import Account, AdcpError

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclass
class _Req:
    """Stand-in for a Pydantic request model — adopters' real platform
    methods take :class:`adcp.types.GetProductsRequest` etc.; tests
    use a plain dataclass to keep the fixture surface minimal."""

    account_id: str = "acct_1"
    advertiser_id: str = "adv_1"
    organization_id: str = "org_1"
    payload: str = "p"


@dataclass
class _Res:
    value: str = "inner-result"


def _make_ctx(
    account_id: str = "acct_1",
    metadata: dict[str, Any] | None = None,
) -> RequestContext[dict[str, Any]]:
    """Build a :class:`RequestContext` with a typed :class:`Account`
    for security-hook tests. Production code receives ``ctx`` from
    the dispatch hydration helper; tests construct directly."""
    if metadata is None:
        metadata = {"advertiser_id": "adv_1", "organization_id": "org_1"}
    return RequestContext(
        account=Account(id=account_id, metadata=metadata),
    )


# ---------------------------------------------------------------------------
# compose_method — core semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_returns_none_falls_through_to_inner() -> None:
    """``before`` returning ``None`` runs the inner method
    unchanged."""
    inner_calls: list[_Req] = []

    async def inner(req: _Req, ctx: RequestContext[Any]) -> _Res:
        inner_calls.append(req)
        return _Res(value="from-inner")

    async def before(req: _Req, ctx: RequestContext[Any]) -> None:
        return None

    wrapped = compose_method(inner, before=before)
    result = await wrapped(_Req(), _make_ctx())

    assert result.value == "from-inner"
    assert len(inner_calls) == 1


@pytest.mark.asyncio
async def test_before_short_circuit_skips_inner() -> None:
    """``before`` returning :class:`ShortCircuit` skips the inner
    method entirely; the wrapped value is returned to the caller."""
    inner_calls: list[_Req] = []

    async def inner(req: _Req, ctx: RequestContext[Any]) -> _Res:
        inner_calls.append(req)
        return _Res(value="from-inner")

    async def before(req: _Req, ctx: RequestContext[Any]) -> ShortCircuit[_Res] | None:
        return ShortCircuit(value=_Res(value="short-circuited"))

    wrapped = compose_method(inner, before=before)
    result = await wrapped(_Req(), _make_ctx())

    assert result.value == "short-circuited"
    assert inner_calls == []


@pytest.mark.asyncio
async def test_after_runs_on_inner_result() -> None:
    """``after`` wraps the inner method's return value."""

    async def inner(req: _Req, ctx: RequestContext[Any]) -> _Res:
        return _Res(value="raw")

    async def after(result: _Res, req: _Req, ctx: RequestContext[Any]) -> _Res:
        return _Res(value=f"wrapped:{result.value}")

    wrapped = compose_method(inner, after=after)
    result = await wrapped(_Req(), _make_ctx())

    assert result.value == "wrapped:raw"


@pytest.mark.asyncio
async def test_after_runs_on_short_circuit_with_original_params_and_ctx() -> None:
    """``after`` runs even when ``before`` short-circuits, and sees
    the original ``params`` and ``ctx`` (not the short-circuit
    payload)."""
    seen: dict[str, Any] = {}

    async def inner(req: _Req, ctx: RequestContext[Any]) -> _Res:
        raise AssertionError("inner must not be called on short-circuit")

    async def before(req: _Req, ctx: RequestContext[Any]) -> ShortCircuit[_Res] | None:
        return ShortCircuit(value=_Res(value="cached"))

    async def after(result: _Res, req: _Req, ctx: RequestContext[Any]) -> _Res:
        seen["result"] = result.value
        seen["req_payload"] = req.payload
        seen["account_id"] = ctx.account.id
        return _Res(value=f"after:{result.value}")

    wrapped = compose_method(inner, before=before, after=after)
    result = await wrapped(_Req(payload="ping"), _make_ctx(account_id="acct_x"))

    assert result.value == "after:cached"
    assert seen == {
        "result": "cached",
        "req_payload": "ping",
        "account_id": "acct_x",
    }


@pytest.mark.asyncio
async def test_after_wraps_both_paths_uniformly() -> None:
    """A single ``after`` hook wraps both the inner-result path and
    the short-circuit path identically — adopters wire enrichment
    once and it covers cache-hit + cache-miss without branching."""

    async def inner(req: _Req, ctx: RequestContext[Any]) -> _Res:
        return _Res(value="inner")

    async def before(req: _Req, ctx: RequestContext[Any]) -> ShortCircuit[_Res] | None:
        if req.payload == "skip":
            return ShortCircuit(value=_Res(value="cached"))
        return None

    async def after(result: _Res, req: _Req, ctx: RequestContext[Any]) -> _Res:
        return _Res(value=f"wrapped:{result.value}")

    wrapped = compose_method(inner, before=before, after=after)

    inner_path = await wrapped(_Req(payload="run"), _make_ctx())
    short_path = await wrapped(_Req(payload="skip"), _make_ctx())

    assert inner_path.value == "wrapped:inner"
    assert short_path.value == "wrapped:cached"


def test_typeerror_when_inner_not_callable() -> None:
    """Wrap-time validation: passing a non-callable ``inner`` raises
    immediately so adopters who reference an optional method that
    wasn't implemented on the platform fail at module load, not at
    first traffic."""
    with pytest.raises(TypeError, match="must be callable"):
        compose_method(None)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="must be callable"):
        compose_method("not a function")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_typeerror_when_before_returns_bare_value() -> None:
    """The discriminated :class:`ShortCircuit` wrapper catches the
    ``None``-as-sentinel footgun: an adopter who returns a bare value
    (forgetting the wrapper) gets a clear :class:`TypeError`, not
    silent short-circuit-with-bare-value."""

    async def inner(req: _Req, ctx: RequestContext[Any]) -> _Res:
        return _Res(value="inner")

    async def before(req: _Req, ctx: RequestContext[Any]) -> Any:
        return _Res(value="bare-not-wrapped")

    wrapped = compose_method(inner, before=before)
    with pytest.raises(TypeError, match="ShortCircuit"):
        await wrapped(_Req(), _make_ctx())


# ---------------------------------------------------------------------------
# Security composer helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_account_match_falls_through_on_match() -> None:
    """When ``ctx.account.id`` matches the request's ``account_id``,
    the hook returns ``None`` (fall through) so the inner method
    runs."""
    hook = require_account_match()
    result = await hook(_Req(account_id="acct_1"), _make_ctx(account_id="acct_1"))
    assert result is None


@pytest.mark.asyncio
async def test_require_account_match_raises_on_mismatch() -> None:
    """When ``ctx.account.id`` does not match the request's
    ``account_id``, the hook raises :class:`AdcpError` with
    ``PERMISSION_DENIED``."""
    hook = require_account_match()
    with pytest.raises(AdcpError) as excinfo:
        await hook(
            _Req(account_id="acct_other"),
            _make_ctx(account_id="acct_1"),
        )
    assert excinfo.value.code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_require_account_match_custom_field() -> None:
    """The factory accepts a custom field name for non-default
    request shapes."""

    @dataclass
    class _CustomReq:
        tenant_id: str

    hook = require_account_match(expected_account_field="tenant_id")
    assert await hook(_CustomReq(tenant_id="acct_1"), _make_ctx(account_id="acct_1")) is None
    with pytest.raises(AdcpError):
        await hook(_CustomReq(tenant_id="other"), _make_ctx(account_id="acct_1"))


@pytest.mark.asyncio
async def test_require_advertiser_match_falls_through_on_match() -> None:
    """When ``ctx.account.metadata['advertiser_id']`` equals the
    request's ``advertiser_id``, the hook returns ``None``."""
    hook = require_advertiser_match()
    result = await hook(
        _Req(advertiser_id="adv_1"),
        _make_ctx(metadata={"advertiser_id": "adv_1"}),
    )
    assert result is None


@pytest.mark.asyncio
async def test_require_advertiser_match_raises_on_mismatch() -> None:
    """When the request's advertiser does not match the metadata
    scope, the hook raises :class:`AdcpError`."""
    hook = require_advertiser_match()
    with pytest.raises(AdcpError) as excinfo:
        await hook(
            _Req(advertiser_id="adv_other"),
            _make_ctx(metadata={"advertiser_id": "adv_1"}),
        )
    assert excinfo.value.code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_require_advertiser_match_raises_on_missing_metadata() -> None:
    """When ``ctx.account.metadata`` lacks ``advertiser_id``, the
    hook denies — adopters who claim per-advertiser scope must wire
    ``advertiser_id`` into the metadata; missing it is a
    misconfiguration that defaults to deny."""
    hook = require_advertiser_match()
    with pytest.raises(AdcpError):
        await hook(_Req(advertiser_id="adv_1"), _make_ctx(metadata={}))


@pytest.mark.asyncio
async def test_require_org_scope_falls_through_on_match() -> None:
    """When ``ctx.account.metadata['organization_id']`` equals the
    request's ``organization_id``, the hook returns ``None``."""
    hook = require_org_scope()
    result = await hook(
        _Req(organization_id="org_1"),
        _make_ctx(metadata={"organization_id": "org_1"}),
    )
    assert result is None


@pytest.mark.asyncio
async def test_require_org_scope_raises_on_mismatch() -> None:
    """When the request's org does not match the metadata scope, the
    hook raises :class:`AdcpError`."""
    hook = require_org_scope()
    with pytest.raises(AdcpError) as excinfo:
        await hook(
            _Req(organization_id="org_other"),
            _make_ctx(metadata={"organization_id": "org_1"}),
        )
    assert excinfo.value.code == "PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# Composing security hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chained_security_hooks_pass_when_all_match() -> None:
    """Adopters chain multiple security composers by routing through
    a single ``before`` that calls each in turn. Inner runs only when
    every hook falls through."""
    inner_calls: list[_Req] = []

    async def inner(req: _Req, ctx: RequestContext[Any]) -> _Res:
        inner_calls.append(req)
        return _Res(value="ok")

    account_hook = require_account_match()
    advertiser_hook = require_advertiser_match()
    org_hook = require_org_scope()

    async def chained(req: _Req, ctx: RequestContext[Any]) -> ShortCircuit[_Res] | None:
        for hook in (account_hook, advertiser_hook, org_hook):
            early = await hook(req, ctx)
            if early is not None:
                return early
        return None

    wrapped = compose_method(inner, before=chained)
    result = await wrapped(
        _Req(account_id="acct_1", advertiser_id="adv_1", organization_id="org_1"),
        _make_ctx(
            account_id="acct_1",
            metadata={"advertiser_id": "adv_1", "organization_id": "org_1"},
        ),
    )

    assert result.value == "ok"
    assert len(inner_calls) == 1


@pytest.mark.asyncio
async def test_chained_security_hooks_deny_at_first_failure() -> None:
    """When chained, the first failing hook raises and short-
    circuits the chain — subsequent hooks and the inner method don't
    run."""
    inner_calls: list[_Req] = []
    advertiser_hook_calls: list[_Req] = []

    async def inner(req: _Req, ctx: RequestContext[Any]) -> _Res:
        inner_calls.append(req)
        return _Res(value="ok")

    account_hook = require_account_match()

    async def tracking_advertiser_hook(
        req: _Req, ctx: RequestContext[Any]
    ) -> ShortCircuit[_Res] | None:
        advertiser_hook_calls.append(req)
        return await require_advertiser_match()(req, ctx)

    async def chained(req: _Req, ctx: RequestContext[Any]) -> ShortCircuit[_Res] | None:
        for hook in (account_hook, tracking_advertiser_hook):
            early = await hook(req, ctx)
            if early is not None:
                return early
        return None

    wrapped = compose_method(inner, before=chained)

    with pytest.raises(AdcpError) as excinfo:
        await wrapped(
            _Req(account_id="acct_other"),
            _make_ctx(account_id="acct_1"),
        )

    assert excinfo.value.code == "PERMISSION_DENIED"
    assert advertiser_hook_calls == []
    assert inner_calls == []
