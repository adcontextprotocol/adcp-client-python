"""A schema-valid wire document survives parse → dump unchanged.

AdCP binds bytes to several objects: ``content_digest`` is SHA-256 over the
RFC 8785 form of an entry's declared fields, a ``context`` is "echoed
unchanged", and a buyer verifies a seller's digest over what it received. A
generated model that rewrites a value on the way through breaks those
contracts without failing anything, which is how #1610 shipped: ``AnyUrl``
appended a slash to a bare origin and ``AwareDatetime`` rewrote ``+00:00`` as
``Z``, and a parsed-then-dumped ``AudienceEvidence`` no longer matched its own
digest. #1384 (``agent_url``), #1449 (defaults added to a published format),
#1587 (nested nulls) and #1575 (context null keys) were earlier instances of
the same missing invariant, each fixed at one field or one object.

This suite states the invariant and measures it. For every bundle schema with
a model it takes schema-valid documents — the bundle's ``examples``, a
synthesized document, the differential fixtures, and a checked-in corpus for
the digest-covered schemas — derives variants that exercise each form a
normalizing parser rewrites, runs each through
``Model.model_validate_json(doc).model_dump(mode="json")`` with the SDK's
default settings, and compares the result to the input under JCS.

Two tiers. A schema whose bytes the protocol binds
(:data:`_round_trip.DIGEST_COVERED`, :data:`_round_trip.ECHO_COVERED`) must
round-trip with no difference of any class. Every other schema may show only
the classes named in :data:`ACCEPTED`, each with the reason it remains and the
issue that tracks it; the set only shrinks, and a class that stops occurring
must be deleted from it. A class outside ``ACCEPTED`` — a rewritten URL,
datetime, UUID or email, an emitted default, a dropped null, a Python-named
key, a changed value, a changed shape — fails anywhere.
"""

from __future__ import annotations

from collections import defaultdict
from functools import cache

import pytest

from tests.conformance import _round_trip as rt

#: The classes of difference this SDK still produces under its default dump
#: settings, each with the reason it is here. A difference of any other class
#: is a regression. A class that stops occurring is deleted — this set only
#: shrinks.
ACCEPTED: dict[str, str] = {
    # A document routed to an arm whose required discriminator it does not
    # carry gains that arm's injected single-value ``Literal``. The generated
    # arm classes merge the root's properties, so a document valid under a
    # sibling arm validates under the first arm that accepts it. Arm routing
    # is #1414's domain (closed by #1522's construction dispatch); a schema
    # default the document omitted is no longer emitted (#1613).
    "discriminator_emitted": (
        "a document routed to an arm it does not satisfy gains the arm's discriminator (#1414)"
    ),
}

#: Schemas whose bytes the protocol binds: no difference of any class, under
#: the default dump and under the explicit as-received settings alike.
BOUND = sorted(rt.DIGEST_COVERED | rt.ECHO_COVERED)

#: Schemas with at least one schema-valid document the generated model refuses
#: (fails validation). Refusing is the schema/model parity suite's domain, not
#: this one's; the set is recorded so a refusal cannot silently remove a schema
#: from the round-trip grade, and so that it only shrinks. Each refuses
#: documents the bundled schema accepts (``publisher-property-selector``
#: through the mutual-exclusivity validator ``_ergonomic`` adds, which the
#: schema does not express). A model that *raises* instead of refusing is not
#: recorded here: :func:`test_no_schema_valid_document_raises_outside_validation`
#: forbids it outright (#1616).
REFUSING = frozenset(
    {
        "brand/creative-approval-request.json",
        "brand/verify-brand-claims-response.json",
        "core/publisher-property-selector.json",
        "media-buy/change-term.json",
    }
)


@cache
def _corpus() -> dict[str, list[rt.Document]]:
    """Every base document, by schema."""
    by_schema: dict[str, list[rt.Document]] = defaultdict(list)
    for document in rt.documents():
        if rt.has_model(document):
            by_schema[document.schema].append(document)
    return dict(by_schema)


@cache
def _graded(schema: str) -> tuple[list[tuple[rt.Document, list[rt.Difference]]], list[rt.Refusal]]:
    """Every document and variant for ``schema``, with what the round trip changed.

    A bound schema is graded on every document and every variant of each. For
    the rest, variants are derived from the first document only: every
    ``format: uri`` field in a schema is typed the same way, so varying one
    document's leaves covers the type and varying ten multiplies runtime.
    """
    documents = _corpus().get(schema, [])
    graded: list[tuple[rt.Document, list[rt.Difference]]] = []
    refusals: list[rt.Refusal] = []
    for index, base in enumerate(documents):
        expand = schema in rt.DIGEST_COVERED or schema in rt.ECHO_COVERED or index == 0
        for document in [base, *(rt.variants(base) if expand else [])]:
            differences, refusal = rt.round_trip(document)
            if refusal is not None:
                refusals.append(refusal)
            else:
                graded.append((document, differences))
    return graded, refusals


SCHEMAS = sorted(_corpus())


def test_every_bound_schema_has_a_document() -> None:
    """A digest- or echo-covered schema with no document is ungraded, which is a coverage hole."""
    missing = [schema for schema in BOUND if not _corpus().get(schema)]
    assert not missing, (
        f"no schema-valid document for {missing}; add one to "
        f"{rt.FIXTURE_CORPUS.relative_to(rt.REPO_ROOT)}"
    )


