"""Per-buy reporting coverage frozen at a period's scope-resolution boundary.

``core/reporting-coverage.json`` partitions the exact media-buy and package
denominators: every buy is fully covered, partially covered, unsupported or
unknown, and every package is covered, unsupported or unknown. An obligation
retains its coverage as :class:`ReportingCoverageRecord`; the status handler
projects it verbatim, so one unprovable buy is reported as that buy, not as the
whole account.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

if TYPE_CHECKING:
    from adcp.reporting.ledger.package_roster import (
        CompletePackageRoster,
        MissingHistoricalPackageRoster,
        PerMediaBuyPackageRoster,
        UnsupportedMediaBuyRoster,
    )

ReportingCoverageStatus: TypeAlias = Literal["full", "partial", "none", "unknown"]
ReportingCoverageLimitationReason: TypeAlias = Literal[
    "offering_unsupported",
    "account_entitlement_unavailable",
    "credential_scope_insufficient",
    "provider_limitation",
    "capability_unknown",
]
_LIMITATION_REASONS = frozenset(
    (
        "offering_unsupported",
        "account_entitlement_unavailable",
        "credential_scope_insufficient",
        "provider_limitation",
        "capability_unknown",
    )
)


def _ids(values: Iterable[str], what: str) -> tuple[str, ...]:
    values = tuple(values)
    if any(type(value) is not str or not value for value in values):
        raise ValueError(f"{what} must be nonempty strings")
    return tuple(sorted(set(values)))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ReportingCoverageLimitation:
    """Why one buy (and optionally some of its packages) is not covered."""

    reason: ReportingCoverageLimitationReason
    media_buy_id: str
    package_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.reason not in _LIMITATION_REASONS:
            raise ValueError("unsupported coverage limitation reason")
        if type(self.media_buy_id) is not str or not self.media_buy_id:
            raise ValueError("a coverage limitation names one media buy")
        object.__setattr__(self, "package_ids", _ids(self.package_ids, "package IDs"))

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"reason": self.reason, "media_buy_id": self.media_buy_id}
        if self.package_ids:
            wire["package_ids"] = list(self.package_ids)
        return wire


@dataclass(frozen=True, slots=True)
class ReportingCoverageRecord:
    """One obligation's coverage; construction enforces the schema partition and status."""

    status: ReportingCoverageStatus
    evaluated_at: datetime
    media_buy_ids: tuple[str, ...]
    fully_covered_media_buy_ids: tuple[str, ...] = ()
    partially_covered_media_buy_ids: tuple[str, ...] = ()
    unsupported_media_buy_ids: tuple[str, ...] = ()
    unknown_media_buy_ids: tuple[str, ...] = ()
    package_ids: tuple[str, ...] = ()
    covered_package_ids: tuple[str, ...] = ()
    unsupported_package_ids: tuple[str, ...] = ()
    unknown_package_ids: tuple[str, ...] = ()
    limitations: tuple[ReportingCoverageLimitation, ...] = ()

    def __post_init__(self) -> None:
        if self.evaluated_at.tzinfo is None:
            raise ValueError("coverage evaluated_at must be timezone-aware")
        object.__setattr__(self, "evaluated_at", self.evaluated_at.astimezone(timezone.utc))
        for name in (
            "media_buy_ids",
            "fully_covered_media_buy_ids",
            "partially_covered_media_buy_ids",
            "unsupported_media_buy_ids",
            "unknown_media_buy_ids",
            "package_ids",
            "covered_package_ids",
            "unsupported_package_ids",
            "unknown_package_ids",
        ):
            object.__setattr__(self, name, _ids(getattr(self, name), name))
        object.__setattr__(
            self,
            "limitations",
            tuple(
                sorted(
                    set(self.limitations),
                    key=lambda item: (item.media_buy_id, item.reason, item.package_ids),
                )
            ),
        )
        buys = (
            self.fully_covered_media_buy_ids,
            self.partially_covered_media_buy_ids,
            self.unsupported_media_buy_ids,
            self.unknown_media_buy_ids,
        )
        packages = (
            self.covered_package_ids,
            self.unsupported_package_ids,
            self.unknown_package_ids,
        )
        if sum(len(group) for group in buys) != len(self.media_buy_ids) or set().union(
            *buys
        ) != set(self.media_buy_ids):
            raise ValueError("media-buy coverage must partition media_buy_ids exactly")
        if sum(len(group) for group in packages) != len(self.package_ids) or set().union(
            *packages
        ) != set(self.package_ids):
            raise ValueError("package coverage must partition package_ids exactly")
        if any(
            item.media_buy_id not in self.media_buy_ids
            or not set(item.package_ids) <= set(self.package_ids)
            for item in self.limitations
        ):
            raise ValueError("coverage limitations must name items of the denominators")
        if self.status != _status(
            covered=bool(buys[0] or buys[1] or packages[0]),
            incomplete=bool(buys[1] or buys[2] or buys[3] or packages[1] or packages[2]),
            unknown=bool(buys[3] or packages[2]),
        ):
            raise ValueError("coverage status contradicts its partition")

    def to_wire(self) -> dict[str, Any]:
        """The ``core/reporting-coverage.json`` object."""
        return {
            "status": self.status,
            "evaluated_at": _iso(self.evaluated_at),
            "media_buy_ids": list(self.media_buy_ids),
            "fully_covered_media_buy_ids": list(self.fully_covered_media_buy_ids),
            "partially_covered_media_buy_ids": list(self.partially_covered_media_buy_ids),
            "unsupported_media_buy_ids": list(self.unsupported_media_buy_ids),
            "unknown_media_buy_ids": list(self.unknown_media_buy_ids),
            "package_ids": list(self.package_ids),
            "covered_package_ids": list(self.covered_package_ids),
            "unsupported_package_ids": list(self.unsupported_package_ids),
            "unknown_package_ids": list(self.unknown_package_ids),
            "limitations": [item.to_wire() for item in self.limitations],
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> ReportingCoverageRecord:
        return cls(
            status=payload["status"],
            evaluated_at=datetime.fromisoformat(
                str(payload["evaluated_at"]).replace("Z", "+00:00")
            ),
            media_buy_ids=tuple(payload["media_buy_ids"]),
            fully_covered_media_buy_ids=tuple(payload["fully_covered_media_buy_ids"]),
            partially_covered_media_buy_ids=tuple(payload["partially_covered_media_buy_ids"]),
            unsupported_media_buy_ids=tuple(payload["unsupported_media_buy_ids"]),
            unknown_media_buy_ids=tuple(payload["unknown_media_buy_ids"]),
            package_ids=tuple(payload["package_ids"]),
            covered_package_ids=tuple(payload["covered_package_ids"]),
            unsupported_package_ids=tuple(payload["unsupported_package_ids"]),
            unknown_package_ids=tuple(payload["unknown_package_ids"]),
            limitations=tuple(
                ReportingCoverageLimitation(
                    reason=item["reason"],
                    media_buy_id=item["media_buy_id"],
                    package_ids=tuple(item.get("package_ids", ())),
                )
                for item in payload["limitations"]
            ),
        )


def _status(*, covered: bool, incomplete: bool, unknown: bool) -> ReportingCoverageStatus:
    if not incomplete:
        return "full"
    if covered:
        return "partial"
    return "unknown" if unknown else "none"


def build_reporting_coverage(
    media_buy_ids: Iterable[str],
    roster: (
        CompletePackageRoster
        | MissingHistoricalPackageRoster
        | PerMediaBuyPackageRoster
        | UnsupportedMediaBuyRoster
        | None
    ),
    evaluated_at: datetime,
) -> ReportingCoverageRecord:
    """Partition a frozen buy denominator from its period-end package roster evidence.

    ``roster`` is what the package roster resolver proved at the period end:

    * ``None`` -- no package dimension; every buy is fully covered.
    * :class:`CompletePackageRoster` -- every buy is fully covered and every
      package covered.
    * :class:`MissingHistoricalPackageRoster` -- nothing is proven: every buy is
      unknown with limitation ``capability_unknown``.
    * :class:`UnsupportedMediaBuyRoster` -- every buy is unsupported for its
      reason, with the roster's packages unsupported.
    * :class:`PerMediaBuyPackageRoster` -- each buy by its own evidence; its keys
      must equal ``media_buy_ids`` exactly.

    The empty denominator is ``full``. ``evaluated_at`` is the obligation's
    ``scope_resolved_at`` (the period end).
    """
    from adcp.reporting.ledger.package_roster import (
        CompletePackageRoster,
        MissingHistoricalPackageRoster,
        PerMediaBuyPackageRoster,
        UnsupportedMediaBuyRoster,
    )

    buys = _ids(media_buy_ids, "media buy IDs")
    if roster is None:
        return ReportingCoverageRecord(
            status="full",
            evaluated_at=evaluated_at,
            media_buy_ids=buys,
            fully_covered_media_buy_ids=buys,
        )
    if isinstance(roster, PerMediaBuyPackageRoster):
        if set(roster.by_media_buy) != set(buys):
            raise ValueError("a per-buy package roster must name exactly the frozen media buys")
        evidence = dict(roster.by_media_buy)
    elif isinstance(
        roster,
        (CompletePackageRoster, MissingHistoricalPackageRoster, UnsupportedMediaBuyRoster),
    ):
        if isinstance(roster, CompletePackageRoster):
            # A whole-configuration roster proves the flat package set; it has
            # no per-buy attribution, so every buy shares it.
            return _whole_complete(buys, roster, evaluated_at)
        evidence = {buy: roster for buy in buys}
    else:
        raise TypeError("package roster resolver returned an unsupported result")
    fully: list[str] = []
    unsupported: list[str] = []
    unknown: list[str] = []
    covered: set[str] = set()
    unsupported_packages: set[str] = set()
    limitations: list[ReportingCoverageLimitation] = []
    for buy, item in evidence.items():
        if isinstance(item, CompletePackageRoster):
            fully.append(buy)
            covered.update(item.package_ids)
        elif isinstance(item, MissingHistoricalPackageRoster):
            unknown.append(buy)
            limitations.append(ReportingCoverageLimitation("capability_unknown", buy))
        elif isinstance(item, UnsupportedMediaBuyRoster):
            unsupported.append(buy)
            unsupported_packages.update(item.package_ids)
            limitations.append(ReportingCoverageLimitation(item.reason, buy, item.package_ids))
        else:
            raise TypeError("per-buy package roster holds an unsupported result")
    if covered & unsupported_packages:
        raise ValueError("a package cannot be both covered and unsupported")
    return ReportingCoverageRecord(
        status=_status(
            covered=bool(fully or covered),
            incomplete=bool(unsupported or unknown or unsupported_packages),
            unknown=bool(unknown),
        ),
        evaluated_at=evaluated_at,
        media_buy_ids=buys,
        fully_covered_media_buy_ids=tuple(fully),
        unsupported_media_buy_ids=tuple(unsupported),
        unknown_media_buy_ids=tuple(unknown),
        package_ids=tuple(covered | unsupported_packages),
        covered_package_ids=tuple(covered),
        unsupported_package_ids=tuple(unsupported_packages),
        limitations=tuple(limitations),
    )


def _whole_complete(
    buys: tuple[str, ...], roster: CompletePackageRoster, evaluated_at: datetime
) -> ReportingCoverageRecord:
    return ReportingCoverageRecord(
        status="full",
        evaluated_at=evaluated_at,
        media_buy_ids=buys,
        fully_covered_media_buy_ids=buys,
        package_ids=roster.package_ids,
        covered_package_ids=roster.package_ids,
    )


def _covered_subset_publishable(
    coverage_requirement: str, coverage: ReportingCoverageRecord | None
) -> bool:
    """Whether an ``allow_partial`` period publishes over its fully covered buys.

    ``coverage_requirement`` admits a partial period's covered packages
    (``core/reporting-delivery-config.json``). Publication is over the fully
    covered buys: no source constituent names only a buy's covered packages,
    so a period with a partially covered buy is withheld, as are ``none`` and
    ``unknown`` periods, which cover nothing. :func:`build_reporting_coverage`
    never produces a partially covered buy.
    """
    return (
        coverage_requirement == "allow_partial"
        and coverage is not None
        and coverage.status == "partial"
        and bool(coverage.fully_covered_media_buy_ids)
        and not coverage.partially_covered_media_buy_ids
    )


__all__ = [
    "ReportingCoverageLimitation",
    "ReportingCoverageRecord",
    "ReportingCoverageStatus",
    "build_reporting_coverage",
]
