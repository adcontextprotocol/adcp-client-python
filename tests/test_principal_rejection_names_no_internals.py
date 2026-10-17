"""``sync_principal``'s rejection names the buyer's field, not our classes.

``core/error.json`` is what ``result.errors[]`` items are (``$ref`` to
``core/error.json``), so the string lands in ``error.message``. The MUST-NOT on
producer-internal context sits on ``buyer_reason.message``, not here — but the
spec never asks for a class name in ``message`` either, and a path that names a
generated union branch does not resolve against what the peer sent.

Two leaks, both measured on this path before the fix: pydantic's ``msg`` for a
type mismatch reads "instance of Configuration", and its ``loc`` carries the
selected union branch's class name (``AgentReportingDestination1``) for any
union, with no wrapping validator involved.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from adcp.server.principal import _first_message
from adcp.types import SyncPrincipalRequest

#: Long enough to satisfy the field's own minLength, so a probe reaches the
#: content validators instead of stopping on idempotency_key.
IDEM = "idem-0123456789abcdef"

#: Class names reachable on this path. Each is a generated model or a pydantic
#: wrapper spelling; none is a field a buyer could act on.
INTERNAL_NAMES = (
    "AgentReportingDestination",
    "AgentNotificationConfig",
    "AgentDeclarations",
    "Configuration",
    "SyncPrincipalRequest",
    "ExtensionObject",
    "function-after",
)

#: Each rejects, and each reaches a CONTENT validator rather than requiredness
#: on idempotency_key — verified by ``test_every_payload_reaches_a_rejection``.
REJECTED_PAYLOADS = [
    pytest.param({"idempotency_key": IDEM, "configuration": "x"}, id="configuration-not-object"),
    pytest.param(
        {"idempotency_key": IDEM, "configuration": {"declarations": "x"}},
        id="declarations-not-object",
    ),
    pytest.param(
        {"idempotency_key": IDEM, "configuration": {"reporting_destinations": [{}]}},
        id="union-branch-missing-field",
    ),
    pytest.param(
        {"idempotency_key": IDEM, "configuration": {"reporting_destinations": [{"kind": "x"}]}},
        id="union-branch-unknown-shape",
    ),
    pytest.param(
        {"idempotency_key": IDEM, "configuration": {"notification_configs": [{}]}},
        id="notification-config-missing-field",
    ),
    pytest.param(
        {"idempotency_key": IDEM, "configuration": {"declarations": {"unknown_key": "v"}}},
        id="extra-property",
    ),
    pytest.param("not-an-object", id="scalar-payload"),
]


def _message_for(payload: object) -> str:
    try:
        SyncPrincipalRequest.model_validate(payload)
    except ValidationError as error:
        return _first_message(error, payload)
    raise AssertionError("payload validated when the test needs it rejected")


@pytest.mark.parametrize("payload", REJECTED_PAYLOADS)
def test_rejection_names_no_internal_class(payload):
    """Asks: does a producer-internal name reach the peer? Nothing more.

    It does NOT establish that the message is still ACTIONABLE. Those are
    adjacent questions, and answering this one was read as answering both:
    the projection that removed these names also removed the rule name a
    conjunct failure carries, and only the upstream delta caught it
    (salesagent-8qela.34). A green result here means no leak, not no loss.
    """
    message = _message_for(payload)
    for name in INTERNAL_NAMES:
        assert name not in message, f"{name!r} reached the peer: {message}"


@pytest.mark.parametrize("payload", REJECTED_PAYLOADS)
def test_rejection_path_resolves_against_the_payload(payload):
    """Every path segment must be a key or index the peer actually sent.

    A union branch name is a plain string indistinguishable from a key, so
    this walks the document instead of matching spellings. The terminal
    segment is exempt: a required-field failure names a key that is absent by
    definition.
    """
    message = _message_for(payload)
    location = message.split(":", 1)[0] if ":" in message else ""
    if not location:
        return

    node = payload
    segments = [s for s in location.replace("[", ".").replace("]", "").split(".") if s]
    for segment in segments[:-1]:
        if isinstance(node, dict):
            assert segment in node, f"{segment!r} is not a key the peer sent: {location}"
            node = node[segment]
        elif isinstance(node, list):
            assert segment.isdigit() and int(segment) < len(node), location
            node = node[int(segment)]
        else:
            raise AssertionError(f"{location} descends into a {type(node).__name__}")


def test_every_payload_reaches_a_rejection():
    """Guards against a probe that never gets past requiredness.

    Four earlier iterations of this measurement were stopped by
    ``idempotency_key`` requiredness, then its minLength, then
    ``subscriber_id``, then ``event_types`` — each time reporting a clean zero
    for a path it had not reached.
    """
    messages = [_message_for(p.values[0]) for p in REJECTED_PAYLOADS]
    assert len(messages) == len(REJECTED_PAYLOADS)
    assert not [m for m in messages if m.startswith("idempotency_key")], messages


def test_the_location_is_jsonpath_lite_not_a_pointer():
    """``error.field``'s format, so the message agrees with it."""
    message = _message_for(
        {"idempotency_key": IDEM, "configuration": {"reporting_destinations": [{}]}}
    )
    assert message.startswith("configuration.reporting_destinations[0]."), message
    assert "/" not in message.split(":", 1)[0]
