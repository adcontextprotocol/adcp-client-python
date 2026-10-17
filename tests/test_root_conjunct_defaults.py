"""A model must not accept a document and then emit one it would itself refuse.

A compiled root conjunct asks SENT-ness -- ``model_fields_set`` -- which is the
right question for ``required`` on an input and the wrong one for a conditional
the schema applies to the SERIALIZED document. There a default is
indistinguishable from a value the caller sent, so a defaulted field slips
through ``_c.descend`` (vacuous for a field that was not supplied) and is then
serialized. ``protocol/get-principal-response.json`` ties ``status`` to
``result.kind``; with ``status`` unset the model accepted a failed result and
dumped ``status='completed'``, which that conditional forbids (salesagent-38fdf).

THIS GRADES THE DEFECT, NOT A SHAPE. Two static descriptions of "the dangerous
shape" were tried first and neither selects the right set: "reads its own
defaulted field in the CHECK" admits 37 sites at this commit, most of them safe
because the check also demands the field be sent; adding "or in a guard with no
predicate" and excluding a check that demands sent-ness gives 9, which is a
DIFFERENT 9 from the 8 the bead's own probe found -- overlapping in the two real
escapes and disagreeing in both directions elsewhere. A count that matches is not
a set that matches.

So no shape is used. For each class that carries a conjunct and reads one of its
own fields that has a serializing default, a document the class ACCEPTS with that
field unset is synthesized, dumped, and fed back. If the class refuses its own
dump, it admitted a state its schema forbids and then emitted it. That is the
defect stated as itself, and it needs no allowlist: the expected answer is none.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import sys
import textwrap
from contextlib import contextmanager
from typing import Any, get_args

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

# Importing the consolidated module loads every generated domain module, and the
# canonical layer is a separate import. The derivation walks what is loaded.
import adcp.types._generated  # noqa: F401
import adcp.types.canonical_creative  # noqa: F401
from tests.conformance import _schema_parity as parity

#: The validator ``enforce_root_constraints`` emits, which is what makes a class
#: carry a compiled root conjunct at all.
ROOT_CONJUNCT = "_require_schema_required_group"


class _LocalDefs:
    """A ``$ref`` resolver over one pydantic schema's own ``$defs``."""

    def __init__(self, schema: dict[str, Any]) -> None:
        self._defs = schema.get("$defs", {})

    @contextmanager
    def resolving(self, ref: str) -> Any:
        yield self._defs[ref.rsplit("/", 1)[-1]]


def _generated_models() -> dict[tuple[str, str], type[BaseModel]]:
    """Every loaded model, keyed by ``(module, qualname)``.

    Both halves: an exported name hides aliases, and ``__qualname__`` alone
    collapses two distinct classes that share a name.
    """
    found: dict[tuple[str, str], type[BaseModel]] = {}
    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("adcp.types"):
            continue
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, BaseModel):
                found[(value.__module__, value.__qualname__)] = value
    return found


def _carries_conjunct(model: type[BaseModel]) -> bool:
    """Whether ``model`` is graded by a compiled root conjunct, inherited or its own.

    The MRO, not ``model.__dict__``: a subclass INHERITS the validator, and
    narrowing a field the inherited conjunct reads is exactly how the generator
    spells a schema that pins a member. ``__dict__`` alone selected the 225
    classes that DECLARE a conjunct and excluded the 238 that inherit one --
    including ``accept_proposal_request.Opportunity``, which pins
    ``status`` to ``Literal['closed']`` over ``OpportunityContext``'s conjunct and
    was one of three classes refusing a document its schema accepts while this
    file was green (salesagent-8qela.51).
    """
    return any(ROOT_CONJUNCT in base.__dict__ for base in model.__mro__)


def _conjunct_fields(model: type[BaseModel]) -> set[str]:
    """Every field name a conjunct grading this class reads off ``self``.

    Read from the validator's source rather than from the schema, because what
    the conjunct actually asks is what decides the escape -- and ``_c.descend``
    is the primitive that is vacuous for an unsupplied field.

    Every base that declares one is read, for the reason
    :func:`_carries_conjunct` gives: the conjunct that grades a subclass is
    frequently not written on it.
    """
    sources = [
        base.__dict__[ROOT_CONJUNCT] for base in model.__mro__ if ROOT_CONJUNCT in base.__dict__
    ]
    return set().union(*(_fields_read_by(function) for function in sources)) if sources else set()


