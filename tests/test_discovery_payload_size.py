"""``tools/list`` has to fit in a buyer's response budget.

A seller whose discovery payload exceeds the buyer's cap is not slow — it is
unreachable. The buyer aborts mid-body and reports a transport error, while
the seller's logs show a response it served happily.

The regression these lock: output schemas used to be generated with every
``$ref`` inlined, unconditionally, while input schemas had been compact since
they were introduced. Inlining expands each shared subtree once per reference,
so ``ListCreativesResponse`` went out at 4.8 MB where its compact form is
25 KB — and the reference seller's 13-tool ``tools/list`` reached 22 MiB.
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest

from adcp.server import ADCPHandler, create_mcp_server
from adcp.server.mcp_tools import get_tools_for_handler
from adcp.server.serve import (
    BUYER_RESPONSE_BYTE_BUDGET,
    _discovery_payload_bytes,
)

# examples/ is not a package; the storyboard tests add it the same way.
_EXAMPLES = str(Path(__file__).parent.parent / "examples")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)

import seller_agent as _sa  # noqa: E402

#: What a seller may spend on discovery before an operator should hear about
#: it. Deliberately far below ``BUYER_RESPONSE_BYTE_BUDGET``: a seller sitting
#: just under the buyer's cap has no room to implement its next tool.
_REFERENCE_SELLER_CEILING = 2 * 1024 * 1024


def _tools_list_payload(handler: ADCPHandler[Any], **kwargs: Any) -> int:
    """Bytes of the ``tools/list`` result this handler would serve."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return _discovery_payload_bytes(get_tools_for_handler(handler, **kwargs))


def test_reference_seller_discovery_fits_a_buyers_budget() -> None:
    """The repo's own example is the one an interop lane actually dials."""
    size = _tools_list_payload(_sa.DemoSeller())

    assert size < _REFERENCE_SELLER_CEILING, (
        f"examples/seller_agent.py advertises {size} bytes of tools/list. "
        f"Budget is {BUYER_RESPONSE_BYTE_BUDGET}; stay under "
        f"{_REFERENCE_SELLER_CEILING} so there is room to grow."
    )


def test_reference_seller_declares_its_advertised_tools() -> None:
    """Declared, not inferred: adding a method must not silently widen
    ``tools/list``."""
    assert "advertised_tools" in _sa.DemoSeller.__dict__

    advertised = {
        t["name"] for t in get_tools_for_handler(_sa.DemoSeller(), _include_schemas=False)
    }
    declared = set(_sa.DemoSeller.advertised_tools)
    # The framework adds always-on protocol/discovery tools on top.
    assert declared <= advertised
    assert advertised - declared == {"get_adcp_capabilities"}


def test_a_typical_seller_spends_little_on_discovery() -> None:
    """A handful of tools leaves most of the budget unspent."""

    class _TypicalSeller(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def get_products(self, params, context=None):
            return {"products": []}

        async def create_media_buy(self, params, context=None):
            return {"media_buy_id": "mb-1"}

    size = _tools_list_payload(_TypicalSeller())

    assert size < BUYER_RESPONSE_BYTE_BUDGET // 4, (
        f"three tools cost {size} bytes of discovery, over a quarter of the "
        f"{BUYER_RESPONSE_BYTE_BUDGET}-byte buyer budget"
    )


def test_whole_surface_stays_within_an_order_of_magnitude_of_the_budget() -> None:
    """``advertise_all=True`` is the worst case the framework can produce.

    It does not fit the default buyer budget today — 77 tools of compact
    schema is a genuinely large surface, and the startup warning says so. What
    this locks is that it stays *close*: the inlined shape was 5x over this,
    and a regression back toward it would show up here long before an adopter
    hit it.
    """

    class _Everything(ADCPHandler):
        pass

    size = _tools_list_payload(_Everything(), advertise_all=True)

    assert size < 2 * BUYER_RESPONSE_BYTE_BUDGET, (
        f"the full tool surface is {size} bytes; it was ~24 MB when output "
        "schemas were inlined. Check schema_mode defaults."
    )


def test_oversized_discovery_warns_at_startup() -> None:
    """An operator hears about it once, at boot, with the number."""

    class _Everything(ADCPHandler):
        pass

    with pytest.warns(UserWarning, match="will REJECT this tools/list"):
        create_mcp_server(_Everything(), name="everything", advertise_all=True)


def test_a_right_sized_seller_boots_silently() -> None:
    """No warning for a seller that fits — otherwise operators learn to
    ignore the one that matters."""

    class _Narrow(ADCPHandler):
        advertised_tools = {"get_products"}

        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

        async def get_products(self, params, context=None):
            return {"products": []}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        create_mcp_server(_Narrow(), name="narrow")

    oversized = [w for w in caught if "tools/list is" in str(w.message)]
    assert not oversized, [str(w.message) for w in oversized]


def test_inline_schema_mode_is_the_expensive_opt_in() -> None:
    """The old default is still reachable, and still costs what it cost.

    Keeping the comparison in a test is what makes the default's value
    legible: a reviewer can see the number rather than take it on trust.
    """
    compact = _tools_list_payload(_sa.DemoSeller())
    inline = _tools_list_payload(_sa.DemoSeller(), schema_mode="inline")

    assert inline > 10 * compact
    assert inline > BUYER_RESPONSE_BYTE_BUDGET
    assert compact < BUYER_RESPONSE_BYTE_BUDGET


@pytest.mark.parametrize(
    "tool_name,params",
    [
        ("get_products", {"buying_mode": "brief", "brief": "video"}),
        ("list_products", {}),
        ("get_adcp_capabilities", {}),
        ("get_media_buys", {}),
        ("list_creatives", {}),
        # The two tools whose advertised schema is post-processed after
        # generation — `_widen_media_buy_output_schema_for_legacy_statuses`
        # had to learn to resolve `$ref` arms for this change.
        ("create_media_buy", {"buyer_ref": "ref-1", "packages": [], "promoted_offering": "x"}),
        ("control_media_buy", {"media_buy_id": "mb-1", "action": "pause_media_buy"}),
    ],
)
async def test_advertised_output_schemas_still_describe_real_responses(
    tool_name: str, params: dict[str, Any]
) -> None:
    """Compact pruning must stay permissive.

    It reopens ``additionalProperties`` wherever it drops an optional field,
    so what the seller actually returns still validates against what it
    advertises. Checked against real handler output rather than a hand-written
    sample — a sample would drift from the model and prove nothing.
    """
    jsonschema = pytest.importorskip("jsonschema")

    seller = _sa.DemoSeller()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tools = {t["name"]: t for t in get_tools_for_handler(seller)}
    schema = tools[tool_name]["outputSchema"]

    response = await getattr(seller, tool_name)({**params, "adcp_version": "3.2.0"})

    jsonschema.validate(json.loads(json.dumps(response, default=str)), schema)
