"""Small immutable public evidence values, independent of transports and credentials."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar, Literal
from urllib.parse import unquote, urlsplit

from pydantic import ConfigDict

# Credential *shapes*, never bare substrings. A keyword only condemns a value
# when it stands alone as a word and introduces something after it, so ordinary
# operational names -- ``tokenized_inventory_daily``, ``secretariat-report-v17``,
# ``authorization_metrics_v2`` -- survive while ``token=...`` and ``Bearer x``
# do not. Narrowing every provider identity to one ASCII token grammar instead
# would reject wire-valid GAM/FreeWheel/warehouse references.
_CREDENTIAL_WORD = (
    r"bearer|password|passwd|secret|secrets|token|signature|credential|credentials|"
    r"authorization|private[ _-]?key|api[ _-]?key|access[ _-]?key|access[ _-]?token|"
    r"refresh[ _-]?token|client[ _-]?secret|sas[ _-]?token"
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    rf"(?i)(?<![A-Za-z0-9])(?:{_CREDENTIAL_WORD})(?![A-Za-z0-9])\s*(?:[:=]|\s)\s*\S"
)
# Signed-URL material -- Azure SAS, S3/GCS presigned queries -- hides its secret
# in query parameters whose names this boundary cannot enumerate, so any
# ``?name=`` / ``&name=`` pair is refused even with no recognizable keyword. Real
# query strings have no whitespace around the separator, so a label such as
# ``Parquet & decimal=38`` is not one.
_QUERY_PARAMETER = re.compile(r"[?&][A-Za-z0-9_.\-]{1,64}=")
_UNSAFE_SHAPE = re.compile(
    r"(?i)(?:-----BEGIN|(?:^|\s)(?:https?|ftp|file|data|mailto|s3|gs):|[^\s/:]+:[^\s/]+@)"
)
_AWS_KEY_ID = re.compile(r"(?:^|[^A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def _public_text(value: str, *, maximum: int) -> str:
    """Reject recognizable credentials without normalizing a provider's identity.

    Provenance still belongs to the trusted adapter: no syntax check can decide
    whether an arbitrary opaque value is a secret. Inspect percent-decoded forms
    as well, so encoding cannot hide a URL or credential from this boundary.
    """
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or not value.isprintable()
        or not value.strip()
    ):
        raise ValueError("reporting metadata requires non-secret public text")
    inspected = value
    while True:
        if (
            not inspected.isprintable()
            or "://" in inspected
            or inspected.startswith("//")
            or _UNSAFE_SHAPE.search(inspected)
            or _CREDENTIAL_ASSIGNMENT.search(inspected)
            or _QUERY_PARAMETER.search(inspected)
            or _AWS_KEY_ID.search(inspected)
            or _JWT.search(inspected)
        ):
            raise ValueError("reporting metadata requires non-secret public text")
        decoded = unquote(inspected)
        if decoded == inspected:
            break
        inspected = decoded
    return value


def reporting_identifier(value: str, *, maximum: int = 255) -> str:
    """Seller-issued wire identities use the schema's deliberately narrow grammar."""
    _public_text(value, maximum=maximum)
    if re.fullmatch(r"[A-Za-z0-9_.:-]+", value) is None:
        raise ValueError("reporting identity requires a public protocol identifier")
    return value


def principal_reference(value: str) -> str:
    """Trusted public account/consumer identity, not a protocol entity ID."""
    return _public_text(value, maximum=255)


def destination_reference(value: str) -> str:
    """Immutable public destination/configuration reference, with the wire length bound."""
    return _public_text(value, maximum=255)


def resource_location(value: str) -> str:
    """Public provider-native relation/share/object name, kept byte-for-byte."""
    return _public_text(value, maximum=2048)


def file_object_reference(value: str) -> str:
    """Decoded destination-relative object key, never a URL or version query."""
    _public_text(value, maximum=1024)
    if (
        value.startswith(("/", "\\"))
        or "\\" in value
        or "?" in value
        or "#" in value
        or ".." in value.split("/")
    ):
        raise ValueError("reporting object evidence requires a destination-relative decoded key")
    return value


def native_version_reference(value: str) -> str:
    """Decoded provider-native immutable version, including spaces, '/', '+' and '='."""
    return _public_text(value, maximum=1024)


def consumer_commit_reference(value: str) -> str:
    """Public checkpoint, transaction or load ID; no protocol entity-ID grammar."""
    return _public_text(value, maximum=512)


