"""Every response branch carries the root ``allOf`` protocol envelope (#1136).

Each AdCP response schema composes ``core/protocol-envelope.json`` at its root
via ``allOf``, so ``status``, ``task_id``, ``message``, ``context_id``,
``replayed``, ``timestamp``, ``push_notification_config``,
``governance_context``, ``context``, ``payload`` and ``adcp_error`` are part of
every response's contract — on the success branch, the error branch and the submitted
branch alike. The code generator used to attach :class:`ProtocolEnvelope` only to
the ``status: submitted`` branch, leaving those fields untyped on the rest: a
seller that set one wrote a pydantic extra and a buyer that read one got an
``AttributeError``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel

from adcp._version import _read_packaged_version
from adcp.types import ProtocolEnvelope, canonical_creative
from adcp.types import aliases as aliases_module
from adcp.types.domains.enums.task_status import TaskStatus
from adcp.validation.version import resolve_bundle_key

_ENVELOPE_FIELDS = frozenset(ProtocolEnvelope.model_fields)


def _schema_dir() -> Path:
    return Path("schemas") / "cache" / resolve_bundle_key(_read_packaged_version())


def _public_response_classes() -> list[tuple[str, type[BaseModel]]]:
    """Public ``*SuccessResponse`` / ``*ErrorResponse`` aliases that are classes.

    Non-discriminated ``oneOf`` responses also expose bare union aliases, which
    are ``types.UnionType`` rather than classes; those are out of scope here.
    """
    found: list[tuple[str, type[BaseModel]]] = []
    for name in sorted(dir(aliases_module)):
        if name.startswith("_") or not name.endswith(("SuccessResponse", "ErrorResponse")):
            continue
        obj = getattr(aliases_module, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel):
            found.append((name, obj))
    return found


def test_public_response_aliases_inherit_protocol_envelope() -> None:
    """The alias surface adopters import must carry the envelope."""
    classes = _public_response_classes()
    assert len(classes) > 20, "expected the full public response-alias surface"

    missing = [name for name, obj in classes if not issubclass(obj, ProtocolEnvelope)]
    assert missing == [], "response aliases missing the ProtocolEnvelope base: " + ", ".join(
        missing
    )


def test_public_response_aliases_declare_every_envelope_field() -> None:
    """Envelope fields are declared fields, never ``extra`` bags."""
    for name, obj in _public_response_classes():
        assert _ENVELOPE_FIELDS <= set(obj.model_fields), name


@pytest.mark.parametrize(
    "relative",
    [
        "account/get_account_financials_response.py",
        "account/sync_accounts_response.py",
        "brand/acquire_rights_response.py",
        "brand/get_brand_identity_response.py",
        "brand/get_rights_response.py",
        "brand/update_rights_response.py",
        "content_standards/calibrate_content_response.py",
        "content_standards/get_content_standards_response.py",
        "content_standards/get_media_buy_artifacts_response.py",
        "content_standards/validate_content_delivery_response.py",
        "creative/get_creative_features_response.py",
        "creative/preview_creative_response.py",
        "creative/sync_creatives_response.py",
        "media_buy/build_creative_response.py",
        "media_buy/create_media_buy_response.py",
        "media_buy/log_event_response.py",
        "media_buy/provide_performance_feedback_response.py",
        "media_buy/sync_audiences_response.py",
        "media_buy/sync_catalogs_response.py",
        "media_buy/sync_event_sources_response.py",
        "media_buy/update_media_buy_response.py",
        "signals/activate_signal_response.py",
    ],
)
def test_generated_branches_match_their_root_schema_composition(relative: str) -> None:
    """Every emitted branch mirrors whether its ROOT schema composes the envelope."""
    import importlib
    import re

    module_name = "adcp.types.domains." + relative.removesuffix(".py").replace("/", ".")
    module = importlib.import_module(module_name)

    schema_path = _schema_dir() / Path(relative).with_suffix(".json").as_posix().replace("_", "-")
    schema = json.loads(schema_path.read_text())
    root_refs = [part.get("$ref", "") for part in schema.get("allOf", []) if isinstance(part, dict)]
    root_composes_envelope = any(ref.endswith("core/protocol-envelope.json") for ref in root_refs)
    assert root_composes_envelope, f"{relative}: fixture assumes a root protocol envelope"

    # ``__all__`` is emitted as ``[union_alias, *numbered_branches, *nested]``; the
    # branches are exactly the ``<Base><n>`` names, never the nested helper models.
    base_name = module.__all__[0]
    branch_pattern = re.compile(rf"{re.escape(base_name)}\d+$")
    branches = [
        getattr(module, name)
        for name in module.__all__
        if branch_pattern.fullmatch(name) is not None
    ]
    assert len(branches) == len(schema["oneOf"]), f"{relative}: arm count drifted from the schema"
    for branch in branches:
        assert issubclass(branch, ProtocolEnvelope), f"{relative}: {branch.__name__}"


def test_error_branch_can_carry_envelope_state() -> None:
    """The error branch of a ``oneOf`` is an envelope too, not a bare payload."""
    from adcp.types.aliases import CreateMediaBuyErrorResponse

    error = CreateMediaBuyErrorResponse.model_validate(
        {
            "errors": [{"code": "INVALID_BUDGET", "message": "too low"}],
            "status": "rejected",
            "task_id": "task_1",
            "replayed": True,
        }
    )
    assert error.status == "rejected"
    assert error.task_id == "task_1"
    assert error.replayed is True
    assert error.model_extra == {}


def test_replayed_and_status_round_trip_as_declared_fields() -> None:
    """Idempotency rule 4: a replayed response sets ``replayed`` on the envelope."""
    from adcp.types.aliases import SyncCreativesSuccessResponse

    response = SyncCreativesSuccessResponse.model_validate({"creatives": []})
    assert response.replayed is False
    assert response.model_extra == {}

    response.replayed = True
    dumped = response.model_dump(mode="json")
    assert dumped["replayed"] is True
    # ``status`` is required on every task response envelope and now serializes
    # from a declared field rather than being dropped.
    assert dumped["status"] == "completed"

    reparsed = SyncCreativesSuccessResponse.model_validate(dumped)
    assert reparsed.replayed is True
    assert reparsed.model_extra == {}


def test_envelope_state_used_to_land_in_extra() -> None:
    """Pin the exact regression: envelope keys are not ``extra`` any more."""
    from adcp.types.aliases import LogEventSuccessResponse

    response = LogEventSuccessResponse.model_validate(
        {
            "events_received": 0,
            "events_processed": 0,
            "status": "completed",
            "replayed": True,
            "context_id": "ctx_1",
        }
    )
    assert response.model_extra == {}
    assert response.context_id == "ctx_1"


# ---- canonical_creative public models ----


_CANONICAL_RESPONSES = (
    "GetProductsResponse",
    "CreateMediaBuyResponse1",
    "CreateMediaBuyResponse2",
    "CreateMediaBuyResponse3",
    "UpdateMediaBuyResponse1",
    "UpdateMediaBuyResponse2",
    "UpdateMediaBuyResponse3",
    "ListCreativesResponse",
    "GetMediaBuysResponse",
    "GetMediaBuyDeliveryResponse",
    "GetCreativeDeliveryResponse",
)


@pytest.mark.parametrize("name", _CANONICAL_RESPONSES)
def test_canonical_response_preserves_envelope_ancestry(name: str) -> None:
    """A canonical response inherits the generated model, envelopes included.

    The ancestry used to be re-declared: ``_canonical_clone`` copied fields and
    had to list the envelopes as extra bases to keep ``issubclass`` true. The
    models are real subclasses now, so the ancestry is transitive and nothing
    re-declares it — which is the thing this test must keep proving.
    """
    model = getattr(canonical_creative, name)
    assert issubclass(model, ProtocolEnvelope), name
    assert _ENVELOPE_FIELDS <= set(model.model_fields), name


@pytest.mark.parametrize("name", [*_CANONICAL_RESPONSES, "GetProductsRequest", "Product"])
def test_canonical_model_keeps_the_boundary_extra_policy(name: str) -> None:
    """The generated base must not override ``CanonicalBoundaryModel``'s config.

    Pydantic merges ``model_config`` left to right across bases, so a generated
    model listed AFTER the boundary model would reinstate
    :class:`AdCPBaseModel`'s ``extra="ignore"`` and silently drop
    caller-supplied extension keys. ``CanonicalBoundaryModel`` is therefore
    always the last base.
    """
    model = getattr(canonical_creative, name)
    assert model.model_config["extra"] == "allow", name


def test_canonical_model_still_round_trips_extension_keys() -> None:
    """The config regression above is observable through an unknown key."""
    request = canonical_creative.GetProductsRequest.model_validate(
        {"promoted_offering": "test", "buying_mode": "brief"}
    )
    assert request.promoted_offering == "test"


def test_canonical_response_keeps_its_schema_pinned_status() -> None:
    """Adding the envelope base must not relax an branch's pinned ``status``."""
    response = canonical_creative.UpdateMediaBuyResponse1.model_validate(
        {"media_buy_id": "mb_1", "revision": 2}
    )
    assert response.status == "completed"
    assert (
        canonical_creative.UpdateMediaBuyResponse1.model_fields["status"].annotation
        == Literal["completed"]
    )


