"""Support for the wire round-trip suite.

A schema-valid wire document that passes through a generated model —
``Model.model_validate_json(doc).model_dump(mode="json")`` with the SDK's
default dump settings — must come back as the same JSON value. AdCP binds
bytes to that value: ``content_digest`` is SHA-256 over the RFC 8785 (JCS)
form of an object's declared fields, a ``context`` is "echoed unchanged",
and a buyer verifies a seller's digest over what it received. A model that
rewrites a field on the way through breaks every one of those contracts
without failing anything (#1610).

This module builds the corpus and names what changed:

* :func:`documents` collects schema-valid documents — every top-level
  ``examples`` entry in the pinned bundle, a synthesized request for every
  tool, and the checked-in differential fixtures — and :func:`variants`
  derives from each of them the forms a normalizing type rewrites (a bare
  URL origin, a ``+00:00`` offset with ``.000`` fractional seconds, an
  upper-case UUID, ...), keeping only the ones the bundled validator accepts;
* :func:`round_trip` runs one document through its model and returns every
  leaf-level :class:`Difference`, each with a :data:`CLASSES` name;
* :data:`DIGEST_COVERED` and :data:`ECHO_COVERED` list the schemas whose
  bytes the protocol binds, where no difference of any class is acceptable.

Comparison is under JCS, so key order and number formatting (``1`` vs
``1.0``) are already normalized away; what remains is a real rewrite.
"""

from __future__ import annotations

import copy
import importlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from types import UnionType
from typing import Annotated, Any, Union, get_origin

import rfc8785
from pydantic import TypeAdapter, ValidationError

import adcp.types._eager  # noqa: F401  (see below)
from adcp.validation.schema_loader import (
    _ensure_state,
    _make_ref_resolver,
    get_named_validator,
)
from tests.conformance import _schema_parity as parity

# The models graded here are the ones adopters get: ``adcp.types._eager`` runs
# the import-time patchers (``_ergonomic``, ``_forward_compat``) that add
# validators and coercions to the generated classes. Importing a domain module
# directly before that has happened grades an unpatched class, and the verdict
# then depends on test order (a mutual-exclusivity validator on
# ``PublisherPropertySelector`` refused a document in CI's full session that an
# isolated run accepted).

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Schemas whose bytes the protocol binds.
# ---------------------------------------------------------------------------

#: Schemas that carry a digest or signature computed over their own declared
#: fields (or over a declared sub-object), read off the bundle's descriptions
#: at pin 3.2.4. A model for any of these must dump exactly what it parsed:
#: ``digest(model.model_dump(mode="json")) == model.<digest>``.
DIGEST_COVERED: frozenset[str] = frozenset(
    {
        # content_digest = SHA-256(JCS(entry minus content_digest, attestation_refs))
        "core/audience-evidence.json",
        # The compact snapshot carries the same content_digest; a pin and a
        # selection repeat it and a seller MUST match all four identity fields.
        "core/canonical-audience-evidence.json",
        "core/audience-evidence-pin.json",
        "core/audience-evidence-selection.json",
        "core/canonical-audience-evidence-selection.json",
        # content_digest = SHA-256(JCS(rights_constraint minus content_digest,
        # attestation_refs, verification_url))
        "core/rights-constraint.json",
        "core/rights-attestation-evaluation.json",
        # revision_content_digest = SHA-256(JCS(set minus $schema, creative_id,
        # revision_id, revision_content_digest, name))
        "core/creative-representation-set.json",
        # selected_output_digest is computed over the derived CreativeManifest
        # minus a fixed exclusion list; the manifest is the digested object.
        "core/representation-selection.json",
        "core/creative-manifest.json",
        # content_digest = SHA-256(JCS(profile minus content_digest))
        "media-buy/acceptance-policy-profile.json",
        # terms_digest = SHA-256(JCS(commercial_terms))
        "core/canonical-proposal.json",
        "media-buy/commercial-terms.json",
        # tracker_execution_contract_digest = SHA-256(JCS(tracker_execution_contract));
        # product_snapshot_digest over the snapshot's product-binding preimage.
        "core/package-format-snapshot.json",
        # reference_digest = SHA-256(JCS(complete AttestationReference))
        "core/attestation-reference.json",
        "core/attestation-evaluation.json",
        # statement_digest = SHA-256(JCS({seller_reference, delivery_metrics}))
        "governance/check-governance-request.json",
        # runtime_attestation_binding_digest = SHA-256(JCS({evaluations, findings}))
        "governance/check-governance-response.json",
        # plan_hash = SHA-256(JCS(plan_payload)); the governance_context JWS
        # carries runtime_attestation_binding_digest.
        "governance/get-plan-audit-logs-response.json",
    }
)

