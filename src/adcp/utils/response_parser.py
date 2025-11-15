from __future__ import annotations

"""Utilities for parsing protocol responses into structured types."""

import json
import logging
from typing import Any, TypeVar, Union, cast, get_args, get_origin

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _validate_union_type(data: dict[str, Any], response_type: type[T]) -> T:
    """
    Validate data against a Union type by trying each variant.

    Args:
        data: Data to validate
        response_type: Union type to validate against

    Returns:
        Validated model instance

    Raises:
        ValidationError: If data doesn't match any Union variant
    """
    # Check if this is a Union type (handles both typing.Union and types.UnionType)
    origin = get_origin(response_type)

    # In Python 3.10+, X | Y creates a types.UnionType, not typing.Union
    # We need to check both the origin and the type itself
    is_union = origin is Union or str(type(response_type).__name__) == "UnionType"

    if is_union:
        # Get union args - works for both typing.Union and types.UnionType
        args = get_args(response_type)
        if not args:  # types.UnionType case
            # For types.UnionType, we need to access __args__ directly
            args = getattr(response_type, "__args__", ())

        errors = []
        for variant in args:
            try:
                return cast(T, variant.model_validate(data))
            except ValidationError as e:
                errors.append((variant.__name__, e))
                continue

        # If we get here, none of the variants worked
        error_msgs = [f"{name}: {str(e)}" for name, e in errors]
        # Raise a ValueError instead of ValidationError for better error messages
        raise ValueError(
            f"Data doesn't match any Union variant. "
            f"Attempted variants: {', '.join([e[0] for e in errors])}. "
            f"Errors: {'; '.join(error_msgs)}"
        )

    # Not a Union type, use regular validation
    # Cast is needed because response_type is typed as type[T] | Any
    return cast(T, response_type.model_validate(data))  # type: ignore[redundant-cast]


def parse_mcp_content(content: list[dict[str, Any]], response_type: type[T]) -> T:
    """
    Parse MCP content array into structured response type.

    MCP tools return content as a list of content items:
    [{"type": "text", "text": "..."}, {"type": "resource", ...}]

    The MCP adapter is responsible for serializing MCP SDK Pydantic objects
    to plain dicts before calling this function.

    For AdCP, we expect JSON data in text content items.

    Args:
        content: MCP content array (list of plain dicts)
        response_type: Expected Pydantic model type

    Returns:
        Parsed and validated response object

    Raises:
        ValueError: If content cannot be parsed into expected type
    """
    if not content:
        raise ValueError("Empty MCP content array")

    # Look for text content items that might contain JSON
    for item in content:
        if item.get("type") == "text":
            text = item.get("text", "")
            if not text:
                continue

            try:
                # Try parsing as JSON
                data = json.loads(text)
                # Validate against expected schema (handles Union types)
                return _validate_union_type(data, response_type)
            except json.JSONDecodeError:
                # Not JSON, try next item
                continue
            except ValidationError as e:
                logger.warning(
                    f"MCP content doesn't match expected schema {response_type.__name__}: {e}"
                )
                raise ValueError(f"MCP response doesn't match expected schema: {e}") from e
        elif item.get("type") == "resource":
            # Resource content might have structured data
            try:
                return _validate_union_type(item, response_type)
            except ValidationError:
                # Try next item
                continue

    # If we get here, no content item could be parsed
    # Include content preview for debugging (first 2 items, max 500 chars each)
    content_preview = json.dumps(content[:2], indent=2, default=str)
    if len(content_preview) > 500:
        content_preview = content_preview[:500] + "..."

    raise ValueError(
        f"No valid {response_type.__name__} data found in MCP content. "
        f"Content types: {[item.get('type') for item in content]}. "
        f"Content preview:\n{content_preview}"
    )


def parse_json_or_text(data: Any, response_type: type[T]) -> T:
    """
    Parse data that might be JSON string, dict, or other format.

    Used by A2A adapter for flexible response parsing.

    Args:
        data: Response data (string, dict, or other)
        response_type: Expected Pydantic model type

    Returns:
        Parsed and validated response object

    Raises:
        ValueError: If data cannot be parsed into expected type
    """
    # If already a dict, try direct validation
    if isinstance(data, dict):
        try:
            return _validate_union_type(data, response_type)
        except ValidationError as e:
            # Get the type name, handling Union types
            type_name = getattr(response_type, "__name__", str(response_type))
            raise ValueError(f"Response doesn't match expected schema {type_name}: {e}") from e

    # If string, try JSON parsing
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
            return _validate_union_type(parsed, response_type)
        except json.JSONDecodeError as e:
            raise ValueError(f"Response is not valid JSON: {e}") from e
        except ValidationError as e:
            # Get the type name, handling Union types
            type_name = getattr(response_type, "__name__", str(response_type))
            raise ValueError(f"Response doesn't match expected schema {type_name}: {e}") from e

    # Unsupported type
    type_name = getattr(response_type, "__name__", str(response_type))
    raise ValueError(f"Cannot parse response of type {type(data).__name__} into {type_name}")
