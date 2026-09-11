"""Adopter pattern: read and write protocol-envelope state on a response arm.

Every AdCP response schema composes ``core/protocol-envelope.json`` at its
root, so ``status``, ``task_id``, ``replayed`` and friends belong to every
response arm — the success arm, the error arm and the submitted arm alike.
Before #1136 the generator attached the envelope base only to the submitted
arm, so a seller setting ``response.replayed = True`` on any of the other 19
success shapes wrote a pydantic *extra*, and a buyer reading it got an
``AttributeError``. Statically the attribute did not exist at all.

This file pins the typed surface: the envelope fields must be visible to a type
checker on ordinary success and error arms, on the canonical-boundary clones,
and through ``ProtocolEnvelope`` as a common ancestor of the arms of one
``oneOf`` — which previously had no shared base below ``AdcpVersionEnvelope``.
"""

from __future__ import annotations

from typing import Literal

from typing_extensions import assert_type

from adcp.types import (
    CreateMediaBuyErrorResponse,
    CreateMediaBuyResponse1,
    GeneratedTaskStatus,
    GetMediaBuysResponse,
    GetProductsResponse,
    ListCreativesResponse,
    ProtocolEnvelope,
    SyncCreativesResponse1,
    UpdateMediaBuyResponse3,
)


def echo_envelope(response: ProtocolEnvelope) -> str | None:
    """A boundary can take any response arm through the shared ancestor."""
    return response.task_id


# --- A plain success arm carries envelope state ---

synced = SyncCreativesResponse1(creatives=[])
synced.replayed = True
synced.task_id = "task_1"
synced.context_id = "ctx_1"

replayed: bool | None = synced.replayed
task_id: str | None = synced.task_id
assert replayed is True
assert task_id == "task_1"
assert echo_envelope(synced) == "task_1"

# --- The canonical-boundary clones keep their generated source's ancestry ---

created = CreateMediaBuyResponse1(
    media_buy_id="mb_1",
    status="completed",
    confirmed_at=None,
    revision=1,
    packages=[],
)
assert echo_envelope(created) is None

# The error arm of the same ``oneOf`` is an envelope too, so both arms share a
# base a seller can annotate against.
rejected = CreateMediaBuyErrorResponse.model_validate(
    {"errors": [{"code": "INVALID_BUDGET", "message": "too low"}]}
)
rejected.replayed = True
assert echo_envelope(rejected) is None

# --- ``status`` keeps its exact runtime type on every concrete response ---
#
# The stub ancestor relaxes ``status`` to ``Any`` purely to make the arms'
# narrowing legal (see ``_CanonicalResponseEnvelope``). ``assert_type`` is what
# proves that relaxation does not leak: an annotation like ``pinned: str =
# created.status`` would pass silently from ``Any`` and prove nothing.

# Pinned to the single synchronous outcome by the 3.2 schema arm.
assert_type(created.status, Literal["completed"])

# Ordinary arm — the wide envelope enum, no narrowing in the schema.
products = GetProductsResponse()
assert_type(products.status, GeneratedTaskStatus)

# Async task-envelope arm — pinned to the submitted member.
submitted = UpdateMediaBuyResponse3(task_id="task_1")
assert_type(submitted.status, Literal[GeneratedTaskStatus.submitted])


# The envelope's own view stays wide, which is what makes the arms' narrowing
# a genuine refinement rather than a redefinition.
def read_envelope_status(response: ProtocolEnvelope) -> GeneratedTaskStatus:
    return response.status


assert read_envelope_status(created) == "completed"

# --- ``status`` is defaulted, so construction must not demand it ---
#
# ``status`` has a default on every runtime response. If the stub declared it
# without ``= ...`` the synthesized ``__init__`` would require it, and these
# three plain constructions would fail to type-check even though the runtime
# model defaults the field. (``ListCreativesResponse`` also has required
# ``query_summary``/``pagination`` fields the stub does not enumerate, so only
# the ``status`` half of its signature is asserted here; the runtime
# construction is covered in tests/test_protocol_envelope_inheritance.py.)

listed = ListCreativesResponse(creatives=[])
assert_type(listed.status, GeneratedTaskStatus)

buys = GetMediaBuysResponse(media_buys=[])
assert_type(buys.status, GeneratedTaskStatus)
assert buys.status == "completed"

accepted = UpdateMediaBuyResponse3(task_id="task_2")
assert_type(accepted.status, Literal[GeneratedTaskStatus.submitted])
assert accepted.status == "submitted"