@pytest.mark.parametrize("schema", BOUND)
def test_bound_object_round_trips_byte_for_byte_as_received(schema: str) -> None:
    """``model_dump_as_received`` of a digest- or echo-covered object is the document."""
    graded, refusals = _graded(schema)
    assert not refusals, "\n".join(
        [f"{len(refusals)} schema-valid document(s) for {schema} refused by the model:"]
        + [f"  [{r.document}] {sorted(r.error_types)}: {r.message}" for r in refusals[:10]]
    )
    assert graded, f"{schema} graded no documents"
    changed = [d for document, _ in graded for d in rt.round_trip(document, as_received=True)[0]]
    assert not changed, "\n".join(
        [f"{len(changed)} difference(s) in a digest- or echo-covered object ({schema}):"]
        + [f"  {d.describe()}" for d in changed[:20]]
    )


@pytest.mark.parametrize("schema", BOUND)
def test_bound_object_round_trips_byte_for_byte_by_default(schema: str) -> None:
    """The default dump of a digest- or echo-covered object is the document."""
    graded, _refusals = _graded(schema)
    changed = [d for _, differences in graded for d in differences]
    assert not changed, "\n".join(
        [f"{len(changed)} difference(s) in a digest- or echo-covered object ({schema}):"]
        + [f"  {d.describe()}" for d in changed[:20]]
    )


@pytest.mark.parametrize("schema", SCHEMAS)
def test_schema_round_trips_or_declares_its_differences(schema: str) -> None:
    """Every difference belongs to a class this SDK declares, with its reason."""
    graded, _refusals = _graded(schema)
    undeclared = [d for _, differences in graded for d in differences if d.kind not in ACCEPTED]
    assert not undeclared, "\n".join(
        [
            f"{len(undeclared)} rewrite(s) of a class this SDK does not declare ({schema}):",
            *(f"  {d.describe()}" for d in undeclared[:20]),
            "",
            "A wire value the model did not change must dump as received. Either fix the",
            "type (see adcp.types._wire) or, for a new class of difference, add it to",
            "tests/conformance/test_wire_round_trip.py ACCEPTED with its reason.",
        ]
    )


def test_every_accepted_class_still_occurs() -> None:
    """An accepted class that no longer happens is deleted, not kept."""
    observed = {
        d.kind for schema in SCHEMAS for _, differences in _graded(schema)[0] for d in differences
    }
    stale = sorted(set(ACCEPTED) - observed)
    assert not stale, f"accepted classes that no longer occur: {stale}; delete them from ACCEPTED"
    unknown = sorted(set(ACCEPTED) - set(rt.CLASSES))
    assert not unknown, f"ACCEPTED names classes the sweep does not produce: {unknown}"


def test_the_explicit_as_received_settings_leave_no_residue() -> None:
    """``exclude_unset=True, exclude_none=False, by_alias=True`` is the document too.

    The explicit settings SDK 9's ``model_dump_as_received`` used recover the
    same bytes as the default dump; nothing the model held is left behind.
    """
    residue: dict[str, set[str]] = defaultdict(set)
    for schema in SCHEMAS:
        for document, _ in _graded(schema)[0]:
            if document.origin == "variant":
                continue
            for d in rt.round_trip(document, as_received=True)[0]:
                residue[d.kind].add(schema)
    unexpected = {
        kind: sorted(schemas) for kind, schemas in residue.items() if kind not in ACCEPTED
    }
    assert not unexpected, f"as-received residue: {unexpected}"


def test_refusing_schemas_are_declared() -> None:
    """A schema the model refuses a valid document for is named; the set only shrinks."""
    refusing = {schema for schema in SCHEMAS if _graded(schema)[1]}
    new = sorted(refusing - REFUSING)
    assert not new, "\n".join(
        [f"schema-valid documents refused by the model for {len(new)} undeclared schema(s):"]
        + [
            f"  {schema}: {sorted(_graded(schema)[1][0].error_types)} "
            f"{_graded(schema)[1][0].message}"
            for schema in new[:10]
        ]
    )
    fixed = sorted(REFUSING - refusing)
    assert not fixed, f"schemas that no longer refuse: {fixed}; delete them from REFUSING"


def test_no_schema_valid_document_raises_outside_validation() -> None:
    """A model refuses with ``ValidationError`` or not at all; it never raises (#1616).

    ``mcp-webhook-payload`` and ``async-response-data`` used to raise a raw
    ``TypeError`` from a ``min_length`` applied to an empty-model union arm.
    Unlike a refusal, a raise is never a schema disagreement to declare: it is
    a generator defect, and this test has no allowlist.
    """
    raised = [
        f"  {schema} [{refusal.document}] {sorted(refusal.error_types)}: {refusal.message}"
        for schema in SCHEMAS
        for refusal in _graded(schema)[1]
        if refusal.error_types & rt.RAISED_OUTSIDE_VALIDATION_NAMES
    ]
    assert not raised, "\n".join(
        [f"{len(raised)} schema-valid document(s) made a generated model raise:"] + raised[:20]
    )
