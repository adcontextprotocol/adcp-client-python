"""Per-specialism Protocol classes.

Adopters claim specialisms via ``DecisioningCapabilities.specialisms``
and implement the matching Protocol's methods on their
:class:`DecisioningPlatform` subclass. Method names are unified
across specialisms — a platform claiming both ``sales-non-guaranteed``
and ``sales-broadcast-tv`` implements ``create_media_buy`` once and
returns a hybrid :class:`SalesResult` that branches per call.

Public surface re-exported from :mod:`adcp.decisioning.specialisms`:

* :class:`SalesPlatform` — covers all 9 ``sales-*`` specialisms
  (non-guaranteed, guaranteed, broadcast-tv, streaming-tv, social,
  exchange, proposal-mode, catalog-driven, retail-media) under one
  unified hybrid shape.

Other specialism Protocols (audience, signals, creative-*, governance,
property-lists, etc.) are added as adopters need them — first
:class:`SalesPlatform` because that's the v6.0 vertical-slice the
foundation PR proves out.
"""

from __future__ import annotations

from adcp.decisioning.specialisms.sales import SalesPlatform

__all__ = ["SalesPlatform"]