#: Objects the protocol says are carried through unchanged: opaque buyer
#: data every response echoes, and the extension object. A model that holds
#: one must return the bytes it was given.
ECHO_COVERED: frozenset[str] = frozenset(
    {
        "core/context.json",
        "core/ext.json",
    }
)

# ---------------------------------------------------------------------------
# Naming a difference.
# ---------------------------------------------------------------------------

#: Every kind of rewrite the sweep distinguishes, with what produces it.
CLASSES: dict[str, str] = {
    "url_normalized": (
        "a format: uri field is parsed as pydantic AnyUrl, which re-serializes "
        "the URL it parsed (trailing slash on a bare origin, lower-cased host "
        "and scheme, dropped default port, percent-encoding)"
    ),
    "datetime_reformatted": (
        "a format: date-time field is parsed as AwareDatetime and re-serialized "
        "in pydantic's form (Z for +00:00, fractional seconds dropped when zero "
        "or truncated to microseconds)"
    ),
    "date_reformatted": "a format: date field is re-serialized in a different form",
    "uuid_normalized": "a format: uuid field is parsed as UUID and re-serialized lower-case",
    "email_normalized": (
        "a format: email field is parsed as EmailStr, which lower-cases the domain"
    ),
    "alias_emitted": (
        "the dump writes the Python field name where the wire has a name that is "
        "not an identifier ($schema, from, pass): model_dump() without by_alias"
    ),
    "default_emitted": (
        "the model emits a field the document omitted: a schema default the "
        "generator wrote into the field, or a value the model computes"
    ),
    "null_dropped": ("an explicit null the schema admits is dropped by the dump's exclude_none"),
    "field_dropped": (
        "a property the document carried is absent after the round trip: an "
        "unknown field dropped by extra='ignore', or a union arm that does not "
        "carry it"
    ),
    "value_rewritten": "a scalar value changed in a way no other class explains",
    "shape_changed": "a value changed JSON type (object, array, scalar)",
}


@dataclass(frozen=True)
class Difference:
    """One leaf the round trip changed."""

    schema: str
    document: str
    pointer: str
    kind: str
    before: Any
    after: Any

    def describe(self) -> str:
        where = f"{self.schema} [{self.document}] {self.pointer or '/'}"
        return f"{where}: {self.kind} {self.before!r} -> {self.after!r}"


@dataclass(frozen=True)
class Refusal:
    """A schema-valid document the model does not accept."""

    schema: str
    document: str
    error_types: frozenset[str]
    message: str


@dataclass
class Document:
    """A schema-valid wire document and the schema it is valid under."""

    schema: str
    name: str
    payload: Any
    #: The tool name, when the document is a task request.
    tool: str | None = None
    origin: str = "example"
    extra: dict[str, Any] = field(default_factory=dict)


_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://\S+$")
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _canonical(value: Any) -> bytes:
    return rfc8785.dumps(value)


def classify_change(before: Any, after: Any) -> str:
    """Name the class of a changed leaf."""
    if isinstance(before, str) and isinstance(after, str):
        if _URL.match(before):
            return "url_normalized"
        if _DATE_TIME.match(before):
            return "datetime_reformatted"
        if _DATE.match(before):
            return "date_reformatted"
        if _UUID.match(before):
            return "uuid_normalized"
        if _EMAIL.match(before):
            return "email_normalized"
        return "value_rewritten"
    if isinstance(before, (dict, list)) or isinstance(after, (dict, list)):
        return "shape_changed"
    if type(before) is not type(after) and not (
        isinstance(before, (int, float)) and isinstance(after, (int, float))
    ):
        return "shape_changed"
    return "value_rewritten"


