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
    """Validate publisher_properties item discriminated union.

    AdCP v2.4.0+ uses discriminated unions with selection_type discriminator:
    - selection_type: "by_id" requires property_ids
    - selection_type: "by_tag" requires property_tags

    For backward compatibility, also validates the old mutual exclusivity constraint.

    Args:
        item: A single item from publisher_properties array

    Raises:
        ValidationError: If discriminator or field constraints are violated
    """
    selection_type = item.get("selection_type")
    has_property_ids = "property_ids" in item and item["property_ids"] is not None
    has_property_tags = "property_tags" in item and item["property_tags"] is not None

    # If selection_type discriminator is present, validate discriminated union
    if selection_type:
        if selection_type == "by_id" and not has_property_ids:
            raise ValidationError(
                "publisher_properties item with selection_type='by_id' must have property_ids"
            )
        elif selection_type == "by_tag" and not has_property_tags:
            raise ValidationError(
                "publisher_properties item with selection_type='by_tag' must have property_tags"
            )
        elif selection_type not in ("by_id", "by_tag"):
            raise ValidationError(
                f"publisher_properties item has invalid selection_type: {selection_type}"
            )

    # Validate mutual exclusivity (for both old and new formats)
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
