"""Wire-emit projections for AdCP v3 :class:`Account` payloads.

This module ships two layers of projection:

1. **Pydantic-model helpers** — :func:`project_account_for_response`
   and :func:`project_business_entity_for_response` operate on the
   codegen'd wire :class:`adcp.types.Account` / :class:`BusinessEntity`
   models. Adopters that already hold a wire-shaped Pydantic model
   (e.g. echoing through a translator) call these to strip the
   write-only :attr:`BusinessEntity.bank` before serializing.

2. **Framework dataclass helpers** — :func:`to_wire_account`,
   :func:`to_wire_sync_accounts_row`, and
   :func:`to_wire_sync_governance_row` project the framework's
   internal :class:`adcp.decisioning.Account[TMeta]` /
   :class:`SyncAccountsResultRow` / :class:`SyncGovernanceResultRow`
   shapes to plain dicts ready for JSON serialization. These run on
   every emit path the framework controls; adopters typically don't
   call them directly.

The AdCP v3 spec marks two write-only paths that the framework MUST
strip on every response:

* :attr:`BusinessEntity.bank` — bank coordinates flow buyer→seller in
  ``sync_accounts`` requests but MUST NOT appear in any response
  payload.
* :attr:`GovernanceAgent.authentication.credentials` — bearer
  credentials the seller persists for outbound ``check_governance``
  calls but MUST NOT echo to the buyer or land in the idempotency
  replay cache.

The schemas describe both rules in docstrings; neither is structurally
enforced by Pydantic. The helpers here ARE the structural enforcement.
Defense-in-depth: even when an adopter returns a loosely-typed row
that smuggles a ``credentials`` field through ``cast`` / ``Any``, the
projection drops it.

Why a separate function instead of a Pydantic ``field_serializer``?
The framework's typed wire models are auto-generated from the spec
schema — patching them in-place would drift on every regen. Keeping
projection in adopter-callable / framework-internal helpers means the
wire shape stays exactly what the spec defines while the strips run
at the emit boundary.

Quickstart::

    from adcp.types import Account
    from adcp.decisioning import project_account_for_response

    # Adopter persists `account` with billing_entity.bank populated
    # for invoicing. On the response path:
    response_payload = project_account_for_response(account).model_dump(
        mode="json", exclude_none=True,
    )
    # `response_payload['billing_entity']` no longer carries 'bank'.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from adcp.error_sanitization import sanitize_account_authorization, sanitize_error_details

if TYPE_CHECKING:
    from adcp.decisioning.types import (
        Account as DecisioningAccount,
    )
    from adcp.decisioning.types import (
        SyncAccountsResultRow,
        SyncGovernanceResultRow,
    )
    from adcp.types import Account, BusinessEntity


def project_account_for_response(account: Account) -> Account:
    """Return a copy of ``account`` safe to serialize on a response.

    Strips :attr:`Account.billing_entity.bank` — the AdCP v3 spec
    marks bank details as write-only. Adopters that persist the full
    :class:`BusinessEntity` (with bank populated for invoicing) MUST
    project through this helper before serializing on any response.

    Returns the input unchanged when ``billing_entity`` is ``None``
    or ``billing_entity.bank`` is already absent — defensive copy
    via ``model_copy()`` so callers can mutate the returned object
    freely without touching the caller's input.

    The original ``account`` object is not modified.
    """
    if account.billing_entity is None or account.billing_entity.bank is None:
        return account.model_copy()
    safe_billing_entity = account.billing_entity.model_copy(update={"bank": None})
    return account.model_copy(update={"billing_entity": safe_billing_entity})


def project_business_entity_for_response(entity: BusinessEntity) -> BusinessEntity:
    """Return a copy of ``entity`` with ``bank`` cleared.

    Same posture as :func:`project_account_for_response` but
    operating on a :class:`BusinessEntity` directly — useful for
    adopters serializing standalone billing-entity payloads (admin
    APIs, brand-rights flows) that don't go through the
    :class:`Account` envelope.

    The original ``entity`` is not modified.
    """
    if entity.bank is None:
        return entity.model_copy()
    return entity.model_copy(update={"bank": None})


# ---------------------------------------------------------------------------
# Framework-dataclass → wire-dict projections
# ---------------------------------------------------------------------------


def _project_billing_entity(entity: Any) -> dict[str, Any] | None:
    """Project a :class:`BusinessEntity` to a wire dict, stripping
    ``bank`` per the schema's write-only constraint.

    Returns ``None`` when the entity carries only ``bank`` (no other
    fields). The wire schema requires ``legal_name`` on every emitted
    entity; a bank-only input would project to an empty dict that
    fails downstream validation, so the caller skips emission entirely
    in that case.

    Accepts any object with ``model_dump`` (Pydantic) OR a plain dict
    so adopters returning either shape on
    :class:`SyncAccountsResultRow` work without coercion.
    """
    if entity is None:
        return None
    if hasattr(entity, "model_dump"):
        dumped = entity.model_dump(mode="json", exclude_none=True)
    elif isinstance(entity, dict):
        dumped = {k: v for k, v in entity.items() if v is not None}
    else:
        return None
    dumped.pop("bank", None)
    return dumped if dumped else None


def _project_governance_agent(agent: Any) -> dict[str, Any] | None:
    """Project one ``governance_agents[i]`` to a wire dict carrying
    only the buyer-visible fields.

    The wire schema for response payloads exposes ``url`` and
    ``categories`` only — :attr:`GovernanceAgent.authentication`
    (bearing the write-only credentials) is stripped.

    Defense-in-depth: even if an adopter returns a loosely-typed
    record with an ``authentication`` key (Python type hints aren't
    enforced at runtime), the projection drops it. Same posture as
    the JS-side ``projectGovernanceAgent``.

    Returns ``None`` for unknown shapes so callers can drop the entry
    rather than emit ``{}`` onto the wire (silent corruption).
    """
    if hasattr(agent, "model_dump"):
        dumped = agent.model_dump(mode="json", exclude_none=True)
    elif isinstance(agent, dict):
        dumped = {k: v for k, v in agent.items() if v is not None}
    else:
        return None
    out: dict[str, Any] = {}
    if "url" in dumped:
        out["url"] = dumped["url"]
    if "categories" in dumped and dumped["categories"] is not None:
        out["categories"] = dumped["categories"]
    return out if out else None


def _maybe_dump(value: Any) -> Any:
    """Dump a Pydantic model to JSON-mode dict; pass through dicts /
    primitives unchanged. Used by the wire-emit projections to handle
    adopters that return either typed Pydantic models or plain
    dicts."""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def _project_account_authorization(value: Any) -> dict[str, Any] | None:
    """Project account authorization to its public metadata allowlist."""
    return sanitize_account_authorization(value)


def _enum_value(value: Any) -> Any:
    """Return ``value.value`` for Enum-like inputs, else the value
    unchanged. Used to project codegen'd enum types AND adopter-supplied
    string literals to the same wire string shape."""
    if hasattr(value, "value"):
        return value.value
    return value


def to_wire_account(account: DecisioningAccount[Any]) -> dict[str, Any]:
    """Project a framework :class:`Account[TMeta]` to the wire
    ``Account`` shape.

    Strips ``metadata`` and ``auth_info`` (framework-internal — never
    on the wire); renames ``id`` → ``account_id``; passes through
    wire-shaped optional fields. Strips ``billing_entity.bank``
    per the schema's write-only constraint.

    For ``governance_agents``, strips ``authentication`` from every
    element (defense-in-depth — TypeScript erasure means Python type
    hints can't enforce credentials-out at runtime, so the projection
    is explicit at every emit boundary).

    Used by the framework when emitting any response that surfaces an
    :class:`Account`. Adopters never call this directly — they return
    :class:`Account[TMeta]` from :meth:`AccountStore.resolve` /
    :meth:`AccountStore.list` and the framework projects.
    """
    wire: dict[str, Any] = {
        "account_id": account.id,
        "name": account.name,
        "status": account.status,
    }
    projected_entity = _project_billing_entity(account.billing_entity)
    if projected_entity is not None:
        wire["billing_entity"] = projected_entity
    if account.setup is not None:
        wire["setup"] = _maybe_dump(account.setup)
    if account.governance_agents is not None:
        projected_agents = [
            p
            for p in (_project_governance_agent(a) for a in account.governance_agents)
            if p is not None
        ]
        wire["governance_agents"] = projected_agents
    if account.account_scope is not None:
        scope = account.account_scope
        wire["account_scope"] = _enum_value(scope)
    if account.payment_terms is not None:
        terms = account.payment_terms
        wire["payment_terms"] = _enum_value(terms)
    if account.credit_limit is not None:
        wire["credit_limit"] = _maybe_dump(account.credit_limit)
    if account.rate_card is not None:
        wire["rate_card"] = account.rate_card
    if account.reporting_bucket is not None:
        wire["reporting_bucket"] = _maybe_dump(account.reporting_bucket)
    if account.authorization is not None:
        authorization = _project_account_authorization(account.authorization)
        if authorization is not None:
            wire["authorization"] = authorization
    return wire


def to_wire_sync_accounts_row(row: SyncAccountsResultRow) -> dict[str, Any]:
    """Project a :class:`SyncAccountsResultRow` to the wire shape
    returned by ``sync_accounts``.

    Applies the same ``billing_entity.bank`` strip as
    :func:`to_wire_account` — the wire schema marks bank coordinates
    write-only on EVERY response, not just ``list_accounts``.
    Adopters returning a row that spreads a DB record carrying
    ``bank`` (e.g., ``{**db.findByBrand(r.brand), 'action':
    'updated'}``) have it stripped before emit.

    Used by the framework when emitting ``sync_accounts`` responses.
    Adopters never call this directly — they return
    ``list[SyncAccountsResultRow]`` from
    :meth:`AccountStore.upsert` and the framework projects.
    """
    action = row.action
    status = row.status
    wire: dict[str, Any] = {
        "brand": _maybe_dump(row.brand),
        "operator": row.operator,
        "action": _enum_value(action),
        "status": _enum_value(status),
    }
    if row.account_id is not None:
        wire["account_id"] = row.account_id
    if row.name is not None:
        wire["name"] = row.name
    if row.billing is not None:
        wire["billing"] = row.billing
    projected_entity = _project_billing_entity(row.billing_entity)
    if projected_entity is not None:
        wire["billing_entity"] = projected_entity
    if row.account_scope is not None:
        scope = row.account_scope
        wire["account_scope"] = _enum_value(scope)
    if row.setup is not None:
        wire["setup"] = _maybe_dump(row.setup)
    if row.rate_card is not None:
        wire["rate_card"] = row.rate_card
    if row.payment_terms is not None:
        terms = row.payment_terms
        wire["payment_terms"] = _enum_value(terms)
    if row.credit_limit is not None:
        wire["credit_limit"] = _maybe_dump(row.credit_limit)
    if row.errors is not None:
        wire["errors"] = list(row.errors)
    if row.warnings is not None:
        wire["warnings"] = list(row.warnings)
    if row.sandbox is not None:
        wire["sandbox"] = row.sandbox
    return wire


def to_wire_sync_governance_row(row: SyncGovernanceResultRow) -> dict[str, Any]:
    """Project a :class:`SyncGovernanceResultRow` to the wire shape
    returned by ``sync_governance``.

    Critically: each ``governance_agents[i]`` is reduced to
    ``{url, categories?}`` only — the spec marks
    ``authentication.credentials`` write-only (the buyer sends the
    bearer; the seller persists it for outbound ``check_governance``
    calls but MUST NOT echo it back). The natural ``{**entry_agent}``
    echo idiom would compile silently against a loose return type
    and ship credentials over the wire AND into the idempotency
    replay cache, arming the buyer (and any subsequent caller hitting
    the same key) to impersonate the seller against the governance
    agent.

    Defense-in-depth: this dispatcher-level strip runs even when an
    adopter returns a loosely-typed row that spreads the input
    governance-agent record verbatim. Same posture as the JS-side
    ``toWireSyncGovernanceRow``.
    """
    wire: dict[str, Any] = {
        "account": _maybe_dump(row.account),
        "status": _enum_value(row.status),
    }
    if row.governance_agents is not None:
        wire["governance_agents"] = [
            p
            for p in (_project_governance_agent(a) for a in row.governance_agents)
            if p is not None
        ]
    if row.errors is not None:
        wire["errors"] = list(row.errors)
    return wire


# ---------------------------------------------------------------------------
# Defense-in-depth scrubber — runs at every wire-emit boundary
# ---------------------------------------------------------------------------


#: Methods whose response payload may carry credential-shaped fields.
#: The dispatcher gates the recursive scrubber on this set so unrelated
#: tools (``get_products``, ``get_signals``) skip the walk entirely —
#: the scrubber is O(n) in result size and not free for large catalogs.
#:
#: This set is intentionally broad: any tool whose response surfaces
#: an ``Account`` envelope (``billing_entity``, ``governance_agents``)
#: or a joined record carrying those keys belongs here.
CREDENTIAL_BEARING_METHODS: frozenset[str] = frozenset(
    {
        "list_accounts",
        "sync_accounts",
        "sync_governance",
        "create_media_buy",
        "update_media_buy",
        "get_media_buys",
        "sync_creatives",
        "list_creatives",
    }
)


def _scrub_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``value`` with credential-shaped fields stripped.

    Strips:

    * ``governance_agents[i].authentication`` — write-only credential.
    * ``billing_entity.bank`` — write-only bank coordinates.

    Walks recursively into nested dicts and lists. Returns a NEW dict —
    the input is not mutated, so callers (idempotency replay cache,
    middleware) can rely on input stability.
    """
    out: dict[str, Any] = {}
    for key, sub in value.items():
        if key == "governance_agents" and isinstance(sub, list):
            out[key] = [_scrub_governance_agent_dict(a) if isinstance(a, dict) else a for a in sub]
        elif key == "billing_entity" and isinstance(sub, dict):
            out[key] = {k: v for k, v in _scrub_value(sub).items() if k != "bank"}
        elif key == "authorization":
            authorization = _project_account_authorization(sub)
            if authorization is not None:
                out[key] = authorization
        elif key == "errors" and isinstance(sub, list):
            out[key] = [_scrub_error_dict(e) if isinstance(e, dict) else e for e in sub]
        elif key == "adcp_error" and isinstance(sub, dict):
            out[key] = _scrub_error_dict(sub)
        else:
            out[key] = _scrub_value(sub)
    return out


