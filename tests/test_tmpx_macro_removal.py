"""The deprecated 3.1.8 ``TmpxMacro`` compatibility classes are gone (#1894)."""

from __future__ import annotations

import importlib

import pytest

import adcp
import adcp.types
from adcp.types import aliases

REMOVED = ("IdentityMatchTmpxMacro", "ProviderRegistrationTmpxMacro")
DOMAINS = (
    "adcp.types.domains.trusted_match.identity_match_response",
    "adcp.types.domains.trusted_match.provider_registration",
)


@pytest.mark.parametrize("name", REMOVED)
@pytest.mark.parametrize("module", [adcp, adcp.types, aliases], ids=lambda m: m.__name__)
def test_removed_names_absent_from_public_modules(module: object, name: str) -> None:
    assert not hasattr(module, name)
    assert name not in getattr(module, "__all__", ())


def test_import_from_adcp_types_raises() -> None:
    with pytest.raises(ImportError):
        from adcp.types import IdentityMatchTmpxMacro  # noqa: F401


@pytest.mark.parametrize("module_name", DOMAINS)
def test_domain_modules_have_no_tmpx_macro(module_name: str) -> None:
    assert not hasattr(importlib.import_module(module_name), "TmpxMacro")


def test_identity_match_response_is_router_publisher() -> None:
    from adcp.types.domains.trusted_match import identity_match_response as imr

    assert adcp.types.IdentityMatchResponse is imr.IdentityMatchResponseRouterPublisher


@pytest.mark.parametrize(
    ("module_name", "attr"),
    [
        ("tmpx_chunk", "TmpxChunk"),
        ("identity_match_response", "TmpxProviders"),
        ("provider_registration", "TmpxSlot"),
        ("publisher_tmpx_config", "PublisherTmpxMacroMapping"),
    ],
)
def test_replacement_types_still_import(module_name: str, attr: str) -> None:
    module = importlib.import_module(f"adcp.types.domains.trusted_match.{module_name}")
    assert hasattr(module, attr)
