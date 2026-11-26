"""Keep obligations and revisions inside the coverage each obligation froze.

An obligation is immutable, and its coverage is the contract every revision of
it is read against (``core/reporting-obligation.json``: nested revisions match
the obligation's scope; ``core/reporting-revision.json``: rows, ``row_count``,
control totals and the digest cover only ``coverage.covered_package_ids``). A
revision whose rows name a media buy or package outside that coverage can never
be accepted by a buyer, and no later revision of the same obligation can fix
it. These guards refuse such content on the seller side, with an error that
names what contradicts what, instead of publishing it.

Two checks:

* **At freeze** (:func:`_package_scope_conflict`): a package-grain period must
  take its package scope from period-end roster evidence, and a zero-buy
  period owns no packages. An empty package set over frozen buys is *not* a
  conflict by itself: a buy can legitimately have no package in flight in a
  period (package flights narrower than the buy's, or an explicitly configured
  buy outside its flight), which :class:`~adcp.reporting.ledger.CompletePackageRoster`
  proves with an empty set.
* **At publish** (:func:`_require_rows_within_frozen_coverage`): every
  media buy a row names must be in the obligation's frozen ``media_buy_ids``
  and, at package grain, every package it names in the covered packages.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from adcp.reporting.ledger.store import LedgerConflictError

if TYPE_CHECKING:
    from adcp.reporting.ledger.models import ReportingObligationRecord
    from adcp.reporting.ledger.package_roster import PackageRosterResult

#: How many offending IDs an error message lists; the attributes keep them all.
_MESSAGE_ID_LIMIT = 20


def _listed(values: Sequence[str]) -> str:
    shown = ", ".join(values[:_MESSAGE_ID_LIMIT])
    hidden = len(values) - _MESSAGE_ID_LIMIT
    return f"[{shown}{f', +{hidden} more' if hidden > 0 else ''}]"


class ReportingRowCoverageError(LedgerConflictError):
    """A revision's rows name media buys or packages outside its obligation's frozen coverage.

    ``code`` is ``ROW_COVERAGE_MISMATCH``. Nothing was committed. The
    obligation is immutable, so the same rows can never publish against it:
    the fix is upstream (the package roster or media-buy scope the seller
    freezes for new periods, or a source that returned rows it was not asked
    for), not a retry.
    """

    def __init__(
        self,
        obligation: ReportingObligationRecord,
        *,
        media_buy_ids: Iterable[str] = (),
        package_ids: Iterable[str] = (),
        unattributed_rows: int = 0,
    ) -> None:
        self.reporting_obligation_id = obligation.reporting_obligation_id
        self.account_id = obligation.account_id
        self.delivery_config_id = obligation.delivery_config_id
        self.delivery_config_version = obligation.delivery_config_version
        self.period_start = obligation.period.start
        self.period_end = obligation.period.end
        self.period_source_timezone = obligation.period.source_timezone
        self.coverage_status = obligation.coverage_status
        #: The offending media buys and packages, sorted.
        self.media_buy_ids: tuple[str, ...] = tuple(sorted(set(media_buy_ids)))
        self.package_ids: tuple[str, ...] = tuple(sorted(set(package_ids)))
        #: Rows naming neither a media buy nor a package (refused only where the
        #: covered subset must be attributable, under ``allow_partial``).
        self.unattributed_rows = unattributed_rows
        named = [
            *(["media buys " + _listed(self.media_buy_ids)] if self.media_buy_ids else []),
            *(["packages " + _listed(self.package_ids)] if self.package_ids else []),
            *(
                [f"{unattributed_rows} rows naming no media buy or package"]
                if unattributed_rows
                else []
            ),
        ]
        super().__init__(
            "ROW_COVERAGE_MISMATCH",
            f"revision rows for obligation {self.reporting_obligation_id} (account "
            f"{self.account_id}, configuration {self.delivery_config_id}@"
            f"{self.delivery_config_version}, period {self.period_start.isoformat()} to "
            f"{self.period_end.isoformat()} {self.period_source_timezone}) fall outside its "
            f"frozen {self.coverage_status} coverage: {'; '.join(named)}. Nothing was "
            "committed. The obligation is immutable, so fix the package roster or source "
            "for new periods rather than retrying this one",
        )


class ReportingPackageScopeError(LedgerConflictError):
    """A period's package scope contradicts its frozen buys; the obligation is not frozen.

    ``code`` is ``PACKAGE_SCOPE_INCONSISTENT``.
    """

    def __init__(self, message: str) -> None:
        super().__init__("PACKAGE_SCOPE_INCONSISTENT", message)


def _frozen_row_scope(
    obligation: ReportingObligationRecord,
) -> tuple[frozenset[str], frozenset[str]]:
    """The media buys and packages a revision of ``obligation`` may name in its rows.

    Media buys: the frozen denominator, ``media_buy_ids``. The revision
    inherits it as its own ``media_buy_ids`` and slice identity, and a buy
    outside it belongs to no period this obligation describes.

    Packages: the covered packages of the wire coverage
    :func:`~adcp.reporting.ledger.status._obligation_coverage` serves, because
    rows "cover only ``coverage.covered_package_ids``"
    (``core/reporting-revision.json``). A retained partition contributes its
    ``covered_package_ids``; status-only coverage contributes every frozen
    package when ``full`` and none otherwise.
    """
    coverage = obligation.coverage
    if coverage is not None:
        packages = frozenset(coverage.covered_package_ids)
    elif obligation.coverage_status == "full":
        packages = frozenset(obligation.package_ids)
    else:
        packages = frozenset()
    return frozenset(obligation.media_buy_ids), packages


def _require_rows_within_frozen_coverage(
    obligation: ReportingObligationRecord,
    rows: Sequence[Mapping[str, Any]],
    *,
    package_grain: bool,
) -> None:
    """Refuse rows naming a media buy or package outside the obligation's frozen coverage.

    A row's ``media_buy_id`` must be in the frozen ``media_buy_ids``. At
    package grain -- the report requests the ``package`` dimension, or the
    obligation froze packages -- its ``package_id`` must also be a covered
    package (:func:`_frozen_row_scope`). At buy grain ``package_id`` is not a
    coverage dimension: the SDK's own delivery flattening emits it, and the
    obligation has no package denominator to hold it against. IDs compare as
    strings, as the inline source matches rows to constituents. A row naming
    neither carries no scope to contradict and is left alone here.
    """
    buys, packages = _frozen_row_scope(obligation)
    package_grain = package_grain or bool(obligation.package_ids)
    outside_buys: set[str] = set()
    outside_packages: set[str] = set()
    for row in rows:
        buy = row.get("media_buy_id")
        package = row.get("package_id")
        if buy is not None and str(buy) not in buys:
            outside_buys.add(str(buy))
        if package_grain and package is not None and str(package) not in packages:
            outside_packages.add(str(package))
    if outside_buys or outside_packages:
        raise ReportingRowCoverageError(
            obligation, media_buy_ids=outside_buys, package_ids=outside_packages
        )


def _package_scope_conflict(
    media_buy_ids: Sequence[str],
    roster: PackageRosterResult | None,
    *,
    package_grain: bool,
) -> str | None:
    """Why a period's package evidence cannot be frozen, or ``None``.

    ``package_grain`` is whether the period's report has the ``package``
    dimension, which is when the freezing path consults a package roster.

    * A package-grain period with media buys needs roster evidence. Without
      it, an empty package set means "never resolved", not "proven empty",
      and a ``full`` obligation would promise that no package rows exist.
    * A zero-buy period owns no packages: a package set over an empty buy
      denominator names packages of no frozen buy.

    Not a conflict: a proven-empty roster (``CompletePackageRoster(())``,
    whole-configuration or per buy) over frozen buys, which freezes ``full``
    with no packages; and ``MissingHistoricalPackageRoster``, which freezes
    ``unknown``.
    """
    from adcp.reporting.ledger.package_roster import (
        CompletePackageRoster,
        UnsupportedMediaBuyRoster,
    )

    if package_grain and media_buy_ids and roster is None:
        return "a package-grain period with media buys needs period-end package roster evidence"
    if not media_buy_ids and (
        isinstance(roster, (CompletePackageRoster, UnsupportedMediaBuyRoster))
        and roster.package_ids
    ):
        return "a zero-buy period cannot own packages; the roster names packages of no frozen buy"
    return None


__all__ = ["ReportingPackageScopeError", "ReportingRowCoverageError"]
