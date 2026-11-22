"""Wire fixtures shared by reporting tests across installed SDK versions."""

from typing import Any

REPORTING_OFFERING: dict[str, Any] = {
    "offering_id": "HOURLY_SNAPSHOT_V1",
    "feed_purpose": "analytics",
    "report_definition_id": "daily_delivery_v1",
    "report_definition_uri": "https://contracts.example.test/reporting/daily-delivery-v1",
    "report_definition_sha256": "a" * 64,
    "reporting_profile": {
        "id": "paid_media_delivery",
        "version": "1.0.0",
        "schema_uri": "https://contracts.example.test/reporting/rows-v1.json",
        "schema_sha256": "b" * 64,
        "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
        "schema_ref_policy": "local_fragment_only",
        "grain": "one row per media buy per period",
        "primary_keys": ["media_buy_id"],
    },
    "schedule": {"period_duration": "PT1H", "delivery_sla": "PT1H", "alignment": "utc"},
    "supported_finality": ["snapshot"],
    "reconciliation_mode": "delivery_only",
}