def differences(schema: str, name: str, payload: Any, dumped: Any) -> list[Difference]:
    """Every leaf-level difference between ``payload`` and ``dumped`` under JCS.

    Walks both values together. A property present on one side only is
    reported once, at the property, not once per descendant; a property whose
    Python field name was emitted instead of its wire alias (``field_schema``
    for ``$schema``) is reported as one ``alias_emitted`` difference.
    """
    if _canonical(payload) == _canonical(dumped):
        return []
    found: list[Difference] = []
    _walk(schema, name, "", payload, dumped, found)
    return found


def _walk(
    schema: str, name: str, pointer: str, before: Any, after: Any, found: list[Difference]
) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        gone = {key for key in before if key not in after}
        new = {key for key in after if key not in before}
        for key in sorted(gone):
            alias = _ALIASES.get(key)
            if alias is not None and alias in new:
                new.discard(alias)
                found.append(
                    Difference(schema, name, f"{pointer}/{key}", "alias_emitted", key, alias)
                )
                _walk(schema, name, f"{pointer}/{key}", before[key], after[alias], found)
                continue
            kind = "null_dropped" if before[key] is None else "field_dropped"
            found.append(Difference(schema, name, f"{pointer}/{key}", kind, before[key], None))
        for key in sorted(new):
            found.append(
                Difference(schema, name, f"{pointer}/{key}", "default_emitted", None, after[key])
            )
        for key in before:
            if key in after:
                _walk(schema, name, f"{pointer}/{key}", before[key], after[key], found)
        return
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            found.append(Difference(schema, name, pointer, "shape_changed", before, after))
            return
        for index, (left, right) in enumerate(zip(before, after)):
            _walk(schema, name, f"{pointer}/{index}", left, right, found)
        return
    if _canonical(before) != _canonical(after):
        found.append(
            Difference(schema, name, pointer, classify_change(before, after), before, after)
        )


#: Wire names the generator cannot use as Python identifiers, with the field
#: name it emits instead when ``model_dump`` runs without ``by_alias``.
_ALIASES: dict[str, str] = {
    "$schema": "field_schema",
    "pass": "pass_",
    "from": "from_",
    "media-buy": "media_buy",
    "_truncation": "field_truncation",
    "2xl": "field_2xl",
}


# ---------------------------------------------------------------------------
# Locating the model for a schema.
# ---------------------------------------------------------------------------


