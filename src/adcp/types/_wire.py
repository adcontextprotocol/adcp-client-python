"""Scalar types that keep the wire text they were parsed from.

AdCP binds bytes to several objects: ``content_digest`` is SHA-256 over the
RFC 8785 (JCS) form of an entry's declared fields, a ``context`` is "echoed
unchanged", and a buyer verifies a seller's digest over what it received. A
model that parses ``https://measurement.example`` into pydantic's ``AnyUrl``
re-serializes it as ``https://measurement.example/``; one that parses
``2026-10-01T00:00:00.000+00:00`` into ``AwareDatetime`` writes
``2026-10-01T00:00:00Z``. Either rewrite changes the digested bytes, so a
parsed-then-dumped entry no longer matches its own digest (#1610).

The types here validate exactly as the pydantic types the generator would
have used, expose the same attribute API (``.year``, ``.tzinfo``, ``.host``,
``.hex``), compare and hash as the same values, and differ in one thing: in
JSON mode they serialize the text the wire delivered. A value constructed in
Python rather than parsed from text has no wire text and serializes in the
pydantic form it always did, so nothing an adopter builds changes shape:

* :class:`WireDatetime`: RFC 3339 with ``Z`` for UTC and ``±HH:MM`` otherwise,
  fractional seconds written only when non-zero (six digits);
* :class:`WireAnyUrl`: pydantic's normalized URL string;
* :class:`WireUuid`: lower-case hyphenated.

``scripts/post_generate_fixes.py`` points every generated ``format: date-time``,
``format: uri``, ``format: uuid`` and ``format: email`` field here
(``preserve_wire_text_on_normalizing_fields``). The published JSON Schema is
unchanged: each type advertises the same ``type``/``format`` pair the pydantic
type did. ``adcp.types.base.WireUrl`` (#1384) is the ``str``-typed sibling for
a URL that was already a plain string to adopters.

To a type checker the names re-exported from ``adcp.types.base`` are the
pydantic types they replace (``datetime``, ``AnyUrl``, ``UUID``), so nothing
an adopter wrote against the generated annotations changes its verdict; the
``.wire`` property is a runtime attribute, and :func:`wire_text` is the typed
way to read it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import AnyUrl, EmailStr, GetCoreSchemaHandler, StrictStr, TypeAdapter
from pydantic.functional_validators import AfterValidator
from pydantic.json_schema import WithJsonSchema
from pydantic_core import CoreSchema, SchemaSerializer, core_schema
from pydantic_core.core_schema import SerializationInfo
from typing_extensions import Self

from adcp._deferred_adapters import deferred_adapter

__all__ = ["WireAnyUrl", "WireDatetime", "WireEmail", "WireUuid", "rfc3339", "wire_text"]


def rfc3339(value: datetime) -> str:
    """The RFC 3339 text pydantic writes for an aware ``datetime``.

    ``Z`` for a zero offset, ``±HH:MM`` otherwise; fractional seconds only
    when the microsecond is non-zero, and then all six digits. This is the
    form every generated model has always emitted for a datetime built in
    Python, and it stays the form for one.
    """
    text = value.isoformat()
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


class WireDatetime(datetime):
    """An aware ``datetime`` that remembers the RFC 3339 text it was parsed from.

    A ``datetime`` in every respect — attributes, arithmetic, comparison,
    hashing, ``isinstance`` — plus :attr:`wire`, the text it serializes to in
    JSON mode. A value parsed from text serializes to that text; one built in
    Python, or derived by ``replace()``, ``astimezone()`` or arithmetic,
    serializes in :func:`rfc3339` form.
    """

    __slots__ = ("_wire",)

    _wire: str | None

    def __new__(cls, *args: Any, **kwargs: Any) -> Self:
        self = super().__new__(cls, *args, **kwargs)
        self._wire = None
        return self

    @classmethod
    def from_datetime(cls, value: datetime, wire: str | None = None) -> Self:
        """``value`` as a :class:`WireDatetime` carrying ``wire`` as its text."""
        self = cls(
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            value.second,
            value.microsecond,
            value.tzinfo,
            fold=value.fold,
        )
        self._wire = wire
        return self

    @property
    def wire(self) -> str:
        """The text this value serializes to in JSON mode."""
        # ``getattr``: on Python 3.10 and 3.11 ``replace()``, ``astimezone()``
        # and arithmetic build the subclass instance without calling
        # ``__new__``, so the slot is unset on a derived value.
        wire = getattr(self, "_wire", None)
        return wire if wire is not None else rfc3339(self)

    def __reduce_ex__(self, protocol: Any) -> Any:
        # ``datetime`` pickles as ``(cls, (bytes[, tzinfo]))``; ``copy`` and
        # ``pickle`` both go through here, so the wire text travels with the
        # value through ``deepcopy`` and ``model_copy(deep=True)``.
        reduced = datetime.__reduce_ex__(self, protocol)
        return (_rebuild_datetime, (reduced, getattr(self, "_wire", None)))

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        inner = core_schema.datetime_schema(tz_constraint="aware")

        def validate(value: Any, nested: core_schema.ValidatorFunctionWrapHandler) -> Any:
            if isinstance(value, cls):
                return value
            parsed = nested(value)
            return cls.from_datetime(parsed, value if isinstance(value, str) else None)

        return core_schema.no_info_wrap_validator_function(
            validate,
            inner,
            serialization=core_schema.plain_serializer_function_ser_schema(
                _serialize_datetime, info_arg=True, when_used="always"
            ),
        )


def _rebuild_datetime(reduced: Any, wire: str | None) -> WireDatetime:
    cls, args = reduced[0], reduced[1]
    value: WireDatetime = cls(*args)
    value._wire = wire
    return value


def _serialize_datetime(value: Any, info: SerializationInfo) -> Any:
    if info.mode != "json":
        return value
    if isinstance(value, WireDatetime):
        return value.wire
    return rfc3339(value)


# ``AdCPBaseModel.model_dump`` passes ``serialize_as_any=True``, under which
# pydantic-core serializes a value by its runtime type rather than the field's
# schema -- and infers "datetime" for a ``datetime`` subclass, which would
# write the normalized form. A ``__pydantic_serializer__`` on the class is what
# the inference consults first (``AnyUrl`` carries one for the same reason).
WireDatetime.__pydantic_serializer__ = SchemaSerializer(  # type: ignore[attr-defined]
    core_schema.any_schema(
        serialization=core_schema.plain_serializer_function_ser_schema(
            _serialize_datetime, info_arg=True, when_used="always"
        )
    )
)


class WireAnyUrl(AnyUrl):
    """An ``AnyUrl`` that remembers the text it was parsed from.

    Validated by the same parser as ``AnyUrl`` and exposing the same parsed
    view (``.scheme``, ``.host``, ``.path``, ...). ``str()`` and JSON-mode
    serialization return the text as received; a value built from an
    ``AnyUrl`` or by :meth:`build` has no wire text and serializes in
    pydantic's normalized form. Compares equal to any ``AnyUrl`` for the same
    parsed URL, so ``model.url == AnyUrl("https://x.example/")`` keeps holding.
    """

    __slots__ = ("_wire",)

    def __init__(self, url: Any) -> None:
        super().__init__(url)
        self._wire = url if isinstance(url, str) else None

    @property
    def wire(self) -> str:
        """The text this value serializes to in JSON mode."""
        wire = getattr(self, "_wire", None)
        return wire if wire is not None else str(self._url)

    def __str__(self) -> str:
        return self.wire

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.wire!r})"

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, AnyUrl) and self._url == other._url

    def __hash__(self) -> int:
        return hash(self._url)

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        clone = type(self).__new__(type(self))
        clone._url = self._url
        clone._wire = getattr(self, "_wire", None)
        return clone

    def __reduce__(self) -> Any:
        return (_rebuild_url, (type(self), str(self._url), getattr(self, "_wire", None)))

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        base = AnyUrl.__get_pydantic_core_schema__(cls, handler)

        def validate(value: Any, nested: core_schema.ValidatorFunctionWrapHandler) -> Any:
            if isinstance(value, cls):
                return value
            instance = nested(value)
            instance._wire = value if isinstance(value, str) else None
            return instance

        return core_schema.no_info_wrap_validator_function(
            validate,
            base,
            serialization=core_schema.plain_serializer_function_ser_schema(
                _serialize_url, info_arg=True, when_used="always"
            ),
        )


def _rebuild_url(cls: type[WireAnyUrl], parsed: str, wire: str | None) -> WireAnyUrl:
    value = cls(parsed)
    value._wire = wire
    return value


def _serialize_url(value: Any, info: SerializationInfo) -> Any:
    if info.mode != "json":
        return value
    return str(value)


class WireUuid(uuid.UUID):
    """A ``UUID`` that remembers the text it was parsed from.

    ``uuid.UUID`` serializes lower-case; RFC 4122 reads hex case-insensitively
    and the bundled ``format: uuid`` validator accepts either case. A value
    parsed from text serializes to that text; one built in Python serializes
    lower-case hyphenated as before.
    """

    __slots__ = ("_wire",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        object.__setattr__(self, "_wire", None)

    @property
    def wire(self) -> str:
        """The text this value serializes to in JSON mode."""
        wire = getattr(self, "_wire", None)
        return wire if wire is not None else str(uuid.UUID(int=self.int))

    def __reduce__(self) -> Any:
        return (_rebuild_uuid, (type(self), self.int, getattr(self, "_wire", None)))

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        return self

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        inner = core_schema.uuid_schema()

        def validate(value: Any, nested: core_schema.ValidatorFunctionWrapHandler) -> Any:
            if isinstance(value, cls):
                return value
            parsed = nested(value)
            instance = cls(int=parsed.int)
            object.__setattr__(instance, "_wire", value if isinstance(value, str) else None)
            return instance

        return core_schema.no_info_wrap_validator_function(
            validate,
            inner,
            serialization=core_schema.plain_serializer_function_ser_schema(
                _serialize_uuid, info_arg=True, when_used="always"
            ),
        )


def _rebuild_uuid(cls: type[WireUuid], value: int, wire: str | None) -> WireUuid:
    instance = cls(int=value)
    object.__setattr__(instance, "_wire", wire)
    return instance


def _serialize_uuid(value: Any, info: SerializationInfo) -> Any:
    if info.mode != "json":
        return value
    if isinstance(value, WireUuid):
        return value.wire
    return str(value)


# See the note on ``WireDatetime.__pydantic_serializer__``.
WireUuid.__pydantic_serializer__ = SchemaSerializer(  # type: ignore[attr-defined]
    core_schema.any_schema(
        serialization=core_schema.plain_serializer_function_ser_schema(
            _serialize_uuid, info_arg=True, when_used="always"
        )
    )
)


def wire_text(value: datetime | AnyUrl | uuid.UUID) -> str:
    """The text ``value`` serializes to in JSON mode.

    The text as received for a value parsed from text; the deterministic form
    the module docstring lists for one built in Python.
    """
    if isinstance(value, (WireDatetime, WireAnyUrl, WireUuid)):
        return value.wire
    if isinstance(value, datetime):
        return rfc3339(value)
    return str(value)


def _require_email(value: str) -> str:
    """Validate ``value`` as ``EmailStr`` does and return it unchanged.

    ``EmailStr`` returns the address email-validator normalized (the domain
    lower-cased), which rewrites a digested ``provider.data_subject_contestation.email``.
    """
    _email_adapter().validate_python(value)
    return value


@deferred_adapter
def _email_adapter() -> TypeAdapter[EmailStr]:
    return TypeAdapter(EmailStr)


#: A ``format: email`` string, validated as ``EmailStr`` and carried as the
#: ``str`` the wire delivered. ``EmailStr`` is itself a ``str`` to adopters, so
#: nothing about the attribute changes.
WireEmail = Annotated[
    StrictStr,
    AfterValidator(_require_email),
    WithJsonSchema({"type": "string", "format": "email"}),
]
