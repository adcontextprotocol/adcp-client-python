"""Convert schema validation failures into thrown errors and the AdCP
``VALIDATION_ERROR`` envelope used by server middleware."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from adcp.validation.schema_validator import (
    SchemaValidationError,
    ValidationIssue,
    _issue_to_wire,
    pointer_to_field,
)


@dataclass(frozen=True)
class ValidationErrorDetails:
    """Mirror of :attr:`SchemaValidationError.details` as a typed dataclass.

    Retained as a public type so callers can annotate their own
    intermediate structures without depending on the exception class.
    """

    tool: str
    side: str
    issues: list[ValidationIssue]


@dataclass(frozen=True)
class AdcpValidationErrorDetails:
    """Shape of ``adcp_error.details`` inside a server-side
    ``VALIDATION_ERROR`` envelope. Shipped so buyers can index every
    pointer programmatically instead of parsing the free-text message."""

    tool: str
    side: str
    issues: list[ValidationIssue]


def build_validation_error(
    tool: str, side: str, issues: list[ValidationIssue]
) -> SchemaValidationError:
    """Build a :class:`SchemaValidationError` carrying every failure.

    Strict-mode client hooks raise this so callers can inspect the full
    pointer list via ``.issues`` and the ``details`` dict.
    """
    return SchemaValidationError(tool, side, issues)


def build_adcp_validation_error_payload(
    tool: str, side: str, issues: list[ValidationIssue]
) -> dict[str, Any]:
    """Serialize issues into the kwargs expected by the AdCP ``Error`` model.

    Returns ``code``, ``message``, ``recovery`` and ``details``, plus
    ``issues`` and ``field`` when there is at least one issue — ready to splat
    into ``Error(**build_adcp_validation_error_payload(...))`` or into the
    server's ``adcp_error`` response envelope. The one owner of this envelope:
    both the jsonschema oracle and the pydantic projection reach the wire
    through here, so neither can disagree about its shape.

    Messages on every ``ValidationIssue`` are already sanitized (see
    :func:`adcp.validation.schema_validator._safe_message`) — they do
    not echo user-supplied values, so the wire envelope cannot leak
    bearer tokens / PII / prompt-injection strings from the offending
    payload back to the peer.
    """
    # ``field`` IS ``issues[0].pointer`` translated, and ``core/error.json``
    # types it as a "Field path ... in JSONPath-lite format (e.g.
    # 'packages[0].targeting')". The document root has no such spelling -- it
    # translates to the empty string -- so an entry pointing at the root cannot
    # lead the list without leaving ``field`` unpopulated, and a present
    # ``field`` is graded rather than advisory (``field_present`` on
    # ``adcp_error.field`` in the pinned conformance suite's
    # ``refine_finalize_exclusivity`` and ``proposal_finalize`` scenarios).
    #
    # A root entry still SHIPS. The pin's own envelope in
    # ``protocol/calling-an-agent.mdx`` lists ``anyOf``/``oneOf`` among the
    # legal ``issues[].keyword`` values and calls a union entry's ``variants[]``
    # "the fastest recovery path" -- what it never does is place one at the
    # document root, because every union it illustrates sits at a FIELD
    # (``/account``). A split root is the case it does not cover, so only the
    # ORDER is decided here. Stable, so each group keeps its own order --
    # including the oracle's shallowest-first sort among located entries, which
    # is the order the pin's example shows (``/account`` before
    # ``/brand/domain``).
    issues = sorted(issues, key=lambda issue: not pointer_to_field(issue.pointer))
    first = issues[0] if issues else None
    if first is not None:
        message = f"{tool} {side} failed schema validation at {first.pointer}: {first.message}"
    else:
        message = f"{tool} {side} failed schema validation"

    wire_issues = [_issue_to_wire(i) for i in issues]
    payload: dict[str, Any] = {
        "code": "VALIDATION_ERROR",
        "message": message,
        # core/error.json: details.issues is the pre-3.1 mirror of the
        # top-level `issues`, which new consumers SHOULD prefer.
        "details": {
            "tool": tool,
            "side": side,
            "issues": wire_issues,
        },
        # 3.2 producers MUST populate `recovery`; a malformed request is the
        # enum's own example of correctable.
        "recovery": "correctable",
    }
    if wire_issues:
        payload["issues"] = wire_issues
    if any(issue.keyword == "schema_unavailable" for issue in issues):
        from adcp.validation.envelope import SUPPORTED_WIRE_VERSIONS

        payload["code"] = "VERSION_UNSUPPORTED"
        payload["details"]["supported_versions"] = list(SUPPORTED_WIRE_VERSIONS)
    # core/error.json: when issues[] is present, `field` is issues[0].pointer
    # translated to JSONPath-lite. One translator so every lane agrees on the
    # format, VERSION_UNSUPPORTED included.
    #
    # PRESENT WHENEVER ``issues`` IS, including when the translation is the empty
    # string. The pin's rule is mechanical and its condition is the presence of
    # ``issues``, not the shape of the pointer: "When `issues` is present, sellers
    # MUST also populate `field` from `issues[0]` ... MUST (not SHOULD) so
    # consumers reading `field` get deterministic behavior across sellers".
    # ``field`` carries no ``minLength``, so the root's empty translation is a
    # legal value for it.
    #
    # This used to be gated on the translation being truthy, which omitted
    # ``field`` whenever EVERY issue sat at the document root -- measured on four
    # create_media_buy documents, all four. The gate's rationale was that the root
    # has no field path to name and ``field`` is not in error.json's ``required``
    # list; both are true and neither is the condition the pin states.
    # Deterministic presence is the thing a pre-3.1 consumer reading ``field``
    # alone cannot get from an absent key (salesagent-8qela.125).
    #
    # ``first is None`` still omits both, which is the case the pin does not
    # reach: no ``issues`` means no MUST and nothing to translate.
    if first is not None:
        payload["field"] = pointer_to_field(first.pointer)
    return payload
