from __future__ import annotations

"""Base model for AdCP types with spec-compliant serialization."""

from typing import Any

from pydantic import BaseModel


class AdCPBaseModel(BaseModel):
    """Base model for AdCP types with spec-compliant serialization.

    AdCP JSON schemas use additionalProperties: false and do not allow null
    for optional fields. Therefore, optional fields must be omitted entirely
    when not present (not sent as null).
    """

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        if "exclude_none" not in kwargs:
            kwargs["exclude_none"] = True
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        if "exclude_none" not in kwargs:
            kwargs["exclude_none"] = True
        return super().model_dump_json(**kwargs)
