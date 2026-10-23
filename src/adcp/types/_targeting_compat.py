"""The rc.3 mutation overlay type, and the beta.14 object it still accepts.

A mutation field declares ``TargetingOverlayInput``: that is the schema's
shape, and it is what the JSON schema and MCP ``tools/list`` advertise. The
SDK additionally accepts an already-resolved ``TargetingOverlay`` instance,
because a caller that read an overlay off a response and passed it back should
not have to round-trip it through ``model_dump()`` first.

This module holds that type as two aliases, and the generated mutation fields
carry one of them BY GENERATION -- ``COMPATIBILITY_ANNOTATIONS`` in
``scripts/post_generate_fixes.py`` writes it at each field's declaration. It
used to be patched onto four generated classes and two canonical facades after
import, which reached only those six classes: a model that nested one of them
by its generated type got the un-bridged field.

Import layering: this module imports the generated targeting modules, and the
generated mutation modules import this one. There is no cycle because
``core/targeting.py`` and ``core/targeting_input.py`` import nothing from
``media_buy/``.
"""

from __future__ import annotations

import json
from functools import partial
from typing import Annotated, Any, get_args

from pydantic import (
    BaseModel,
    Field,
    GetCoreSchemaHandler,
    GetPydanticSchema,
    SerializerFunctionWrapHandler,
    ValidationError,
    ValidatorFunctionWrapHandler,
)
from pydantic.json_schema import SkipJsonSchema
from pydantic_core import CoreSchema, InitErrorDetails, PydanticCustomError, core_schema
from pydantic_core.core_schema import ErrorType

from adcp.types.domains.core.targeting import TargetingOverlay
from adcp.types.domains.core.targeting_input import TargetingOverlayInput

__all__ = [
    "TargetingOverlayMutation",
    "TargetingOverlayMutationInMaterializedJson",
    "mutation_overlay_field",
]