def test_canonical_responses_construct_without_status() -> None:
    """``status`` is defaulted at runtime, so construction must not demand it.

    The static half of this — that the stub's synthesized ``__init__`` agrees —
    is pinned in ``tests/type_checks/response_envelope_fields.py``.
    """
    listed = canonical_creative.ListCreativesResponse(
        creatives=[],
        query_summary={"total_matching": 0, "returned": 0},
        pagination={"has_more": False},
    )
    assert listed.status == TaskStatus.completed

    buys = canonical_creative.GetMediaBuysResponse(media_buys=[])
    assert buys.status == TaskStatus.completed

    accepted = canonical_creative.UpdateMediaBuyResponse3(task_id="task_2")
    assert accepted.status == TaskStatus.submitted


def test_canonical_response_status_is_the_generated_one() -> None:
    """Every concrete canonical response carries its generated ``status`` exactly.

    This replaces ``test_canonical_response_stub_status_matches_runtime``, whose
    subject was ``canonical_creative.pyi``: the stub declared a private
    ``_CanonicalResponseEnvelope`` bridge relaxing ``status`` to ``Any`` so the
    branches' ``Literal`` pins would not trip mypy's Liskov check, and this test
    pinned every concrete response's hand-written re-declaration against the
    runtime so the bridge could not leak ``Any`` to adopters.

    The stub is deleted and the canonical responses are real subclasses of the
    generated wire models, so there is no second declaration of ``status`` to
    drift from the first — the branch's pinned ``Literal`` is inherited. The
    property worth grading is therefore the one that now carries the contract:
    a canonical response's ``status`` annotation must be IDENTICAL to the
    annotation of the generated class it refines. A redeclaration that widened
    or relaxed it would fail here, which is the leak the old test was watching.
    """
    runtime_responses = {
        name: model
        for name in dir(canonical_creative)
        if not name.startswith("_")
        and isinstance(model := getattr(canonical_creative, name), type)
        and issubclass(model, ProtocolEnvelope)
        # ``ProtocolEnvelope`` itself is imported into this namespace as a base;
        # only the canonical models built on it are in scope here.
        and issubclass(model, canonical_creative.CanonicalBoundaryModel)
    }
    assert runtime_responses, "no canonical responses discovered"

    for name, model in runtime_responses.items():
        generated = next(
            base
            for base in model.__bases__
            if base is not canonical_creative.CanonicalBoundaryModel
        )
        assert generated.__module__.startswith("adcp.types.domains."), (
            f"{name} does not refine a generated wire model (base {generated!r}); "
            f"a canonical response must be a subclass, never a copy"
        )
        assert (
            model.model_fields["status"].annotation == generated.model_fields["status"].annotation
        ), (
            f"{name}.status is {model.model_fields['status'].annotation!r} but the "
            f"generated {generated.__name__}.status is "
            f"{generated.model_fields['status'].annotation!r}"
        )
        # The other half of the old stub contract: ``status`` is defaulted, so a
        # plain construction never has to supply it.
        assert not model.model_fields["status"].is_required(), name
