"""The rc.3 Reliable Reporting aliases must bind by *value*, not by class name.

``datamodel-code-generator`` names anonymous nested schemas by traversal order,
so ``ConsumerStatus``, ``MismatchCode``, ``IssueState``, ``OperationsContact``,
``AuthoritativeParty``, and ``ObligationCounts`` are all names it could hand to
a different schema after an unrelated sibling is added. Asserting on the class
name would then pass while the alias silently pointed somewhere else.

Every assertion here is therefore about the *members or fields* the rc.3
contract fixes, so a reshuffle fails loudly at the alias layer instead of
shipping a wrong type to adopters.
"""

from __future__ import annotations

from adcp.types import (
    ReportingAuthoritativeParty,
    ReportingConsumerFailureCode,
    ReportingConsumerMismatchCode,
    ReportingConsumerStatusValue,
    ReportingIssueRecommendedAction,
    ReportingIssueState,
    ReportingObligationCounts,
    ReportingOperationsContact,
)


def test_consumer_status_alias_carries_the_rc3_content_mismatch_value() -> None:
    """rc.3 adds a fifth consumer status; the other four are unchanged."""
    assert {member.value for member in ReportingConsumerStatusValue} == {
        "received",
        "obligation_missing",
        "revision_missing",
        "unreadable",
        "content_mismatch",
    }


def test_mismatch_code_alias_is_the_closed_rc3_reason_set() -> None:
    """The set is closed: agents dispatch on these values, never on prose."""
    assert {member.value for member in ReportingConsumerMismatchCode} == {
        "scope_media_buy_missing",
        "coverage_short",
        "metric_missing",
        "schema_nonconformant",
        "currency_mismatch",
        "period_mismatch",
    }


def test_failure_code_alias_stays_distinct_from_mismatch_code() -> None:
    """``unreadable`` and ``content_mismatch`` use different closed vocabularies.

    A revision that could not be read at all is a ``failure_code``; one that was
    read but contradicts the accepted generation is a ``mismatch_code``. The
    schema forbids carrying both, so conflating the two enums would let a
    statement be built that no seller can accept.
    """
    assert {member.value for member in ReportingConsumerFailureCode} == {
        "access_denied",
        "resource_not_found",
        "integrity_mismatch",
        "reader_incompatible",
        "transport_failed",
    }
    failure = {member.value for member in ReportingConsumerFailureCode}
    mismatch = {member.value for member in ReportingConsumerMismatchCode}
    assert failure.isdisjoint(mismatch)


def test_issue_state_alias_is_the_forward_only_lifecycle() -> None:
    assert {member.value for member in ReportingIssueState} == {
        "open",
        "acknowledged",
        "resolved",
        "waived",
    }


def test_recommended_action_alias_exposes_the_contact_escalation_family() -> None:
    """Escalation past ``opened_at`` + window must pick a ``contact_*`` value."""
    values = {member.value for member in ReportingIssueRecommendedAction}
    assert {"contact_buyer", "contact_seller", "contact_provider"} <= values
    assert "wait_for_retry" in values


def test_authoritative_party_alias_reserves_consumer_without_accepting_it() -> None:
    """The value exists in the schema; 3.2 sellers still reject it."""
    assert {member.value for member in ReportingAuthoritativeParty} == {"seller", "consumer"}


def test_operations_contact_alias_is_the_two_field_escalation_path() -> None:
    assert set(ReportingOperationsContact.model_fields) == {"url", "email"}


def test_obligation_counts_alias_carries_consumer_status_pending() -> None:
    """The five health counts partition; ``consumer_status_pending`` overlaps them."""
    fields = set(ReportingObligationCounts.model_fields)
    assert {"total", "waiting", "healthy", "delayed", "action_required", "complete"} <= fields
    assert "consumer_status_pending" in fields
    # Visibility only: it must be optional, because a seller that does not
    # advertise consumer_status_task never emits it.
    assert ReportingObligationCounts.model_fields["consumer_status_pending"].is_required() is False
