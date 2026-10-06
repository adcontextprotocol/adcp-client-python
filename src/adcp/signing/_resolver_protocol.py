"""Protocol bootstrap over an already SSRF-pinned HTTP client."""

from __future__ import annotations

import codecs
from collections.abc import AsyncIterator
from typing import Any

import httpx
import httpx2

from adcp.signing._bounded_http import ResponseTooLargeError, async_read_limited_bytes
from adcp.signing._strict_json import parse_strict_json


def protocol_validation_failed(error: BaseException) -> bool:
    """Recognize SDK parse failures, including task-group wrappers on 3.10+."""
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (ValueError, TypeError)):
            return True
        children = getattr(current, "exceptions", ())
        if isinstance(children, tuple):
            pending.extend(child for child in children if isinstance(child, BaseException))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _validate_protocol_object(content: bytes) -> dict[str, Any]:
    """Reject ambiguous JSON and malformed containers before SDK conversion."""
    parsed = parse_strict_json(content)
    if not isinstance(parsed, dict):
        raise ValueError("capabilities response is not a JSON object")
    result = parsed.get("result")
    if isinstance(result, dict):
        for name in ("content", "parts"):
            if name in result and not isinstance(result[name], list):
                raise ValueError(f"capabilities {name} must be an array")
        artifacts = result.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if (
                    isinstance(artifact, dict)
                    and "parts" in artifact
                    and not isinstance(artifact["parts"], list)
                ):
                    raise ValueError("capabilities artifact parts must be an array")
    return parsed


def _extract_strict_mcp_capabilities(result: Any) -> dict[str, Any] | None:
    """Apply MCP extraction while parsing a text trust root as strict JSON."""
    if result.is_error:
        return None
    structured = result.structured_content
    if isinstance(structured, dict) and not (len(structured) == 1 and "adcp_error" in structured):
        return structured
    for part in result.content:
        if part.type != "text":
            continue
        try:
            parsed = parse_strict_json(part.text.encode("utf-8"))
        except ValueError:
            continue
        if isinstance(parsed, dict) and not (len(parsed) == 1 and "adcp_error" in parsed):
            return parsed
    return None


class DiscoveryTransport(httpx.AsyncBaseTransport):
    """Bound every protocol response and keep discovery on the pinned origin.

    The upstream client owns its IP-pinned transport. In particular, an A2A
    card cannot move the subsequent message/send to another origin.
    """

    def __init__(self, client: httpx.AsyncClient, url: str, limit: int, redirects: int) -> None:
        self.client = client
        self.url = httpx.URL(url)
        self.limit = limit
        self.redirects = redirects
        self.error: Exception | None = None
        self.final_url = url

    def check_origin(self, url: httpx.URL) -> None:
        if (url.scheme, url.host, url.port) != (self.url.scheme, self.url.host, self.url.port):
            raise ValueError("capabilities protocol attempted to leave the agent origin")

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        from adcp.signing.agent_resolver import AgentResolverError

        try:
            body = await request.aread()
            url = request.url
            for hop in range(self.redirects + 1):
                self.check_origin(url)
                headers = dict(request.headers)
                headers["accept-encoding"] = "identity"
                upstream = self.client.build_request(
                    request.method, url, headers=headers, content=body
                )
                response = await self.client.send(upstream, stream=True, follow_redirects=False)
                if response.is_redirect:
                    await response.aclose()
                    if hop == self.redirects:
                        raise AgentResolverError(
                            "capabilities_unreachable",
                            f"capabilities fetch hit redirect limit ({self.redirects})",
                        )
                    url = url.join(response.headers["location"])
                    continue
                if response.status_code >= 400 and not (
                    request.method in {"GET", "DELETE"} and response.status_code == 405
                ):
                    await response.aclose()
                    raise AgentResolverError(
                        "capabilities_unreachable",
                        f"capabilities fetch returned HTTP {response.status_code}",
                    )
                self.final_url = str(url)
                if response.headers.get("content-type", "").lower().startswith("text/event-stream"):
                    # The SDK may stop reading after the matching response; do
                    # not buffer an SSE stream that the server keeps open.
                    if (
                        response.headers.get("content-encoding", "identity").strip().lower()
                        != "identity"
                    ):
                        await response.aclose()
                        raise ValueError("encoded HTTP responses are not accepted")
                    if response.is_stream_consumed and len(response.content) > self.limit:
                        await response.aclose()
                        raise ResponseTooLargeError(
                            limit=self.limit, received=len(response.content)
                        )
                    if response.is_stream_consumed:
                        content = response.content
                        await response.aclose()
                        response = httpx.Response(
                            response.status_code,
                            headers=response.headers,
                            stream=httpx.ByteStream(content),
                        )
                    assert isinstance(response.stream, httpx.AsyncByteStream)
                    response.stream = _StrictSseStream(_LimitedStream(response.stream, self), self)
                    return response
                try:
                    content = await async_read_limited_bytes(response, limit=self.limit)
                    if content and 200 <= response.status_code < 300:
                        _validate_protocol_object(content)
                    return httpx.Response(
                        response.status_code, headers=response.headers, content=content
                    )
                finally:
                    await response.aclose()
        except AgentResolverError as exc:
            self.error = exc
            raise
        except ValueError as exc:
            error = AgentResolverError("capabilities_invalid", f"capabilities {exc}")
            self.error = error
            raise error from exc
        except (httpx.HTTPError, OSError) as exc:
            error = AgentResolverError(
                "capabilities_unreachable", f"capabilities fetch failed: {exc}"
            )
            self.error = error
            raise error from exc
        raise AssertionError("redirect loop exhausted")