def reader_feature_reference(value: str) -> str:
    """Public reader/format requirement, which may be a provider's feature label."""
    return _public_text(value, maximum=128)


def sha256_value(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-fA-F0-9]{64}", value) is None:
        raise ValueError("reporting evidence requires a SHA-256 digest")
    return value


def aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("reporting evidence requires an aware timestamp")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ReportingControlTotalRecord:
    """An exact expected or observed wire total, with its declared type and unit."""

    name: str
    value: str
    value_type: Literal["integer", "decimal"]
    unit: str | None = None
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", hide_input_in_errors=True
    )

    def __post_init__(self) -> None:
        if (
            any(type(value) is not str for value in (self.name, self.value, self.value_type))
            or (self.unit is not None and type(self.unit) is not str)
            or self.value_type not in {"integer", "decimal"}
        ):
            raise ValueError("control total evidence requires immutable typed values")
        reporting_identifier(self.name, maximum=128)
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", self.name) is None:
            raise ValueError("reporting total names require public metric identifiers")
        pattern = (
            r"-?(?:0|[1-9][0-9]*)"
            if self.value_type == "integer"
            else r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
        )
        if re.fullmatch(pattern, self.value) is None:
            raise ValueError("control totals require canonical numeric strings matching their type")
        if self.unit is not None:
            _public_text(self.unit, maximum=32)

    def to_wire(self) -> dict[str, str]:
        result = {"name": self.name, "value": self.value, "value_type": self.value_type}
        if self.unit is not None:
            result["unit"] = self.unit
        return result

    @classmethod
    def from_wire(cls, value: object) -> ReportingControlTotalRecord:
        if (
            not isinstance(value, dict)
            or not {"name", "value", "value_type"} <= set(value)
            or set(value) - {"name", "value", "value_type", "unit"}
        ):
            raise ValueError("invalid retained control total evidence")
        return cls(value["name"], value["value"], value["value_type"], value.get("unit"))


def freeze_control_totals(
    totals: tuple[ReportingControlTotalRecord, ...], pairs: tuple[tuple[str, str], ...]
) -> tuple[ReportingControlTotalRecord, ...]:
    if not isinstance(totals, (tuple, list)) or any(
        type(item) is not ReportingControlTotalRecord for item in totals
    ):
        raise ValueError("control total evidence requires closed immutable records")
    result = tuple(totals)
    if len({item.name for item in result}) != len(result) or sorted(
        (item.name, item.value) for item in result
    ) != sorted(pairs):
        raise ValueError("control total evidence must match the retained names and values")
    return result


@dataclass(frozen=True, slots=True)
class ReportingCanonicalDigest:
    """A trusted publisher's logical-row digest under a pinned managed contract.

    This is distinct from Core's fixed ``revision_content_sha256``. A future
    managed publisher must verify the pinned contract and compute this value
    before destination work; destination output cannot establish the expectation.
    """

    value: str
    canonicalization_id: str
    canonicalization_uri: str
    canonicalization_sha256: str

    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", hide_input_in_errors=True
    )

    def __post_init__(self) -> None:
        if any(
            type(value) is not str
            for value in (
                self.value,
                self.canonicalization_id,
                self.canonicalization_uri,
                self.canonicalization_sha256,
            )
        ):
            raise ValueError("canonical evidence requires immutable string values")
        sha256_value(self.value)
        sha256_value(self.canonicalization_sha256)
        _public_text(self.canonicalization_id, maximum=128)
        uri = self.canonicalization_uri
        try:
            parsed = urlsplit(uri)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname is not None
                and re.fullmatch(r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}", parsed.hostname)
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and uri.isascii()
                and not re.search(r"[\s\\%]", uri)
                and not re.search(r"(?i)(token|secret|signature|credential)", uri)
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("canonicalization requires a public HTTPS contract URI")

    def to_wire(self) -> dict[str, str]:
        return {
            "algorithm": "sha256",
            "value": self.value,
            "canonicalization_id": self.canonicalization_id,
            "canonicalization_uri": self.canonicalization_uri,
            "canonicalization_sha256": self.canonicalization_sha256,
        }

    @classmethod
    def from_wire(cls, value: object) -> ReportingCanonicalDigest:
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "algorithm",
                "value",
                "canonicalization_id",
                "canonicalization_uri",
                "canonicalization_sha256",
            }
            or value.get("algorithm") != "sha256"
        ):
            raise ValueError("invalid retained canonical evidence")
        return cls(
            value["value"],
            value["canonicalization_id"],
            value["canonicalization_uri"],
            value["canonicalization_sha256"],
        )
