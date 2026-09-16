"""Copied outside the checkout and executed with Python 3.10 -I against a wheel."""

import asyncio
import hashlib
import importlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path


async def main():
    config = json.load(sys.stdin)
    assert sys.version_info[:2] == (3, 10)
    assert importlib.util.find_spec("psycopg") is None
    assert importlib.util.find_spec("psycopg_pool") is None

    assert "adcp.reporting.materializer" not in sys.modules
    for name in (
        "adcp.reporting.materializer",
        "adcp.reporting.revision_selection",
        "adcp.reporting.ledger",
        "adcp.reporting.outbox",
    ):
        module = importlib.import_module(name)
        assert len(module.__all__) == len(set(module.__all__))
        for symbol in module.__all__:
            getattr(module, symbol)
    from adcp.reporting.ledger import (
        InMemoryReportingReconciliationStore,
        ReportingConfiguration,
        ReportingDeliveryScope,
        ReportingObligationRecord,
        ReportingRevisionRecord,
        ReportingScheduleSpec,
        derive_period,
        revision_content_sha256,
    )
    from adcp.reporting.materializer import (
        ReportingDestinationBinding,
        ReportingMaterializationAttempt,
        ReportingObligationDeliveryRecord,
        reference_digest,
        reference_verifier,
    )

    example_path = Path(config["example"])
    spec = importlib.util.spec_from_file_location("installed_example", example_path)
    example = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = example
    spec.loader.exec_module(example)
    verifier = reference_verifier()
    actual = {
        name: hashlib.sha256(
            files("adcp.reporting.materializer").joinpath("assets", name).read_bytes()
        ).hexdigest()
        for name in config["assets"]
    }
    assert actual == config["assets"]
    assert (
        files("adcp.reporting.ledger").joinpath("reporting_status_selector_version.sql").is_file()
    )
    assert files("adcp.reporting.outbox").joinpath("required_status_selector_schema.json").is_file()
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    schedule = ReportingScheduleSpec("PT1H", "PT1H", period_anchor=start)
    period = derive_period(schedule, account_timezone="UTC", ordinal=0)
    for count in (0, 501):
        ledger = InMemoryReportingReconciliationStore()
        configuration = ReportingConfiguration(
            delivery_config_id="daily",
            delivery_config_version=1,
            account_id="account",
            report_definition_id=verifier.key.report_definition_id,
            reporting_profile=verifier.key.reporting_profile,
            feed_purpose="analytics",
            required_finality="snapshot",
            schedule=schedule,
            activated_at=start,
            definition=verifier.key.definition,
        )
        await ledger.put_configuration(configuration)
        obligation = ReportingObligationRecord(
            reporting_obligation_id="obligation",
            account_id="account",
            delivery_config_id="daily",
            delivery_config_version=1,
            report_definition_id=verifier.key.report_definition_id,
            reporting_profile=verifier.key.reporting_profile,
            feed_purpose="analytics",
            period=period,
            scope_resolved_at=period.end,
            media_buy_ids=("buy",),
            required_finality="snapshot",
            automated_recovery_deadline_at=period.expected_at + timedelta(hours=1),
            schedule=schedule,
            definition=verifier.key.definition,
            created_at=period.end,
            currency="USD",
        )
        await ledger.commit_obligation(obligation)
        binding = ReportingDestinationBinding(
            configuration.generation_key,
            "https://buyer.example.test/agents/reporting",
            "reference-destination",
            "trusted-binding",
            "file_transfer",
            "reference-memory",
            "canonical_digest",
            "delivery_only",
            "analytics",
            400,
            start,
            "jsonl",
            ("reference-v1",),
        )
        await ledger.put_destination_binding(binding)
        scope = ReportingDeliveryScope(
            configuration.generation_key, binding.consumer_id, obligation.reporting_obligation_id
        )
        delivery = ReportingObligationDeliveryRecord(
            scope, "USD", period.end + timedelta(days=400), period.end
        )
        await ledger.bind_obligation_delivery(delivery)
        rows = [
            {"row_id": f"{i:06d}", "impressions": i, "spend": "1.25", "currency": "USD"}
            for i in range(count)
        ]
        _, totals = verifier.canonicalize(rows)
        pairs = tuple((t.name, t.value) for t in totals)
        revision = ReportingRevisionRecord(
            "revision",
            "account",
            obligation.reporting_obligation_id,
            "snapshot",
            revision_content_sha256(
                reporting_revision_id="revision",
                row_count=count,
                control_totals=pairs,
                reporting_rows=rows,
                control_total_evidence=totals,
            ),
            count,
            pairs,
            period.end,
            period.end,
            period.end,
            canonical_content_digest=reference_digest(verifier, rows),
            managed_control_totals=totals,
        )
        await ledger.commit_revision(revision, rows)
        attempt = ReportingMaterializationAttempt(
            scope, revision.reporting_revision_id, "materialization", 1, period.end
        )
        await ledger.commit_materialization_attempt(attempt)
        destination = example.development_destination(binding)
        results = []
        for _ in range(2):
            results.append(
                await destination.verify_revision(
                    reader=ledger,
                    binding=binding,
                    delivery=delivery,
                    obligation=obligation,
                    revisions=(revision,),
                    attempt=attempt,
                    deadline_at=datetime.now(timezone.utc) + timedelta(seconds=60),
                    cancel=asyncio.Event(),
                )
            )
        assert all(result.verification.row_count == count for result in results)
        assert results[0].request.external_id == results[1].request.external_id
        assert destination.writer.write_effects == 1 and not destination.writer.production_eligible
        assert destination.writer.open_count == destination.writer.close_count == 4
    workspace = Path(config["workspace"]).resolve()
    assert all(not Path(path).resolve().is_relative_to(workspace) for path in sys.path)
    assert all(
        not Path(module.__file__).resolve().is_relative_to(workspace)
        for name, module in sys.modules.items()
        if name == "adcp" or name.startswith("adcp.")
        if getattr(module, "__file__", None)
    )
    assert not any(name.startswith("psycopg") for name in sys.modules)
    print(json.dumps({"python": "3.10", "rows": [0, 501], "installed": True, "assets": actual}))


asyncio.run(main())
