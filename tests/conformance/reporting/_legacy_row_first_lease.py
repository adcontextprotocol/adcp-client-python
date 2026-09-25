"""Frozen wrong-order control from 71c2bc0f, never a production implementation.

The exact predecessor SQL retains its real trigger wait and transaction rollback
behavior. Fixing the live base method must not silently disable this control.
"""

from datetime import datetime, timedelta

from adcp.reporting.ledger.pg import _utc
from adcp.reporting.ledger.store import LeasedConfiguration


async def row_first_period_close(
    self, *, worker_id: str, now: datetime, lease_seconds: float
) -> LeasedConfiguration | None:
    expires = _utc(now) + timedelta(seconds=lease_seconds)
    async with self._connection() as connection, connection.transaction():
        # The fairness rank is the primary ordering term. The WHERE clause
        # has already excluded every live lease, so among the survivors the
        # expiry carries no fairness information: ordering by it first
        # starves a crashed generation forever, because a peer that is
        # leased and released each turn is always NULL and NULL sorts
        # first. Without the rank at all, every released generation ties
        # and whichever tuple the scan yields first is re-leased forever.
        # Expiry and the generation key only break exact ties, which keeps
        # the order total and independent of physical layout.
        #
        # The rank is joined from a private table rather than held on
        # `reporting_configurations`, so the enumerated `reporting_*`
        # catalog that older binaries validate stays byte-identical. A
        # generation with no row yet has never been leased, which is what
        # COALESCE to 0 means, so a newly accepted generation is served
        # before any that already took a turn.
        row = await (
            await connection.execute(
                "UPDATE reporting_configurations SET lease_worker_id = %s,"
                " lease_expires_at = %s"
                " WHERE (account_id, delivery_config_id, delivery_config_version) = ("
                "   SELECT c.account_id, c.delivery_config_id, c.delivery_config_version"
                "   FROM reporting_configurations c"
                "   LEFT JOIN adcp_reporting_configuration_lease_turns t"
                "     ON (t.account_id, t.delivery_config_id, t.delivery_config_version)"
                "      = (c.account_id, c.delivery_config_id, c.delivery_config_version)"
                "   WHERE c.lease_expires_at IS NULL OR c.lease_expires_at <= %s"
                "   ORDER BY COALESCE(t.lease_turn, 0), c.lease_expires_at NULLS FIRST,"
                "     c.account_id, c.delivery_config_id, c.delivery_config_version"
                "   FOR UPDATE OF c SKIP LOCKED"
                "   LIMIT 1)"
                " RETURNING account_id, delivery_config_id, delivery_config_version",
                (worker_id, expires, _utc(now)),
            )
        ).fetchone()
        if row is not None:
            # Same transaction as the acquisition, so the rank can never
            # advance without the lease or the lease without the rank.
            await connection.execute(
                "INSERT INTO adcp_reporting_configuration_lease_turns"
                " (account_id, delivery_config_id, delivery_config_version, lease_turn)"
                " VALUES (%s, %s, %s,"
                "   nextval('adcp_reporting_configuration_lease_turn_seq'))"
                " ON CONFLICT (account_id, delivery_config_id, delivery_config_version)"
                " DO UPDATE SET lease_turn ="
                "   nextval('adcp_reporting_configuration_lease_turn_seq')",
                (row[0], row[1], row[2]),
            )
    if row is None:
        return None
    return LeasedConfiguration(
        account_id=row[0],
        delivery_config_id=row[1],
        delivery_config_version=row[2],
        lease_expires_at=expires,
    )