def _scrub_error_dict(error: dict[str, Any]) -> dict[str, Any]:
    """Strip private fields from code-specific error details."""
    out = {k: _scrub_value(v) for k, v in error.items() if k != "details"}
    code = error.get("code")
    details = error.get("details")
    if isinstance(code, str) and details:
        sanitized = sanitize_error_details(code, details)
        if sanitized:
            out["details"] = sanitized
    return out


def _scrub_governance_agent_dict(agent: dict[str, Any]) -> dict[str, Any]:
    """Strip ``authentication`` from a governance-agent dict and recurse
    into the remaining fields."""
    return {k: _scrub_value(v) for k, v in agent.items() if k != "authentication"}


def _scrub_value(value: Any) -> Any:
    """Recurse into dicts / lists; return primitives unchanged."""
    if isinstance(value, dict):
        return _scrub_dict(value)
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    return value


def strip_credentials_from_wire_result(method_name: str, result: Any) -> Any:
    """Strip write-only credential fields from a wire-shape result.

    Defense-in-depth boundary called by the dispatcher on every
    response that may surface an :class:`Account` envelope. Removes
    ``governance_agents[i].authentication`` and ``billing_entity.bank``
    recursively — the same fields the typed projections
    (:func:`to_wire_account`, :func:`to_wire_sync_governance_row`)
    strip when the adopter returns the framework's typed dataclasses.

    Adopters returning a loose dict (or a Pydantic model with
    ``extra='allow'``) bypass the typed projections; this scrubber
    catches them. Adopters returning the typed dataclasses get
    double-stripped — the second pass is a no-op since the typed
    projections already removed the fields.

    Method gate: the scrubber is O(n) in result size; we only run it
    on methods in :data:`CREDENTIAL_BEARING_METHODS`. Non-account
    methods (``get_products``, ``get_signals``, ``activate_signal``)
    skip the walk entirely and pass through unchanged.

    The input is not mutated — returns a new value.
    """
    if method_name not in CREDENTIAL_BEARING_METHODS:
        return result
    if isinstance(result, dict):
        return _scrub_dict(result)
    if isinstance(result, list):
        return [_scrub_value(v) for v in result]
    # Typed Pydantic response models pass through unchanged — the
    # response-side codegen'd shapes don't define ``authentication``
    # on ``GovernanceAgent`` or ``bank`` on the response-side
    # ``BusinessEntity``, so the schema enforces the strip
    # structurally. Dumping-and-scrubbing a model would force
    # downstream callers to lose typed-model identity for no
    # security gain. The leak vector is loose dicts and Pydantic
    # ``extra='allow'`` models that smuggle credentials past the
    # codegen schema; both arrive as ``dict`` after the adopter's
    # method returns or via the registry's ``model_dump`` path.
    return result


__all__ = [
    "CREDENTIAL_BEARING_METHODS",
    "project_account_for_response",
    "project_business_entity_for_response",
    "strip_credentials_from_wire_result",
    "to_wire_account",
    "to_wire_sync_accounts_row",
    "to_wire_sync_governance_row",
]
