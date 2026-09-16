"""Small immutable public evidence values, independent of transports and credentials."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict


def native_version_reference(value: str) -> str:
    """Retain a decoded, publicly classified native version without URI encoding.

    Native versions are not entity IDs: for example, ``/``, ``+`` and ``=`` are
    valid characters. The trusted adapter must establish that the value is public;
    this guard rejects recognizable authorization material, not opaque secrets.
    """
    if (
        type(value) is not str
        or not 1 <= len(value) <= 1024
        or not value.isprintable()
        or "://" in value
        or re.search(
            r"(?i)(?:bearer\s|password|secret|token|signature|credential|private.key|-----BEGIN)",
            value,
        )
    ):
        raise ValueError("native version evidence requires a non-secret decoded public reference")
    return value


def public_reference(value: str, *, maximum: int = 1024, path: bool = False) -> str:
    """Accept inert identifiers, never URLs, query strings or authentication material.

    These are trusted public labels, not an arbitrary provider response sanitiser.
    Adapters needing a richer provider identifier must retain it behind the trusted
    binding and publish an opaque label. Error messages never interpolate input.
    """
    pattern = r"[A-Za-z0-9_.:/-]+" if path else r"[A-Za-z0-9_.:-]+"
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or re.fullmatch(pattern, value) is None
        or "://" in value
        or value.startswith("/")
        or any(part == ".." for part in value.split("/"))
        or re.search(
            r"(?i)(?:bearer|password|secret|token|signature|credential|private.key)", value
        )
    ):
        raise ValueError("reporting metadata requires a non-secret public reference")
    return value


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
        public_reference(self.name, maximum=128)
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
            public_reference(self.unit, maximum=32)

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
        public_reference(self.canonicalization_id, maximum=128)
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
