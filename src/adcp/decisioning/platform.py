"""DecisioningPlatform base class + capabilities declaration.

:class:`DecisioningPlatform` is the adopter-facing base. Adopters subclass
it, attach an :class:`AccountStore`, declare :class:`DecisioningCapabilities`,
and implement specialism methods (``get_products``, ``create_media_buy``,
``sync_audiences``, etc.) directly on the class. The dispatch adapter
discovers methods via ``hasattr`` at server boot, validates against the
declared capabilities, and routes requests through the framework's
existing transport machinery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adcp.decisioning.accounts import AccountStore


@dataclass
class DecisioningCapabilities:
    """What a platform claims to support.

    Read by ``validate_platform`` at server boot to confirm each
    declared specialism has the methods it requires, and surfaced via
    the framework's auto-generated ``get_adcp_capabilities`` response
    so buyers can pre-flight without trial-and-error tool calls.

    :param specialisms: AdCP specialism slugs the platform claims —
        e.g. ``['sales-non-guaranteed', 'sales-broadcast-tv']``,
        ``['audience-sync']``, ``['signal-marketplace',
        'signal-owned']``. Each maps to a ``Protocol`` class under
        :mod:`adcp.decisioning.specialisms`.
    :param channels: Inventory channels the platform serves —
        ``'display'``, ``'video'``, ``'olv'``, ``'ctv'``, ``'audio'``,
        ``'dooh'``. Surfaced on capabilities; not enforced.
    :param pricing_models: Pricing models the platform supports —
        ``'cpm'``, ``'cpc'``, ``'cpa'``, ``'cpcv'``. Surfaced on
        capabilities.
    :param creative_agents: Optional list of creative-agent endpoints
        the platform delegates creative review/generation to. Empty
        list means "no creative-agent integration; review is in-house."
    :param config: Free-form adopter-defined config exposed on
        capabilities. Use sparingly — strongly-typed fields above are
        preferred.
    """

    specialisms: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    pricing_models: list[str] = field(default_factory=list)
    creative_agents: list[Any] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)


class DecisioningPlatform:
    """Adopter-facing base class for the v6.0 framework.

    Subclasses set:

    * :attr:`capabilities` — what the platform claims to support
    * :attr:`accounts` — an :class:`AccountStore` instance defining
      how to resolve a wire reference + auth context to an
      :class:`Account`

    Then implement specialism methods directly on the subclass
    (``get_products``, ``create_media_buy``, ``sync_audiences``, etc.).
    Each method takes a typed Pydantic request model + a
    :class:`RequestContext[TMeta]` and returns a typed response (or
    raises :class:`AdcpError`).

    The dispatch adapter (:func:`adcp.decisioning.create_adcp_server_from_platform`)
    discovers methods via ``hasattr``, validates against
    ``capabilities.specialisms``, and routes requests through the
    framework's existing ``adcp.server.serve()`` infrastructure.

    Example::

        class HelloSeller(DecisioningPlatform):
            capabilities = DecisioningCapabilities(
                specialisms=["sales-non-guaranteed"],
                channels=["display"],
                pricing_models=["cpm"],
            )
            accounts = SingletonAccounts(account_id="hello")

            def get_products(self, req, ctx):
                return GetProductsResponse(products=[...])

            def create_media_buy(self, req, ctx):
                return CreateMediaBuySuccess(media_buy_id="mb_1", ...)

    Per-method signatures are documented in the per-specialism
    Protocol classes under :mod:`adcp.decisioning.specialisms` —
    those are the canonical contract reference. The base class
    itself is intentionally minimal so adopters can mix in
    cross-cutting helpers without inheritance constraints.
    """

    #: Required: the platform's capability declaration. Subclasses
    #: override.
    capabilities: DecisioningCapabilities = DecisioningCapabilities()

    #: Required: the platform's account-resolution strategy.
    #: Subclasses set to a :class:`SingletonAccounts`,
    #: :class:`ExplicitAccounts`, :class:`FromAuthAccounts`, or
    #: custom :class:`AccountStore` instance. Type erased to ``Any``
    #: at the base because the typed shape is platform-specific
    #: (different ``TMeta`` per adopter); ``validate_platform``
    #: confirms an :class:`AccountStore` instance is set.
    accounts: AccountStore[Any] = None  # type: ignore[assignment]