def _snake(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()


def _camel(text: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^0-9a-zA-Z]+", text) if part)


@cache
def schema_root() -> Path:
    state = _ensure_state(None)
    assert state is not None, "the pinned schema bundle is not on disk"
    return state.root.root


def _request_model_for(tool: str) -> Any:
    import adcp.types as types

    camel = "".join(part.capitalize() for part in tool.split("_"))
    for name in (f"{camel}Request", camel):
        model = getattr(types, name, None)
        if model is not None and hasattr(model, "model_validate"):
            return model
    return None


@cache
def model_for(schema: str) -> Any:
    """The generated class for a bundle schema path, or ``None``.

    The generator writes one module per schema file at
    ``adcp.types.domains.<domain>.<schema>`` and names the root class after
    the schema's ``title`` (falling back to the file stem).
    """
    rel = Path(schema)
    if rel.parts[0] in ("bundled", "mcp") or len(rel.parts) == 1:
        return None
    module_name = (
        "adcp.types.domains." + ".".join(_snake(p) for p in rel.parts[:-1]) + "." + _snake(rel.stem)
    )
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    document = json.loads((schema_root() / rel).read_text())
    candidates = [_camel(document.get("title", "")), _camel(rel.stem)]
    by_lower = {name.lower(): obj for name, obj in vars(module).items()}
    for name in candidates:
        found = getattr(module, name, None)
        if found is None:
            found = by_lower.get(name.lower())
        if found is None:
            continue
        if hasattr(found, "model_validate_json"):
            return found
        if get_origin(found) in (Annotated, Union, UnionType):
            # A schema whose root is a oneOf/anyOf is exposed as a type alias
            # over its arms (post_generate_fixes.unwrap_rootmodel_unions).
            return TypeAdapter(found)
    return None


def parse_json(grader: Any, text: str) -> Any:
    if isinstance(grader, TypeAdapter):
        return grader.validate_json(text)
    return grader.model_validate_json(text)


def dump_json(grader: Any, parsed: Any) -> Any:
    """The SDK's default dump: ``model_dump(mode="json")`` as AdCPBaseModel defines it."""
    if hasattr(parsed, "model_dump"):
        # A union arm is an AdCP model: dump it the way the SDK does.
        return parsed.model_dump(mode="json")
    return grader.dump_python(parsed, mode="json", exclude_none=True)


def dump_as_received(grader: Any, parsed: Any) -> Any:
    """``AdCPBaseModel.model_dump_as_received``: only what the document carried.

    ``exclude_unset`` leaves off every field the document did not set, which
    is where a generated default would otherwise appear; ``exclude_none=False``
    keeps an explicit ``null`` the schema admits (a field the schema refuses
    ``null`` on is left off by its own ``NoExplicitNull`` exclusion either
    way); ``by_alias`` writes the wire name. A union alias root gets the same
    settings through its adapter.
    """
    if hasattr(parsed, "model_dump_as_received"):
        return parsed.model_dump_as_received()
    kwargs: dict[str, Any] = dict(
        mode="json", exclude_unset=True, exclude_none=False, by_alias=True
    )
    if hasattr(parsed, "model_dump"):
        # A RootModel over a union: pydantic's own dump with the same settings.
        return parsed.model_dump(**kwargs)
    return grader.dump_python(parsed, **kwargs)


# ---------------------------------------------------------------------------
# The corpus.
# ---------------------------------------------------------------------------


def _bundle_schemas() -> Iterator[Path]:
    root = schema_root()
    for file in sorted(root.rglob("*.json")):
        rel = file.relative_to(root)
        if rel.parts[0] in ("bundled", "mcp") or len(rel.parts) == 1:
            continue
        yield rel


def is_valid(schema: str, payload: Any) -> bool:
    validator = get_named_validator(schema)
    return validator is not None and validator.is_valid(payload)


@cache
def synthesize_named(schema: str) -> Any:
    """A schema-valid document for any bundle schema, or ``None``.

    :func:`_schema_parity.synthesize` instantiates a tool's request schema
    with the request validator as the oracle; this does the same for a named
    schema with :func:`get_named_validator`, then grows the document with
    every optional property that keeps it valid.
    """
    validator = get_named_validator(schema)
    if validator is None:
        return None
    state = _ensure_state(None)
    assert state is not None
    file = state.root.root / schema
    node = json.loads(file.read_text())
    resolver = _make_ref_resolver(state, file, node)
    try:
        candidates = parity.instances(node, resolver, 0, 64)
    except parity.UnsynthesizableError:
        return None
    for candidate in candidates:
        if validator.is_valid(candidate):
            return _grow_named(validator, candidate, node, resolver)
    return None


def _grow_named(validator: Any, payload: Any, schema: Any, resolver: Any) -> Any:
    if not isinstance(payload, dict):
        return payload

    def walk(node: Any, obj: Any, depth: int = 0) -> None:
        if depth > 5 or not isinstance(obj, dict) or not isinstance(node, dict):
            return
        if "$ref" in node:
            with resolver.resolving(node["$ref"]) as target:
                walk(target, obj, depth)
            return
        for keyword in ("allOf", "oneOf", "anyOf"):
            for member in node.get(keyword) or []:
                walk(member, obj, depth)
        properties = node.get("properties") or {}
        for name, sub in properties.items():
            if name in obj:
                continue
            try:
                value = parity.instances(sub, resolver, depth + 1, 1)[0]
            except parity.UnsynthesizableError:
                continue
            obj[name] = value
            if not validator.is_valid(payload):
                del obj[name]
        for name, sub in properties.items():
            if isinstance(obj.get(name), dict):
                walk(sub, obj[name], depth + 1)

    walk(schema, payload)
    return payload


def _merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        merged = dict(base)
        for key, value in over.items():
            merged[key] = _merge(merged[key], value) if key in merged else value
        return merged
    return over


def complete_example(schema: str, example: Any) -> Any:
    """``example`` as the bundle wrote it if valid, else completed over a synthesized base.

    Most bundle examples are fragments: they illustrate a few properties and
    omit the required ones. Laying the fragment over a synthesized valid
    document keeps every byte the example chose and fills in only what the
    schema demands; the validator decides whether the result counts.
    """
    if is_valid(schema, example):
        return example
    base = synthesize_named(schema)
    if base is None or not isinstance(base, dict) or not isinstance(example, dict):
        return None
    candidate = _merge(copy.deepcopy(base), example)
    return candidate if is_valid(schema, candidate) else None


def bundle_examples() -> Iterator[Document]:
    """Every top-level ``examples`` entry, as written or completed to validity."""
    root = schema_root()
    for rel in _bundle_schemas():
        document = json.loads((root / rel).read_text())
        examples = document.get("examples") if isinstance(document, dict) else None
        if not isinstance(examples, list):
            continue
        for index, example in enumerate(examples):
            if not isinstance(example, (dict, list)):
                continue
            payload = complete_example(rel.as_posix(), example)
            if payload is not None:
                origin = "example" if payload is example else "completed-example"
                yield Document(rel.as_posix(), f"examples[{index}]", payload, origin=origin)


def named_documents() -> Iterator[Document]:
    """One synthesized document for every bundle schema that has a model."""
    for rel in _bundle_schemas():
        schema = rel.as_posix()
        if model_for(schema) is None:
            continue
        payload = synthesize_named(schema)
        if payload is not None:
            yield Document(schema, "synthesized", copy.deepcopy(payload), origin="synthesized")


def tool_requests() -> Iterator[Document]:
    """A synthesized schema-valid request for every tool the SDK models."""
    state = _ensure_state(None)
    assert state is not None
    for tool in parity.request_tools():
        file = state.file_index[(tool, "request")]
        if file.is_relative_to(state.root.bundled):
            continue
        try:
            payload = parity.synthesize(tool)
        except parity.UnsynthesizableError:
            continue
        rel = file.relative_to(state.root.root).as_posix()
        yield Document(rel, f"synthesized:{tool}", payload, tool=tool, origin="synthesized")


def fixture_requests() -> Iterator[Document]:
    """The checked-in differential fixtures the bundled schema accepts."""
    state = _ensure_state(None)
    assert state is not None
    cases = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "validation-parity-cases.json").read_text()
    )
    for case in cases:
        if not case.get("canonical_valid") or case.get("direction") != "request":
            continue
        file = state.file_index.get((case["tool"], "request"))
        if file is None or file.is_relative_to(state.root.bundled):
            continue
        rel = file.relative_to(state.root.root).as_posix()
        yield Document(
            rel, f"fixture:{case['id']}", case["payload"], tool=case["tool"], origin="fixture"
        )