def _fields_read_by(function: Any) -> set[str]:
    """The names one compiled validator passes to a ``_c.*`` primitive on ``self``."""
    raw = getattr(function, "__func__", function)
    raw = getattr(raw, "wrapped", raw)
    try:
        source = textwrap.dedent(inspect.getsource(raw))
    except (OSError, TypeError):
        return set()
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "_c"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "self"
            and isinstance(node.args[1], ast.Constant)
        ):
            names.add(node.args[1].value)
    return names


def _serializing_defaults(model: type[BaseModel], fields: set[str]) -> list[str]:
    """Those fields whose default is a value, so it reaches the wire."""
    out = []
    for name in sorted(fields):
        info = model.model_fields.get(name)
        if info is None:
            continue  # a nested member's field that happens to share a name
        if info.default is None or info.default is PydanticUndefined:
            continue  # nothing is emitted for it, so nothing can escape
        out.append(name)
    return out


def _accepted_documents_without(model: type[BaseModel], unset: list[str]) -> list[dict[str, Any]]:
    """Documents the model accepts which carry none of ``unset``."""
    schema = model.model_json_schema(mode="validation")
    try:
        candidates = parity.instances(schema, _LocalDefs(schema), budget=8)
    except Exception:
        return []
    good = []
    for candidate in candidates[:40]:
        if not isinstance(candidate, dict) or any(name in candidate for name in unset):
            continue
        try:
            model.model_validate(candidate)
        except ValidationError:
            continue
        good.append(candidate)
    return good


def _exposure() -> tuple[list[tuple[str, str, list[str]]], list[str]]:
    """``(graded, ungraded)`` over every class a conjunct could let a default escape."""
    graded: list[tuple[str, str, list[str]]] = []
    ungraded: list[str] = []
    for (module, qualname), model in sorted(_generated_models().items()):
        risky = _serializing_defaults(model, _conjunct_fields(model))
        if not risky:
            continue
        documents = _accepted_documents_without(model, risky)
        if not documents:
            ungraded.append(f"{module}.{qualname}")
            continue
        graded.append((module, qualname, risky))
    return graded, ungraded


def test_no_conjunct_lets_a_defaulted_value_escape_and_then_serializes_it() -> None:
    """The escape, stated as itself: accepted, then emitted and refused.

    Re-validating the DUMP is the whole assertion. If a class accepts a document
    and its own dump of that object does not validate, the conjunct graded a
    state the wire never carries -- which is what sent-ness asks and what the
    schema's conditional does not.
    """
    graded, _ungraded = _exposure()
    # A derivation that matches nothing is green, so the population is asserted.
    # Measured over the classes a conjunct GRADES rather than those that declare
    # one: 26 classes, 31 (class, field) pairs, with 16 more carrying a risky
    # default and no synthesizable document. Keyed on ``__dict__`` it was 10
    # classes (salesagent-8qela.51).
    assert len(graded) >= 26, f"the derivation selected only {len(graded)} classes to grade"

    escapes: list[str] = []
    for module, qualname, risky in graded:
        model = _generated_models()[(module, qualname)]
        for document in _accepted_documents_without(model, risky):
            dumped = model.model_validate(document).model_dump(mode="json", exclude_none=True)
            emitted = [name for name in risky if name in dumped]
            if not emitted:
                continue
            try:
                model.model_validate(dumped)
            except ValidationError as refusal:
                escapes.append(
                    f"{module}.{qualname}: accepted a document with {emitted} unset, then "
                    f"emitted {{{', '.join(f'{n}={dumped[n]!r}' for n in emitted)}}} and "
                    f"refused its own dump ({refusal.errors()[0]['type']})"
                )
                break

    assert not escapes, "\n".join(escapes)


