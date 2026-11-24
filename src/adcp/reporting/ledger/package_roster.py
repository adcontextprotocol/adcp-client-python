"""Period-close package roster evidence for buy-scoped reporting.

A :data:`PackageRosterResolver` proves, at a period end, which packages the
frozen buys own. A whole-configuration result (:class:`CompletePackageRoster`,
:class:`MissingHistoricalPackageRoster`) applies to every buy;
:class:`PerMediaBuyPackageRoster` gives each buy its own evidence, so one
unprovable or unsupported buy is reported as that buy rather than as the whole
obligation (see :func:`adcp.reporting.ledger.coverage.build_reporting_coverage`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, TypeVar

from adcp.reporting.ledger.models import ReportingConfiguration, ReportingPeriodBoundary

_Result = TypeVar("_Result")


@dataclass(frozen=True, slots=True)
class CompletePackageRoster:
    """The complete flat package ID set for the configured buys and report period."""

    package_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(type(value) is not str or not value for value in self.package_ids):
            raise ValueError("package roster IDs must be nonempty strings")
        object.__setattr__(self, "package_ids", tuple(sorted(set(self.package_ids))))


@dataclass(frozen=True, slots=True)
class MissingHistoricalPackageRoster:
    """The package roster at period end could not be proven complete."""


@dataclass(frozen=True, slots=True)
class UnsupportedMediaBuyRoster:
    """The selected offering cannot report this buy; ``package_ids`` are its known packages."""

    reason: Literal[
        "offering_unsupported",
        "account_entitlement_unavailable",
        "credential_scope_insufficient",
        "provider_limitation",
    ]
    package_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.reason not in (
            "offering_unsupported",
            "account_entitlement_unavailable",
            "credential_scope_insufficient",
            "provider_limitation",
        ):
            raise ValueError("unsupported media buy roster reason")
        if any(type(value) is not str or not value for value in self.package_ids):
            raise ValueError("package roster IDs must be nonempty strings")
        object.__setattr__(self, "package_ids", tuple(sorted(set(self.package_ids))))


@dataclass(frozen=True, slots=True)
class PerMediaBuyPackageRoster:
    """Package evidence per frozen buy; the keys are exactly the obligation's media buys."""

    by_media_buy: Mapping[
        str, CompletePackageRoster | MissingHistoricalPackageRoster | UnsupportedMediaBuyRoster
    ]

    def __post_init__(self) -> None:
        items = dict(self.by_media_buy)
        if any(type(key) is not str or not key for key in items):
            raise ValueError("per-buy roster keys must be nonempty media buy IDs")
        if any(
            not isinstance(
                value,
                (CompletePackageRoster, MissingHistoricalPackageRoster, UnsupportedMediaBuyRoster),
            )
            for value in items.values()
        ):
            raise TypeError("per-buy roster values must be package roster results")
        object.__setattr__(self, "by_media_buy", MappingProxyType(dict(sorted(items.items()))))


PackageRosterResult = (
    CompletePackageRoster
    | MissingHistoricalPackageRoster
    | UnsupportedMediaBuyRoster
    | PerMediaBuyPackageRoster
)

PackageRosterResolver = Callable[
    [ReportingConfiguration, ReportingPeriodBoundary],
    Awaitable[PackageRosterResult],
]


def _observe_roster_task(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def _resolve_roster_until(
    resolver: Callable[[ReportingConfiguration, ReportingPeriodBoundary], Awaitable[_Result]],
    configuration: ReportingConfiguration,
    period: ReportingPeriodBoundary,
    *,
    deadline: float,
    what: str = "package roster",
) -> _Result:
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(resolver(configuration, period))
    try:
        try:
            result = await asyncio.wait_for(
                asyncio.shield(task), timeout=max(0.0, deadline - loop.time())
            )
        except asyncio.TimeoutError as error:
            raise TimeoutError(f"{what} resolution exceeded its deadline") from error
        if loop.time() >= deadline:
            raise TimeoutError(f"{what} resolution exceeded its deadline")
        return result
    except BaseException:
        task.cancel()
        task.add_done_callback(_observe_roster_task)
        raise
