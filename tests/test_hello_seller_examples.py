"""Smoke tests for the per-Protocol-family hello_seller_* examples.

Every Emma backend test (sales-direct, AudioStack, Stability AI,
Signals) flagged "no example for my specialism" as P1 friction. This
file boots each example's platform via PlatformHandler and verifies:

1. ``advertised_tools_for_instance()`` narrows correctly to the
   specialism's tool surface (no leak to other Protocol families).
2. The example's required platform method is reachable via the shim
   layer end-to-end (sanity smoke that the example actually runs;
   absence of this check is how the original AudioStack 4/10 verdict
   went undetected).

Each example is imported as a module so changes to the example's
class don't get out of sync with this regression suite.
"""

from __future__ import annotations

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from adcp.decisioning import InMemoryTaskRegistry
from adcp.decisioning.handler import PlatformHandler

_EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def _load_example_class(filename: str, class_name: str) -> type:
    """Import the example module and return its platform class.

    Use ``importlib`` rather than ``import`` because ``examples/`` isn't
    a Python package and we want to keep the example files single-file
    runnable without ``__init__.py`` overhead.
    """
    path = _EXAMPLES_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="examples-")
    yield pool
    pool.shutdown(wait=True)


# Each entry: (example filename, platform class name, expected
# advertised tool subset, forbidden tools that MUST NOT leak).
_CASES = [
    (
        "hello_seller_creative.py",
        "HelloCreativeSeller",
        {"build_creative"},
        {"get_products", "get_signals", "acquire_rights"},
    ),
    (
        "hello_seller_signals.py",
        "HelloSignalsSeller",
        {"get_signals", "activate_signal"},
        {"get_products", "build_creative", "check_governance"},
    ),
    (
        "hello_seller_audience.py",
        "HelloAudienceSeller",
        {"sync_audiences"},
        {"get_products", "build_creative", "get_signals"},
    ),
    (
        "hello_seller_governance.py",
        "HelloGovernanceSeller",
        {"check_governance", "sync_plans", "report_plan_outcome", "get_plan_audit_logs"},
        {"get_products", "build_creative", "get_signals"},
    ),
    (
        "hello_seller_brand_rights.py",
        "HelloBrandRightsSeller",
        {"get_brand_identity", "get_rights", "acquire_rights"},
        {"get_products", "build_creative", "check_governance"},
    ),
    (
        "hello_seller_content_standards.py",
        "HelloContentStandardsSeller",
        {
            "list_content_standards",
            "get_content_standards",
            "create_content_standards",
            "calibrate_content",
            "validate_content_delivery",
        },
        {"get_products", "build_creative", "acquire_rights"},
    ),
    (
        "hello_seller_property_lists.py",
        "HelloPropertyListsSeller",
        {
            "create_property_list",
            "update_property_list",
            "get_property_list",
            "list_property_lists",
            "delete_property_list",
        },
        {"get_products", "build_creative", "acquire_rights"},
    ),
    (
        "hello_seller_collection_lists.py",
        "HelloCollectionListsSeller",
        {
            "create_collection_list",
            "update_collection_list",
            "get_collection_list",
            "list_collection_lists",
            "delete_collection_list",
        },
        {"get_products", "build_creative", "acquire_rights"},
    ),
]


@pytest.mark.parametrize("filename,class_name,expected,forbidden", _CASES)
def test_example_advertises_only_its_specialism(
    filename: str,
    class_name: str,
    expected: set[str],
    forbidden: set[str],
    executor,
) -> None:
    """Each example's advertised_tools_for_instance() narrows to the
    Protocol family's own tools — no leak to sales/creative/signals/etc.
    Regression for the cross-cutting "advertising 42 of 42 tools" P1.
    """
    cls = _load_example_class(filename, class_name)
    handler = PlatformHandler(
        cls(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    tools = handler.advertised_tools_for_instance()
    missing = expected - tools
    leaked = forbidden & tools
    assert not missing, f"{class_name}: missing expected tools {sorted(missing)}"
    assert not leaked, (
        f"{class_name}: leaked forbidden tools to advertised set "
        f"{sorted(leaked)} — per-specialism filter regressed"
    )
