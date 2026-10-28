"""The tolerant node a RESPONSE carries where a request carries a strict input.

A read-back manifest must accept what a seller sends: an unknown field kept
rather than dropped, and a manifest INSTANCE of a sibling class rather than a
dict. A request model must not accept either -- #1241 asked that delivery
tolerance not weaken creative input validation -- so the tolerance cannot go on
the input class. It goes on a separate class that COPIES the input's fields
without inheriting from it, which is what this module builds.

The factory takes the source class as an argument and imports nothing from the
generated tree, so a generated module can import it. The clone definitions
themselves are EMITTED into the generated modules by ``READBACK_CLONES`` in
``scripts/post_generate_fixes.py``, next to the class each one copies.

Why emitted rather than kept here: ``_BuildReadbackCreative`` copies
``media_buy/build_creative_response.Creative``, and the field that carries it --
``BuildCreativeResponse4.creatives`` -- is in that same module. A hand-written
module holding the clone would have to import that module while that module
imported the clone. Every other clone could live here; one of them cannot, and
one mechanism for all five is worth more than four of them living somewhere
tidier.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import ConfigDict, create_model, model_validator

from adcp.types._split_root import shared_base
from adcp.types.base import AdCPBaseModel
from adcp.types.domains.core.version_envelope import AdcpVersionEnvelope

__all__ = ["readback_clone", "readback_normalizer"]


class _ManifestReadbackModel(AdCPBaseModel):
    """Independent from strict inputs: tolerant instances must not validate as them."""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _normalize_readback(cls, data: Any) -> Any:
        if isinstance(data, AdCPBaseModel) and not isinstance(data, cls):
            return data.model_dump(mode="python")
        return data


class _VersionedManifestReadbackModel(AdcpVersionEnvelope, _ManifestReadbackModel):
    """Keep the shared version envelope on build response nodes."""


def readback_clone(
    name: str,
    source: type[AdCPBaseModel],
    overrides: dict[str, Any] | None = None,
    *,
    validators: dict[str, Any] | None = None,
) -> type[AdCPBaseModel]:
    """A tolerant model carrying ``source``'s fields without inheriting from it.

    Copies all field constraints. A tolerant SUBCLASS would pass its parent's
    own instance validation, which is the bypass #1241 is about.
    """
    fields: dict[str, Any] = {
        key: ((overrides or {}).get(key, field.annotation), deepcopy(field))
        for key, field in source.model_fields.items()
    }
    return create_model(
        name,
        __base__=(
            _VersionedManifestReadbackModel
            if issubclass(source, AdcpVersionEnvelope)
            else _ManifestReadbackModel
        ),
        __module__=source.__module__,
        __validators__=validators,
        **fields,
    )


def readback_normalizer(source: Any, attribute: str) -> Any:
    """Bind ``source``'s own before-validator, for the clone to reuse.

    The clone does not inherit from ``source``, so it does not inherit the
    normalization either -- and a read-back manifest's assets normalize exactly
    the way an input manifest's do. Resolved on each call rather than at
    definition: pydantic binds the generated validator proxy to a callable at
    runtime, and the validator is declared on the shared base, because the
    branches differ in which fields they require, not in how assets normalize.
    """

    def normalize(data: Any) -> Any:
        return getattr(shared_base(source), attribute)(data)

    return normalize
