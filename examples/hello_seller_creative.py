"""Hello-seller-creative — minimal CreativeBuilderPlatform adopter.

The smallest possible ``creative-generative`` (or ``creative-template``)
seller. Buyers send a brief; the agent returns a CreativeManifest with
the synthesized asset URL.

This is the template for AI-generated-creative integrators (AudioStack,
Stability AI, Runway, ElevenLabs, etc.). Three return-shape arms are
supported by the framework's projection layer:

1. **Bare manifest** — ``return CreativeManifest(...)``. Framework
   wraps it into the wire envelope ``{creative_manifest: {...}}``.
2. **List of manifests** — ``return [m1, m2, ...]``. Framework wraps
   into ``{creative_manifests: [...]}`` (multi-format build).
3. **Full envelope** — ``return BuildCreativeSuccessResponse(...)``
   if you want explicit control over the wire shape.

Run::

    uv run python examples/hello_seller_creative.py

Then connect any AdCP MCP buyer and call ``build_creative`` with a
brief.
"""

from __future__ import annotations

import uuid
from typing import Any

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    RequestContext,
    SingletonAccounts,
    serve,
)
from adcp.types import AudioContent, CreativeManifest, FormatReferenceStructuredObject


class HelloCreativeSeller(DecisioningPlatform):
    """The canonical minimal ``creative-generative`` adopter.

    Implements only ``build_creative`` — the one required method on
    :class:`CreativeBuilderPlatform`. Optional ``preview_creative``
    is omitted; the framework's ``_require_platform_method`` gate
    returns ``UNSUPPORTED_FEATURE`` to buyers who call it on this
    seller.

    Replace the stub asset_url with your generation pipeline (a real
    AudioStack/Stability/Runway integration would call the upstream
    API here and return the resulting CDN URL).
    """

    capabilities = DecisioningCapabilities(
        specialisms=["creative-generative"],
        channels=["audio"],
    )
    accounts = SingletonAccounts(account_id="hello-creative")

    def build_creative(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> CreativeManifest:
        """Synthesize a single audio creative from the buyer's brief.

        Returns a bare :class:`CreativeManifest` — the framework's
        projection layer wraps it into the wire envelope. The brief
        is on ``req.brief``; the requested format is on
        ``req.format_id``.
        """
        # Real adopters call their generation API here; this stub
        # synthesizes a placeholder URL for the example.
        creative_id = f"cr-{uuid.uuid4().hex[:12]}"
        return CreativeManifest(
            creative_id=creative_id,
            format_id=FormatReferenceStructuredObject(
                agent_url="https://creative.adcontextprotocol.org/",
                id="audio_30s",
            ),
            assets={
                # Note: ``AudioContent`` (not ``AudioAsset``) — 4.0
                # renamed payload-describing types to ``*Content`` so
                # they don't collide with ``*FormatAsset`` slot types.
                # The framework's MIGRATION_v3_to_v4.md has the full
                # rationale.
                "primary_audio": AudioContent(
                    asset_id=f"{creative_id}-audio",
                    asset_role="primary_audio",
                    url=f"https://cdn.example.com/synth/{creative_id}.mp3",
                    duration_ms=30000,
                ),
            },
        )


def main() -> None:
    """Boot the seller on http://localhost:3001/mcp.

    Buyer-facing surface after boot:

    * ``tools/list`` advertises ``build_creative`` and the framework's
      always-on protocol/discovery tools — NOT the sales / signals /
      governance tools (per-specialism filter from PR #338's
      follow-up).
    * ``tools/call build_creative`` returns the synthesized manifest.
    """
    serve(HelloCreativeSeller())


if __name__ == "__main__":
    main()
