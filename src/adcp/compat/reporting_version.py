"""Release gates for reporting controls added in AdCP 3.2 beta.6."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from adcp._version import is_adcp_version_at_least
from adcp.exceptions import ADCPFeatureUnsupportedError

_BETA6_REPORTING_DIMENSIONS = frozenset({"catalog_item", "creative", "keyword", "format"})
_BETA6_SORT_METRICS = frozenset(
    {
        "commissionable_value",
        "plays",
        "cost_per_completed_view",
        "cpm",
        "downloads",
        "units_sold",
        "new_to_brand_units",
        "viewable_rate",
        "viewable_impressions",
        "measurable_impressions",
        "viewed_seconds",
        "quartile_25",
        "quartile_50",
        "quartile_75",
        "quartile_100",
    }
)
_BETA6_AVAILABLE_METRICS = frozenset(
    {
        "measurable_impressions",
        "quartile_25",
        "quartile_50",
        "quartile_75",
        "quartile_100",
        "time_based_views",
        "viewable_impressions",
        "viewable_rate",
        "viewed_seconds",
    }
)
_BETA6_REPORTING_TASKS = frozenset(
    {
        "get_products",
        "create_media_buy",
        "update_media_buy",
        "provide_performance_feedback",
        "list_products",
        "request_proposals",
        "refine_proposals",
        "buy_products",
        "accept_proposal",
        "control_media_buy",
    }
)


@dataclass(frozen=True)
class Beta6ReportingIssue:
    field: str
    detail: str


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def beta6_delivery_request_issue(params: Any) -> Beta6ReportingIssue | None:
    """Return the first beta.6-only delivery request feature in ``params``."""
    payload = _record(params)
    if "requested_metrics" in payload:
        return Beta6ReportingIssue("requested_metrics", "requested delivery metrics")

    for dimension, value in _record(payload.get("reporting_dimensions")).items():
        if dimension in _BETA6_REPORTING_DIMENSIONS:
            return Beta6ReportingIssue(
                f"reporting_dimensions.{dimension}",
                f"the {dimension} delivery breakdown",
            )
        settings = _record(value)
        if "sort_direction" in settings:
            return Beta6ReportingIssue(
                f"reporting_dimensions.{dimension}.sort_direction",
                "delivery sort direction",
            )
        if settings.get("sort_by") in _BETA6_SORT_METRICS:
            metric = settings["sort_by"]
            return Beta6ReportingIssue(
                f"reporting_dimensions.{dimension}.sort_by",
                f"delivery sort metric {metric}",
            )
    return None


def _beta6_metric_issue(value: Any, path: str = "") -> Beta6ReportingIssue | None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            if issue := _beta6_metric_issue(item, f"{path}[{index}]"):
                return issue
        return None
    if not isinstance(value, dict):
        return None

    if value.get("scope") == "standard" and value.get("metric_id") in _BETA6_AVAILABLE_METRICS:
        metric = value["metric_id"]
        return Beta6ReportingIssue(
            f"{path}.metric_id" if path else "metric_id",
            f"the beta.6 metric {metric}",
        )
    if value.get("scope") == "vendor" and "qualifier" in value:
        return Beta6ReportingIssue(
            f"{path}.qualifier" if path else "qualifier",
            "the beta.6 vendor metric qualifier",
        )

    for key, child in value.items():
        field = f"{path}.{key}" if path else key
        if key in {"required_metrics", "requested_metrics"} and isinstance(child, list):
            metric = next(
                (
                    item
                    for item in child
                    if isinstance(item, str) and item in _BETA6_AVAILABLE_METRICS
                ),
                None,
            )
            if metric is not None:
                return Beta6ReportingIssue(field, f"the beta.6 metric {metric}")
        if key == "committed_metrics" and isinstance(child, list):
            for index, item in enumerate(child):
                metric = _record(item)
                if (
                    metric.get("scope") == "standard"
                    and metric.get("metric_id") in _BETA6_AVAILABLE_METRICS
                ):
                    return Beta6ReportingIssue(
                        f"{field}[{index}].metric_id",
                        f"the beta.6 metric {metric['metric_id']}",
                    )
        if issue := _beta6_metric_issue(child, field):
            return issue
    return None


def beta6_reporting_request_issue(tool_name: str, params: Any) -> Beta6ReportingIssue | None:
    """Return the first beta.6-only reporting feature used by a request."""
    if tool_name == "get_media_buy_delivery":
        return beta6_delivery_request_issue(params)
    if tool_name in _BETA6_REPORTING_TASKS:
        return _beta6_metric_issue(params)
    return None


def assert_reporting_request_supported(tool_name: str, params: Any) -> None:
    """Fail before transport when the target predates a used reporting feature."""
    payload = _record(params)
    target_version = payload.get("adcp_version")
    issue = beta6_reporting_request_issue(tool_name, payload)
    if (
        issue is None
        or not isinstance(target_version, str)
        or is_adcp_version_at_least(target_version, "3.2.0-beta.6")
    ):
        return
    raise ADCPFeatureUnsupportedError(
        [
            f"{issue.detail} at {issue.field} requires AdCP 3.2-beta.6 or newer "
            f"(target is {target_version}); no request was sent"
        ]
    )
