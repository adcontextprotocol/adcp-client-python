"""Resolve a reporting-subscription selector to offerings, as both sides must.

``core/reporting-subscription.json`` (``x-adcp-validation.selection``): an
offering is a candidate when ``feed_purpose``, ``period_duration``,
``reconciliation_mode``, ``required_finality`` (in ``supported_finality``) and
a present ``alignment`` match, a present destination matches its ``method``
(no destination selects only API-delivered offerings), and the product lists
it in ``reporting_delivery_offering_ids``. ``match`` then applies:

* ``exact``: the offering's dimension set equals the selector's. Native
  dimensions that do not map to the standard vocabulary count as distinct.
* ``finest``: the single offering with the most mapped dimensions among those
  including every selector dimension.
* ``all``: every offering including every selector dimension.

Ties under ``exact`` and ``finest`` go to the lowest ``offering_id`` in byte
order. ``match`` defaults to ``exact`` when ``dimensions`` is present and to
``finest`` when it is absent.

Offerings do not publish their mapped dimensions on the wire; the seller maps
each report definition's native dimensions. :class:`SubscriptionOffering`
therefore carries the mapping explicitly, so a buyer that knows the mapping
(for example from the seller's documentation or from the obligations it has
already received) resolves exactly what the seller resolves.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from adcp.reporting.subscriptions._clock import PeriodCalendar, require_zone, utc_instant

#: The 3.3 standard breakdown vocabulary (``enums/reporting-dimension.json``).
#: It is extensible: a selector naming a value no offering maps matches nothing.
STANDARD_REPORTING_DIMENSIONS: frozenset[str] = frozenset(
    {"package", "creative", "country", "region", "metro"}
)

SubscriptionMatch = Literal["exact", "finest", "all"]
_ALIGNMENTS = frozenset({"utc", "account_timezone", "source_timezone", "billing_cycle"})
#: Zone names a ``utc`` schedule can produce periods in under ``period_timezone``.
UTC_ZONE_NAMES: frozenset[str] = frozenset(
    {
        "UTC",
        "Etc/UTC",
        "Etc/UCT",
        "UCT",
        "Etc/Universal",
        "Universal",
        "Etc/Zulu",
        "Zulu",
        "GMT",
        "Etc/GMT",
        "Etc/GMT+0",
        "Etc/GMT-0",
        "Etc/GMT0",
        "GMT+0",
        "GMT-0",
        "GMT0",
        "Etc/Greenwich",
        "Greenwich",
    }
)


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        value = item.get(name, default)
    else:
        value = getattr(item, name, default)
    return default if value is None else value


def _text(value: Any) -> str:
    """Enum-safe string projection of generated-model values."""
    inner = getattr(value, "value", value)
    return str(inner)


def _byte_key(offering_id: str) -> bytes:
    return offering_id.encode("utf-8")


@dataclass(frozen=True)
class SubscriptionOffering:
    """One reporting-delivery offering, with the seller's dimension mapping.

    ``dimensions`` are the standard-vocabulary values the offering's report
    definition maps onto. ``unmapped_dimensions`` are native dimensions with no
    vocabulary mapping; they make an ``exact`` match fail and are ignored when
    ``finest`` counts mapped dimensions.

    ``period_timezone`` is the schedule's IANA zone for ``source_timezone``
    (fixed, or the seller's account-resolved upstream zone) and fixed
    ``billing_cycle`` offerings. ``period_timezone_policy`` is the
    ``source_timezone`` policy: an ``account_resolved`` schedule can produce
    periods in any zone a ``period_timezone`` override names.
    ``available_from`` and ``withdrawn_at`` bound when the offering is a
    candidate; a change in resolution is a cutover.
    """

    offering_id: str
    report_definition_id: str
    reporting_profile: str
    feed_purpose: str
    period_duration: str
    alignment: str
    delivery_sla: str
    supported_finality: frozenset[str]
    reconciliation_mode: str
    dimensions: frozenset[str] = frozenset()
    unmapped_dimensions: frozenset[str] = frozenset()
    period_timezone: str | None = None
    period_anchor: datetime | None = None
    period_anchor_policy: str | None = None
    period_timezone_policy: str | None = None
    method_pattern: str | None = None
    method_transport: str | None = None
    available_from: datetime | None = None
    withdrawn_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.offering_id:
            raise ValueError("offering_id is required")
        if self.alignment not in _ALIGNMENTS:
            raise ValueError(f"unknown schedule alignment {self.alignment!r}")
        object.__setattr__(self, "supported_finality", frozenset(self.supported_finality))
        object.__setattr__(self, "dimensions", frozenset(self.dimensions))
        object.__setattr__(self, "unmapped_dimensions", frozenset(self.unmapped_dimensions))
        overlap = self.dimensions & self.unmapped_dimensions
        if overlap:
            raise ValueError(f"dimensions are both mapped and unmapped: {sorted(overlap)}")
        if self.period_timezone is not None:
            require_zone(self.period_timezone, field="period_timezone")
        if self.period_anchor is not None:
            object.__setattr__(
                self, "period_anchor", utc_instant(self.period_anchor, field="period_anchor")
            )
        for name in ("available_from", "withdrawn_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_instant(value, field=name))
        if (
            self.available_from is not None
            and self.withdrawn_at is not None
            and self.withdrawn_at <= self.available_from
        ):
            raise ValueError("withdrawn_at must be after available_from")

    @classmethod
    def from_offering(
        cls,
        offering: Any,
        *,
        dimensions: Iterable[str] = (),
        unmapped_dimensions: Iterable[str] = (),
        period_timezone: str | None = None,
        available_from: datetime | None = None,
        withdrawn_at: datetime | None = None,
    ) -> SubscriptionOffering:
        """Project a ``ReportingDeliveryOffering`` (model or wire mapping).

        ``period_timezone`` supplies the account-resolved zone of an
        ``account_resolved`` ``source_timezone`` offering; a fixed zone on the
        offering wins when both are present and they must agree.
        """
        if hasattr(offering, "model_dump"):
            offering = offering.model_dump(mode="json", exclude_none=True)
        if not isinstance(offering, Mapping):
            raise TypeError("offering must be a ReportingDeliveryOffering or a mapping")
        schedule = offering["schedule"]
        fixed_zone = schedule.get("period_timezone")
        if fixed_zone is not None and period_timezone is not None and fixed_zone != period_timezone:
            raise ValueError("period_timezone disagrees with the offering's fixed timezone")
        anchor = schedule.get("period_anchor")
        method = offering.get("method") or {}
        profile = offering["reporting_profile"]
        return cls(
            offering_id=str(offering["offering_id"]),
            report_definition_id=str(offering["report_definition_id"]),
            reporting_profile=str(profile["id"] if isinstance(profile, Mapping) else profile),
            feed_purpose=str(offering["feed_purpose"]),
            period_duration=str(schedule["period_duration"]),
            alignment=str(schedule["alignment"]),
            delivery_sla=str(schedule["delivery_sla"]),
            supported_finality=frozenset(str(item) for item in offering["supported_finality"]),
            reconciliation_mode=str(offering["reconciliation_mode"]),
            dimensions=frozenset(dimensions),
            unmapped_dimensions=frozenset(unmapped_dimensions),
            period_timezone=fixed_zone or period_timezone,
            period_anchor=(
                datetime.fromisoformat(str(anchor).replace("Z", "+00:00")) if anchor else None
            ),
            period_anchor_policy=schedule.get("period_anchor_policy"),
            period_timezone_policy=schedule.get("period_timezone_policy"),
            method_pattern=method.get("pattern"),
            method_transport=method.get("transport"),
            available_from=available_from,
            withdrawn_at=withdrawn_at,
        )

    def available_at(self, instant: datetime) -> bool:
        return (self.available_from is None or self.available_from <= instant) and (
            self.withdrawn_at is None or instant < self.withdrawn_at
        )

    @property
    def schedulable(self) -> bool:
        """Whether a subscription can derive this offering's boundaries.

        A subscription carries no billing anchor, so a ``billing_cycle``
        offering whose anchor is not fixed is never resolvable.
        """
        return self.alignment != "billing_cycle" or (
            self.period_anchor is not None and self.period_timezone is not None
        )

    def can_produce(self, period_timezone: str | None) -> bool:
        """Whether the schedule can produce periods in a ``period_timezone`` override.

        A ``utc`` schedule only in a UTC alias; a fixed-timezone schedule only
        in that zone; an ``account_timezone`` or ``account_resolved`` schedule
        in any zone.
        """
        if not self.schedulable:
            return False
        if period_timezone is None:
            return True
        if self.alignment == "utc":
            return period_timezone in UTC_ZONE_NAMES
        if self.alignment == "account_timezone":
            return True
        if self.alignment == "source_timezone":
            return self.period_timezone_policy != "fixed" or self.period_timezone == period_timezone
        return self.period_timezone == period_timezone

    def zone(self, account_timezone: str | None, period_timezone: str | None = None) -> str:
        """The IANA zone this offering's periods use for an account.

        ``period_timezone`` is a single-account subscription's override; it
        applies to ``account_timezone`` and ``account_resolved`` schedules.
        """
        if self.alignment == "utc":
            return "UTC"
        if self.alignment == "account_timezone":
            zone = period_timezone or account_timezone
            if zone is None:
                raise ValueError(
                    f"offering {self.offering_id!r} is account_timezone aligned; "
                    "the account timezone is required"
                )
            return zone
        if self.alignment == "source_timezone":
            zone = (
                self.period_timezone
                if self.period_timezone_policy == "fixed"
                else (period_timezone or self.period_timezone)
            )
            if zone is None:
                raise ValueError(
                    f"offering {self.offering_id!r} resolves its source timezone per account; "
                    "pass the resolved zone as period_timezone"
                )
            return zone
        if self.period_timezone is None:
            raise ValueError(f"offering {self.offering_id!r} has no period_timezone")
        return self.period_timezone

    def calendar(
        self, account_timezone: str | None, period_timezone: str | None = None
    ) -> PeriodCalendar:
        """This offering's normative period calendar for an account."""
        return PeriodCalendar.for_schedule(
            period_duration=self.period_duration,
            delivery_sla=self.delivery_sla,
            zone_name=self.zone(account_timezone, period_timezone),
            anchor=self.period_anchor if self.alignment == "billing_cycle" else None,
        )


@dataclass(frozen=True)
class SubscriptionDestination:
    """The delivery pattern and transport of a principal reporting destination."""

    pattern: str
    transport: str | None = None

    @classmethod
    def coerce(cls, value: Any) -> SubscriptionDestination:
        if isinstance(value, SubscriptionDestination):
            return value
        configuration = _value(value, "configuration")
        source = configuration if configuration is not None else value
        if hasattr(source, "model_dump"):
            source = source.model_dump(mode="json", exclude_none=True)
        root = getattr(source, "root", None)
        if root is not None:
            source = root.model_dump(mode="json", exclude_none=True)
        pattern = _value(source, "pattern")
        if pattern is None:
            raise ValueError("a reporting destination needs a pattern")
        transport = _value(source, "transport")
        return cls(
            pattern=_text(pattern), transport=None if transport is None else _text(transport)
        )


@dataclass(frozen=True)
class SubscriptionSelector:
    """Normalized ``selector`` plus the subscription's ``reconciliation_mode``."""

    feed_purpose: str
    period_duration: str
    required_finality: str
    reconciliation_mode: str
    match: SubscriptionMatch
    dimensions: frozenset[str] | None = None
    alignment: str | None = None
    period_timezone: str | None = None
    destination_id: str | None = None

    @classmethod
    def from_subscription(cls, subscription: Any) -> SubscriptionSelector:
        """Read ``selector``, ``reconciliation_mode`` and destination from a subscription."""
        if hasattr(subscription, "model_dump"):
            subscription = subscription.model_dump(mode="json", exclude_none=True)
        selector = _value(subscription, "selector")
        if selector is None:
            raise ValueError("subscription has no selector")
        raw_dimensions = _value(selector, "dimensions")
        dimensions = (
            None if raw_dimensions is None else frozenset(_text(item) for item in raw_dimensions)
        )
        raw_match = _value(selector, "match")
        match: str = (
            _text(raw_match)
            if raw_match is not None
            else ("exact" if dimensions is not None else "finest")
        )
        if match not in {"exact", "finest", "all"}:
            raise ValueError(f"unknown selector match {match!r}")
        destination = _value(subscription, "destination")
        alignment = _value(selector, "alignment")
        period_timezone = _value(selector, "period_timezone")
        return cls(
            feed_purpose=_text(_value(selector, "feed_purpose")),
            period_duration=_text(_value(selector, "period_duration")),
            required_finality=_text(_value(selector, "required_finality")),
            reconciliation_mode=_text(_value(subscription, "reconciliation_mode")),
            match=match,  # type: ignore[arg-type]
            dimensions=dimensions,
            alignment=None if alignment is None else _text(alignment),
            period_timezone=None if period_timezone is None else _text(period_timezone),
            destination_id=(
                None if destination is None else _text(_value(destination, "destination_id"))
            ),
        )


@dataclass(frozen=True)
class SubscriptionResolution:
    """Offerings a selector resolved to for one product, in byte order."""

    offerings: tuple[SubscriptionOffering, ...]
    candidates: tuple[str, ...] = field(default=())

    @property
    def offering_ids(self) -> tuple[str, ...]:
        return tuple(item.offering_id for item in self.offerings)


def _is_candidate(
    offering: SubscriptionOffering,
    selector: SubscriptionSelector,
    *,
    destination: SubscriptionDestination | None,
) -> bool:
    if (
        offering.feed_purpose != selector.feed_purpose
        or offering.period_duration != selector.period_duration
        or offering.reconciliation_mode != selector.reconciliation_mode
        or selector.required_finality not in offering.supported_finality
    ):
        return False
    if selector.alignment is not None and offering.alignment != selector.alignment:
        return False
    if destination is None:
        if offering.method_pattern is not None:
            return False
    elif (
        offering.method_pattern != destination.pattern
        or offering.method_transport != destination.transport
    ):
        return False
    return offering.can_produce(selector.period_timezone)


def resolve_subscription_offerings(
    subscription: Any,
    offerings: Sequence[SubscriptionOffering],
    *,
    product_offering_ids: Iterable[str],
    destination: Any | None = None,
    at: datetime | None = None,
) -> SubscriptionResolution:
    """Resolve one subscription for one product, deterministically.

    ``subscription`` is a ``ReportingSubscription`` (model or mapping) or a
    :class:`SubscriptionSelector`. ``destination`` is the named principal
    reporting destination (model, mapping, or :class:`SubscriptionDestination`)
    and is required when the subscription names one. ``at`` restricts
    candidates to offerings available at that instant.
    """
    selector = (
        subscription
        if isinstance(subscription, SubscriptionSelector)
        else SubscriptionSelector.from_subscription(subscription)
    )
    if selector.destination_id is not None and destination is None:
        raise ValueError(
            f"subscription names destination {selector.destination_id!r}; pass its pattern"
        )
    target = None if destination is None else SubscriptionDestination.coerce(destination)
    listed = set(product_offering_ids)
    instant = None if at is None else utc_instant(at, field="at")
    candidates = sorted(
        (
            offering
            for offering in offerings
            if offering.offering_id in listed
            and (instant is None or offering.available_at(instant))
            and _is_candidate(offering, selector, destination=target)
        ),
        key=lambda item: _byte_key(item.offering_id),
    )
    wanted = selector.dimensions
    if selector.match == "exact":
        matches = [
            item
            for item in candidates
            if wanted is None or (item.dimensions == wanted and not item.unmapped_dimensions)
        ]
        chosen: tuple[SubscriptionOffering, ...] = tuple(matches[:1])
    else:
        including = [item for item in candidates if wanted is None or wanted <= item.dimensions]
        if selector.match == "all":
            chosen = tuple(including)
        elif including:
            most = max(len(item.dimensions) for item in including)
            chosen = (next(item for item in including if len(item.dimensions) == most),)
        else:
            chosen = ()
    return SubscriptionResolution(
        offerings=chosen, candidates=tuple(item.offering_id for item in candidates)
    )


__all__ = [
    "STANDARD_REPORTING_DIMENSIONS",
    "UTC_ZONE_NAMES",
    "SubscriptionDestination",
    "SubscriptionMatch",
    "SubscriptionOffering",
    "SubscriptionResolution",
    "SubscriptionSelector",
    "resolve_subscription_offerings",
]
