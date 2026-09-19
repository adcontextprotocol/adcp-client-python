"""PostgreSQL JSON timestamps retain their exact instant on the Python floor."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from adcp.reporting._timestamp import aware_timestamp
from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.ledger.status_snapshot import snapshot_from_storage
from adcp.reporting.projection.capture import decode_projection_input
from adcp.validation.schema_loader import get_named_validator

from ._generation_support import configuration
from ._projection_support import projection_harness


@pytest.mark.parametrize("microsecond", [1, 100000, 120000, 123000, 123400, 123450, 123456])
@pytest.mark.parametrize("offset", ["+00:00:00", "-00:00:00", "+05:45:03", "-02:30:01"])
def test_fractional_offsets_preserve_the_exact_instant(microsecond, offset):
    hours, minutes, seconds = map(int, offset[1:].split(":"))
    delta = timedelta(hours=hours, minutes=minutes, seconds=seconds, microseconds=microsecond)
    if offset[0] == "-":
        delta = -delta
    value = "2026-09-01T12:00:00.12345" + offset + "." + f"{microsecond:06d}".rstrip("0")
    expected = datetime(2026, 9, 1, 12, 0, 0, 123450, tzinfo=timezone.utc) - delta
    assert aware_timestamp(value) == expected


@pytest.mark.parametrize("fraction", ["", ".1", ".12345", ".123456"])
def test_utc_z_is_aware_and_lossless(fraction):
    microsecond = int(fraction.lstrip(".").ljust(6, "0"))
    assert aware_timestamp("2026-09-01T12:00:00" + fraction + "Z") == datetime(
        2026, 9, 1, 12, 0, 0, microsecond, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("microsecond", [0, 100000, 120000, 123000, 123400, 123450, 123456])
@pytest.mark.parametrize("zone", ["UTC", "Asia/Kathmandu", "America/St_Johns", "Europe/Paris"])
async def test_actual_pg_json_precision_and_offset_survive_captured_decode(microsecond, zone):
    # Paris before 1911 also exercises a real seconds-bearing historical offset.
    at = datetime(1890 if zone == "Europe/Paris" else 2026, 9, 1, 12, tzinfo=timezone.utc)
    at = at.replace(microsecond=microsecond)
    async with projection_harness("postgres") as h:
        config = configuration()
        config = replace(
            config,
            schedule=replace(config.schedule, period_anchor=at - timedelta(days=1)),
            activated_at=at - timedelta(days=1),
            deactivated_at=at + timedelta(days=1),
        )
        await h.store.put_configuration(config)
        async with h.pool.connection() as c:
            await c.execute("SELECT set_config('TimeZone',%s,false)", (zone,))
            row = await (
                await c.execute(
                    "SELECT reporting_projection_document(%s,%s), to_jsonb(%s::timestamptz)",
                    (config.account_id, at, at),
                )
            ).fetchone()
        document, timestamp = row
        assert document["as_of"] == document["core"]["as_of"] == timestamp
        fraction = timestamp.split("T", 1)[1].split("+", 1)[0].split("-", 1)[0]
        fraction = fraction.partition(".")[2]
        assert fraction == (f"{microsecond:06d}".rstrip("0") if microsecond else "")
        raw = canonical_json_utf8_v1(document)
        core = snapshot_from_storage(document["core"])
        decoded = decode_projection_input(document)
        assert core == decoded.core
        assert core.as_of == at and core.as_of.microsecond == microsecond
        assert core.configurations == (config,)
        assert decoded.document == raw == canonical_json_utf8_v1(document)
        # Exercise the actual PostgreSQL representation through the public
        # named-schema path as well. RFC 3339 excludes the seconds-bearing
        # historical Paris offset that the private lossless decoder permits.
        validator = get_named_validator(
            "core/reporting-delivery-config-state.json", version="3.2.0-rc.3"
        )
        assert validator is not None
        field = validator.evolve(schema=validator.schema["properties"]["activated_at"])
        assert field.is_valid(timestamp) is (zone != "Europe/Paris")
        assert canonical_json_utf8_v1(document) == raw


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-01T12:00:00.12345",
        "2026-09-01T12:00:00",
        "2026-09-01T12:00:00.1234567+00:00",
        "2026-09-01T12:00:00.12x45+00:00",
        "2026-09-01T25:00:00.12345+00:00",
        "2026-02-30T12:00:00.12345+00:00",
        "2026-09-01T12:00:00.12345+25:00",
        "2026-09-01T12:00:00.12345+01:60",
        "2026-09-01T12:00:00.12345+00:09:60",
        "not-a-timestamp",
    ],
)
async def test_sql_boundary_rejects_invalid_naive_or_precision_losing_timestamps(timestamp):
    async with projection_harness("postgres") as h:
        async with h.pool.connection() as c:
            row = await (
                await c.execute(
                    "SELECT reporting_projection_document('acct_a',%s)",
                    (datetime(2026, 9, 1, tzinfo=timezone.utc),),
                )
            ).fetchone()
        document = deepcopy(row[0])
        document["as_of"] = document["core"]["as_of"] = timestamp
        with pytest.raises(ReportingNotificationError, match="status_projection_history_corrupt"):
            decode_projection_input(document)
