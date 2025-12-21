"""Type coercion utilities for improved type ergonomics.

This module provides validators and utilities that enable flexible input types
while maintaining type safety. It allows developers to use natural Python
patterns (strings for enums, dicts for models) without explicit type construction.

Examples:
    # With coercion, these are equivalent:
    ListCreativeFormatsRequest(type="video")
    ListCreativeFormatsRequest(type=FormatCategory.video)

    # Dict coercion for context:
    ListCreativeFormatsRequest(context={"key": "value"})
    ListCreativeFormatsRequest(context=ContextObject(key="value"))
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from pydantic import BaseModel

T = TypeVar("T", bound=Enum)
M = TypeVar("M", bound="BaseModel")


def coerce_to_enum(enum_class: type[T]) -> Callable[[Any], T | None]:
    """Create a validator that coerces strings to enum values.

    This allows users to pass string values where enums are expected,
    which Pydantic will coerce at runtime anyway, but this makes it
    type-checker friendly.

    Args:
        enum_class: The enum class to coerce to.

    Returns:
        A validator function for use with Pydantic's BeforeValidator.

    Example:
        ```python
        from pydantic import BeforeValidator
        from typing import Annotated

        type: Annotated[
            FormatCategory | None,
            BeforeValidator(coerce_to_enum(FormatCategory))
        ] = None
        ```
    """

    def validator(value: Any) -> T | None:
        if value is None:
            return None
        if isinstance(value, enum_class):
            return value
        if isinstance(value, str):
            try:
                return enum_class(value)
            except ValueError:
                # Let Pydantic handle the validation error
                return value  # type: ignore
        return value  # type: ignore

    return validator


def coerce_to_enum_list(enum_class: type[T]) -> Callable[[Any], list[T] | None]:
    """Create a validator that coerces a list of strings to enum values.

    Args:
        enum_class: The enum class to coerce to.

    Returns:
        A validator function for use with Pydantic's BeforeValidator.
    """

    def validator(value: Any) -> list[T] | None:
        if value is None:
            return None
        if not isinstance(value, (list, tuple)):
            return value  # type: ignore
        result: list[T] = []
        for item in value:
            if isinstance(item, enum_class):
                result.append(item)
            elif isinstance(item, str):
                try:
                    result.append(enum_class(item))
                except ValueError:
                    # Let Pydantic handle the validation error
                    result.append(item)  # type: ignore
            else:
                result.append(item)
        return result

    return validator


def coerce_to_model(model_class: type[M]) -> Callable[[Any], M | None]:
    """Create a validator that coerces dicts to Pydantic model instances.

    This allows users to pass dict values where model objects are expected,
    making the API more ergonomic.

    Args:
        model_class: The Pydantic model class to coerce to.

    Returns:
        A validator function for use with Pydantic's BeforeValidator.

    Example:
        ```python
        from pydantic import BeforeValidator
        from typing import Annotated

        context: Annotated[
            ContextObject | None,
            BeforeValidator(coerce_to_model(ContextObject))
        ] = None
        ```
    """

    def validator(value: Any) -> M | None:
        if value is None:
            return None
        if isinstance(value, model_class):
            return value
        if isinstance(value, dict):
            return model_class(**value)
        return value  # type: ignore

    return validator


def coerce_subclass_list(base_class: type[M]) -> Callable[[Any], list[M] | None]:
    """Create a validator that accepts lists containing subclass instances.

    This addresses Python's list invariance limitation where `list[Subclass]`
    cannot be assigned to `list[BaseClass]` despite being type-safe at runtime.

    The validator:
    1. Accepts Any as input (satisfies mypy for subclass lists)
    2. Validates each item is an instance of base_class (or subclass)
    3. Returns list[base_class] (satisfies the field type)

    Args:
        base_class: The base Pydantic model class for list items.

    Returns:
        A validator function for use with Pydantic's BeforeValidator.

    Example:
        ```python
        class ExtendedCreative(CreativeAsset):
            internal_id: str = Field(exclude=True)

        # Without coercion: requires cast()
        # PackageRequest(creatives=cast(list[CreativeAsset], [extended]))

        # With coercion: just works
        PackageRequest(creatives=[extended])  # No cast needed!
        ```
    """

    def validator(value: Any) -> list[M] | None:
        if value is None:
            return None
        if not isinstance(value, (list, tuple)):
            return value  # type: ignore
        # Return the list as-is - Pydantic will validate each item
        # is an instance of base_class (including subclasses)
        return list(value)

    return validator


# =============================================================================
# List Variance Notes
# =============================================================================
#
# The coerce_subclass_list validator above handles the common case of passing
# `list[Subclass]` to a field expecting `list[BaseClass]` when constructing
# request models.
#
# For function signatures in your own code, use Sequence[T] which is covariant:
#
#     from collections.abc import Sequence
#     def process_creatives(creatives: Sequence[CreativeAsset]) -> None:
#         ...  # Accepts list[ExtendedCreative] without cast()
#
