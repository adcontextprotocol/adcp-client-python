"""A reporting subscription's destination ``path_template`` on managed delivery bindings.

The template is frozen on the binding and expanded per delivery; account-level
bindings never carry one, so their fingerprints and payloads are unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from adcp.reporting.ledger import ReportingConfigurationGenerationKey
from adcp.reporting.ledger.delivery_models import ReportingDestinationBinding
from adcp.reporting.ledger.models import ledger_asdict
from adcp.reporting.materializer.contracts import binding_fingerprint

START = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _binding(offering_id: str | None, path_template: str | None = None):
    key = ReportingConfigurationGenerationKey(
        "acct/1", "https://agency.example/agent", "analytics", 1, offering_id
    )
    return ReportingDestinationBinding(
        key,
        "https://agency.example/agent",
        "destination",
        "trusted-reference-binding",
        "file_transfer",
        "gcs",
        "manifest_checksums",
        "delivery_only",
        "analytics",
        400,
        START,
        "jsonl",
        path_template=path_template,
    )


def test_the_path_template_expands_one_segment_per_value() -> None:
    binding = _binding("pkg", "analytics/{account_id}/{offering_id}/{period_start}")
    assert (
        binding.delivery_path(datetime(2026, 11, 1, 4, tzinfo=timezone.utc))
        == "analytics/acct%2F1/pkg/2026-11-01T04:00:00Z"
    )
    assert _binding("pkg").delivery_path(START) is None


@pytest.mark.parametrize(
    "template", ["", "/absolute/{account_id}", "a/../{account_id}", "a//b", "x/{bogus}", "x/{a"]
)
def test_unsafe_templates_are_refused(template: str) -> None:
    with pytest.raises(ValueError):
        _binding("pkg", template)


def test_only_a_subscription_binding_carries_a_path_template() -> None:
    with pytest.raises(ValueError):
        _binding(None, "analytics/{account_id}")


def test_account_bindings_keep_their_fingerprint_and_payload() -> None:
    account = _binding(None)
    payload = ledger_asdict(account)
    assert "path_template" not in payload
    assert "offering_id" not in payload["generation_key"]
    # Exactly the fields a binding serialized to before subscriptions, so
    # binding_fingerprint and stored payloads are byte-for-byte unchanged.
    assert binding_fingerprint(account)
    assert set(payload) == {
        "generation_key",
        "consumer_id",
        "destination_ref",
        "trusted_binding_ref",
        "method",
        "transport",
        "verification_profile",
        "reconciliation_mode",
        "feed_purpose",
        "resource_retention_days",
        "created_at",
        "format",
        "reader_compatibility",
        "success_status",
        "kind",
    }
    subscription = ledger_asdict(_binding("pkg", "analytics/{account_id}"))
    assert subscription["path_template"] == "analytics/{account_id}"
