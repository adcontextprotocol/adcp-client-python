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
JSON mode they serialize the text the wire delivered -- when that text passes
the strict wire check the bundled schema validator applies
(:mod:`adcp._formats`). A string pydantic's lenient parser accepts but the
schema rejects (``"2026-10-01 00:00:00+00:00"``, ``"https:x.example"``, a
braced UUID, ``"Bob <bob@example.com>"``) is normalized exactly as before, so
nothing invalid is ever echoed. A ``datetime``, ``AnyUrl`` or ``UUID``
*object* has no wire text and serializes in the pydantic form it always did:

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
from typing_extensions import Self

from adcp import _formats
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

    A ``datetime`` in every respect -- attributes, arithmetic, comparison,
    hashing, ``isinstance`` -- plus :attr:`wire`, the text it serializes to in
    JSON mode. A value parsed from strictly valid text serializes to that
    text; one built in Python, parsed from a lenient spelling, or derived by
    ``replace()``, ``astimezone()`` or arithmetic, serializes in
    :func:`rfc3339` form.

    The slot is left unset on a value that carries no wire text: ``datetime``
    builds derived values and unpickles without calling ``__new__``, and a
    constructor override costs every parse three times as much.
    """

    __slots__ = ("_wire",)

    _wire: str

    @classmethod
    def from_datetime(cls, value: datetime, wire: str | None = None) -> Self:
        """``value`` as a :class:`WireDatetime` carrying ``wire`` as its text."""
        # The pickle state (packed bytes plus tzinfo) is the cheapest way
        # through ``datetime.__new__``: one C call instead of nine arguments.
        self = cls(*datetime.__reduce_ex__(value, 2)[1])
        if wire is not None:
            self._wire = wire
        return self

    @property
    def wire(self) -> str:
        """The text this value serializes to in JSON mode."""
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
        is_strict = _formats.is_rfc3339_date_time

        def validate(value: Any, nested: core_schema.ValidatorFunctionWrapHandler) -> Any:
            if isinstance(value, cls):
                return value
            parsed = nested(value)
            if isinstance(value, str) and is_strict(value):
                return cls.from_datetime(parsed, value)
            return cls.from_datetime(parsed)

        return core_schema.no_info_wrap_validator_function(
            validate, inner, serialization=_DATETIME_SERIALIZATION
        )


def _rebuild_datetime(reduced: Any, wire: str | None) -> WireDatetime:
    cls, args = reduced[0], reduced[1]
    value: WireDatetime = cls(*args)
    if wire is not None:
        value._wire = wire
    return value


def _serialize_datetime(value: Any) -> str:
    wire = getattr(value, "_wire", None)
    return wire if wire is not None else rfc3339(value)


# JSON mode only: a python-mode dump returns the value itself, with no
# Python call per value, exactly as pydantic serializes a datetime.
_DATETIME_SERIALIZATION = core_schema.plain_serializer_function_ser_schema(
    _serialize_datetime, when_used="json"
)

# ``AdCPBaseModel.model_dump`` passes ``serialize_as_any=True``, under which
# pydantic-core serializes a value by its runtime type rather than the field's
# schema -- and infers "datetime" for a ``datetime`` subclass, which would
# write the normalized form. A ``__pydantic_serializer__`` on the class is what
# the inference consults first (``AnyUrl`` carries one for the same reason).
WireDatetime.__pydantic_serializer__ = SchemaSerializer(  # type: ignore[attr-defined]
    core_schema.any_schema(serialization=_DATETIME_SERIALIZATION)
)


class WireAnyUrl(AnyUrl):
    """An ``AnyUrl`` that remembers the text it was parsed from.

    Validated by the same parser as ``AnyUrl`` and exposing the same parsed
    view (``.scheme``, ``.host``, ``.path``, ...). ``str()`` and JSON-mode
    serialization return the text as received when it is strict RFC 3986
    syntax naming the same host the parser reports; otherwise, and for a
    value built from an ``AnyUrl`` or by :meth:`build`, pydantic's normalized
    form -- so ``str(url)`` can never hand a client a host ``.host`` did not
    name. Compares equal to any ``AnyUrl`` for the same parsed URL, so
    ``model.url == AnyUrl("https://x.example/")`` keeps holding.
    """

    __slots__ = ("_wire",)

    _wire: str

    def __init__(self, url: Any) -> None:
        super().__init__(url)
        if isinstance(url, str):
            _keep_url_text(self, url)

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
        wire = getattr(self, "_wire", None)
        if wire is not None:
            clone._wire = wire
        return clone

    def __reduce__(self) -> Any:
        return (_rebuild_url, (type(self), str(self._url), getattr(self, "_wire", None)))

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        # One Python layer, not ``AnyUrl``'s wrap plus another: the core URL
        # parser does the work and the instance is assembled here.
        inner = core_schema.url_schema()

        def validate(value: Any, nested: core_schema.ValidatorFunctionWrapHandler) -> Any:
            if isinstance(value, cls):
                return value
            if isinstance(value, AnyUrl):
                value = str(value)
            instance = cls.__new__(cls)
            instance._url = nested(value)
            if isinstance(value, str):
                _keep_url_text(instance, value)
            return instance

        return core_schema.no_info_wrap_validator_function(
            validate, inner, serialization=_URL_SERIALIZATION
        )


def _serialize_url(value: Any, info: core_schema.SerializationInfo) -> Any:
    return str(value) if info.mode == "json" else value


# ``always``, as ``AnyUrl``'s own serializer is: the core URL serializer the
# python-mode fallback would use expects the Rust URL, not the instance.
_URL_SERIALIZATION = core_schema.plain_serializer_function_ser_schema(
    _serialize_url, info_arg=True, when_used="always"
)


def _keep_url_text(instance: WireAnyUrl, text: str) -> None:
    """Attach ``text`` to ``instance`` when it is strict syntax for the parsed host.

    The fast grammar (:func:`adcp._formats.is_plain_uri`) settles the common
    case and guarantees the host agrees; anything else takes the full RFC 3986
    check and an explicit host comparison, so ``https://good.example\\@evil.example``
    (a backslash the WHATWG parser reads as a slash) falls back to the
    normalized text whose host is the one ``.host`` reports.
    """
    if not (_formats.is_plain_uri(text) or _formats.is_uri_syntax(text)):
        return
    # Compare hosts on every path: the WHATWG parser rewrites numeric hosts
    # (``2130706433``, ``0x7f.1``, ``127.1`` all become ``127.0.0.1``), so text
    # that is valid syntax can still name a host other than ``.host``.
    named = _formats.uri_host(text)
    if named is None:
        return
    url = instance._url
    if named == (url.host or "").lower() or named == (url.unicode_host() or "").lower():
        instance._wire = text


def _rebuild_url(cls: type[WireAnyUrl], parsed: str, wire: str | None) -> WireAnyUrl:
    value = cls.__new__(cls)
    value._url = AnyUrl(parsed)._url
    if wire is not None:
        value._wire = wire
    return value


class WireUuid(uuid.UUID):
    """A ``UUID`` that remembers the text it was parsed from.

    ``uuid.UUID`` serializes lower-case; RFC 4122 reads hex case-insensitively
    and the bundled ``format: uuid`` validator accepts either case of the
    hyphenated 8-4-4-4-12 form, which is the only spelling kept. A braced,
    ``urn:uuid:`` or unhyphenated input ``uuid.UUID`` accepts is normalized
    as before, and so is a value built in Python.
    """

    __slots__ = ("_wire",)

    _wire: str

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
        is_strict = _formats.is_uuid_text

        def validate(value: Any, nested: core_schema.ValidatorFunctionWrapHandler) -> Any:
            if isinstance(value, cls):
                return value
            parsed = nested(value)
            instance = cls(int=parsed.int)
            if isinstance(value, str) and is_strict(value):
                object.__setattr__(instance, "_wire", value)
            return instance

        return core_schema.no_info_wrap_validator_function(
            validate, inner, serialization=_UUID_SERIALIZATION
        )


def _rebuild_uuid(cls: type[WireUuid], value: int, wire: str | None) -> WireUuid:
    instance = cls(int=value)
    if wire is not None:
        object.__setattr__(instance, "_wire", wire)
    return instance


def _serialize_uuid(value: Any) -> str:
    wire = getattr(value, "_wire", None)
    return wire if wire is not None else str(value)


_UUID_SERIALIZATION = core_schema.plain_serializer_function_ser_schema(
    _serialize_uuid, when_used="json"
)

# See the note on ``WireDatetime.__pydantic_serializer__``.
WireUuid.__pydantic_serializer__ = SchemaSerializer(  # type: ignore[attr-defined]
    core_schema.any_schema(serialization=_UUID_SERIALIZATION)
)


def wire_text(value: datetime | AnyUrl | uuid.UUID) -> str:
    """The text ``value`` serializes to in JSON mode.

    The text as received for a value parsed from strictly valid text; the
    deterministic form the module docstring lists otherwise.
    """
    if isinstance(value, (WireDatetime, WireAnyUrl, WireUuid)):
        return value.wire
    if isinstance(value, datetime):
        return rfc3339(value)
    return str(value)


def _email_as_received(value: str) -> str:
    """``value`` when it is the trimmed address ``EmailStr`` normalizes it to.

    ``EmailStr`` lower-cases the domain, which rewrites a digested
    ``provider.data_subject_contestation.email``; the address as written is
    kept when it differs only by case. A display-name form
    (``"Bob <bob@example.com>"``) or surrounding whitespace is not an address
    the schema admits, and comes back as the normalized address, as before.
    """
    normalized: str = _email_adapter().validate_python(value)
    if value.strip() == value and value.lower() == normalized.lower():
        return value
    return normalized


@deferred_adapter
def _email_adapter() -> TypeAdapter[EmailStr]:
    return TypeAdapter(EmailStr)


#: A ``format: email`` string, validated as ``EmailStr`` and carried as the
#: ``str`` the wire delivered when that is the address itself. ``EmailStr`` is
#: itself a ``str`` to adopters, so nothing about the attribute changes.
WireEmail = Annotated[
    StrictStr,
    AfterValidator(_email_as_received),
    WithJsonSchema({"type": "string", "format": "email"}),
]
