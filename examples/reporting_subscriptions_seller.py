"""The reference seller for AdCP 3.3 reporting subscriptions, as a standalone MCP server.

Run it, then drive it with the storyboard runner::

    python examples/reporting_subscriptions_seller.py --port 3000
    npx adcp storyboard run http://127.0.0.1:3000/mcp --storyboards reporting_subscriptions \
        --auth storyboard-buyer --allow-http

It is ``adcp.testing.reporting.create_reference_reporting_seller``: a seller
that already serves ``list_accounts`` and ``get_media_buys`` (about a hundred
lines over an in-memory world), with ``ReliableReportingService(subscriptions=
SubscriptionOptions(...))`` adding everything else. The bearer token names the
principal, storage is process-local, and the virtual clock moves only through
``comply_test_controller`` ``reporting_subscription_probe`` ``advance_time``.
Equivalent: ``python -m adcp reference-seller reporting --port 3000``.
"""

from __future__ import annotations

import sys

from adcp.testing.reporting import main

if __name__ == "__main__":
    sys.exit(main(["reporting", *sys.argv[1:]]))
