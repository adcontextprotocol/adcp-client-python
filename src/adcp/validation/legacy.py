"""Runtime validation for AdCP data structures.

This module provides runtime validation that complements schema validation:

1. **For adagents.json (v2.4.0+)**: Validates discriminated union structure
   - Checks for proper authorization_type discriminator
   - Validates publisher_properties selection_type discriminator
   - These constraints ARE enforced in upstream schemas via oneOf + discriminators

2. **For product.json**: Validates mutual exclusivity constraints
   - publisher_properties must have either property_ids OR property_tags
   - These constraints are NOT yet enforced in upstream schemas (pending fix)

Note: When using Pydantic models directly, discriminated union validation happens
automatically during model construction. This module is for validating raw dict data
before Pydantic parsing (e.g., in fetch_adagents()).
"""

from __future__ import annotations

from typing import Any


class ValidationError(ValueError):
    """Raised when runtime validation fails."""

    pass


def validate_publisher_properties_item(item: dict[str, Any]) -> None:
    """Validate a single ``publisher_properties[]`` entry.

    Two XORs are enforced per the publisher-property-selector JSON Schema
    (adcp#4504):

    * Selector XOR: exactly one of ``property_ids`` / ``property_tags``
      is present for ``by_id`` / ``by_tag`` (``all`` requires neither).
    * Publisher XOR: exactly one of ``publisher_domain`` (singular) or
      ``publisher_domains`` (compact array) is present — both or neither
      both fail. ``publisher_domains`` is NOT allowed on
      ``selection_type='by_id'`` since property IDs are publisher-scoped;
      callers wanting per-publisher ID sets must use one entry per
      publisher.

    Args:
        item: A single item from publisher_properties array

    Raises:
        ValidationError: If discriminator or field constraints are violated
    """
    selection_type = item.get("selection_type")
    has_property_ids = "property_ids" in item and item["property_ids"] is not None
    has_property_tags = "property_tags" in item and item["property_tags"] is not None
    has_publisher_domain = "publisher_domain" in item and item["publisher_domain"] is not None
    publisher_domains = item.get("publisher_domains")
    has_publisher_domains = publisher_domains is not None

    if selection_type:
        if selection_type == "by_id" and not has_property_ids:
            raise ValidationError(
                "publisher_properties item with selection_type='by_id' must have property_ids"
            )
        elif selection_type == "by_tag" and not has_property_tags:
            raise ValidationError(
                "publisher_properties item with selection_type='by_tag' must have property_tags"
            )
        elif selection_type not in ("all", "by_id", "by_tag"):
            raise ValidationError(
                f"publisher_properties item has invalid selection_type: {selection_type}"
            )

    if has_property_ids and has_property_tags:
        raise ValidationError(
            "publisher_properties item cannot have both property_ids and property_tags. "
            "These fields are mutually exclusive."
        )

    # selection_type='all' carries neither selector array; older callers
    # without the discriminator must still provide one of the two.
    if selection_type not in ("all",) and not has_property_ids and not has_property_tags:
        raise ValidationError(
            "publisher_properties item must have either property_ids or property_tags. "
            "At least one is required."
        )

    if has_publisher_domain and has_publisher_domains:
        raise ValidationError(
            "publisher_properties item cannot have both publisher_domain and "
            "publisher_domains. These fields are mutually exclusive (XOR)."
        )

    if not has_publisher_domain and not has_publisher_domains:
        raise ValidationError(
            "publisher_properties item must have exactly one of publisher_domain "
            "or publisher_domains."
        )

    if has_publisher_domains and selection_type == "by_id":
        # by_id is single-publisher only — property IDs are publisher-scoped,
        # so fanning the same ID set across multiple publishers is meaningless.
        raise ValidationError(
            "publisher_properties item with selection_type='by_id' cannot use "
            "publisher_domains[]; property IDs are publisher-scoped. Use one "
            "entry per publisher with publisher_domain."
        )

    if has_publisher_domains:
        if not isinstance(publisher_domains, list) or len(publisher_domains) == 0:
            raise ValidationError(
                "publisher_properties item publisher_domains must be a non-empty array"
            )
        if any(not isinstance(d, str) or not d for d in publisher_domains):
            raise ValidationError(
                "publisher_properties item publisher_domains entries must be non-empty strings"
            )
        if len(set(publisher_domains)) != len(publisher_domains):
            raise ValidationError(
                "publisher_properties item publisher_domains entries must be unique"
            )