def test_the_two_repaired_responses_grade_the_status_they_emit() -> None:
    """The two escapes 38fdf fixed, named, so the repair cannot be quietly undone.

    38fdf repaired these with ``DERIVED_DEFAULTS``, a ``mode='after'`` validator
    that supplied ``status`` from ``result.kind`` so the conjunct would grade a
    SENT value. That table is gone, because the conjunct now grades the status
    the DOCUMENT carries whether or not the caller typed it, and a derivation
    running after it could not be reached: both documents it existed to repair
    are refused first (salesagent-8qela.27).

    So the contract this holds is the same obligation with the other outcome.
    ``protocol/get-principal-response.json`` permits only 'failed' or 'rejected'
    beside a failed result, and the envelope's ergonomic default is 'completed';
    the response therefore REFUSES the construction that leaves status to that
    default, naming it, instead of accepting it and emitting a document the
    schema forbids. Named as well as covered by the derivations above, because
    both depend on documents remaining synthesizable for these two classes and a
    schema refresh could take that away.
    """
    from adcp.types.domains.protocol.get_principal_response import GetPrincipalResponse
    from adcp.types.domains.protocol.sync_principal_response import SyncPrincipalResponse

    failed = {
        "adcp_version": "3.2",
        "result": {"kind": "failed", "errors": [{"code": "AUTH_MISSING", "message": "no"}]},
    }
    for model in (GetPrincipalResponse, SyncPrincipalResponse):
        name = model.__name__
        # Unset: the ergonomic default contradicts the result, and is refused for
        # the value the document would carry -- not for having been left out.
        with pytest_raises_validation(model, failed):
            pass
        # The same refusal with ``kind`` left to its own default, which is the
        # escape 38fdf's before-validator attempt could not see and left open.
        no_kind = {"result": {"errors": [{"code": "AUTH_MISSING", "message": "no"}]}}
        with pytest_raises_validation(model, {"adcp_version": "3.2", **no_kind}):
            pass
        # Explicitly contradicting the result is still refused, unchanged.
        with pytest_raises_validation(model, {**failed, "status": "completed"}):
            pass

        # A status the conditional permits is accepted, and its dump is a document
        # the class accepts -- which is what makes the refusals above a contract
        # about the value and not a class that refuses every failed result.
        stated = model.model_validate({**failed, "status": "failed"})
        assert stated.status.value == "failed"
        dumped = stated.model_dump(mode="json", exclude_none=True)
        assert dumped["status"] == "failed"
        model.model_validate(dumped)

        # A result that does not fire the conditional keeps the ergonomic default.
        # The non-failing arm is DERIVED per class: the two responses do not share
        # one -- get_principal has 'unconfigured', sync_principal does not -- and a
        # hardcoded arm made this test fail for the wrong reason.
        other = _a_non_failing_result(model)
        kept = model.model_validate({"adcp_version": "3.2", "result": other})
        assert kept.status.value == "completed", (
            f"{name} refused result={other!r}, which does not fire the conditional, so "
            "the ergonomic default should stand"
        )


def _reaching_documents(model: type[BaseModel], without: str) -> list[dict[str, Any]]:
    """Documents that reach the arms of ``model``'s conditional, none carrying ``without``.

    The synthesizer alone does not reach them, and cannot: a root
    ``if``/``then`` is precisely the content ``datamodel-code-generator``
    dropped, so it is absent from the model's own JSON schema and
    ``_object_instances`` populates only what that schema marks ``required`` --
    which, on a response whose every field is optional, is the empty document.
    Measured: over the whole tree it reached one of the four instances the bead
    proved and missed three.

    So each field the conjunct READS is treated as a knob, since those are the
    fields a conditional turns on, and the family is the synthesized base plus
    every one- and two-knob combination. Two is what the proven instances need
    (``{unchanged, wholesale_feed_version}``), and the knobs are few enough per
    class that the pairs are cheap. Values come from the same synthesizer, per
    property, so the arm that keys on a literal gets that literal.
    """
    schema = model.model_json_schema(mode="validation")
    defs = _LocalDefs(schema)
    try:
        base = [d for d in parity.instances(schema, defs, budget=4) if isinstance(d, dict)][:3]
    except Exception:
        base = []
    values: dict[str, Any] = {}
    for knob in _conjunct_fields(model):
        if knob == without or knob not in model.model_fields:
            continue
        prop = (schema.get("properties") or {}).get(knob)
        if prop is None:
            continue
        try:
            synthesized = parity.instances(prop, defs, budget=2)
        except Exception:
            continue
        if synthesized and synthesized[0] is not None:
            values[knob] = synthesized[0]

    family = []
    for document in base or [{}]:
        family.append(document)
        for size in (1, 2):
            for combination in itertools.combinations(sorted(values), size):
                family.append({**document, **{knob: values[knob] for knob in combination}})
    return [document for document in family if without not in document]


def _sent_value(model: type[BaseModel], name: str) -> Any:
    """The field's own default, as a caller would type it."""
    default = model.model_fields[name].default
    return getattr(default, "value", default)