#: Hand-written documents for the bound schemas the instantiator cannot derive
#: from the schema alone (a ``oneOf`` on ``audience.value``, a ``required`` set
#: inside an ``allOf`` branch) and for the opaque echo objects. Each carries
#: a digest computed over its own JCS form where the schema declares one, a
#: bare-origin URL, a ``+00:00`` offset with ``.000`` fractional seconds, a
#: mixed-case host, and -- for the opaque objects -- nested ``null`` values:
#: the forms a normalizing type or an ``exclude_none`` dump rewrites.
FIXTURE_CORPUS = REPO_ROOT / "tests" / "fixtures" / "round_trip" / "corpus.json"


def fixture_documents() -> Iterator[Document]:
    """The checked-in digest-covered corpus, each confirmed valid by the bundled validator."""
    corpus = json.loads(FIXTURE_CORPUS.read_text())
    for schema, payload in corpus.items():
        assert is_valid(schema, payload), f"{FIXTURE_CORPUS.name}: {schema} is not schema-valid"
        yield Document(schema, "fixture:corpus", payload, origin="fixture")


def documents() -> list[Document]:
    return [
        *bundle_examples(),
        *named_documents(),
        *tool_requests(),
        *fixture_requests(),
        *fixture_documents(),
    ]


# ---------------------------------------------------------------------------
# Variants: the forms a normalizing type rewrites.
# ---------------------------------------------------------------------------

