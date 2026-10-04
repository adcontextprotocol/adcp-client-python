"""Scalar base classes for generated types whose JSON Schema root is a scalar.

A schema such as ``core/property-tag.json`` — ``{"type": "string", "pattern":
"^[a-z0-9_]+$"}`` — describes a *value*, not an object.
``datamodel-code-generator`` has no way to express that and emits
``class PropertyTag(RootModel[str])``, which is not a ``str`` anywhere Python
treats one: ``str(x)`` renders ``"root='sports'"``, ``x == "sports"`` is
``False``, and ``{x} & {"sports"}`` raises ``TypeError: unhashable type``.

``post_generate_fixes.rewrite_scalar_rootmodels()`` rewrites those classes to
subclass ``ScalarStr`` / ``ScalarInt`` / ``ScalarFloat`` instead.  The generated
subclass carries the schema's constraint keywords in ``_constraints`` and its
documentation keywords in ``_json_schema_extra``; the bases below turn those
into the Pydantic core schema, so the value validates identically at
construction and inside a model field, and the emitted JSON Schema is unchanged.

A root that *composes* — a union, an array, a ``$ref`` to an object, ``AnyUrl``,
``AwareDatetime``, ``Literal`` — keeps its ``RootModel``.  Those have no scalar
identity to collapse onto, and ``RootModel`` is the correct representation.

``.root`` and the ``root=`` keyword argument remain available behind a
``DeprecationWarning`` so existing call sites keep working unchanged.

See: https://github.com/adcontextprotocol/adcp-client-python/issues/1277
"""

from __future__ import annotations

import warnings
from typing import Any, ClassVar

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler, ValidatorFunctionWrapHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, SchemaValidator, core_schema
from typing_extensions import Self

# Distinguishes "no argument" from an explicit ``None``, which must still reach
# the validator and fail there the way the RootModel wrapper used to.
_MISSING: Any = object()


