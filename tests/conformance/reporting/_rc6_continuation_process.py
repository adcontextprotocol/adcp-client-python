"""Actual installed B2.4 rc.6 snapshot and current rc.7 refusal in separate processes."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace


def verify_installation(settings):
    assert list(sys.version_info[:2]) == [3, 10]
    workspace = Path(settings["workspace"])
    assert not any(Path(p).resolve().is_relative_to(workspace) for p in sys.path)
    origins = {}
    for name, expected in settings["modules"].items():
        path = Path(importlib.import_module(name).__file__).resolve()
        assert path.is_relative_to(Path(sys.prefix)) and not path.is_relative_to(workspace)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
        origins[name] = str(path)
    sys.path.insert(0, settings["fixtures"])
    return origins


async def fresh_read_after_legacy_refusals(mount, account_id, positions):
    """Use one initialized MCP session for legacy refusals and the fresh read."""
    from tests.conformance.reporting._receipt_transport import error_code

    async with mount.client() as client:
        for position in positions:
            request = {
                "adcp_version": "3.2-rc.6",
                "account": {"account_id": account_id},
                "view": "periods",
                **position,
            }
            for call in (mount.mcp, mount.a2a):
                _, rejected = await call(client, request)
                assert error_code(rejected) == "VERSION_UNSUPPORTED", rejected
        _, current = await mount.mcp(
            client,
            {
                "adcp_version": "3.2-rc.7",
                "account": {"account_id": account_id},
                "view": "periods",
            },
        )
    return current


async def main(settings):
    origins = verify_installation(settings)
    from psycopg_pool import AsyncConnectionPool

    from adcp.reporting.canonical_json import canonical_json_utf8_v1
    from adcp.reporting.feed import pg as feed_pg
    from adcp.reporting.ledger import ProducerOfferings, ReportingProducer
    from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
    from adcp.reporting.projection.pg import PgReportingProjectionStore, PgReportingStatusProjection
    from adcp.types import GetReportingStatusRequest
    from adcp.validation.schema_loader import get_named_validator, get_validator
    from tests.conformance.reporting._feed_support import MountedFeed
    from tests.conformance.reporting._generation_support import (
        START,
        UncalledSource,
        configuration,
        revision_for,
    )
    from tests.conformance.reporting._reliable_support import ManualClock

    consumer = "https://buyer.example.test/installed-rc6"
    clock = ManualClock(START + timedelta(hours=1.5 if settings["phase"] == "seed" else 8.5))
    config = replace(configuration(), deactivated_at=None)
    caller = ReportingDeliveryPrincipal(config.account_id, consumer)

    async def captured_now(connection):
        return (await (await connection.execute("SELECT %s::timestamptz", (clock(),))).fetchone())[
            0
        ]

    feed_pg._now = captured_now  # deterministic clock only; real locked PG capture
    async with AsyncConnectionPool(
        settings["conninfo"], kwargs=settings["kwargs"], min_size=1, max_size=3, open=False
    ) as pool:
        await pool.wait(timeout=10)
        store = PgReportingProjectionStore(
            pool=pool, clock=clock, notifications=settings["notifications"]
        )
        h = SimpleNamespace(store=store, clock=clock, pool=pool)
        if settings["phase"] == "seed":
            await store.create_schema()
            await store.put_configuration(config)
            producer = ReportingProducer(
                source=UncalledSource(), offerings=ProducerOfferings(), store=store
            )
            obligations = await producer.close_elapsed_periods(config, now=clock())
            assert len(obligations) == 1
            assert obligations[0].period.expected_at == START + timedelta(hours=2)
            revision, rows = revision_for(obligations[0])
            await store.commit_revision(revision, rows)
            projection = PgReportingStatusProjection(store, revision_ownership=True)
            await projection.activate(account_id=config.account_id)
        else:
            # Real new write and time change; old pages must keep their capture.
            await store.put_configuration(replace(config, deactivated_at=START))

        def mounted(version):
            result = MountedFeed(h, hydrated=True, registry_kind="oauth", version=version)
            result.authorize(
                SimpleNamespace(obligation=config, binding=SimpleNamespace(consumer_id=consumer))
            )
            return result

        prior = settings.get("prior")
        if prior is not None:
            from adcp.exceptions import ConfigurationError
            from adcp.reporting.feed.errors import ReportingFeedError

            first = prior["pages"][0]
            snapshot = await store.read_reporting_feed_snapshot(
                first["ledger_snapshot_id"], caller=caller
            )
            document = canonical_json_utf8_v1(snapshot.to_storage())
            assert hashlib.sha256(document).hexdigest() == prior["snapshot_sha256"]
            assert json.loads(snapshot.filters_json)["adcp_version"] == "3.2-rc.6"
            try:
                mounted("3.2-rc.6")
            except ConfigurationError:
                pass
            else:
                raise AssertionError("current runtime mounted an unsupported rc.6 pin")
            new_mount = mounted("3.2-rc.7")
            version_boundary = None
            positions = (
                {"pagination": {"max_results": 1, "cursor": first["pagination"]["cursor"]}},
                {"changes_after": prior["checkpoint"]},
            )
            current = await fresh_read_after_legacy_refusals(
                new_mount, config.account_id, positions
            )
            assert current["health"] == "complete"
            assert current["ledger_snapshot_id"] != first["ledger_snapshot_id"]
            get_named_validator(
                "media-buy/get-reporting-status-response.json", version="3.2-rc.7"
            ).validate(current)
            for position in positions:
                # An authenticated position still reports the captured version
                # boundary when presented to a supported rc.7 read.
                request = {
                    "adcp_version": "3.2-rc.7",
                    "account": {"account_id": config.account_id},
                    "view": "periods",
                    **position,
                }
                try:
                    await store.read_reporting_feed(request, caller=caller)
                except ReportingFeedError as exc:
                    assert exc.code == "REPORTING_FEED_VERSION_MISMATCH"
                    version_boundary = exc.code
                else:
                    raise AssertionError("cross-version stored position was accepted")
            preserved = await store.read_reporting_feed_snapshot(
                first["ledger_snapshot_id"], caller=caller
            )
            assert canonical_json_utf8_v1(preserved.to_storage()) == document
            async with pool.connection() as connection:
                row = await (
                    await connection.execute(
                        "SELECT document,content_sha256 FROM reporting_projection_feed_snapshots"
                        " WHERE account_id=%s AND consumer_id=%s AND snapshot_id=%s",
                        (caller.account_id, caller.consumer_id, first["ledger_snapshot_id"]),
                    )
                ).fetchone()
            assert row is not None and row[0].encode("utf-8") == document
            assert row[1] == prior["snapshot_sha256"]
            return {
                "phase": settings["phase"],
                "python": sys.version,
                "modules": origins,
                "artifact": settings["artifact"],
                "pages": prior["pages"],
                "pages_sha256": prior["pages_sha256"],
                "snapshot_sha256": prior["snapshot_sha256"],
                "snapshot_bytes": len(document),
                "version_boundary": version_boundary,
                "checkpoint": prior["checkpoint"],
                "fresh_version": new_mount.version,
                "fresh_snapshot_id": current["ledger_snapshot_id"],
            }

        old_mount = mounted("3.2-rc.6")
        req = {
            "adcp_version": "3.2-rc.6",
            "account": {"account_id": config.account_id},
            "view": "periods",
            "pagination": {"max_results": 1},
        }
        pages = []
        async with old_mount.sdk_clients("1.0") as (clients, observed):
            # Replay each page over the same public transport: protobuf Struct
            # can preserve a JSON integer as an equivalent float. The stored
            # snapshot byte check below is independent of transport spelling.
            protocol = "mcp"
            for _ in range(20):
                result = await clients[protocol].get_reporting_status(
                    GetReportingStatusRequest.model_validate(req)
                )
                assert result.success, result
                page = result.data.model_dump(mode="json", exclude_unset=True)
                get_validator("get_reporting_status", "sync", version="3.2-rc.6").validate(page)
                pages.append(page)
                if not page["pagination"]["has_more"]:
                    break
                req["pagination"]["cursor"] = page["pagination"]["cursor"]
                protocol = "a2a" if protocol == "mcp" else "mcp"
            else:
                raise AssertionError("unbounded installed continuation")
            assert observed and all(p[2]["adcp_version"] == "3.2-rc.6" for p in observed)
        first = pages[0]
        assert first["health"] == "complete"
        assert "next_expected_at" not in first
        assert first["pagination"]["total_count"] == 2
        assert len(pages) == 2
        checkpoint = first["changes_checkpoint"]
        assert all(
            p["changes_checkpoint"] == checkpoint
            and p["ledger_as_of"] == first["ledger_as_of"]
            and p["ledger_snapshot_id"] == first["ledger_snapshot_id"]
            for p in pages
        )
        snapshot = await store.read_reporting_feed_snapshot(
            first["ledger_snapshot_id"], caller=caller
        )
        document = canonical_json_utf8_v1(snapshot.to_storage())
        assert json.loads(snapshot.filters_json)["adcp_version"] == "3.2-rc.6"
        async with pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT document,content_sha256 FROM reporting_projection_feed_snapshots"
                    " WHERE account_id=%s AND consumer_id=%s AND snapshot_id=%s",
                    (caller.account_id, caller.consumer_id, first["ledger_snapshot_id"]),
                )
            ).fetchone()
        assert row is not None and row[0].encode("utf-8") == document
        digest = hashlib.sha256(document).hexdigest()
        assert row[1] == digest
        raw_digest = hashlib.sha256(
            json.dumps(
                pages, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
        ).hexdigest()
        version_boundary = None
        return {
            "phase": settings["phase"],
            "python": sys.version,
            "modules": origins,
            "artifact": settings["artifact"],
            "pages": pages,
            "pages_sha256": raw_digest,
            "snapshot_sha256": digest,
            "snapshot_bytes": len(document),
            "version_boundary": version_boundary,
            "checkpoint": checkpoint,
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(json.load(sys.stdin))), sort_keys=True), flush=True)
