"""Bound counterparty-asserted domain lists in capabilities (AdCP 3.3).

``media_buy.portfolio.publisher_domains`` and ``signals.data_provider_domains``
are populated from counterparties' ``/.well-known/adagents.json``, which any
domain owner publishes without the named agent's consent. AdCP 3.3
(``docs/building/by-layer/L1/security.mdx`` §Response Size Bounds): "A single
counterparty-asserted field MUST NOT exceed one eighth of
``MAX_CAPABILITIES_BYTES`` (8,192 bytes), and all such fields together MUST NOT
exceed one quarter (16,384 bytes). Measure uncompressed serialized JSON — the
member name, the array punctuation, and the quoted elements". The structural
bounds in each field's ``x-adcp-validation`` are "at most 100 unique entries,
each at most 253 characters". Where the real set does not fit, an agent "MUST
truncate and mark the list incomplete (``publisher_domains_complete: false``,
``data_provider_domains_complete: false``) rather than exceed the budget, and
MUST NOT drop a field it does in fact populate or serve a truncated list as
complete."

:func:`bound_counterparty_domains` is the capability-builder helper: it keeps
declaration order, drops duplicates and entries over 253 characters, then cuts
on the entry and byte bounds, and reports whether the result is complete. The
framework applies :func:`bound_capability_domain_lists` to every
``get_adcp_capabilities`` response it projects, so a declared list that does
not fit is truncated and flagged rather than served whole.

Both lists are routing hints, never allowlists: a buyer MUST NOT treat a
domain's absence as evidence that the agent is unauthorized for it, complete
flag or not. The domain's own ``adagents.json`` is the authority.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

__all__ = [
    "COUNTERPARTY_DOMAIN_LIST_BYTE_BUDGET",
    "COUNTERPARTY_DOMAIN_LISTS_COMBINED_BYTE_BUDGET",
    "MAX_COUNTERPARTY_DOMAIN_LENGTH",
    "MAX_COUNTERPARTY_DOMAINS",
    "BoundedDomainList",
    "bound_capability_domain_lists",
    "bound_counterparty_domains",
]

#: Per-field budget: one eighth of ``MAX_CAPABILITIES_BYTES`` (64 KiB).
COUNTERPARTY_DOMAIN_LIST_BYTE_BUDGET = 8 * 1024
#: Budget across every counterparty-asserted field: one quarter of 64 KiB.
COUNTERPARTY_DOMAIN_LISTS_COMBINED_BYTE_BUDGET = 16 * 1024
#: ``x-adcp-validation.advisory_bounds.max_items``.
MAX_COUNTERPARTY_DOMAINS = 100
#: ``x-adcp-validation.advisory_bounds.item_max_length``.
MAX_COUNTERPARTY_DOMAIN_LENGTH = 253

CounterpartyDomainField = Literal["publisher_domains", "data_provider_domains"]


@dataclass(frozen=True)
class BoundedDomainList:
    """A counterparty-asserted list cut to the AdCP 3.3 bounds.

    ``complete`` is ``False`` when any declared entry was left out; set the
    field's ``*_complete`` flag from it. ``serialized_bytes`` is the size of
    ``"<field>":[...]`` as compact JSON, the unit the budget is stated in.
    """

    field: str
    domains: tuple[str, ...]
    complete: bool
    serialized_bytes: int


def _member_bytes(field: str, domains: Iterable[str]) -> int:
    return len(json.dumps({field: list(domains)}, separators=(",", ":")).encode("utf-8")) - 2


def bound_counterparty_domains(
    domains: Iterable[str],
    *,
    field: CounterpartyDomainField = "publisher_domains",
    byte_budget: int = COUNTERPARTY_DOMAIN_LIST_BYTE_BUDGET,
    max_entries: int = MAX_COUNTERPARTY_DOMAINS,
) -> BoundedDomainList:
    """Cut a ``publisher_domains`` / ``data_provider_domains`` list to the bounds.

    Keeps the declared order, so put the publishers or providers you most want
    buyers to find first. Duplicates and entries longer than 253 characters are
    dropped (and make the list incomplete); then entries are kept while the
    count stays within ``max_entries`` and ``"<field>":[...]`` stays within
    ``byte_budget`` bytes. Raises :class:`ValueError` if no entry fits: the
    spec forbids dropping a field the agent populates, so the caller must fix
    the list rather than serve it empty.
    """
    if field not in ("publisher_domains", "data_provider_domains"):
        raise ValueError(f"{field!r} is not a counterparty-asserted domain list")
    declared = list(domains)
    kept: list[str] = []
    seen: set[str] = set()
    # ``"<field>":[]`` and one comma per entry after the first.
    size = _member_bytes(field, ())
    for domain in declared:
        if domain in seen or len(domain) > MAX_COUNTERPARTY_DOMAIN_LENGTH:
            continue
        entry = len(json.dumps(domain).encode("utf-8")) + (1 if kept else 0)
        if len(kept) >= max_entries or size + entry > byte_budget:
            break
        seen.add(domain)
        kept.append(domain)
        size += entry
    if declared and not kept:
        raise ValueError(f"no {field} entry fits the AdCP counterparty-asserted list bounds")
    return BoundedDomainList(
        field=field,
        domains=tuple(kept),
        # Dropping a duplicate leaves the set whole; anything else does not.
        complete=len(kept) == len(set(declared)),
        serialized_bytes=size,
    )


def bound_capability_domain_lists(
    response: MutableMapping[str, Any],
    *,
    combined_byte_budget: int = COUNTERPARTY_DOMAIN_LISTS_COMBINED_BYTE_BUDGET,
) -> bool:
    """Bound both counterparty-asserted lists of a capabilities response in place.

    ``response`` is a ``get_adcp_capabilities`` wire dict. A list that exceeds
    a bound is truncated and its ``*_complete`` flag set to ``False``; a list
    that fits is left exactly as declared (including an explicit
    ``*_complete: false``). The per-field budget applies to each list and the
    combined budget to both together, ``publisher_domains`` first. Returns
    whether anything was truncated.
    """
    remaining = combined_byte_budget
    truncated = False
    locations: tuple[tuple[tuple[str, ...], CounterpartyDomainField], ...] = (
        (("media_buy", "portfolio"), "publisher_domains"),
        (("signals",), "data_provider_domains"),
    )
    for path, field in locations:
        parent: Any = response
        for key in path:
            parent = parent.get(key) if isinstance(parent, MutableMapping) else None
        if not isinstance(parent, MutableMapping):
            continue
        domains = parent.get(field)
        if not isinstance(domains, list) or not all(isinstance(d, str) for d in domains):
            continue
        bounded = bound_counterparty_domains(
            domains,
            field=field,
            byte_budget=min(COUNTERPARTY_DOMAIN_LIST_BYTE_BUDGET, remaining),
        )
        remaining -= bounded.serialized_bytes
        if bounded.complete:
            continue
        truncated = True
        parent[field] = list(bounded.domains)
        parent[f"{field}_complete"] = False
        logger.warning(
            "[adcp.decisioning] get_adcp_capabilities %s truncated from %d to %d "
            "entries (%d bytes) to stay within the AdCP 3.3 counterparty-asserted "
            "list bounds; %s_complete is false. Declare the publishers or providers "
            "you represent rather than mirroring adagents.json, or bound the list "
            "with adcp.decisioning.bound_counterparty_domains.",
            field,
            len(domains),
            len(bounded.domains),
            bounded.serialized_bytes,
            field,
        )
    return truncated