#: Substitutions tried at every leaf whose value has the matching shape. Each
#: is schema-valid wherever the original was, which the validator confirms
#: before the variant is kept; each is a form a normalizing parser rewrites.
URL_VARIANTS = (
    "https://example.com",  # bare origin: AnyUrl appends "/"
    "https://Example.COM/a",  # host case: AnyUrl lower-cases
    "HTTPS://example.com/a",  # scheme case: AnyUrl lower-cases
    "https://example.com:443/a",  # default port: AnyUrl drops it
    "https://example.com/a%2Fb",  # percent-encoding: kept or re-encoded
    "https://example.com/a?b=1&c=2#frag",
    "https://example.com/a/",
)
DATE_TIME_VARIANTS = (
    "2026-10-01T00:00:00.000+00:00",  # +00:00 and .000: pydantic writes Z, drops .000
    "2026-10-01T00:00:00+00:00",
    "2026-10-01T00:00:00-05:00",
    "2026-10-01T00:00:00.5Z",
    "2026-10-01T00:00:00.123456789Z",  # nanoseconds: pydantic keeps microseconds
    "2026-10-01t00:00:00z",  # RFC 3339 permits lower-case T and Z
)
UUID_VARIANTS = ("00000000-0000-4000-8000-00000000ABCD",)
EMAIL_VARIANTS = ("Ops@Example.COM",)  # EmailStr lower-cases the domain


def _shape_variants(value: Any) -> tuple[Any, ...]:
    if not isinstance(value, str):
        return ()
    if _URL.match(value):
        return URL_VARIANTS
    if _DATE_TIME.match(value):
        return DATE_TIME_VARIANTS
    if _UUID.match(value):
        return UUID_VARIANTS
    if _EMAIL.match(value):
        return EMAIL_VARIANTS
    return ()


def variants(document: Document, *, limit_per_pointer: int | None = None) -> Iterator[Document]:
    """Schema-valid variants of ``document`` exercising each risky form.

    One leaf is replaced at a time, so a difference names the leaf that caused
    it. A leaf is only varied once per form, at the first pointer of each
    shape, because every ``format: uri`` field in one document is typed the
    same way and varying all of them multiplies runtime without adding
    coverage — except that a form may be valid at one field and not another,
    so each pointer is still tried until one accepts the form.
    """
    accepted: set[tuple[str, Any]] = set()
    for pointer, value in parity.leaves(document.payload):
        for substitute in _shape_variants(value):
            key = (_shape_key(value), substitute)
            if key in accepted:
                continue
            candidate = copy.deepcopy(document.payload)
            parity.replace_at(candidate, pointer, substitute)
            if not is_valid(document.schema, candidate):
                continue
            accepted.add(key)
            yield Document(
                document.schema,
                f"{document.name}{pointer}={substitute}",
                candidate,
                tool=document.tool,
                origin="variant",
                extra={"pointer": pointer, "form": substitute},
            )


def _shape_key(value: str) -> str:
    if _URL.match(value):
        return "url"
    if _DATE_TIME.match(value):
        return "date-time"
    if _UUID.match(value):
        return "uuid"
    return "email"


# ---------------------------------------------------------------------------
# The round trip.
# ---------------------------------------------------------------------------


