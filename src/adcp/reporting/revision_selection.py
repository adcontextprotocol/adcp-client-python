"""Strict, pure publication selection shared by seller and buyer projections.

Validate the *whole* obligation history before choosing finality. Destination
state, timestamps and readability never choose a publication. In particular,
an official close does not supersede (or excuse damage in) the snapshot chain.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Generic, Literal, Protocol, TypeVar

__all__ = [
    "REPORTING_SELECTOR_VERSION",
    "ReportingRevisionCorrupt",
    "ReportingRevisionNotReady",
    "ReportingRevisionSelected",
    "ReportingRevisionSelection",
    "RevisionHistoryEntry",
    "select_reporting_revision",
]

REPORTING_SELECTOR_VERSION = 2


class _Revision(Protocol):
    @property
    def account_id(self) -> str: ...

    @property
    def reporting_obligation_id(self) -> str: ...

    @property
    def reporting_revision_id(self) -> str: ...

    @property
    def finality(self) -> str: ...

    @property
    def supersedes_reporting_revision_id(self) -> str | None: ...


R = TypeVar("R", bound=_Revision)


@dataclass(frozen=True, slots=True)
class RevisionHistoryEntry:
    """Adapter for wire revisions whose ownership is established by their caller."""

    account_id: str
    reporting_obligation_id: str
    reporting_revision_id: str
    finality: str
    supersedes_reporting_revision_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReportingRevisionSelected(Generic[R]):
    revision: R
    kind: Literal["selected"] = field(default="selected", init=False)


@dataclass(frozen=True, slots=True)
class ReportingRevisionNotReady:
    reason: Literal["empty_history", "official_required"]
    kind: Literal["not_ready"] = field(default="not_ready", init=False)


@dataclass(frozen=True, slots=True)
class ReportingRevisionCorrupt:
    # Closed diagnostics: no row data, credentials or provider errors.
    reason: Literal[
        "ownership_mismatch",
        "duplicate_revision_id",
        "invalid_revision_identity",
        "invalid_finality",
        "missing_predecessor",
        "cross_finality_edge",
        "official_predecessor",
        "forked_snapshot_history",
        "disconnected_snapshot_history",
        "revision_cycle",
        "multiple_officials",
    ]
    kind: Literal["corrupt"] = field(default="corrupt", init=False)


ReportingRevisionSelection = (
    ReportingRevisionSelected[R] | ReportingRevisionNotReady | ReportingRevisionCorrupt
)


def select_reporting_revision(
    revisions: Sequence[R],
    *,
    account_id: str,
    reporting_obligation_id: str,
    required_finality: str,
) -> ReportingRevisionSelection[R]:
    """Linear-time, order-independent selection, including disconnected cycles.

    Only empty history or a missing required official is ordinary not-ready.
    The caller supplies every retained revision belonging to the obligation,
    without filtering by finality, readability, or existing materializations.
    A selected unreadable revision remains selected: repair it, never fall back.
    """
    if (
        type(account_id) is not str
        or type(reporting_obligation_id) is not str
        or any(
            type(r.account_id) is not str
            or type(r.reporting_obligation_id) is not str
            or r.account_id != account_id
            or r.reporting_obligation_id != reporting_obligation_id
            for r in revisions
        )
    ):
        return ReportingRevisionCorrupt("ownership_mismatch")
    if any(
        type(r.reporting_revision_id) is not str
        or not r.reporting_revision_id
        or (
            r.supersedes_reporting_revision_id is not None
            and type(r.supersedes_reporting_revision_id) is not str
        )
        for r in revisions
    ):
        return ReportingRevisionCorrupt("invalid_revision_identity")
    by_id = {r.reporting_revision_id: r for r in revisions}
    if len(by_id) != len(revisions):
        return ReportingRevisionCorrupt("duplicate_revision_id")
    if (
        type(required_finality) is not str
        or required_finality not in {"snapshot", "official"}
        or any(
            type(r.finality) is not str or r.finality not in {"snapshot", "official"}
            for r in revisions
        )
    ):
        return ReportingRevisionCorrupt("invalid_finality")
    if any(
        r.supersedes_reporting_revision_id is not None
        and r.supersedes_reporting_revision_id not in by_id
        for r in revisions
    ):
        return ReportingRevisionCorrupt("missing_predecessor")
    if any(
        r.supersedes_reporting_revision_id is not None
        and by_id[r.supersedes_reporting_revision_id].finality != r.finality
        for r in revisions
    ):
        return ReportingRevisionCorrupt("cross_finality_edge")
    if any(
        r.finality == "official" and r.supersedes_reporting_revision_id is not None
        for r in revisions
    ):
        return ReportingRevisionCorrupt("official_predecessor")
    officials = [r for r in revisions if r.finality == "official"]
    if len(officials) > 1:
        return ReportingRevisionCorrupt("multiple_officials")
    snapshots = [r for r in revisions if r.finality == "snapshot"]
    successors: dict[str, str] = {}
    for revision in snapshots:
        predecessor = revision.supersedes_reporting_revision_id
        if predecessor is not None:
            if predecessor in successors:
                return ReportingRevisionCorrupt("forked_snapshot_history")
            successors[predecessor] = revision.reporting_revision_id
    # Walk each component once. A unique-looking leaf cannot hide a cycle.
    visited: set[str] = set()
    for revision in snapshots:
        path: set[str] = set()
        node: str | None = revision.reporting_revision_id
        while node is not None and node not in visited:
            if node in path:
                return ReportingRevisionCorrupt("revision_cycle")
            path.add(node)
            node = by_id[node].supersedes_reporting_revision_id
        visited.update(path)
    roots = [r for r in snapshots if r.supersedes_reporting_revision_id is None]
    if snapshots and len(roots) != 1:
        return ReportingRevisionCorrupt("disconnected_snapshot_history")
    if not revisions:
        return ReportingRevisionNotReady("empty_history")
    if officials:
        return ReportingRevisionSelected(officials[0])
    if required_finality == "official":
        return ReportingRevisionNotReady("official_required")
    leaf = next(r for r in snapshots if r.reporting_revision_id not in successors)
    return ReportingRevisionSelected(leaf)
