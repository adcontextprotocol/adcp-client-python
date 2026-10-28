"""The wire-preserving scalar types keep the bytes the wire delivered (#1610).

``AudienceEvidence.content_digest`` is SHA-256 over the RFC 8785 form of the
entry's declared fields. The generated model used to parse ``baseline.system``
into ``AnyUrl`` (which appends a slash to a bare origin) and ``last_updated``
into ``AwareDatetime`` (which rewrites ``+00:00`` as ``Z`` and drops ``.000``),
so a parsed-then-dumped entry no longer matched its own digest. The types in
:mod:`adcp.types._wire` validate the same way, keep the attribute API, and in
JSON mode serialize the text they were parsed from.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pickle
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import rfc8785
from pydantic import AnyUrl, BaseModel, TypeAdapter, ValidationError

from adcp.types import AudienceEvidence
from adcp.types._wire import WireAnyUrl, WireDatetime, WireEmail, WireUuid, rfc3339
from adcp.types.base import AdCPBaseModel


class Plain(BaseModel):
    when: WireDatetime
    url: WireAnyUrl
    uid: WireUuid
    email: WireEmail
    optional_url: WireAnyUrl | None = None


class Model(AdCPBaseModel):
    """An AdCP model: ``model_dump`` passes ``serialize_as_any=True``."""

    when: WireDatetime
    url: WireAnyUrl
    uid: WireUuid
    email: WireEmail


WIRE = {
    "when": "2026-10-01T00:00:00.000+00:00",
    "url": "https://Measurement.Example",
    "uid": "00000000-0000-4000-8000-00000000ABCD",
    "email": "Ops@Example.COM",
}


@pytest.mark.parametrize("model", [Plain, Model])
def test_parsed_text_is_dumped_byte_for_byte(model: type[BaseModel]) -> None:
    parsed = model.model_validate_json(json.dumps(WIRE))
    assert parsed.model_dump(mode="json") == {
        **WIRE,
        **({"optional_url": None} if model is Plain else {}),
    }
    assert json.loads(parsed.model_dump_json())["when"] == WIRE["when"]


def test_python_mode_validation_keeps_the_text_too() -> None:
    assert Model.model_validate(WIRE).model_dump(mode="json") == WIRE


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-01T00:00:00+00:00",
        "2026-10-01T00:00:00-05:00",
        "2026-10-01T00:00:00.5Z",
        "2026-10-01T00:00:00.123456789Z",
        "2026-10-01t00:00:00z",
    ],
)
def test_every_rfc3339_spelling_survives(text: str) -> None:
    parsed = Model.model_validate({**WIRE, "when": text})
    assert parsed.model_dump(mode="json")["when"] == text


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com",
        "https://Example.COM/a",
        "HTTPS://example.com/a",
        "https://example.com:443/a",
        "https://example.com/a%2Fb",
        "https://example.com/a?b=1&c=2#frag",
    ],
)
def test_every_url_spelling_survives(text: str) -> None:
    parsed = Model.model_validate({**WIRE, "url": text})
    assert parsed.model_dump(mode="json")["url"] == text
    assert str(parsed.url) == text


def test_attribute_api_is_the_parsed_type() -> None:
    parsed = Model.model_validate(WIRE)
    assert isinstance(parsed.when, datetime)
    assert (parsed.when.year, parsed.when.tzinfo.utcoffset(None)) == (2026, timedelta(0))
    assert parsed.when == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert isinstance(parsed.url, AnyUrl)
    assert (parsed.url.scheme, parsed.url.host, parsed.url.path) == (
        "https",
        "measurement.example",
        "/",
    )
    assert parsed.url.unicode_string() == "https://measurement.example/"
    assert isinstance(parsed.uid, uuid.UUID)
    assert parsed.uid.hex == "0000000000004000800000000000abcd"
    assert parsed.uid == uuid.UUID("00000000-0000-4000-8000-00000000abcd")
    assert isinstance(parsed.email, str)


def test_equality_and_hashing_match_the_pydantic_types() -> None:
    parsed = Model.model_validate(WIRE)
    plain = AnyUrl("https://measurement.example/")
    assert parsed.url == plain
    assert plain == parsed.url
    assert len({parsed.url, plain}) == 1
    assert len({parsed.when, datetime(2026, 10, 1, tzinfo=timezone.utc)}) == 1
    assert len({parsed.uid, uuid.UUID(int=parsed.uid.int)}) == 1


def test_copies_keep_the_wire_text() -> None:
    parsed = Model.model_validate(WIRE)
    dumped = parsed.model_dump(mode="json")
    assert copy.deepcopy(parsed).model_dump(mode="json") == dumped
    assert copy.copy(parsed).model_dump(mode="json") == dumped
    assert parsed.model_copy(deep=True).model_dump(mode="json") == dumped
    assert pickle.loads(pickle.dumps(parsed)).model_dump(mode="json") == dumped
    assert Model.model_validate(parsed.model_dump()).model_dump(mode="json") == dumped


def test_a_value_built_in_python_serializes_in_the_pydantic_form() -> None:
    built = Model(
        when=datetime(2026, 10, 1, 12, 30, 0, 500000, tzinfo=timezone(timedelta(hours=-5))),
        url=AnyUrl("https://x.example"),
        uid=uuid.UUID("00000000-0000-4000-8000-00000000ABCD"),
        email="a@b.example",
    )
    assert built.model_dump(mode="json") == {
        "when": "2026-10-01T12:30:00.500000-05:00",
        "url": "https://x.example/",
        "uid": "00000000-0000-4000-8000-00000000abcd",
        "email": "a@b.example",
    }
    utc = Model(**{**built.model_dump(), "when": datetime(2026, 10, 1, tzinfo=timezone.utc)})
    assert utc.model_dump(mode="json")["when"] == "2026-10-01T00:00:00Z"


def test_a_derived_datetime_drops_the_wire_text() -> None:
    parsed = Model.model_validate(WIRE)
    assert parsed.when.wire == WIRE["when"]
    assert type(parsed.when.replace(year=2027)) is WireDatetime
    assert parsed.when.replace(year=2027).wire == "2027-10-01T00:00:00Z"
    assert (parsed.when + timedelta(hours=1)).wire == "2026-10-01T01:00:00Z"
    assert (
        rfc3339(parsed.when.astimezone(timezone(timedelta(hours=5)))) == "2026-10-01T05:00:00+05:00"
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("when", "2026-10-01T00:00:00"),
        ("when", "not a date"),
        ("url", "not a url"),
        ("url", ""),
        ("uid", "not a uuid"),
        ("email", "not an email"),
    ],
)
def test_validation_is_unchanged(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        Model.model_validate({**WIRE, field: value})


def test_json_schema_advertises_the_same_formats() -> None:
    properties = Plain.model_json_schema()["properties"]
    assert properties["when"] == {"format": "date-time", "title": "When", "type": "string"}
    assert properties["url"] == {"format": "uri", "minLength": 1, "title": "Url", "type": "string"}
    assert properties["uid"] == {"format": "uuid", "title": "Uid", "type": "string"}
    assert properties["email"] == {"format": "email", "title": "Email", "type": "string"}
    assert properties["optional_url"]["anyOf"] == [
        {"format": "uri", "minLength": 1, "type": "string"},
        {"type": "null"},
    ]
    assert TypeAdapter(WireEmail).json_schema() == {"type": "string", "format": "email"}


def test_python_mode_dump_returns_the_typed_values() -> None:
    dumped = Model.model_validate(WIRE).model_dump()
    assert isinstance(dumped["when"], WireDatetime)
    assert isinstance(dumped["url"], WireAnyUrl)
    assert isinstance(dumped["uid"], WireUuid)


# ---------------------------------------------------------------------------
# The issue's reproduction.
# ---------------------------------------------------------------------------

_DIGEST_EXCLUDED = {"content_digest", "attestation_refs"}


def _content_digest(entry: dict[str, Any]) -> str:
    core = {k: v for k, v in entry.items() if k not in _DIGEST_EXCLUDED}
    return "sha256:" + hashlib.sha256(rfc8785.dumps(core)).hexdigest()


def _entry() -> dict[str, Any]:
    entry = {
        "evidence_id": "evidence_age",
        "snapshot_id": "evidence_age_2026_q3",
        "version": "2026-q3",
        "audience": {"dimension": "age", "range": {"min": 25, "max": 54}, "label": "Adults 25-54"},
        "relationship": "composition",
        "value": 0.6,
        "unit": "fraction",
        "baseline": {
            "system": "https://measurement.example",
            "population_id": "us_adults",
            "version": "2026",
        },
        "evidence_type": "measured",
        "methodology": "projected",
        "subject_type": "individual",
        "provider": {"domain": "measurement.example"},
        "measurement_window": {"start": "2026-07-01", "end": "2026-09-30"},
        "last_updated": "2026-10-01T00:00:00.000+00:00",
    }
    entry["content_digest"] = _content_digest(entry)
    return entry


def test_audience_evidence_round_trips_its_own_digest() -> None:
    entry = _entry()
    assert _content_digest(entry) == entry["content_digest"]
    dumped = AudienceEvidence.model_validate(entry).model_dump(mode="json")
    assert dumped["baseline"]["system"] == "https://measurement.example"
    assert dumped["last_updated"] == "2026-10-01T00:00:00.000+00:00"
    assert _content_digest(dumped) == dumped["content_digest"]
    assert AudienceEvidence.model_validate_json(json.dumps(entry)).model_dump(mode="json") == entry


def test_audience_evidence_attributes_are_still_typed() -> None:
    parsed = AudienceEvidence.model_validate(_entry())
    assert parsed.last_updated.year == 2026
    assert parsed.last_updated.tzinfo is not None
    assert parsed.baseline.system.host == "measurement.example"


def test_audience_evidence_is_exported_from_adcp_types() -> None:
    import adcp.types

    assert adcp.types.AudienceEvidence is AudienceEvidence
    assert AudienceEvidence.__module__ == "adcp.types.domains.core.audience_evidence"


def test_model_dump_as_received_leaves_off_what_the_document_omitted() -> None:
    from adcp.types.domains.core.canonical_proposal import CanonicalProposal

    terms: dict[str, Any] = {
        "brand": {"domain": "acme.example"},
        "purchases": [
            {
                "product_id": "prod_1",
                "pricing_option_id": "cpm_1",
                "pricing": {
                    "pricing_option_id": "cpm_1",
                    "pricing_model": "cpm",
                    "fixed_price": 5,
                    "currency": "USD",
                },
                "start_time": "2026-11-01T00:00:00.000+00:00",
                "end_time": "2026-11-30T23:59:59+00:00",
                "targeting_overlay": {"age_restriction": {"min": 21}},
            }
        ],
        "start_time": "2026-11-01T00:00:00.000+00:00",
        "end_time": "2026-11-30T23:59:59+00:00",
    }
    digest = hashlib.sha256(rfc8785.dumps(terms)).digest()
    import base64

    proposal = {
        "proposal_id": "prop_1",
        "proposal_kind": "new_media_buy",
        "proposal_status": "committed",
        "name": "Q4",
        "expires_at": "2026-10-15T00:00:00.000+00:00",
        "commercial_terms": terms,
        "terms_digest": "sha256:" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode(),
    }
    parsed = CanonicalProposal.model_validate_json(json.dumps(proposal))
    # The default dump materializes ``verification_required: false`` (#1613)...
    overlay = parsed.model_dump(mode="json")["commercial_terms"]["purchases"][0][
        "targeting_overlay"
    ]
    assert overlay["age_restriction"] == {"min": 21, "verification_required": False}
    # ...and the as-received dump is the document, so terms_digest verifies.
    received = parsed.model_dump_as_received()
    assert received == proposal
    assert rfc8785.dumps(received["commercial_terms"]) == rfc8785.dumps(terms)


def test_model_dump_as_received_keeps_an_admitted_null_and_the_wire_alias() -> None:
    from adcp.types.domains.core.targeting_input import TargetingOverlayInput as TargetingInput

    document = {"signal_targeting_groups": None}
    parsed = TargetingInput.model_validate_json(json.dumps(document))
    # A received null is re-emitted by the default dump too (SDK 10, #1615).
    assert parsed.model_dump(mode="json") == document
    assert parsed.model_dump_as_received() == document


# ---------------------------------------------------------------------------
# Only strictly valid wire text is kept; lenient spellings normalize as before.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        (str(datetime(2026, 10, 1, tzinfo=timezone.utc)), "2026-10-01T00:00:00Z"),  # space, not T
        ("2026-10-01T00:00:00+0000", "2026-10-01T00:00:00Z"),  # offset without a colon
        ("2026-10-01T00:00Z", "2026-10-01T00:00:00Z"),  # no seconds
        ("1790000000", "2026-09-21T14:13:20Z"),  # epoch string
    ],
)
def test_a_lenient_datetime_spelling_is_normalized(text: str, expected: str) -> None:
    parsed = Model.model_validate({**WIRE, "when": text})
    assert parsed.model_dump(mode="json")["when"] == expected
    assert parsed.when.wire == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        (" https://x.example ", "https://x.example/"),  # surrounding whitespace
        ("https:x.example", "https://x.example/"),  # no authority
        ("https://x.example/a b", "https://x.example/a%20b"),  # unescaped space
        ("https://x.example/%zz", "https://x.example/%zz"),  # malformed escape: parser keeps it
        ("https://x.example\n", "https://x.example/"),  # trailing newline
        ("https://x.example/a\n", "https://x.example/a"),  # trailing newline after a path
    ],
)
def test_a_lenient_url_spelling_is_normalized(text: str, expected: str) -> None:
    parsed = Model.model_validate({**WIRE, "url": text})
    assert parsed.model_dump(mode="json")["url"] == expected
    assert str(parsed.url) == expected


def test_str_and_host_cannot_disagree() -> None:
    """A backslash the WHATWG parser reads as a slash must not reach a client as written."""
    parsed = Model.model_validate({**WIRE, "url": "https://good.example\\@evil.example"})
    assert parsed.url.host == "good.example"
    assert str(parsed.url) == "https://good.example/@evil.example"
    assert parsed.model_dump(mode="json")["url"] == "https://good.example/@evil.example"
    direct = WireAnyUrl("https://good.example\\@evil.example")
    assert (direct.host, str(direct)) == ("good.example", "https://good.example/@evil.example")


@pytest.mark.parametrize(
    "text",
    [
        "https://user@x.example/",  # userinfo: full RFC 3986 path, host agrees
        "https://[::1]/a",  # IP literal
        "https://straße.de/x",  # internationalized host, compared to unicode_host()
    ],
)
def test_strict_but_unusual_url_text_is_kept_when_the_host_agrees(text: str) -> None:
    parsed = Model.model_validate({**WIRE, "url": text})
    assert parsed.model_dump(mode="json")["url"] == text


@pytest.mark.parametrize(
    "text",
    [
        "{00000000-0000-4000-8000-00000000abcd}",
        "urn:uuid:00000000-0000-4000-8000-00000000abcd",
        "00000000000040008000000000000abc",
    ],
)
def test_a_lenient_uuid_spelling_is_normalized(text: str) -> None:
    parsed = Model.model_validate({**WIRE, "uid": text})
    assert parsed.model_dump(mode="json")["uid"] == str(uuid.UUID(text))


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Bob <bob@example.com>", "bob@example.com"),
        (" bob@example.com", "bob@example.com"),
        ("Bob@Example.COM", "Bob@Example.COM"),
    ],
)
def test_an_email_is_kept_only_as_the_trimmed_address(text: str, expected: str) -> None:
    parsed = Model.model_validate({**WIRE, "email": text})
    assert parsed.model_dump(mode="json")["email"] == expected


def test_a_lenient_spelling_passed_to_a_constructor_is_normalized_too() -> None:
    built = Model(
        when="2026-10-01 00:00:00+00:00",
        url=" https://x.example ",
        uid=WIRE["uid"],
        email="a@b.example",
    )
    assert built.model_dump(mode="json")["when"] == "2026-10-01T00:00:00Z"
    assert built.model_dump(mode="json")["url"] == "https://x.example/"


@pytest.mark.parametrize(
    "text",
    ["https://2130706433/", "https://0x7f.1/", "https://127.1/x", "https://0177.0.0.1/"],
)
def test_a_shorthand_ipv4_host_is_not_echoed(text: str) -> None:
    """``.host`` reads 127.0.0.1, so ``str()`` must name that host too (an SSRF guard reads it)."""
    from urllib.parse import urlsplit

    parsed = Model.model_validate({**WIRE, "url": text})
    assert parsed.url.host == "127.0.0.1"
    assert urlsplit(str(parsed.url)).hostname == "127.0.0.1"
    assert parsed.model_dump(mode="json")["url"] == str(parsed.url)
