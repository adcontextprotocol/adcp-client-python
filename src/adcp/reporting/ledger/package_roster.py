"""Internal period-close package roster evidence for buy-scoped reporting."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from adcp.reporting.ledger.models import ReportingConfiguration


@dataclass(frozen=True, slots=True)
class CompletePackageRoster:
    """The complete flat package ID set for the configured buys at period end."""

    package_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(type(value) is not str or not value for value in self.package_ids):
            raise ValueError("package roster IDs must be nonempty strings")
        object.__setattr__(self, "package_ids", tuple(sorted(set(self.package_ids))))


@dataclass(frozen=True, slots=True)
class MissingHistoricalPackageRoster:
    """The package roster at period end could not be proven complete."""


PackageRosterResolver = Callable[
    [ReportingConfiguration, datetime],
    Awaitable[CompletePackageRoster | MissingHistoricalPackageRoster],
]