class _LimitedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, transport: DiscoveryTransport) -> None:
        self.stream = stream
        self.transport = transport

    async def __aiter__(self) -> AsyncIterator[bytes]:
        from adcp.signing.agent_resolver import AgentResolverError

        size = 0
        decoder = codecs.getincrementaldecoder("utf-8")()
        async for chunk in self.stream:
            size += len(chunk)
            if size > self.transport.limit:
                error = AgentResolverError(
                    "capabilities_invalid",
                    f"capabilities response exceeds {self.transport.limit} bytes",
                )
                self.transport.error = error
                raise error
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError as exc:
                error = AgentResolverError("capabilities_invalid", "capabilities invalid_utf8")
                self.transport.error = error
                raise error from exc
            yield chunk
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            error = AgentResolverError("capabilities_invalid", "capabilities invalid_utf8")
            self.transport.error = error
            raise error from exc

    async def aclose(self) -> None:
        await self.stream.aclose()


class _StrictSseStream(httpx.AsyncByteStream):
    """Validate each complete SSE message before its dispatching blank line."""

    def __init__(self, stream: httpx.AsyncByteStream, transport: DiscoveryTransport) -> None:
        self.stream = stream
        self.transport = transport

    async def __aiter__(self) -> AsyncIterator[bytes]:
        from adcp.signing.agent_resolver import AgentResolverError

        response = httpx.Response(200, stream=self.stream)
        event = "message"
        data: list[str] = []
        try:
            async for line in response.aiter_lines():
                if not line:
                    if event == "message" and data and any(data):
                        _validate_protocol_object("\n".join(data).encode("utf-8"))
                    event = "message"
                    data = []
                elif not line.startswith(":"):
                    name, _, value = line.partition(":")
                    if value.startswith(" "):
                        value = value[1:]
                    if name == "event":
                        event = value or "message"
                    elif name == "data":
                        data.append(value)
                yield (line + "\n").encode("utf-8")
        except ValueError as exc:
            error = AgentResolverError("capabilities_invalid", f"capabilities {exc}")
            self.transport.error = error
            raise error from exc

    async def aclose(self) -> None:
        await self.stream.aclose()


class _MCPStream(httpx2.AsyncByteStream):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.response.aiter_raw():
            yield chunk

    async def aclose(self) -> None:
        await self.response.aclose()


