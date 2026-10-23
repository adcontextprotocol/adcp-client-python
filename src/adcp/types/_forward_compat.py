"""Six request names, bound to the class that serves each one.

This module used to patch the generated classes at import time: it opened the
asset unions, threaded the read-back nodes through the response classes,
replaced the bundled capability clones, and bridged the targeting overlay. Every
one of those is now DECLARED where the field is declared -- ``PIN_GAP_FIELDS``
in ``scripts/generate_types.py`` for a field the pin does not have, and
``COMPATIBILITY_ANNOTATIONS`` / ``READBACK_CLONES`` / ``_OPEN_ASSET_UNIONS`` in
``scripts/post_generate_fixes.py`` for a field whose type the implementation
replaces. A patch reached the class it named; a declaration reaches every class
that nests it, which is what a parsed document actually goes through.

What is left is name binding. Four of these are the generated class under its
public name, and two are the canonical class rather than the generated one --
which is a fact about which class serves the name, not a patch.
"""

from __future__ import annotations

from adcp.types.canonical_creative import CreateMediaBuyRequest as _PublicCreateMediaBuyRequest
from adcp.types.canonical_creative import UpdateMediaBuyRequest as _PublicUpdateMediaBuyRequest
from adcp.types.domains.core.reporting_webhook import (
    ReportingWebhook as _GeneratedReportingWebhook,
)
from adcp.types.domains.media_buy.accept_proposal_request import (
    AcceptProposalRequest as _GeneratedAcceptProposalRequest,
)
from adcp.types.domains.media_buy.buy_products_request import (
    BuyProductsRequest as _GeneratedBuyProductsRequest,
)
from adcp.types.domains.media_buy.control_media_buy_request import (
    ControlMediaBuyRequest as _GeneratedControlMediaBuyRequest,
)

# The generated classes carry ``operation_id`` -- ``PIN_GAP_FIELDS`` in
# scripts/generate_types.py declares it on core/reporting-webhook.json and the
# generator emits it, so all five nesting request schemas get it by generation.
# A TYPE_CHECKING shadow used to restate the field on each of those five plus
# one per split-root branch; a generated field needs no static view.
ReportingWebhook = _GeneratedReportingWebhook
AcceptProposalRequest = _GeneratedAcceptProposalRequest
BuyProductsRequest = _GeneratedBuyProductsRequest
ControlMediaBuyRequest = _GeneratedControlMediaBuyRequest
CreateMediaBuyRequest = _PublicCreateMediaBuyRequest
UpdateMediaBuyRequest = _PublicUpdateMediaBuyRequest
