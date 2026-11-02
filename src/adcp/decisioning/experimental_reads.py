"""Declaration gates for the AdCP 3.3 experimental read tasks.

``list_proposals`` (``media_buy.open_opportunities``) and ``list_collections``
(``media_buy.collection_catalog``) are synchronous, side-effect-free reads
added in AdCP 3.3. Neither exists in an earlier release, and a seller serves
each one only when its capabilities declare it:

* ``list_proposals``: ``media_buy.lifecycle_tools`` contains
  ``list_proposals``, or ``media_buy.open_opportunities.list_proposals`` is
  ``true``. Sellers list ``media_buy.open_opportunities`` in
  ``experimental_features``.
* ``list_collections``: ``experimental_features`` contains
  ``media_buy.collection_catalog`` (the capabilities schema requires
  ``media_buy.collection_cards`` beside it).

:class:`~adcp.decisioning.handler.PlatformHandler` advertises a tool only for
such a declaration backed by a platform method, and
:func:`~adcp.decisioning.dispatch.validate_platform` fails boot when a
declaration has no method behind it. The spec forbids serving
``list_collections`` to a caller below 3.3 (``UNSUPPORTED_FEATURE``);
:func:`refuse_caller_below_release` applies the same rule to both tasks for
servers that run their handlers on an older release, where no conversion
step refuses the call first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from adcp.decisioning.types import AdcpError

if TYPE_CHECKING:
    from adcp.decisioning.platform import DecisioningCapabilities

#: Experimental feature id that declares ``list_collections``.
COLLECTION_CATALOG_FEATURE = "media_buy.collection_catalog"

#: The first release that defines either task.
FIRST_RELEASE = "3.3.0-beta.0"

#: The experimental read tools; each platform method has the tool's name.
EXPERIMENTAL_READ_TOOLS: frozenset[str] = frozenset({"list_proposals", "list_collections"})


def _value(entry: Any) -> str:
    raw = getattr(entry, "value", entry)
    root = getattr(raw, "root", raw)
    return str(root)


def declares_list_proposals(capabilities: DecisioningCapabilities) -> bool:
    """Whether the capabilities declare ``list_proposals``."""
    media_buy = capabilities.media_buy
    if media_buy is None:
        return False
    lifecycle = getattr(media_buy, "lifecycle_tools", None) or []
    if any(_value(entry) == "list_proposals" for entry in lifecycle):
        return True
    open_opportunities = getattr(media_buy, "open_opportunities", None)
    return getattr(open_opportunities, "list_proposals", None) is True


def declares_collection_catalog(capabilities: DecisioningCapabilities) -> bool:
    """Whether the capabilities declare ``list_collections``."""
    features = capabilities.experimental_features or []
    return any(_value(entry) == COLLECTION_CATALOG_FEATURE for entry in features)


def declared_experimental_reads(capabilities: DecisioningCapabilities) -> list[tuple[str, str]]:
    """``(declaration, method)`` pairs for each experimental read declared."""
    declared: list[tuple[str, str]] = []
    if declares_list_proposals(capabilities):
        declared.append(("media_buy.open_opportunities.list_proposals", "list_proposals"))
    if declares_collection_catalog(capabilities):
        declared.append(
            (f"experimental_features[{COLLECTION_CATALOG_FEATURE}]", "list_collections")
        )
    return declared


def refuse_caller_below_release(tool_name: str, version: str | None) -> None:
    """Fail a 3.3-only read for a caller whose release predates it.

    ``version`` is the caller's resolved wire release; ``None`` (no
    dispatcher, or an unversioned in-process call) is served.
    """
    if version is None:
        return
    from adcp._version import is_adcp_version_at_least

    try:
        served = is_adcp_version_at_least(version, FIRST_RELEASE)
    except ValueError:
        served = False
    if not served:
        raise AdcpError(
            "UNSUPPORTED_FEATURE",
            message=f"{tool_name} requires AdCP 3.3 or later; the request negotiated {version}",
            recovery="correctable",
            details={"task": tool_name, "adcp_version": version},
        )
