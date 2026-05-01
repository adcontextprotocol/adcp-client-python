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

Remaining specialism Protocols (creative-*, governance-*,
brand-rights, content-standards, property-lists, collection-lists)
are added in subsequent breadth-sprint PRs as adopters need them.
"""

from __future__ import annotations

from adcp.decisioning.specialisms.audience import AudiencePlatform
from adcp.decisioning.specialisms.sales import SalesPlatform
from adcp.decisioning.specialisms.signals import SignalsPlatform

__all__ = ["AudiencePlatform", "SalesPlatform", "SignalsPlatform"]
