"""Multi-agent topology manifest served at ``/.well-known/adcp-agents.json``.

Per AdCP spec (``schemas/source/adcp-agents.json``) every AdCP host
publishes an origin-scoped manifest enumerating the agents it serves.
Buyers, conformance runners, and tooling fetch the well-known URL once
and discover the full topology of the publisher in a single request,
instead of probing tenant URLs out of band.

This module owns:

1. :func:`build_manifest` — a pure function that produces the manifest
   document from the configured handler name + transports + bind
   coordinates. Easy to unit-test, no Starlette dependency.
2. :func:`make_discovery_route` — wires the document into a Starlette
   :class:`~starlette.routing.Route` so the SDK's ``serve()`` can
   compose it onto every HTTP transport (``streamable-http``, ``a2a``,
   ``both``).

Stdio has no HTTP surface and skips the route entirely.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

#: Path the manifest is served at. Per AdCP spec — operators MUST NOT
#: change this; consumers fetch from the well-known location only.
DISCOVERY_PATH = "/.well-known/adcp-agents.json"

#: Manifest schema version this builder emits. Consumers SHOULD ignore
#: unknown top-level fields rather than fail on version mismatch (per
#: spec), so bumping minor versions is safe.
MANIFEST_VERSION = "1.0"

#: ``$schema`` URI emitted in the manifest. Matches the canonical
#: location consumers use for validation.
MANIFEST_SCHEMA_URI = "/schemas/adcp-agents.json"


Transport = Literal["mcp", "a2a"]


def _normalize_agent_id(name: str) -> str:
    """Coerce a human-friendly handler name to a manifest-legal
    ``agent_id``.

    The schema requires lowercase alphanumeric with hyphens/underscores,
    no leading/trailing separators, 1-64 characters. Most adopters pass
    something like ``"My Seller"`` to ``serve(name=...)``; lower-case it
    and replace illegal runs with ``-``. Falls back to ``"agent"`` if
    the input lowers to nothing legal (defensive — empty / all-symbol
    names would otherwise produce an invalid manifest).
    """
    out: list[str] = []
    for ch in name.lower():
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        else:
            out.append("-")
    cleaned = "".join(out).strip("-_")
    # Collapse runs of separators — looks better and stays under the
    # 64-char cap on long names. Strip BEFORE the length cap so the
    # truncation never lands on a separator that would be stripped
    # away (which would make ``agent_id`` len differ from len(cleaned)
    # in surprising ways).
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    cleaned = cleaned.strip("-_")
    if not cleaned:
        return "agent"
    return cleaned[:64].strip("-_") or "agent"


def _agent_url(transport: Transport, base_url: str) -> str:
    """Return the agent endpoint URL for a given transport.

    For ``mcp`` the streamable-HTTP endpoint lives at ``/mcp``. For
    ``a2a`` the agent's base URL is the root — the agent-card lives at
    ``<base>/.well-known/agent-card.json``.
    """
    base = base_url.rstrip("/")
    if transport == "mcp":
        return f"{base}/mcp"
    return base or "/"


def build_manifest(
    *,
    name: str,
    transports: list[Transport],
    base_url: str,
    description: str | None = None,
    specialisms: list[str] | None = None,
) -> dict[str, Any]:
    """Build the AdCP multi-agent topology manifest document.

    Pure function — no I/O, no globals — so it's trivial to unit-test
    and reuse in adopter tooling that wants to publish a static
    manifest from CI.

    :param name: Operator-supplied agent / platform name. Becomes the
        ``agent_id`` (after normalization to the schema's character
        class) and informs the contact ``name`` field.
    :param transports: Transports the binary serves. ``["mcp"]``,
        ``["a2a"]``, or ``["mcp", "a2a"]`` for ``transport="both"``.
        One manifest entry is emitted per transport — buyers route by
        transport, so each gets its own row even when they share a
        process.
    :param base_url: Origin the binary is reachable at, e.g.
        ``"https://sales.example.com"``. The manifest URL is built as
        ``<base_url>/mcp`` for MCP and ``<base_url>`` for A2A.
    :param description: Optional human-readable description surfaced in
        operator UIs and conformance reports.
    :param specialisms: Optional AdCP specialisms (e.g.
        ``["sales-non-guaranteed"]``). The schema requires ``minItems:
        1`` so when nothing is supplied we fall back to a minimal
        ``["adcp"]`` placeholder. Adopters who know their specialism
        SHOULD pass it explicitly.
    """
    # TODO(#381): infer specialisms from the handler's advertised
    # tools (e.g. presence of ``get_products`` → sales-non-guaranteed).
    # For now adopters pass them explicitly or accept the placeholder.
    effective_specialisms = list(specialisms) if specialisms else ["adcp"]

    base_id = _normalize_agent_id(name)
    agents: list[dict[str, Any]] = []
    for transport in transports:
        # When emitting two rows from the same binary the schema requires
        # unique agent_ids — suffix with the transport so ``foo-mcp`` and
        # ``foo-a2a`` are both legal and self-describing.
        agent_id = f"{base_id}-{transport}" if len(transports) > 1 else base_id
        entry: dict[str, Any] = {
            "agent_id": agent_id,
            "url": _agent_url(transport, base_url),
            "transport": transport,
            "specialisms": effective_specialisms,
        }
        if description:
            entry["description"] = description
        agents.append(entry)

    # Truncate to whole-hour granularity so consecutive requests within
    # the same hour produce byte-identical manifests — lets HTTP caches
    # (CDNs, conformance runners polling on a loop) collapse repeated
    # fetches instead of seeing a fresh second-resolution timestamp on
    # every hit. Hour-resolution is well within the spec's "informational
    # only" semantics for ``last_updated``.
    last_updated = (
        datetime.now(timezone.utc)
        .replace(minute=0, second=0, microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    manifest: dict[str, Any] = {
        "$schema": MANIFEST_SCHEMA_URI,
        "version": MANIFEST_VERSION,
        "agents": agents,
        "last_updated": last_updated,
    }
    if name:
        manifest["contact"] = {"name": name}
    return manifest


def make_discovery_route(
    *,
    name: str,
    transports: list[Transport],
    base_url: str,
    description: str | None = None,
    specialisms: list[str] | None = None,
) -> Route:
    """Build a Starlette :class:`Route` serving the discovery manifest.

    The route is GET-only — POST / PUT / etc. fall through to
    Starlette's default 405 handler, which is the correct behavior for
    a read-only, unauthenticated discovery document.

    The manifest is rebuilt per request so ``last_updated`` reflects
    the current time. The build is cheap (a few hundred bytes of JSON),
    well below the noise floor of any production traffic.
    """

    async def _handler(_request: Request) -> JSONResponse:
        manifest = build_manifest(
            name=name,
            transports=transports,
            base_url=base_url,
            description=description,
            specialisms=specialisms,
        )
        return JSONResponse(manifest)

    return Route(DISCOVERY_PATH, _handler, methods=["GET"])


#: Hosts the spec lets us project as ``http://`` — the AdCP discovery
#: schema's ``url`` field requires ``^https://`` for non-loopback
#: targets, but consumers MAY accept ``http://`` for literal localhost
#: so a dev binary works without TLS scaffolding.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def resolve_base_url(host: str, port: int, base_url: str | None = None) -> str:
    """Construct an origin URL from a bound host/port pair, enforcing
    the spec's ``https://`` requirement for non-loopback targets.

    The AdCP discovery schema requires ``url`` to match ``^https://``
    on every agent entry; the only documented exception is loopback
    (``127.0.0.1`` / ``localhost`` / ``::1``) for dev binaries that
    haven't terminated TLS yet. This resolver therefore:

    * Projects ``0.0.0.0`` (wildcard) to ``http://127.0.0.1:<port>`` —
      it's a dev-only convenience and the projection IS loopback.
    * Returns ``http://<host>:<port>`` for literal loopback hosts.
    * Pass-through any caller-supplied ``base_url`` that already starts
      with ``https://``.
    * Raises :class:`ValueError` for non-loopback binds without an
      explicit ``base_url=`` (operator MUST publish a TLS URL), and for
      explicit ``base_url=`` that uses ``http://`` against a non-
      loopback host (refuse to publish a non-conformant manifest).

    Raise-at-boot is deliberate: a quietly-mis-published manifest
    survives in CDNs and conformance reports for hours, so we make the
    operator notice on launch instead.
    """
    is_loopback = host in _LOOPBACK_HOSTS or host in ("0.0.0.0", "::", "")

    if base_url is not None:
        if base_url.startswith("https://"):
            return base_url
        if base_url.startswith("http://") and is_loopback:
            return base_url
        raise ValueError(
            "Discovery manifest requires an https:// base_url for non-"
            f"localhost binds (got base_url={base_url!r}, host={host!r}). "
            "The AdCP discovery schema mandates https:// on every "
            "agent entry — pass base_url='https://your-host:port' to "
            "serve()."
        )

    if not is_loopback:
        raise ValueError(
            "Discovery manifest requires base_url= for non-localhost "
            f"binds (host={host!r}); the AdCP discovery schema mandates "
            "https:// URLs and the SDK won't synthesize an http:// URL "
            "for a routable interface. Pass base_url='https://your-"
            "host:port' to serve()."
        )

    # ``0.0.0.0`` is a wildcard bind, not a routable origin. Project to
    # localhost so a default-config dev binary serves a usable manifest
    # for local testing.
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return f"http://{display_host}:{port}"