def validate_agent_authorization(agent: dict[str, Any]) -> None:
    """Validate agent authorization discriminated union.

    AdCP v2.4.0+ uses discriminated unions with authorization_type discriminator:
    - authorization_type: "property_ids" requires property_ids
    - authorization_type: "property_tags" requires property_tags
    - authorization_type: "inline_properties" requires properties
    - authorization_type: "publisher_properties" requires publisher_properties

    For backward compatibility, also validates the old mutual exclusivity constraint.

    Args:
        agent: An agent dict from adagents.json

    Raises:
        ValidationError: If discriminator or field constraints are violated
    """
    authorization_type = agent.get("authorization_type")
    auth_fields = ["properties", "property_ids", "property_tags", "publisher_properties"]
    present_fields = [field for field in auth_fields if field in agent and agent[field] is not None]

    # If authorization_type discriminator is present, validate discriminated union
    if authorization_type:
        if authorization_type == "property_ids" and "property_ids" not in present_fields:
            raise ValidationError(
                "Agent with authorization_type='property_ids' must have property_ids"
            )
        elif authorization_type == "property_tags" and "property_tags" not in present_fields:
            raise ValidationError(
                "Agent with authorization_type='property_tags' must have property_tags"
            )
        elif authorization_type == "inline_properties" and "properties" not in present_fields:
            raise ValidationError(
                "Agent with authorization_type='inline_properties' must have properties"
            )
        elif (
            authorization_type == "publisher_properties"
            and "publisher_properties" not in present_fields
        ):
            raise ValidationError(
                "Agent with authorization_type='publisher_properties' "
                "must have publisher_properties"
            )
        elif authorization_type not in (
            "property_ids",
            "property_tags",
            "inline_properties",
            "publisher_properties",
        ):
            raise ValidationError(f"Agent has invalid authorization_type: {authorization_type}")

    # Validate mutual exclusivity (for both old and new formats)
    if len(present_fields) > 1:
        raise ValidationError(
            f"Agent authorization cannot have multiple fields: {', '.join(present_fields)}. "
            f"Only one of {', '.join(auth_fields)} is allowed."
        )

    if len(present_fields) == 0:
        raise ValidationError(
            f"Agent authorization must have exactly one of: {', '.join(auth_fields)}."
        )

    # If using publisher_properties, validate each item
    if "publisher_properties" in present_fields:
        for pub_prop in agent["publisher_properties"]:
            validate_publisher_properties_item(pub_prop)


def validate_product(product: dict[str, Any]) -> None:
    """Validate a Product object.

    Args:
        product: Product dict

    Raises:
        ValidationError: If validation fails
    """
    if "publisher_properties" in product and product["publisher_properties"]:
        for item in product["publisher_properties"]:
            validate_publisher_properties_item(item)


# Must stay in sync with the `Reason` enum on RevokedPublisherDomain in
# the generated adagents schema. A drift test in
# tests/test_adagents.py compares this frozenset to the generated enum.
_REVOCATION_REASONS = frozenset(
    {"relationship_ended", "compliance_violation", "publisher_request", "other"}
)


def validate_revoked_publisher_domain_entry(entry: dict[str, Any]) -> None:
    """Validate a single ``revoked_publisher_domains[]`` entry.

    Required fields: ``publisher_domain`` (non-empty string) and
    ``revoked_at`` (RFC 3339 / ISO 8601 string). Optional ``reason``
    must be one of the four enum values when present. Wall-clock parsing
    of ``revoked_at`` is left to Pydantic (``AwareDatetime`` in the
    generated model) — this helper only enforces shape on raw dicts.
    """
    publisher_domain = entry.get("publisher_domain")
    if not isinstance(publisher_domain, str) or not publisher_domain:
        raise ValidationError(
            "revoked_publisher_domains entry must have a non-empty " "'publisher_domain' string"
        )

    revoked_at = entry.get("revoked_at")
    if not isinstance(revoked_at, str) or not revoked_at:
        raise ValidationError(
            "revoked_publisher_domains entry must have a non-empty "
            "'revoked_at' ISO 8601 timestamp string"
        )

    reason = entry.get("reason")
    if reason is not None and reason not in _REVOCATION_REASONS:
        raise ValidationError(
            f"revoked_publisher_domains entry has invalid reason={reason!r} "
            f"(expected one of: {', '.join(sorted(_REVOCATION_REASONS))})"
        )


def validate_adagents(adagents: dict[str, Any]) -> None:
    """Validate an adagents.json structure.

    Args:
        adagents: The adagents.json dict

    Raises:
        ValidationError: If validation fails
    """
    authorized_agents = adagents.get("authorized_agents")
    if isinstance(authorized_agents, list):
        for agent in authorized_agents:
            if isinstance(agent, dict):
                validate_agent_authorization(agent)

    revoked = adagents.get("revoked_publisher_domains")
    if revoked is not None:
        if not isinstance(revoked, list):
            raise ValidationError("'revoked_publisher_domains' must be an array")
        for entry in revoked:
            if not isinstance(entry, dict):
                raise ValidationError("revoked_publisher_domains entry must be an object")
            validate_revoked_publisher_domain_entry(entry)
