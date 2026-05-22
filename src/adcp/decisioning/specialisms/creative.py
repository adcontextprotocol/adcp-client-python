"""CreativeBuilderPlatform Protocol — covers ``creative-template`` +
``creative-generative``.

A platform claiming either ``creative-template`` (stateless transform —
Bannerflow, Celtra) or ``creative-generative`` (brief-to-creative AI
agents — Pencil, Omneky, AdCreative.ai) implements the methods on
this Protocol. The slugs mirror ``schemas/cache/enums/specialism.json``.
The wire shape doesn't distinguish "transform a template" from
"generate from a brief" — both produce a :class:`CreativeManifest`
from a :class:`BuildCreativeRequest`. The unified Protocol surface
captures that; the discovery distinction is preserved at the
buyer-facing spec level (so buyers filtering for "AI brief-to-creative"
still find generative agents).

Required:

* :meth:`build_creative` — produces the creative

Optional (present-or-absent, surface UNSUPPORTED_FEATURE if missing):

* :meth:`preview_creative` — sandbox URL or inline HTML preview
* :meth:`sync_creatives` — review surface; hybrid sync/handoff
* :meth:`validate_input` — preflight buyer inputs against a format declaration

**Refinement is via ``build_creative``, not a separate method.** The
spec's ``build-creative-request.json`` describes refinement as
re-invoking ``build_creative`` with ``creative_id`` referencing the
prior build (see the request schema's "For refinement…" description).
There is no ``refine-creative-*.json`` in ``schemas/cache/`` and no
``refine_creative`` wire tool. An earlier port preserved a
``refine_creative`` Protocol method mirroring the JS reference;
expert review (round-3 Emma) caught that as a hallucinated wire
surface — both codebases shipped a method with no spec backing.
Dropped here; JS to follow.

Async story: ``build_creative`` is sync at the wire level — the
per-tool ``build-creative-response.json`` ``oneOf`` doesn't include a
``Submitted`` arm (spec inconsistency tracked as
``adcontextprotocol/adcp#3392``). Until the spec rolls Submitted into
the ``oneOf``, slow operations (TTS, audio mixing, long-running
generation) await in-request; status changes surface via
``ctx.publish_status_change(resource_type='creative', ...)``.

Mirrors the JS-side ``CreativeBuilderPlatform`` interface at
``src/lib/server/decisioning/specialisms/creative.ts`` (commit
``841616d7`` / F13 — unified Template + Generative archetypes).

For full ad-server adopters (library + tag generation + delivery
reporting) declaring ``creative-ad-server``, see
:class:`CreativeAdServerPlatform` instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, Protocol, runtime_checkable

from typing_extensions import TypeVar

if TYPE_CHECKING:
    from collections.abc import Sequence

    from adcp.decisioning.context import RequestContext
    from adcp.decisioning.types import MaybeAsync, SalesResult
    from adcp.types import (
        BuildCreativeRequest,
        BuildCreativeSuccessResponse,
        CreativeManifest,
        PreviewCreativeRequest,
        PreviewCreativeResponse,
        SyncCreativesRequest,
        SyncCreativesSuccessResponse,
        ValidateInputRequest,
        ValidateInputResponse,
    )


#: Per-platform metadata generic; matches ``RequestContext[TMeta]`` and
#: ``Account[TMeta]`` upstream.
TMeta = TypeVar("TMeta", default=dict[str, Any])


@runtime_checkable
class CreativeBuilderPlatform(Protocol, Generic[TMeta]):
    """Produces creatives — template-driven or brief-driven (generative).

    Methods may be sync (return ``T`` directly) or async (return
    ``Awaitable[T]``); the dispatch adapter detects via
    :func:`asyncio.iscoroutinefunction` and runs sync methods on a
    thread pool.

    Throw :class:`adcp.decisioning.AdcpError` for buyer-fixable
    rejection (``UNSUPPORTED_FEATURE`` for missing optionals,
    ``POLICY_VIOLATION`` for buyer rights issues, etc.); the framework
    projects to the wire structured-error envelope.
    """

    def build_creative(
        self,
        req: BuildCreativeRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[BuildCreativeSuccessResponse | Sequence[CreativeManifest] | CreativeManifest]:
        """Build the creative.

        Single method covers template-driven transform
        (``req.template_id`` + asset slots), brief-to-creative
        generation (``req.brief``), and any hybrid the platform
        supports — adopters route internally on ``req`` shape.

        Return shape is discriminated by the wire spec's Single vs
        Multi response arms:

        * **Single manifest, no metadata**: return a :class:`CreativeManifest`
          directly. Framework wraps as ``{creative_manifest: <manifest>}``.
          Use this for single-format requests (``target_format_id``)
          when you don't need to set ``sandbox`` / ``expires_at`` /
          ``preview``.
        * **Multi-format manifests, no metadata**: return a
          ``Sequence[CreativeManifest]``. Framework wraps as
          ``{creative_manifests: [...]}``. Use for multi-format
          requests (``target_format_ids``) when you don't need rich
          metadata.
        * **Fully-shaped envelope**: return a
          :class:`BuildCreativeSuccessResponse` with ``sandbox`` /
          ``expires_at`` / ``preview`` populated. Framework passes
          through unchanged.

        Adopters route on ``req.target_format_ids`` (multi) vs
        ``req.target_format_id`` (single) and return the matching arm.
        Returning the wrong arm shape is an adopter contract violation
        that surfaces as schema-validation failure on the wire response.

        :raises adcp.decisioning.AdcpError: ``code='POLICY_VIOLATION'``
            (buyer lacks rights to the requested template / brand
            inputs), ``code='INVALID_REQUEST'`` (missing or
            unrecognized template_id).
        """
        ...

    def preview_creative(
        self,
        req: PreviewCreativeRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[PreviewCreativeResponse]:
        """Preview-only variant — sandbox URL or inline HTML, expires.

        Always sync. Optional — generative-only adopters that don't
        render preview ahead of generation can omit it; the framework
        returns ``UNSUPPORTED_FEATURE`` to buyers calling
        ``preview_creative`` against a platform that didn't wire this.
        """
        ...

    def sync_creatives(
        self,
        req: SyncCreativesRequest,
        ctx: RequestContext[TMeta],
    ) -> SalesResult[SyncCreativesSuccessResponse]:
        """Sync review surface — present-or-absent.

        Stateless platforms typically auto-approve; adopters needing
        mandatory pre-persist review return
        ``ctx.handoff_to_task(fn)`` to defer to a background task.
        Unified hybrid shape — return the typed
        :class:`SyncCreativesSuccessResponse` for the sync fast path
        OR ``ctx.handoff_to_task(fn)`` for HITL.

        Same wire request type as the sales-* archetypes use
        (``SyncCreativesRequest`` — shared spec shape); the
        per-archetype handler shim narrows the discriminated payload
        when adopters care about archetype-specific fields.
        """
        ...

    def validate_input(
        self,
        req: ValidateInputRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ValidateInputResponse]:
        """Validate buyer-provided creative inputs before build.

        Optional beta 3 preflight surface. Platforms that can predict format
        compatibility without rendering should implement it; otherwise the
        framework returns ``UNSUPPORTED_FEATURE`` when called.
        """
        ...


__all__ = ["CreativeBuilderPlatform"]
