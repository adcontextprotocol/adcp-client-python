"""The bundled JSON Schema and the generated Pydantic model agree.

The SDK ships two validators for every tool: the bundled schema under
``adcp/_schemas/`` and the generated request model in ``adcp.types``. A
consumer calls one, the other, or both, and the SDK's client hooks call the
schema on every request while the model is what a server parses with. Where
they disagree, the SDK accepts a payload the spec's own artifact rejects, or
rejects one it accepts, and nothing surfaces either.

This suite enumerates the bundle, so a schema added by a version bump is
covered without an edit here. For every tool it builds a schema-valid request
payload, replaces one leaf at a time with a value of a different JSON type, and
requires the two validators to return the same verdict. Differences the SDK
knows about are named in :data:`_schema_parity.DECLARED`, each with the reason
it remains; a difference matching none of them fails the suite.
"""

from __future__ import annotations

import copy
from functools import cache
from typing import Any

import pytest
from pydantic import ValidationError

import adcp.types as types
from adcp.validation.schema_validator import validate_request
from tests.conformance import _schema_parity as parity

#: Tools whose request schema this suite cannot instantiate: their ``required``
#: sets live inside composition branches the instantiator does not satisfy, so
#: no schema-valid payload is derived and the tool goes ungraded. The set only
#: shrinks — improving the instantiator removes a name, and a schema bump that
#: adds a name is a coverage regression to fix rather than to record.
UNREACHABLE = frozenset(
    {
        "get_signals",
        "preview_creative",
        "refine_proposals",
        "report_plan_adjustment",
        "report_plan_outcome",
        "sync_principal",
    }
)

#: Tools that ship a request schema and expose no canonical request model under
#: ``adcp.types``, so there is no model verdict to compare. The set only
#: shrinks.
#:
#: ``build_creative`` and ``list_creative_formats`` do expose
#: ``LegacyBuildCreativeRequest`` and ``LegacyListCreativeFormatsRequest``, and
#: each accepts the payload its bundled schema declares valid — so for those two
#: the canonical request model is the thing that is missing, not the type. The
#: other four expose no request model under any spelling.
UNMODELLED = frozenset(
    {
        "build_creative",
        "list_creative_formats",
        "search_brands",
        "tasks_get",
        "tasks_list",
        "validate_property_delivery",
    }
)


def _model_for(tool: str) -> tuple[str, Any] | tuple[None, None]:
    """Return the request model ``adcp.types`` exposes for ``tool``."""
    camel = "".join(part.capitalize() for part in tool.split("_"))
    for name in (f"{camel}Request", camel):
        model = getattr(types, name, None)
        if model is not None and hasattr(model, "model_validate"):
            return name, model
    return None, None


def _model_verdict(model: Any, document: Any) -> tuple[bool, frozenset[str]]:
    try:
        model.model_validate(document)
    except ValidationError as exc:
        return False, frozenset(error["type"] for error in exc.errors())
    return True, frozenset()


def _compare(tool: str, model: Any, document: Any, pointer: str, mutation: Any) -> Any:
    """Return a :class:`Divergence` when the two validators disagree."""
    outcome = validate_request(tool, document)
    model_ok, error_types = _model_verdict(model, document)
    if outcome.valid == model_ok:
        return None
    issue = outcome.issues[0] if outcome.issues else None
    return parity.Divergence(
        tool=tool,
        pointer=pointer,
        mutation=mutation,
        direction=parity.SCHEMA_ACCEPTS if outcome.valid else parity.MODEL_ACCEPTS,
        keyword="" if issue is None else issue.keyword,
        message="" if issue is None else issue.message,
        schema_path="" if issue is None else issue.schema_path,
        error_types=error_types,
    )


@cache
def _divergences(tool: str) -> tuple[parity.Divergence, ...]:
    """Every document for ``tool`` the two validators grade differently.

    Cached: the payload and both verdicts are deterministic, and two tests read
    the same result.
    """
    _name, model = _model_for(tool)
    assert model is not None, f"{tool} has a request schema but no request model"
    payload = parity.synthesize(tool)

    baseline = _compare(tool, model, payload, "", None)
    if baseline is not None:
        # A payload the schema declares valid and the model refuses (or the
        # reverse) makes every mutation of it diverge too; report the payload
        # itself rather than one finding per leaf.
        return (baseline,)

    found = []
    for pointer, _value in parity.leaves(payload):
        for mutation in parity.MUTATIONS:
            document = copy.deepcopy(payload)
            parity.replace_at(document, pointer, mutation)
            divergence = _compare(tool, model, document, pointer, mutation)
            if divergence is not None:
                found.append(divergence)
    return tuple(found)


GRADED = sorted(set(parity.request_tools()) - UNREACHABLE - UNMODELLED)


def test_every_bundled_request_schema_is_accounted_for() -> None:
    """Every tool is graded, instantiation-blocked, or model-less — nothing silently skipped."""
    tools = set(parity.request_tools())
    assert UNREACHABLE <= tools, f"UNREACHABLE names tools the bundle lost: {UNREACHABLE - tools}"
    assert UNMODELLED <= tools, f"UNMODELLED names tools the bundle lost: {UNMODELLED - tools}"
    assert set(GRADED) | UNREACHABLE | UNMODELLED == tools


@pytest.mark.parametrize("tool", sorted(UNMODELLED))
def test_unmodelled_tools_still_have_no_request_model(tool: str) -> None:
    """``UNMODELLED`` shrinks: a tool that gained a model moves into the graded set."""
    name, _model = _model_for(tool)
    assert name is None, f"{tool} now exposes {name} — remove it from UNMODELLED"


@pytest.mark.parametrize("tool", sorted(UNREACHABLE))
def test_unreachable_tools_still_resist_instantiation(tool: str) -> None:
    """``UNREACHABLE`` shrinks: a tool that can be instantiated moves into the graded set."""
    with pytest.raises(parity.UnsynthesizableError):
        parity.synthesize(tool)


@pytest.mark.parametrize("tool", GRADED)
def test_schema_and_model_agree(tool: str) -> None:
    """The bundled schema and the generated model grade the same documents alike."""
    undeclared = [d for d in _divergences(tool) if parity.classify(d) is None]
    assert not undeclared, "\n".join(
        [
            f"{len(undeclared)} document(s) graded differently by the bundled schema "
            f"and {_model_for(tool)[0]}, for a reason this SDK does not declare:",
            *(f"  {d.describe()}" for d in undeclared[:20]),
            "",
            "Either the two artifacts must agree, or the difference belongs in "
            "tests/conformance/_schema_parity.py DECLARED with its reason.",
        ]
    )


def test_every_declared_divergence_still_occurs() -> None:
    """A declared difference that no longer happens is deleted, not kept.

    This is what makes the set shrink-only: once a cause is fixed, the entry
    describing it stops matching and the suite says to remove it.
    """
    observed: set[str] = set()
    for tool in GRADED:
        for divergence in _divergences(tool):
            name = parity.classify(divergence)
            if name is not None:
                observed.add(name)
    stale = sorted(set(parity.DECLARED) - observed)
    assert not stale, (
        f"declared divergences that no longer occur: {stale}. "
        "Delete each from tests/conformance/_schema_parity.py DECLARED."
    )
