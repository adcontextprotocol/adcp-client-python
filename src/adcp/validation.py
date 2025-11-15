"""Runtime validation for constraints not enforced by JSON schemas.

This module provides runtime validation for mutual exclusivity and other constraints
that are documented in schema descriptions but not enforced through JSON Schema validation.

See SCHEMA_VALIDATION_GAPS.md for details on upstream schema issues.
"""

from __future__ import annotations

from typing import Any


class ValidationError(ValueError):
    """Raised when runtime validation fails."""

    pass


def validate_publisher_properties_item(item: dict[str, Any]) -> None:
    """Validate that publisher_properties items have exactly one of property_ids or property_tags.

    Args:
        item: A single item from publisher_properties array

    Raises:
        ValidationError: If both or neither of property_ids/property_tags are present
    """
    has_property_ids = "property_ids" in item and item["property_ids"] is not None
    has_property_tags = "property_tags" in item and item["property_tags"] is not None

    if has_property_ids and has_property_tags:
        raise ValidationError(
            "publisher_properties item cannot have both property_ids and property_tags. "
            "These fields are mutually exclusive."
        )

    if not has_property_ids and not has_property_tags:
        raise ValidationError(
            "publisher_properties item must have either property_ids or property_tags. "
            "At least one is required."
        )


def validate_agent_authorization(agent: dict[str, Any]) -> None:
    """Validate that agent authorization has exactly one of: properties, property_ids, property_tags, or publisher_properties.

    Args:
        agent: An agent dict from adagents.json

    Raises:
        ValidationError: If multiple or no authorization fields are present
    """
    auth_fields = ["properties", "property_ids", "property_tags", "publisher_properties"]
    present_fields = [
        field
        for field in auth_fields
        if field in agent and agent[field] is not None
    ]

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


def validate_adagents(adagents: dict[str, Any]) -> None:
    """Validate an adagents.json structure.

    Args:
        adagents: The adagents.json dict

    Raises:
        ValidationError: If validation fails
    """
    if "agents" in adagents:
        for agent in adagents["agents"]:
            validate_agent_authorization(agent)
