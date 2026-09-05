"""Compatibility dispatch for constructible generated response bases."""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, TypeAdapter
from typing_extensions import Self


def _model_validate_kwargs(
    *,
    strict: bool | None,
    extra: Any,
    from_attributes: bool | None,
    context: Any,
    by_alias: bool | None,
    by_name: bool | None,
) -> dict[str, Any]:
    """Build arguments accepted by both early and current Pydantic 2.x."""
    kwargs: dict[str, Any] = {
        "strict": strict,
        "from_attributes": from_attributes,
        "context": context,
    }
    # These keywords were added after Pydantic 2.0. Omitting their default
    # ``None`` retains the older model_validate() compatibility contract.
    if extra is not None:
        kwargs["extra"] = extra
    if by_alias is not None:
        kwargs["by_alias"] = by_alias
    if by_name is not None:
        kwargs["by_name"] = by_name
    return kwargs


def _model_validate_json_kwargs(
    *,
    strict: bool | None,
    extra: Any,
    context: Any,
    by_alias: bool | None,
    by_name: bool | None,
) -> dict[str, Any]:
    """Build JSON-validation arguments accepted across Pydantic 2.x."""
    kwargs: dict[str, Any] = {"strict": strict, "context": context}
    if extra is not None:
        kwargs["extra"] = extra
    if by_alias is not None:
        kwargs["by_alias"] = by_alias
    if by_name is not None:
        kwargs["by_name"] = by_name
    return kwargs


class ResponseArmDispatchMixin:
    """Validate a stable response base through its generated schema arms.

    Some public response names predate code generation emitting a union of
    numbered response-arm models.  The generated compatibility base keeps the
    public name constructible, while this mixin makes ``Base.model_validate``
    preserve the arm-specific fields that arrived on the wire.
    """

    @classmethod
    def _response_arm_models(cls) -> tuple[type[BaseModel], ...]:
        """Return the generated models that define this response's wire arms."""
        return ()

    @classmethod
    def model_validate(
        cls: type[Self],
        obj: Any,
        *,
        strict: bool | None = None,
        extra: Any = None,
        from_attributes: bool | None = None,
        context: Any = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Validate stable bases as their matching generated response arm."""
        if isinstance(obj, cls):
            return obj

        arms = cls._response_arm_models()
        kwargs = _model_validate_kwargs(
            strict=strict,
            extra=extra,
            from_attributes=from_attributes,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )
        # Concrete arms inherit this method too; their own API must retain
        # single-arm validation rather than dispatching to a sibling arm.
        if not arms or cls in cast(Any, arms):
            parent_model = cast(Any, super())
            return cast(Self, parent_model.model_validate(obj, **kwargs))

        union_type: Any = arms[0]
        for arm in arms[1:]:
            union_type |= arm

        return cast(Self, TypeAdapter(union_type).validate_python(obj, **kwargs))

    @classmethod
    def model_validate_json(
        cls: type[Self],
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: Any = None,
        context: Any = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Validate JSON through response arms without dropping their fields."""
        arms = cls._response_arm_models()
        kwargs = _model_validate_json_kwargs(
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )
        if not arms or cls in cast(Any, arms):
            parent_model = cast(Any, super())
            return cast(Self, parent_model.model_validate_json(json_data, **kwargs))

        union_type: Any = arms[0]
        for arm in arms[1:]:
            union_type |= arm

        return cast(Self, TypeAdapter(union_type).validate_json(json_data, **kwargs))
