"""Lossless aware timestamps for reporting boundaries on every supported Python."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from adcp.reporting.evidence import aware_utc

_TIMESTAMP = re.compile(
    r"(?P<local>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?)"
    r"(?:Z|(?P<sign>[+-])(?P<hours>[01][0-9]|2[0-3]):(?P<minutes>[0-5][0-9])"
    r"(?::(?P<seconds>[0-5][0-9])(?:\.(?P<microseconds>[0-9]{1,6}))?)?)"
)


def aware_timestamp(value: str) -> datetime:
    """Decode the exact microsecond instant without changing captured wire bytes.

    PostgreSQL JSON omits trailing fractional zeros. Python 3.10's ISO parser
    only accepts three or six fractional digits. Padding supplies equivalent
    zeros; excess precision, naive values and invalid dates remain refused.
    Historical PostgreSQL time zones can also carry an offset in seconds.
    """
    matched = _TIMESTAMP.fullmatch(value) if type(value) is str else None
    if matched is None:
        raise ValueError("reporting timestamp requires an aware microsecond instant")
    normalized = re.sub(r"\.([0-9]+)", lambda m: "." + m[1].ljust(6, "0"), matched["local"])
    try:
        # datetime.fromisoformat also drops subsecond offsets when their
        # whole-second part is zero. Construct the exact offset independently.
        offset = timedelta(
            hours=int(matched["hours"] or 0),
            minutes=int(matched["minutes"] or 0),
            seconds=int(matched["seconds"] or 0),
            microseconds=int((matched["microseconds"] or "").ljust(6, "0")),
        )
        if matched["sign"] == "-":
            offset = -offset
        return aware_utc(datetime.fromisoformat(normalized).replace(tzinfo=timezone(offset)))
    except (ValueError, OverflowError):
        raise ValueError("reporting timestamp requires an aware microsecond instant") from None
