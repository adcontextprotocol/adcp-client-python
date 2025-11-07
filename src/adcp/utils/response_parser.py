from __future__ import annotations

"""Utilities for parsing protocol responses into structured types."""

import json
import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def parse_mcp_content(content: list[dict[str, Any]], response_type: type[T]) -> T:
    """
    Parse MCP content array into structured response type.

    MCP tools return content as a list of content items:
    [{"type": "text", "text": "..."}, {"type": "resource", ...}]

    For AdCP, we expect JSON data in text content items.

    Args:
        content: MCP content array
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
                # Validate against expected schema
                return response_type.model_validate(data)
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
                return response_type.model_validate(item)
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
            return response_type.model_validate(data)
        except ValidationError as e:
            raise ValueError(
                f"Response doesn't match expected schema {response_type.__name__}: {e}"
            ) from e

    # If string, try JSON parsing
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
            return response_type.model_validate(parsed)
        except json.JSONDecodeError as e:
            raise ValueError(f"Response is not valid JSON: {e}") from e
        except ValidationError as e:
            raise ValueError(
                f"Response doesn't match expected schema {response_type.__name__}: {e}"
            ) from e

    # Unsupported type
    raise ValueError(
        f"Cannot parse response of type {type(data).__name__} into {response_type.__name__}"
    )