def round_trip(
    document: Document, *, as_received: bool = False
) -> tuple[list[Difference], Refusal | None]:
    """Run ``document`` through its model; return what changed, or why it was refused.

    ``as_received`` dumps with :func:`dump_as_received` instead of the SDK's
    default settings.
    """
    model = grader_for(document)
    if model is None:
        return [], None
    try:
        parsed = parse_json(model, json.dumps(document.payload))
    except ValidationError as exc:
        return [], Refusal(
            document.schema,
            document.name,
            frozenset(error["type"] for error in exc.errors()),
            "; ".join(f"{'/'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3]),
        )
    except (TypeError, ValueError) as exc:
        # A model whose validator raises rather than refuses is a defect of
        # its own; the sweep records it and moves on.
        return [], Refusal(
            document.schema, document.name, frozenset({type(exc).__name__}), str(exc)[:200]
        )
    dumped = dump_json(model, parsed) if not as_received else dump_as_received(model, parsed)
    return differences(document.schema, document.name, document.payload, dumped), None


def grader_for(document: Document) -> Any:
    if document.tool is not None:
        model = _request_model_for(document.tool)
        if model is not None:
            return model
    return model_for(document.schema)


def has_model(document: Document) -> bool:
    return grader_for(document) is not None


def _resolver_for(schema: str) -> Any:
    state = _ensure_state(None)
    assert state is not None
    file = state.root.root / schema
    return _make_ref_resolver(state, file, json.loads(file.read_text()))


if __name__ == "__main__":  # pragma: no cover - exploratory report
    import sys
    from collections import Counter, defaultdict

    include_variants = "--variants" in sys.argv
    per_class: Counter[str] = Counter()
    distinct: dict[str, set[tuple[str, str]]] = defaultdict(set)
    received_distinct: dict[str, set[tuple[str, str]]] = defaultdict(set)
    received_class: Counter[str] = Counter()
    per_schema: dict[str, Counter[str]] = defaultdict(Counter)
    received_schema: dict[str, Counter[str]] = defaultdict(Counter)
    samples: dict[str, list[Difference]] = defaultdict(list)
    schema_samples: dict[tuple[str, str], Difference] = {}
    refusals: list[Refusal] = []
    unmodelled: set[str] = set()
    graded = 0
    origins: Counter[str] = Counter()
    for base in documents():
        origins[base.origin] += 1
        docs = [base, *(variants(base) if include_variants else [])]
        for doc in docs:
            if not has_model(doc):
                unmodelled.add(doc.schema)
                continue
            graded += 1
            diffs, refusal = round_trip(doc)
            if refusal is not None:
                refusals.append(refusal)
                continue
            for d in diffs:
                per_class[d.kind] += 1
                distinct[d.kind].add((d.schema, re.sub(r"/\d+(?=/|$)", "/*", d.pointer)))
                per_schema[d.schema][d.kind] += 1
                schema_samples.setdefault((d.schema, d.kind), d)
                if len(samples[d.kind]) < 8:
                    samples[d.kind].append(d)
            for d in round_trip(doc, as_received=True)[0]:
                received_class[d.kind] += 1
                received_distinct[d.kind].add((d.schema, re.sub(r"/\d+(?=/|$)", "/*", d.pointer)))
                received_schema[d.schema][d.kind] += 1
                schema_samples.setdefault((d.schema, "received:" + d.kind), d)
    print(
        f"graded documents: {graded}; unmodelled schemas: {len(unmodelled)}; "
        f"refusals: {len(refusals)}; origins: {dict(origins)}"
    )
    print("\n== per class ==")
    for kind, count in per_class.most_common():
        fields = distinct[kind]
        schemas = {schema for schema, _ in fields}
        print(f"  {kind:22} {count:6} hits; {len(fields):5} fields; {len(schemas):4} schemas")
    print("\n== per class, dumping as received (exclude_unset, nulls kept, by_alias) ==")
    for kind, count in received_class.most_common():
        fields = received_distinct[kind]
        schemas = {schema for schema, _ in fields}
        print(f"  {kind:22} {count:6} hits; {len(fields):5} fields; {len(schemas):4} schemas")
    print("\n== digest/echo-covered schemas with differences ==")
    for schema in sorted(per_schema):
        if schema in DIGEST_COVERED or schema in ECHO_COVERED:
            print(f"  {schema}: {dict(per_schema[schema])}")
    print("\n== all schemas with differences ==")
    for schema in sorted(per_schema):
        print(f"  {schema}: {dict(per_schema[schema])}")
    print("\n== digest/echo-covered schemas with differences, as received ==")
    for schema in sorted(received_schema):
        if schema in DIGEST_COVERED or schema in ECHO_COVERED:
            print(f"  {schema}: {dict(received_schema[schema])}")
    print("\n== one sample per (schema, class) ==")
    for (schema, kind), d in sorted(schema_samples.items()):
        print(f"  {schema} [{kind}] {d.pointer}: {d.before!r} -> {d.after!r}"[:200])
    print("\n== samples ==")
    for kind, items in samples.items():
        print(f"  [{kind}]")
        for d in items:
            print("    " + d.describe()[:220])
    print("\n== refusals ==")
    for r in refusals[:40]:
        print(f"  {r.schema} [{r.document}] {sorted(r.error_types)} {r.message[:160]}")
    print("\n== unmodelled ==")
    for s in sorted(unmodelled):
        print("  " + s)