def _validate_targeting_overlay(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    """Keep the runtime compatibility union out of request-document error paths."""
    try:
        return handler(value)
    except ValidationError as exc:
        # Raw request validation belongs to the Input schema. On failure the
        # legacy branch repeats its errors, with synthetic class-name loc segments.
        # Normalize at this field boundary so direct Pydantic errors, MCP, A2A,
        # and CLI all agree, including callers that never run error narrowing.
        # Only remove the leading Input branch label; nested genuine unions retain
        # all of their locations and errors. Successful validation is unchanged.
        input_errors: list[InitErrorDetails] = []
        for error in exc.errors(include_url=False):
            if error["loc"][:1] != ("TargetingOverlayInput",):
                continue
            detail: InitErrorDetails = {
                "type": error["type"],
                "loc": error["loc"][1:],
                "input": error["input"],
            }
            # Built-in codes can be reconstructed from their name and context.
            # Schema keyword errors (for example anyOf) need their custom type
            # and message; core cannot look them up in its built-in registry.
            if error["type"] not in get_args(ErrorType):
                detail["type"] = PydanticCustomError(error["type"], error["msg"], error.get("ctx"))
            if "ctx" in error:
                detail["ctx"] = error["ctx"]
            input_errors.append(detail)
        if not input_errors:
            raise
        raise ValidationError.from_exception_data(exc.title, input_errors) from exc


def _serialize_targeting_overlay(value: Any, handler: SerializerFunctionWrapHandler) -> Any:
    """Use the compatibility union for both Python and JSON serialization."""
    return handler(value)


def _targeting_overlay_schema(
    source: Any, handler: GetCoreSchemaHandler, *, materialized_json: bool
) -> CoreSchema:
    """Keep JSON diagnostics native while bridging legacy Python objects."""
    input_schema = handler.generate_schema(TargetingOverlayInput | None)
    compatibility_schema = handler(source)
    json_schema = input_schema
    if materialized_json:
        # A canonical parent materializes JSON in its before validator. Re-enter
        # core JSON validation for just this subtree to restore native error codes
        # and extra/missing error order. A core Json schema preserves caller
        # strictness/context; a separate TypeAdapter call would not.
        json_schema = core_schema.no_info_before_validator_function(
            json.dumps,
            core_schema.json_schema(input_schema),
            json_schema_input_schema=input_schema,
        )
    return core_schema.json_or_python_schema(
        # Native JSON parents need no Python callback or round trip. In
        # particular, retain diagnostics for repeated keys in the raw JSON.
        json_schema=json_schema,
        python_schema=core_schema.no_info_wrap_validator_function(
            _validate_targeting_overlay,
            compatibility_schema,
        ),
        # json-or-python otherwise selects the Input-only JSON serializer for
        # to_json(), which cannot serialize legacy collection representations.
        # Keep the original union serializer and advertise only mutation input.
        serialization=core_schema.wrap_serializer_function_ser_schema(
            _serialize_targeting_overlay,
            schema=compatibility_schema,
        ),
    )


#: The union both aliases carry. Input FIRST is essential: a raw dict keeps its rc.3
#: mutation wrappers, while an already-resolved instance (including a subclass) retains
#: identity and internal fields without a lossy model_dump() conversion. The legacy
#: branch is runtime-only -- advertising it would widen and duplicate the mutation shape
#: in model_json_schema() and in MCP tools/list.
_MutationUnion = TargetingOverlayInput | SkipJsonSchema[TargetingOverlay] | None

#: What a generated mutation field carries. ``Annotated`` flattens, so the generated
#: declaration keeps its own ``Field(description=...)`` beside this.
#:
#: Spelled out rather than built by a factory taking the flag: mypy reads an implicit
#: type alias only from a literal ``Annotated[...]``, and a call is a variable, not a
#: type. The two differ in one token.
TargetingOverlayMutation = Annotated[
    _MutationUnion,
    Field(union_mode="left_to_right"),
    GetPydanticSchema(partial(_targeting_overlay_schema, materialized_json=False)),
]

#: The same type for a mutation field whose enclosing model materializes JSON before its
#: fields are validated -- every ``CanonicalBoundaryModel`` does. Both directions of this
#: split were measured: forcing False everywhere fails six JSON error-ordering cases in
#: tests/test_targeting_overlay_errors.py, and forcing True everywhere fails the two
#: repeated-key diagnostics cases there.
#:
#: A canonical model subclasses its generated parent, so it REDECLARES the field with
#: this alias. A pydantic subclass that redeclares a field replaces the whole
#: ``FieldInfo``, so the declaration assigns ``canonical_creative._inherit(...)`` to keep
#: the generated description and constraints -- dropping the description was measured to
#: fail ``test_advertised_targeting_mutations_keep_input_only_schema[update_media_buy]``.
TargetingOverlayMutationInMaterializedJson = Annotated[
    _MutationUnion,
    Field(union_mode="left_to_right"),
    GetPydanticSchema(partial(_targeting_overlay_schema, materialized_json=True)),
]


def mutation_overlay_field(parent: type[BaseModel]) -> Any:
    """The generated field's default and description, for a canonical redeclaration.

    NOT ``canonical_creative._inherit``, which deep-copies the whole ``FieldInfo``.
    That is right for a field whose parent carries only schema metadata, and wrong
    here: the generated parent's ``FieldInfo`` already carries THIS annotation's
    metadata (``union_mode`` and the ``GetPydanticSchema``), so copying it applies
    both twice and pydantic raises ``Unable to apply constraint 'union_mode' to
    schema of type 'json-or-python'`` -- measured, not predicted.

    Default and description are the whole of what the generated field carries
    besides that metadata, so nothing is dropped: alias, title, constraints,
    json_schema_extra and the rest are all unset on all four of these fields.
    """
    field = parent.model_fields["targeting_overlay"]
    return Field(default=field.default, description=field.description)