class _MCPTransport(httpx2.AsyncBaseTransport):
    """Bridge MCP v2's httpx2 types to the resolver's pinned httpx transport."""

    def __init__(self, transport: DiscoveryTransport) -> None:
        self.transport = transport

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self.transport.handle_async_request(
            httpx.Request(
                request.method,
                str(request.url),
                headers=request.headers.raw,
                content=await request.aread(),
            )
        )
        if response.is_stream_consumed:
            return httpx2.Response(
                response.status_code, headers=response.headers.raw, content=response.content
            )
        return httpx2.Response(
            response.status_code, headers=response.headers.raw, stream=_MCPStream(response)
        )


async def fetch_protocol_capabilities(
    transport: DiscoveryTransport, protocol: str, timeout_seconds: float
) -> dict[str, Any]:
    from adcp.signing.agent_resolver import AgentResolverError

    if protocol == "mcp":
        from mcp import ClientSession, types

        from adcp.protocols.mcp import streamablehttp_client

        def factory(**kwargs: Any) -> httpx2.AsyncClient:
            return httpx2.AsyncClient(
                transport=_MCPTransport(transport),
                trust_env=False,
                follow_redirects=False,
                **kwargs,
            )

        async with streamablehttp_client(
            str(transport.url),
            timeout=timeout_seconds,
            httpx_client_factory=factory,
            terminate_on_close=False,
        ) as (read, write, get_session_id):
            async with ClientSession(read, write, read_timeout_seconds=timeout_seconds) as session:
                initialized = await session.initialize()
                # call_tool also fetches tools/list to validate output schemas.
                # Identity bootstrap needs only this response; the inventory
                # can be much larger than the capabilities response budget.
                result = await session.send_request(
                    types.CallToolRequest(
                        method="tools/call",
                        params=types.CallToolRequestParams(
                            name="get_adcp_capabilities", arguments={}
                        ),
                    ),
                    types.CallToolResult,
                )
                data = _extract_strict_mcp_capabilities(result)
            if not result.is_error and isinstance(data, dict) and (session_id := get_session_id()):
                # Terminate only on success, inside the original total deadline.
                # Network cleanup in the transport's finally would start a new
                # request after timeout/cancellation and could stall indefinitely.
                request = transport.client.build_request(
                    "DELETE",
                    str(transport.url),
                    headers={
                        "mcp-session-id": session_id,
                        "mcp-protocol-version": initialized.protocol_version,
                    },
                )
                try:
                    response = await transport.client.send(
                        request, stream=True, follow_redirects=False
                    )
                    await response.aclose()
                except httpx.HTTPError:
                    # Session termination is best effort; discovery already
                    # completed. Cancellation still propagates unchanged.
                    pass
        if result.is_error:
            raise AgentResolverError("capabilities_unreachable", "get_adcp_capabilities failed")
    else:
        from a2a.client import A2ACardResolver, ClientConfig, ClientFactory

        from adcp.protocols.a2a import A2AAdapter
        from adcp.types.core import AgentConfig, Protocol

        async with httpx.AsyncClient(
            transport=transport, timeout=timeout_seconds, trust_env=False, follow_redirects=False
        ) as client:
            card = await A2ACardResolver(
                httpx_client=client, base_url=str(transport.url)
            ).get_agent_card()
            adapter = A2AAdapter(
                AgentConfig(
                    id="agent-resolver",
                    agent_uri=str(transport.url),
                    protocol=Protocol.A2A,
                    timeout=timeout_seconds,
                )
            )
            # Bootstrap reads only the raw identity fields, including fields
            # from older/newer AdCP versions. Do not gate key discovery on
            # validation against the SDK's current full capabilities schema.
            adapter.response_validation_mode = "off"
            adapter._httpx_client = client
            adapter._a2a_client = ClientFactory(
                ClientConfig(
                    httpx_client=client, streaming=False, supported_protocol_bindings=["JSONRPC"]
                )
            ).create(card)
            try:
                task = await adapter.get_adcp_capabilities({})
                if not task.success:
                    raise AgentResolverError(
                        "capabilities_unreachable", task.error or "get_adcp_capabilities failed"
                    )
                data = task.data
            finally:
                await adapter.close()
    if not isinstance(data, dict):
        raise AgentResolverError(
            "capabilities_invalid", "get_adcp_capabilities returned no JSON object"
        )
    return data
