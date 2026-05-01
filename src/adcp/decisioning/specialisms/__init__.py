"""Per-specialism Protocol classes.

Adopters claim specialisms via ``DecisioningCapabilities.specialisms``
and implement the matching Protocol's methods on their
:class:`DecisioningPlatform` subclass. Method names are unified
across specialisms — a platform claiming both ``sales-non-guaranteed``
and ``sales-broadcast-tv`` implements ``create_media_buy`` once and
returns a hybrid :class:`SalesResult` that branches per call.

Public surface re-exported from :mod:`adcp.decisioning.specialisms`:

* :class:`SalesPlatform` — covers the spec ``sales-*`` slugs
  (non-guaranteed, guaranteed, broadcast-tv, social, proposal-mode,
  catalog-driven) under one unified hybrid shape.
* :class:`SignalsPlatform` — covers ``signal-marketplace`` +
  ``signal-owned``. Two methods: ``get_signals`` (catalog discovery)
  and ``activate_signal`` (provisioning onto destination platforms).
* :class:`AudiencePlatform` — covers ``audience-sync``. Two methods:
  ``sync_audiences`` (push first-party CRM audiences with delta
  upsert) and ``poll_audience_statuses`` (batch state read).
* :class:`CreativeBuilderPlatform` — covers ``creative-template`` +
  ``creative-generative``. Required ``build_creative``; optional
  ``preview_creative``, ``sync_creatives``. Unified shape per JS
  commit ``841616d7`` (F13) — wire spec doesn't distinguish
  template-driven transform from brief-to-creative generation. (No
  separate ``refine_creative`` method — refinement is invoked via
  ``build_creative`` with ``creative_id`` referencing the prior
  build, per ``schemas/cache/media-buy/build-creative-request.json``.)
* :class:`CreativeAdServerPlatform` — covers ``creative-ad-server``.
  Stateful library + per-creative pricing + tag generation. Required
  ``build_creative``, ``preview_creative``, ``list_creatives``,
  ``get_creative_delivery``; optional ``sync_creatives``.
* :class:`CampaignGovernancePlatform` — covers
  ``governance-spend-authority`` + ``governance-delivery-monitor``.
  Required ``check_governance``, ``sync_plans``,
  ``report_plan_outcome``, ``get_plan_audit_logs``. NOTE: a third
  governance slug, ``governance-aware-seller``, names a SELLER claim
  (sales-* archetype that composes with a governance agent) — it
  does NOT implement this Protocol; it integrates WITH a platform
  that does. That slug stays unenforced until sync_governance
  handler shim wiring lands for sales adopters.

Remaining specialism Protocols (brand-rights, content-standards,
property-lists, collection-lists) are added in subsequent
breadth-sprint PRs.
"""

from __future__ import annotations

from adcp.decisioning.specialisms.audience import AudiencePlatform
from adcp.decisioning.specialisms.creative import CreativeBuilderPlatform
from adcp.decisioning.specialisms.creative_ad_server import CreativeAdServerPlatform
from adcp.decisioning.specialisms.governance import CampaignGovernancePlatform
from adcp.decisioning.specialisms.sales import SalesPlatform
from adcp.decisioning.specialisms.signals import SignalsPlatform

__all__ = [
    "AudiencePlatform",
    "CampaignGovernancePlatform",
    "CreativeAdServerPlatform",
    "CreativeBuilderPlatform",
    "SalesPlatform",
    "SignalsPlatform",
]
