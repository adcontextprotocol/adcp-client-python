"""A rejected request must not come back carrying what the peer sent.

``str(ValidationError)`` renders pydantic's diagnostic with ``input_value=``
in it, so a handler that put one in a protocol error reflected the peer's own
payload — a bearer token, PII, a prompt-injection string — back out through a
wire envelope that logs and intermediaries see. It also put pydantic's
internal error-type names (``model_type``) in a field no AdCP consumer can
act on.

What the peer IS owed is ``core/error.json``'s ``issues[]``: the pointer to
the offending field and the rule that rejected it. Both halves are graded
here — silence would be as wrong as echoing.

The handler methods under test are DERIVED from the source of the three
handler classes, not listed, so a method added later is covered without
editing this file; ``test_every_validating_method_is_discovered`` fails if
that derivation ever stops finding them.
"""

from __future__ import annotations

import inspect
import json
import re

import pytest

from adcp.server.base import NotImplementedResponse
from adcp.server.content_standards import ContentStandardsHandler
from adcp.server.governance import GovernanceHandler
from adcp.server.sponsored_intelligence import SponsoredIntelligenceHandler

HANDLER_CLASSES = (ContentStandardsHandler, GovernanceHandler, SponsoredIntelligenceHandler)

# A value a peer could put in a request that must never come back out.
SECRET = "Bearer sk-live-SHOULD-NOT-APPEAR"

# Pydantic's own diagnostic vocabulary. None of it is in the JSON Schema
# keyword vocabulary core/error.json points at, so none of it belongs on the
# wire. ``input_value``/``input_type`` are the value echo itself.
PYDANTIC_INTERNALS = (
    "input_value",
    "input_type",
    "model_type",
    "model_attributes_type",
    "literal_error",
    "string_pattern_mismatch",
    "For further information visit",
)

# Every payload names the secret and is malformed for every request model in
# the suite: a bare string is never a valid request object, and a lone
# unknown/`account` key never satisfies a request model's required fields.
POISON_PAYLOADS = (
    SECRET,
    {"account": SECRET},
    {"idempotency_key": SECRET, "unexpected_field": SECRET},
    [SECRET],
)


def _validating_methods(handler_cls: type) -> list[str]:
    """Names of the request-validation methods this class defines.

    Keyed on ``model_validate(params)`` — what makes a method one of these
    paths — and deliberately NOT on ``invalid_request``, the helper that
    fixes them. Keying on the fix makes the suite grade only the sites that
    already have it: a fire-probe that put ``str(e)`` back at one site saw
    the behavioural tests stay GREEN, because reintroducing the defect
    removed that site from the population instead of failing it.
    """
    found = []
    for name, member in vars(handler_cls).items():
        if name.startswith("_") or name.startswith("handle_"):
            continue
        if not inspect.iscoroutinefunction(member):
            continue
        try:
            source = inspect.getsource(member)
        except OSError:  # pragma: no cover - source always available here
            continue
        if "model_validate(params)" in source:
            found.append(name)
    return sorted(found)


class _Accepted:
    """What a stub ``handle_*`` returns when a payload validated after all.

    Not every request model has a required field — ``list_content_standards``
    and ``list_property_lists`` accept a bare unknown key — so some poison
    payloads legitimately get through to the handler. Returning a sentinel
    rather than a response lets the caller tell "was rejected, assert on it"
    from "was accepted, nothing to assert", and the per-method ``rejected``
    count is what keeps a method from being graded on zero rejections.
    """


def _concrete(handler_cls: type) -> object:
    """A handler whose abstract ``handle_*`` methods all return the sentinel."""

    async def _accepted(*args: object, **kwargs: object) -> object:
        return _Accepted()

    namespace = {name: _accepted for name in handler_cls.__abstractmethods__}
    return type(f"Concrete{handler_cls.__name__}", (handler_cls,), namespace)()


ALL_METHODS = [(cls, name) for cls in HANDLER_CLASSES for name in _validating_methods(cls)]


def test_every_validating_method_is_discovered():
    """The derivation must find the sites, or every test below is vacuous.

    21 is the count measured at d6fff01db by
    ``git grep -n 'message=str(e)' -- src/adcp/server/`` over the three
    handler modules. It is a floor: a new validating method raises it, and
    this assertion only fails if the derivation stops seeing them.
    """
    assert len(ALL_METHODS) >= 21, ALL_METHODS
    for handler_cls in HANDLER_CLASSES:
        assert _validating_methods(handler_cls), handler_cls.__name__


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_cls", "method_name"), ALL_METHODS, ids=lambda v: getattr(v, "__name__", v)
)
async def test_rejection_does_not_echo_the_payload(handler_cls, method_name):
    """No part of the refusal carries the value or pydantic's internals."""
    handler = _concrete(handler_cls)
    method = getattr(handler, method_name)

    rejected = 0
    for payload in POISON_PAYLOADS:
        result = await method(payload)
        if not isinstance(result, NotImplementedResponse):
            continue
        rejected += 1
        blob = json.dumps(result.model_dump(mode="json", exclude_none=True))

        assert SECRET not in blob, f"{method_name} echoed the payload value: {blob}"
        for token in PYDANTIC_INTERNALS:
            assert token not in blob, f"{method_name} leaked pydantic internals ({token}): {blob}"

    assert rejected, f"no poison payload was rejected by {method_name}; the test proved nothing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_cls", "method_name"), ALL_METHODS, ids=lambda v: getattr(v, "__name__", v)
)
async def test_rejection_is_actionable(handler_cls, method_name):
    """The peer is told the field path and the rule, per core/error.json.

    ``issues[]`` requires ``pointer``, ``message`` and ``keyword``, and the
    keyword is "drawn from the JSON Schema vocabulary" — so a pydantic error
    type appearing there is a defect even though it is not a value echo.
    """
    json_schema_keywords = {
        "required",
        "type",
        "enum",
        "const",
        "pattern",
        "format",
        "additionalProperties",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "oneOf",
        "anyOf",
        "allOf",
        "not",
        # schema_validator's own spelling for "no keyword applies".
        "validation",
    }

    handler = _concrete(handler_cls)
    result = await getattr(handler, method_name)(SECRET)

    assert isinstance(result, NotImplementedResponse)
    assert result.error is not None
    assert result.error.code == "VALIDATION_ERROR"
    # core/error.json: 3.2 producers MUST populate recovery; a malformed
    # request is the enum's own example of "correctable".
    assert result.error.recovery == "correctable"

    assert result.error.issues, f"{method_name} reported no issues[]"
    for issue in result.error.issues:
        entry = issue if isinstance(issue, dict) else issue.model_dump()
        assert entry["pointer"].startswith("/"), entry
        assert entry["message"].strip(), entry
        assert entry["keyword"] in json_schema_keywords, entry


def test_source_has_no_remaining_stringified_validation_error():
    """The echo cannot come back by the spelling it arrived in.

    Guards the three handler modules against ``message=str(e)`` /
    ``reason=f"...{e}"`` returning at a new site — the whole point of routing
    every one of them through a single helper.
    """
    offenders = []
    for handler_cls in HANDLER_CLASSES:
        module = inspect.getmodule(handler_cls)
        source = inspect.getsource(module)
        for match in re.finditer(
            r"(?:message|reason)\s*=\s*(?:str\(\s*e\w*\s*\)|f\"[^\"]*\{e\w*\})", source
        ):
            offenders.append(f"{module.__name__}: {match.group(0)}")
    assert not offenders, offenders