class _ScalarRoot:
    """Pydantic plumbing shared by the scalar root bases.

    Concrete bases declare the Python scalar they extend (``_scalar_type``), the
    Pydantic core schema for it (``_value_schema``), and a constructor that
    skips revalidation (``_coerce``).
    """

    __slots__ = ()

    #: The Python scalar this base extends.  ``bool`` cannot appear here:
    #: Python forbids subclassing it, so boolean roots keep their RootModel.
    _scalar_type: ClassVar[type] = object

    #: Core-schema constraint keywords carried over from the JSON Schema:
    #: ``pattern``, ``min_length``, ``max_length``, ``ge``, ``gt``, ``le``,
    #: ``lt``, ``multiple_of``.
    _constraints: ClassVar[dict[str, Any]] = {}

    #: JSON Schema documentation keywords: ``title``, ``description``,
    #: ``examples``.  Merged back into the generated JSON Schema so the
    #: wrapper's documentation survives the rewrite.
    _json_schema_extra: ClassVar[dict[str, Any]] = {}

    #: Populated per-subclass on first use by :meth:`_schema_validator`.
    _validator_cache: ClassVar[SchemaValidator | None] = None

    @classmethod
    def _value_schema(cls) -> CoreSchema:
        """Return the constrained core schema for the wrapped scalar."""
        raise NotImplementedError

    @classmethod
    def _coerce(cls, value: Any) -> Self:
        """Build an instance from an already-validated value."""
        raise NotImplementedError

    @classmethod
    def _schema_validator(cls) -> SchemaValidator:
        # ``__dict__`` rather than attribute access: a subclass must not inherit
        # the parent's validator, which was built from the parent's constraints.
        cached = cls.__dict__.get("_validator_cache")
        if cached is None:
            cached = SchemaValidator(cls._value_schema())
            cls._validator_cache = cached
        return cached

    def __new__(cls, value: Any = _MISSING, *, root: Any = _MISSING) -> Self:
        if root is not _MISSING:
            if value is not _MISSING:
                raise TypeError(
                    f"{cls.__name__}() accepts either a positional value or root=, not both"
                )
            warnings.warn(
                f"{cls.__name__}(root=...) is deprecated; pass the value positionally. "
                f"{cls.__name__} is a {cls._scalar_type.__name__} subclass, not a RootModel.",
                DeprecationWarning,
                stacklevel=2,
            )
            value = root
        if value is _MISSING:
            raise TypeError(f"{cls.__name__}() missing required argument: 'value'")
        return cls._coerce(cls._schema_validator().validate_python(value))

    @property
    def root(self) -> Any:
        """Deprecated: the instance *is* the value.

        Kept so the ``.root`` accesses written against the RootModel wrapper
        keep working.  Returns the plain scalar, not the subclass instance.
        """
        warnings.warn(
            f"{type(self).__name__}.root is deprecated; the value IS a "
            f"{self._scalar_type.__name__}.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._scalar_type(self)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._scalar_type(self)!r})"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        def validate(value: Any, nested: ValidatorFunctionWrapHandler) -> Any:
            # Pass an existing instance through untouched. RootModel defaulted to
            # ``revalidate_instances="never"``, so a typed value composed into a
            # model field without a round trip and kept its identity; adopters
            # rely on that (tests/test_composability_invariant.py).
            if isinstance(value, cls):
                return value
            return cls._coerce(nested(value))

        return core_schema.no_info_wrap_validator_function(
            validate,
            cls._value_schema(),
            serialization=core_schema.plain_serializer_function_ser_schema(
                cls._scalar_type,
                return_schema=cls._value_schema(),
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        json_schema = handler(schema)
        json_schema.update(cls._json_schema_extra)
        return json_schema


class ScalarStr(_ScalarRoot, str):
    """A ``str`` generated from a JSON Schema string root."""

    __slots__ = ()

    _scalar_type: ClassVar[type] = str

    @classmethod
    def _value_schema(cls) -> CoreSchema:
        return core_schema.str_schema(**cls._constraints)

    @classmethod
    def _coerce(cls, value: Any) -> Self:
        return str.__new__(cls, value)


def as_json_schema_integer(value: Any) -> Any:
    """Narrow a float with no fractional part to ``int``.

    JSON Schema's ``integer`` admits any number with a zero fractional part, so
    ``1.0`` is an integer and ``1.5`` is not -- the bundled validator accepts
    the first and rejects the second. A strict ``int`` alone would reject both,
    making the model stricter than the schema it was generated from.
    ``adcp.types.base.SchemaInt`` applies the same narrowing to integer fields.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


class ScalarInt(_ScalarRoot, int):
    """An ``int`` generated from a JSON Schema integer root.

    Validates the way ``adcp.types.base.SchemaInt`` validates an integer field:
    strict, so ``"1"`` and ``True`` are refused, with a float carrying no
    fractional part narrowed to ``int`` because JSON Schema counts it as one.
    """

    __slots__ = ()

    _scalar_type: ClassVar[type] = int

    @classmethod
    def _value_schema(cls) -> CoreSchema:
        return core_schema.no_info_before_validator_function(
            as_json_schema_integer,
            core_schema.int_schema(strict=True, **cls._constraints),
        )

    @classmethod
    def _coerce(cls, value: Any) -> Self:
        return int.__new__(cls, value)


class ScalarFloat(_ScalarRoot, float):
    """A ``float`` generated from a JSON Schema number root.

    Strict, like the ``StrictFloat`` the generator emits for a ``type: number``
    field: an ``int`` or ``float`` is accepted, a ``bool`` or numeric string is
    refused, matching the bundled JSON Schema validator.
    """

    __slots__ = ()

    _scalar_type: ClassVar[type] = float

    @classmethod
    def _value_schema(cls) -> CoreSchema:
        return core_schema.float_schema(strict=True, **cls._constraints)

    @classmethod
    def _coerce(cls, value: Any) -> Self:
        return float.__new__(cls, value)


def plain_scalar(value: Any) -> Any:
    """Return the plain ``str``/``int``/``float`` behind a generated scalar root.

    The normalization seam for code that must not let the subclass reach a
    comparison key, a digest, or a ``repr``. Any other value passes through
    unchanged.
    """
    if isinstance(value, _ScalarRoot):
        return value._scalar_type(value)
    return value


#: Core-schema keywords the rewriter may move into ``_constraints``.
CONSTRAINT_KEYWORDS: frozenset[str] = frozenset(
    {"pattern", "min_length", "max_length", "ge", "gt", "le", "lt", "multiple_of"}
)

#: JSON Schema keywords the rewriter may move into ``_json_schema_extra``.
DOCUMENTATION_KEYWORDS: frozenset[str] = frozenset({"title", "description", "examples"})

#: Generated root annotation name -> base class the rewriter emits for it.
#: ``--strict-types bool int float`` makes the generator spell an integer root
#: ``StrictInt`` and a number root ``StrictFloat``; ``SchemaInt`` is the name
#: ``point_integer_fields_at_the_schema_integer_type`` rewrites ``StrictInt``
#: to. ``bool`` and ``StrictBool`` are absent: Python forbids subclassing
#: ``bool``, so boolean roots keep their ``RootModel``.
SCALAR_BASES: dict[str, type[_ScalarRoot]] = {
    "str": ScalarStr,
    "StrictStr": ScalarStr,
    "int": ScalarInt,
    "StrictInt": ScalarInt,
    "SchemaInt": ScalarInt,
    "float": ScalarFloat,
    "StrictFloat": ScalarFloat,
}
