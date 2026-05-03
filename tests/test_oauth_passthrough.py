"""Tests for ``create_oauth_passthrough_resolver``.

Mirrors the JS-side ``test/server-adapters-oauth-passthrough-resolver.test.js``
coverage. Uses ``respx`` for HTTP mocking against a real
:class:`UpstreamHttpClient` so the full bearer-injection /
404-translation / error-projection path runs end-to-end (matching the
posture in :mod:`tests.test_upstream_helpers`).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from adcp.decisioning import (
    Account,
    AdcpError,
    AuthInfo,
    DynamicBearer,
    ResolveContext,
    create_oauth_passthrough_resolver,
    create_upstream_http_client,
)
from adcp.types import AccountReference
from adcp.types.generated_poc.core.account_ref import AccountReference1

BASE = "https://upstream.example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ref_by_id(account_id: str) -> AccountReference:
    return AccountReference(root=AccountReference1(account_id=account_id))


def _ref_natural_key() -> dict[str, Any]:
    """Natural-key arm — passed as a raw dict, which ``ref_account_id``
    accepts. Avoids constructing the typed ``AccountReference2`` model
    here (test only needs the no-account_id branch)."""
    return {"brand": {"domain": "acme.com"}, "operator": "pinnacle.com"}


async def _passthrough_token(ctx: Any) -> str:
    """``DynamicBearer.get_token`` callback that reads the bearer
    from the per-request auth_context (the AuthInfo dict the resolver
    forwards). Mirrors how a real Shape B adapter wires it."""
    if ctx is None:
        return ""
    # ctx is the AuthInfo instance (default ``get_auth_context``).
    token = getattr(ctx, "credential", None)
    if token is None:
        # Test paths that pass a plain mapping rather than AuthInfo.
        return str(ctx.get("token", "")) if isinstance(ctx, dict) else ""
    # AuthInfo carries the bearer on .credential.token for OAuth.
    return getattr(token, "token", "") or ""


def _to_account(row: dict[str, Any], _ctx: ResolveContext | None) -> Account[Any]:
    return Account(
        id=str(row["id"]),
        name=str(row.get("name", "")),
        status="active",
        metadata={"upstream_id": row["id"]},
    )


def _make_client(token_factory: Any = _passthrough_token) -> Any:
    return create_upstream_http_client(
        BASE,
        auth=DynamicBearer(get_token=token_factory),
    )


# ---------------------------------------------------------------------------
# Ref-shape handling
# ---------------------------------------------------------------------------


@respx.mock
async def test_returns_none_for_natural_key_ref_without_calling_upstream() -> None:
    route = respx.get(f"{BASE}/me/adaccounts").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = _make_client()
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/me/adaccounts",
        to_account=_to_account,
    )

    result = await resolve(_ref_natural_key(), ResolveContext())
    assert result is None
    assert not route.called
    await client.aclose()


@respx.mock
async def test_returns_none_when_ref_is_none_without_calling_upstream() -> None:
    route = respx.get(f"{BASE}/me/adaccounts").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = _make_client()
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/me/adaccounts",
        to_account=_to_account,
    )

    result = await resolve(None, ResolveContext())
    assert result is None
    assert not route.called
    await client.aclose()


# ---------------------------------------------------------------------------
# Upstream lookup + match
# ---------------------------------------------------------------------------


@respx.mock
async def test_returns_mapped_account_when_account_id_matches_upstream_row() -> None:
    respx.get(f"{BASE}/me/adaccounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "acc_1", "name": "Acme"},
                    {"id": "acc_2", "name": "Nike"},
                ]
            },
        )
    )
    client = _make_client()
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/me/adaccounts",
        to_account=_to_account,
    )

    ctx = ResolveContext(
        auth_info=AuthInfo(kind="oauth", principal="p1"),
    )
    # Inject the bearer the DynamicBearer resolver will read.
    ctx.auth_info.credential = type("_C", (), {"token": "t_buyer_1"})()  # type: ignore[union-attr]

    result = await resolve(_ref_by_id("acc_1"), ctx)
    assert result is not None
    assert result.id == "acc_1"
    assert result.name == "Acme"
    assert result.metadata == {"upstream_id": "acc_1"}

    last = respx.calls.last.request
    assert last.headers["Authorization"] == "Bearer t_buyer_1"
    await client.aclose()


@respx.mock
async def test_returns_none_when_no_upstream_row_matches() -> None:
    respx.get(f"{BASE}/me/adaccounts").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "acc_1", "name": "Acme"}]})
    )
    client = _make_client()
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/me/adaccounts",
        to_account=_to_account,
    )

    result = await resolve(_ref_by_id("acc_unknown"), ResolveContext())
    assert result is None
    await client.aclose()


@respx.mock
async def test_returns_none_when_upstream_returns_none_body() -> None:
    # 404 → http client returns None (treat_404_as_none=True default).
    respx.get(f"{BASE}/me/adaccounts").mock(return_value=httpx.Response(404))
    client = _make_client()
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/me/adaccounts",
        to_account=_to_account,
    )

    result = await resolve(_ref_by_id("acc_1"), ResolveContext())
    assert result is None
    await client.aclose()


# ---------------------------------------------------------------------------
# Error pass-through
# ---------------------------------------------------------------------------


@respx.mock
async def test_upstream_401_raises_adcp_error() -> None:
    respx.get(f"{BASE}/me/adaccounts").mock(return_value=httpx.Response(401, text="unauthorized"))
    client = _make_client()
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/me/adaccounts",
        to_account=_to_account,
    )

    with pytest.raises(AdcpError) as exc_info:
        await resolve(_ref_by_id("acc_1"), ResolveContext())
    assert exc_info.value.code == "AUTH_REQUIRED"
    await client.aclose()


@respx.mock
async def test_upstream_500_raises_service_unavailable() -> None:
    respx.get(f"{BASE}/me/adaccounts").mock(return_value=httpx.Response(500))
    client = _make_client()
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/me/adaccounts",
        to_account=_to_account,
    )

    with pytest.raises(AdcpError) as exc_info:
        await resolve(_ref_by_id("acc_1"), ResolveContext())
    assert exc_info.value.code == "SERVICE_UNAVAILABLE"
    await client.aclose()


# ---------------------------------------------------------------------------
# Configurable extraction
# ---------------------------------------------------------------------------


@respx.mock
async def test_custom_id_field() -> None:
    respx.get(f"{BASE}/v1/adaccounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"account_uuid": "snap_act_42", "name": "Acme"},
                    {"account_uuid": "snap_act_43", "name": "Nike"},
                ]
            },
        )
    )
    client = _make_client()
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/v1/adaccounts",
        id_field="account_uuid",
        to_account=lambda row, _ctx: Account(
            id=row["account_uuid"],
            name=row["name"],
            status="active",
        ),
    )

    result = await resolve(_ref_by_id("snap_act_43"), ResolveContext())
    assert result is not None
    assert result.id == "snap_act_43"
    assert result.name == "Nike"
    await client.aclose()


@respx.mock
async def test_custom_extract_rows_receives_raw_response() -> None:
    # Some APIs nest deeper than the default unwrap — TikTok-shaped
    # ``data.list``. The custom callback gets the raw parsed body.
    respx.get(f"{BASE}/v2/me").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"list": [{"id": "a1", "name": "Acme"}]}},
        )
    )
    seen: list[Any] = []

    def extract(body: Any) -> list[dict[str, Any]]:
        seen.append(body)
        return list(body["data"]["list"])

    client = _make_client()
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/v2/me",
        extract_rows=extract,
        to_account=_to_account,
    )

    result = await resolve(_ref_by_id("a1"), ResolveContext())
    assert result is not None
    assert result.id == "a1"
    assert seen == [{"data": {"list": [{"id": "a1", "name": "Acme"}]}}]
    await client.aclose()


@respx.mock
async def test_default_extract_rows_handles_flat_list() -> None:
    respx.get(f"{BASE}/customers").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "a1", "name": "Acme"},
                {"id": "a2", "name": "Nike"},
            ],
        )
    )
    client = _make_client()
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/customers",
        to_account=_to_account,
    )

    result = await resolve(_ref_by_id("a2"), ResolveContext())
    assert result is not None
    assert result.name == "Nike"
    await client.aclose()


# ---------------------------------------------------------------------------
# to_account: sync + async
# ---------------------------------------------------------------------------


@respx.mock
async def test_async_to_account_is_awaited() -> None:
    respx.get(f"{BASE}/me/adaccounts").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "a1", "name": "Acme"}]})
    )

    async def to_account_async(row: dict[str, Any], _ctx: ResolveContext | None) -> Account[Any]:
        return Account(id=row["id"], name=row["name"], status="active")

    client = _make_client()
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/me/adaccounts",
        to_account=to_account_async,
    )

    result = await resolve(_ref_by_id("a1"), ResolveContext())
    assert result is not None
    assert result.id == "a1"
    await client.aclose()


@respx.mock
async def test_sync_to_account_returns_account_directly() -> None:
    respx.get(f"{BASE}/me/adaccounts").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "a1", "name": "Acme"}]})
    )

    def to_account_sync(row: dict[str, Any], _ctx: ResolveContext | None) -> Account[Any]:
        return Account(id=row["id"], name=row["name"], status="active")

    client = _make_client()
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/me/adaccounts",
        to_account=to_account_sync,
    )

    result = await resolve(_ref_by_id("a1"), ResolveContext())
    assert result is not None
    assert result.id == "a1"
    await client.aclose()


# ---------------------------------------------------------------------------
# Auth-context forwarding
# ---------------------------------------------------------------------------


@respx.mock
async def test_default_get_auth_context_forwards_auth_info_verbatim() -> None:
    respx.get(f"{BASE}/me/adaccounts").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "a1", "name": "Acme"}]})
    )

    seen_ctx: list[Any] = []

    async def capture_token(ctx: Any) -> str:
        seen_ctx.append(ctx)
        return "tok_x"

    client = create_upstream_http_client(BASE, auth=DynamicBearer(get_token=capture_token))
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/me/adaccounts",
        to_account=_to_account,
    )

    auth_info = AuthInfo(kind="oauth", principal="p1")
    await resolve(_ref_by_id("a1"), ResolveContext(auth_info=auth_info))
    assert seen_ctx == [auth_info]
    await client.aclose()


@respx.mock
async def test_custom_get_auth_context_threads_through() -> None:
    respx.get(f"{BASE}/me/adaccounts").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "a1", "name": "Acme"}]})
    )

    seen_ctx: list[Any] = []

    async def capture_token(ctx: Any) -> str:
        seen_ctx.append(ctx)
        return "tok_y"

    client = create_upstream_http_client(BASE, auth=DynamicBearer(get_token=capture_token))
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/me/adaccounts",
        get_auth_context=lambda ctx: {
            "principal": ctx.auth_info.principal if ctx and ctx.auth_info else None,
        },
        to_account=_to_account,
    )

    auth_info = AuthInfo(kind="oauth", principal="agent-1")
    await resolve(_ref_by_id("a1"), ResolveContext(auth_info=auth_info))
    assert seen_ctx == [{"principal": "agent-1"}]
    await client.aclose()


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


@respx.mock
async def test_resolve_passes_dict_ref_through() -> None:
    """Adopters that pass dicts straight from JSON deserialization
    should still get a match — ``ref_account_id`` accepts both."""
    respx.get(f"{BASE}/me/adaccounts").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "a1", "name": "Acme"}]})
    )
    client = _make_client()
    resolve = create_oauth_passthrough_resolver(
        http_client=client,
        list_endpoint="/me/adaccounts",
        to_account=_to_account,
    )

    result = await resolve({"account_id": "a1"}, ResolveContext())
    assert result is not None
    assert result.id == "a1"
    await client.aclose()