def _dumps_the_same_unsent(instance: BaseModel, name: str) -> bool:
    """Whether ``instance`` emits ``name`` identically with its sent-ness discarded.

    The whole question. When it holds, the document the caller gets is the same
    one either way, so a refusal that turns on having typed the value is grading
    something the wire does not carry. When it does not, the class's serializer
    drops the unset default -- ``GetMediaBuyDeliveryRequest`` has two such
    fields -- the wire really is missing it, and refusing is correct.
    """
    unsent = instance.model_copy()
    unsent.__pydantic_fields_set__.discard(name)
    return unsent.model_dump(mode="json", exclude_none=True) == instance.model_dump(
        mode="json", exclude_none=True
    )


def test_no_conjunct_demands_a_default_the_document_already_carries() -> None:
    """The mirror of the escape: identical documents, and one of them refused.

    A compiled conjunct asks SENT-ness, and a schema's ``required`` asks what the
    DOCUMENT carries. Where a field's default is one the class emits, those two
    part company: the dump carries the field whether or not the caller typed it,
    so the schema's ``required`` is satisfied either way -- and the conjunct
    refuses the construction that omitted it. The two documents are
    byte-identical, which is the whole proof that the refusal grades nothing the
    wire can express (salesagent-k37fs).

    Stated as itself and not as a shape: a pair is a defect only when the class
    REFUSES the document without the field, ACCEPTS the same document with the
    field's own default typed, and emits the same bytes for both. Each clause
    carries its own weight. A class that refuses for an unrelated reason refuses
    both ways and never qualifies, so no allowlist stands in for that; and a
    class whose serializer drops the unset default fails the third clause, which
    is how the exception is derived rather than listed.

    WHAT THIS OBLIGATION DOES NOT SEE, measured, so nobody re-derives it. The
    population now covers the classes a conjunct GRADES rather than those that
    declare one, which added the 238 inheriting subclasses -- and the three that
    refused a document their bundled schema accepts
    (``{accept_proposal,buy_products,create_media_buy}_request.Opportunity.status``,
    salesagent-8qela.50) are in it and ARE examined. They are disqualified by the
    second clause, correctly: each refuses the document with ``status='closed'``
    TYPED as well as unset, because what is missing is ``close_reason``, so the
    refusal is not about sent-ness and this is not the defect stated here.
    Their defect is a model refusing what the schema accepts, which
    ``tests/conformance/test_schema_model_parity.py`` owns and caught. Widening
    this population does not surface them and no clause here should be stretched
    until it does; that would duplicate an obligation another suite already
    grades.

    EVERY field with an emitted default is a candidate, not only one the conjunct
    NAMES. ``_c.members`` names none: ``core/deadline-policy.json`` states
    ``minProperties: 1`` and the conjunct reads ``self`` whole, so
    ``DeadlinePolicy.business_days_only`` is invisible to the conjunct's text and
    is one of the four instances. Narrowing to the named fields is the kind of
    shape that looked right and selected the wrong set.
    """
    pairs = [
        (module, qualname, name)
        for (module, qualname), model in sorted(_generated_models().items())
        if _carries_conjunct(model)
        for name in _serializing_defaults(model, set(model.model_fields))
    ]
    # A derivation that matches nothing is green, and one whose population is
    # narrower than the thing it grades is green over whatever it left out.
    # Measured over the classes a conjunct GRADES -- 463, of which 225 declare one
    # and 238 inherit one: 615 (class, field) pairs over 262 classes. Keyed on
    # ``__dict__`` this read 189 over 89, so 426 pairs went ungraded
    # (salesagent-8qela.51).
    #
    # A LOWER BOUND, not a count, and taken from the SMALLEST composition.
    # ``_generated_models`` walks what the PROCESS has imported, so a sibling test
    # that loads more generated modules can only enlarge this. Measured both ways
    # at this commit: 615 pairs with this file alone and 615 beside
    # ``test_schema_model_parity.py`` -- but the number of pairs that go on to
    # produce a document moved (585 alone, 587 together), so both floors are
    # written from the file-alone run.
    assert len(pairs) >= 615, f"the derivation selected only {len(pairs)} (class, field) pairs"

    demanded: list[str] = []
    compared: set[tuple[str, str, str]] = set()
    for module, qualname, name in pairs:
        model = _generated_models()[(module, qualname)]
        sent_value = _sent_value(model, name)
        for document in _reaching_documents(model, name):
            try:
                with_field = model.model_validate({**document, name: sent_value})
            except ValidationError:
                continue  # not a document this class accepts, so there is no pair
            if not _dumps_the_same_unsent(with_field, name):
                continue  # the serializer drops it: the wire really is missing it
            # Two spellings of ONE document. The class has to accept both.
            compared.add((module, qualname, name))
            try:
                model.model_validate(document)
            except ValidationError:
                demanded.append(
                    f"{module}.{qualname}: refused a document with {name} unset, accepted the "
                    f"same document with {name}={sent_value!r}, and dumps both as "
                    f"{with_field.model_dump(mode='json', exclude_none=True)}"
                )
                break

    # The search has to have produced pairs, or the comparison compared nothing.
    # This counts the pairs it actually built, not the ones that came out clean --
    # a count of clean pairs falls as defects are found and reads as progress.
    # Measured: 585 of the 615 pairs had one; the other 30 have no document this
    # class accepts that carries the field, so there is no pair to grade. A lower
    # bound for the same reason the pairs floor is one.
    assert len(compared) >= 585, f"only {len(compared)} pair(s) produced a document to compare"
    assert not demanded, "\n".join(demanded)


def test_the_proven_sent_ness_pairs_dump_identically() -> None:
    """The four instances the bead proved, named, so the repair cannot go quiet.

    Named as well as covered by the derivation above, for the reason the two
    repaired responses are: the derivation depends on a document being reachable
    for each class, and a schema refresh could take that away -- and then the
    cases anyone has actually seen would stop being graded with nothing failing.

    ``DeadlinePolicy`` is here because it is the one that is not a ``required``
    at all: ``core/deadline-policy.json`` states ``minProperties: 1``, so the
    count is what ``business_days_only``'s emitted default satisfies, and
    ``_c.members`` had counted ``model_fields_set`` instead of the document.
    """
    from adcp.types.domains.core.deadline_policy import DeadlinePolicy
    from adcp.types.domains.core.reporting_resource import ReportingResource
    from adcp.types.domains.media_buy.get_products_response import GetProductsResponse
    from adcp.types.domains.signals.get_signals_response import GetSignalsResponse

    unchanged = {"unchanged": True, "wholesale_feed_version": "v1"}
    proven: list[tuple[type[BaseModel], str, dict[str, Any]]] = [
        (GetSignalsResponse, "cache_scope", unchanged),
        (GetProductsResponse, "cache_scope", unchanged),
        (DeadlinePolicy, "business_days_only", {}),
        (
            ReportingResource,
            "manifest_version",
            {
                "kind": "manifest",
                "resource_ref": "res_1",
                "location": "https://example.test/m.json",
                "manifest_sha256": "a" * 64,
                "immutability": "immutable_location",
                "expires_at": "2030-01-01T00:00:00Z",
            },
        ),
    ]
    for model, name, document in proven:
        label = f"{model.__name__}.{name}"
        omitted = model.model_validate(document)
        typed = model.model_validate({**document, name: _sent_value(model, name)})
        assert omitted.model_dump(mode="json", exclude_none=True) == typed.model_dump(
            mode="json", exclude_none=True
        ), f"{label}: the two documents differ, so sent-ness is not what this pair proves"
        assert name in typed.model_dump(
            mode="json", exclude_none=True
        ), f"{label}: the class no longer emits the default, so this pair proves nothing"


@contextmanager
def pytest_raises_validation(model: type[BaseModel], document: dict[str, Any]) -> Any:
    """``model`` refuses ``document``, named so the assertion reads in one line."""
    try:
        model.model_validate(document)
    except ValidationError:
        yield
        return
    raise AssertionError(f"{model.__name__} accepted {document!r}, which its conjunct forbids")


def _a_non_failing_result(model: type[BaseModel]) -> dict[str, Any]:
    """A ``result`` the class accepts whose ``kind`` is not the one that fires.

    Derived from the arms rather than written down, because the two repaired
    responses have different result vocabularies and only ``failed`` is common
    to both.
    """
    from adcp.types._split_root import branch_members

    annotation = model.model_fields["result"].annotation
    arms = branch_members(annotation) or get_args(annotation)
    for arm in arms:
        fields = getattr(arm, "model_fields", {})
        kind = fields.get("kind")
        if kind is None or kind.default == "failed":
            continue
        document: dict[str, Any] = {"kind": kind.default}
        schema = arm.model_json_schema(mode="validation")
        try:
            candidates = parity.instances(schema, _LocalDefs(schema), budget=8)
        except Exception:
            candidates = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            merged = {**candidate, **document}
            try:
                model.model_validate({"adcp_version": "3.2", "result": merged})
            except ValidationError:
                continue
            return merged
    raise AssertionError(f"{model.__name__} has no non-failing result arm to build")
