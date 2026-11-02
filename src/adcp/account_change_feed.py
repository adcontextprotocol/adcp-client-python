"""Account change feed records (AdCP 3.3, experimental ``account.change_feed``).

A seller that declares the change feed serves ``list_account_changes`` and
may push ``account.change_recorded`` notifications. Three record rules apply,
and this module is where the SDK checks them:

* **changed_paths.** A record whose ``origin.kind`` is ``adcp`` or
  ``seller_operator`` carries a non-empty ``changed_paths``: RFC 6901 pointers
  naming the material fields that changed, or the root pointer ``""``
  (:data:`ROOT_POINTER`) for a whole-resource create, delete or purge. The
  seller processed those mutations, so it knows what it changed.
* **idempotency_key.** An ``adcp`` record may echo the ``idempotency_key`` of
  the request that produced it, but only to the principal that sent that
  request. Every other reader of the account gets the record without it, and
  ``account.change_recorded`` notifications never carry it.
* **resource_revision.** For a creative, ``resource_revision`` mirrors the
  creative's ``seller_revision`` after the change, in the same JSON type.

The originating principal is not on the wire, so the seller attaches it to the
record with :func:`attribute_account_change` when it builds the page. The
server's ``list_account_changes`` dispatch then projects every record for the
caller: an attributed ``adcp`` record keeps its key only when the caller is
its originator, and any other record loses it. Unattributed records always
lose it, so the projection fails closed::

    from adcp.account_change_feed import ROOT_POINTER, attribute_account_change

    # When the mutation commits, persist the originator with the record:
    originator = principal_ref_for(ctx)  # ctx: the mutating request's ToolContext

    # When serving list_account_changes:
    changes = [attribute_account_change(row.record, row.originator) for row in page]
    return ListAccountChangesResponse(status="completed", changes=changes, ...)

Build notifications from the same record with
:func:`account_change_recorded_payload`, or pass the record straight to
:meth:`adcp.webhook_sender.WebhookSender.prepare_account_change_recorded`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, TypeVar

from pydantic import BaseModel, PrivateAttr

from adcp._principal_ref import PrincipalRef
from adcp.types.domains.core.account_change import AccountChange

__all__ = [
    "ROOT_POINTER",
    "AccountChangeRecordError",
    "AttributedAccountChange",
    "account_change_recorded_payload",
    "attribute_account_change",
    "is_account_change_record",
    "json_pointer",
    "mirror_creative_revision",
    "principal_ref_for",
    "project_account_change",
    "project_list_account_changes_response",
    "validate_account_change",
]

#: RFC 6901 root pointer: the whole resource was created, deleted or purged.
ROOT_POINTER: Final = ""

#: Origins whose records must list what they changed.
_PATHS_REQUIRED_ORIGINS: Final = frozenset({"adcp", "seller_operator"})

_RECORD_ONLY_KEYS: Final = frozenset({"origin", "repair"})

_ChangeT = TypeVar("_ChangeT", bound="AccountChange | Mapping[str, Any]")


class AccountChangeRecordError(ValueError):
    """An account change record breaks an AdCP 3.3 change-feed rule.

    ``pointer`` is the JSON Pointer of the offending field, relative to the
    payload the record was checked in (``/changes/3/changed_paths`` in a
    ``list_account_changes`` response).
    """

    def __init__(self, message: str, *, pointer: str = "") -> None:
        super().__init__(message)
        self.pointer = pointer


class AttributedAccountChange(AccountChange):
    """An :class:`AccountChange` that knows, off the wire, who originated it.

    The originating principal is a private attribute: it never serializes and
    does not affect validation. Build one with :func:`attribute_account_change`.
    """

    _originating_principal: PrincipalRef | None = PrivateAttr(default=None)

    @property
    def originating_principal(self) -> PrincipalRef | None:
        """The principal that sent the AdCP request behind this change, if known."""
        return self._originating_principal


def json_pointer(*tokens: str | int) -> str:
    """Build an RFC 6901 JSON Pointer from unescaped reference tokens.

    ``json_pointer("packages", 0, "budget")`` is ``"/packages/0/budget"``;
    ``~`` and ``/`` inside a token are escaped. With no tokens the result is
    the root pointer ``""``.
    """
    return "".join("/" + str(token).replace("~", "~0").replace("/", "~1") for token in tokens)


def principal_ref_for(
    principal: PrincipalRef | str | Any | None, *, tenant_id: str | None = None
) -> PrincipalRef | None:
    """The :class:`~adcp.PrincipalRef` of a caller, or ``None`` when it is unknown.

    Accepts a ``PrincipalRef``, a principal id string (scoped to ``tenant_id``)
    or a :class:`~adcp.server.ToolContext`-shaped object exposing
    ``caller_identity`` and ``tenant_id``. A context without an authenticated
    caller yields ``None``.
    """
    if principal is None or isinstance(principal, PrincipalRef):
        return principal
    if isinstance(principal, str):
        return PrincipalRef(tenant_id or None, principal) if principal else None
    if hasattr(principal, "caller_identity"):
        caller = principal.caller_identity
        if not isinstance(caller, str) or not caller:
            return None
        tenant = getattr(principal, "tenant_id", None)
        return PrincipalRef(tenant if isinstance(tenant, str) and tenant else None, caller)
    return PrincipalRef.coerce(principal, tenant_id=tenant_id)


def _field(record: Any, name: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _origin_kind(record: Any) -> str | None:
    origin = _field(record, "origin")
    if origin is None:
        return None
    kind = _field(origin, "kind")
    if kind is None:
        return None
    return str(getattr(kind, "value", kind))


def _describe(record: Any) -> str:
    change_id = _field(record, "change_id")
    return f"account change {change_id!r}" if isinstance(change_id, str) else "account change"


def _require_changed_paths(change: Any, *, pointer: str) -> None:
    kind = _origin_kind(change)
    if kind in _PATHS_REQUIRED_ORIGINS and not _field(change, "changed_paths"):
        raise AccountChangeRecordError(
            f"{_describe(change)} has origin.kind {kind!r} but no changed_paths: list the "
            'RFC 6901 pointers of the fields it changed, or [""] for a whole-resource '
            "create, delete or purge",
            pointer=f"{pointer}/changed_paths",
        )


def validate_account_change(
    change: AccountChange | Mapping[str, Any], *, pointer: str = ""
) -> None:
    """Check a record against the change-feed rules the generated model does not enforce.

    Raises :class:`AccountChangeRecordError` when an ``adcp`` or
    ``seller_operator`` record has no non-empty ``changed_paths``, or when a
    record that is not ``adcp``-originated carries an ``idempotency_key``.
    ``pointer`` locates the record in its payload for the error.
    """
    _require_changed_paths(change, pointer=pointer)
    kind = _origin_kind(change)
    if kind != "adcp" and _field(change, "idempotency_key") is not None:
        raise AccountChangeRecordError(
            f"{_describe(change)} carries idempotency_key but origin.kind is {kind!r}; "
            "only adcp-originated records echo the originating request's key",
            pointer=f"{pointer}/idempotency_key",
        )


def attribute_account_change(
    change: AccountChange | Mapping[str, Any],
    originating_principal: PrincipalRef | str | Any | None,
    *,
    tenant_id: str | None = None,
) -> AttributedAccountChange:
    """Validate a change record and attach the principal that originated it.

    ``originating_principal`` is the principal that sent the AdCP request
    behind an ``adcp`` change: a ``PrincipalRef``, a principal id (scoped to
    ``tenant_id``), or the mutating request's ``ToolContext``. Pass ``None``
    for other origins, or when the originator is unknown; the record's
    ``idempotency_key`` is then withheld from every reader.

    Raises :class:`AccountChangeRecordError` for a record that breaks the
    ``changed_paths`` or ``idempotency_key`` rules, and
    :class:`pydantic.ValidationError` for one that fails the schema model.
    """
    data = (
        change.model_dump(mode="json", exclude_none=True)
        if isinstance(change, BaseModel)
        else dict(change)
    )
    record = AttributedAccountChange.model_validate(data)
    validate_account_change(record)
    record._originating_principal = principal_ref_for(originating_principal, tenant_id=tenant_id)
    return record


def _echoes_key_to(change: Any, reader: PrincipalRef | None) -> bool:
    if reader is None or _origin_kind(change) != "adcp":
        return False
    if not isinstance(change, AttributedAccountChange):
        return False
    originator = change.originating_principal
    return originator is not None and originator == reader


def project_account_change(
    change: AccountChange | Mapping[str, Any], *, reader: PrincipalRef | str | Any | None
) -> dict[str, Any]:
    """The wire form of one record as served to ``reader``.

    ``idempotency_key`` survives only on an :class:`AttributedAccountChange`
    with ``origin.kind == "adcp"`` whose originating principal equals
    ``reader``. Every other record, including a plain mapping or an
    unattributed model, is returned without it.
    """
    if isinstance(change, BaseModel):
        data = change.model_dump(mode="json", exclude_none=True)
    elif isinstance(change, Mapping):
        data = dict(change)
    else:
        raise TypeError(f"expected an account change record, got {type(change).__name__}")
    if "idempotency_key" in data and not _echoes_key_to(change, principal_ref_for(reader)):
        del data["idempotency_key"]
    return data


def project_list_account_changes_response(
    result: Any,
    *,
    reader: PrincipalRef | str | Any | None,
    require_changed_paths: bool = True,
) -> Any:
    """Project a ``list_account_changes`` result for the caller ``reader``.

    Each record goes through :func:`project_account_change`, which also drops
    a key a non-``adcp`` record should not carry. With
    ``require_changed_paths`` (AdCP 3.3 and later), an ``adcp`` /
    ``seller_operator`` record without non-empty ``changed_paths`` raises
    :class:`AccountChangeRecordError`, so a non-conformant page is refused
    instead of shipping. A model
    result comes back as a JSON-mode dict; a result without ``changes``
    (an error envelope) comes back unchanged.
    """
    reader_ref = principal_ref_for(reader)
    if isinstance(result, BaseModel):
        records = getattr(result, "changes", None)
        if records is None:
            return result
        data = result.model_dump(mode="json", exclude_none=True, exclude={"changes"})
    elif isinstance(result, Mapping):
        records = result.get("changes")
        if not isinstance(records, (list, tuple)):
            return result
        data = dict(result)
    else:
        return result
    projected = []
    for index, record in enumerate(records):
        if require_changed_paths:
            _require_changed_paths(record, pointer=f"/changes/{index}")
        projected.append(project_account_change(record, reader=reader_ref))
    data["changes"] = projected
    return data


def is_account_change_record(payload: Any) -> bool:
    """Whether ``payload`` is a change record rather than a notification body.

    ``origin`` and ``repair`` are required on a record and forbidden on an
    ``account.change_recorded`` notification.
    """
    if isinstance(payload, AccountChange):
        return True
    return isinstance(payload, Mapping) and not _RECORD_ONLY_KEYS.isdisjoint(payload)


def account_change_recorded_payload(
    change: AccountChange | Mapping[str, Any],
    *,
    subscriber_id: str,
    through_cursor: str | None = None,
    ext: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``account.change_recorded`` wire body for a change record.

    Copies the record's identity (``change_id`` as ``notification_id``,
    ``resource.account_id`` as ``account_id``, ``recorded_at``, ``resource``
    and ``action``). The record's ``idempotency_key`` is never copied: it
    belongs to the originating request, and notifications MUST NOT carry it.
    The delivery ``idempotency_key`` and ``fired_at`` are filled at
    preparation. Raises :class:`AccountChangeRecordError` for a record that
    breaks the change-feed rules.
    """
    record = change if isinstance(change, AccountChange) else AccountChange.model_validate(change)
    validate_account_change(record)
    wire = record.model_dump(mode="json", exclude_none=True)
    resource = wire["resource"]
    payload: dict[str, Any] = {
        "notification_type": "account.change_recorded",
        "notification_id": wire["change_id"],
        "subscriber_id": subscriber_id,
        "account_id": resource["account_id"],
        "change_id": wire["change_id"],
        "recorded_at": wire["recorded_at"],
        "resource": {
            key: resource[key] for key in ("type", "resource_id", "parent_ids") if key in resource
        },
        "action": wire["action"],
    }
    if through_cursor is not None:
        payload["through_cursor"] = through_cursor
    if ext is not None:
        payload["ext"] = dict(ext)
    return payload


def _seller_revision(creative: Any) -> int | str | None:
    revision = _field(creative, "seller_revision")
    if revision is None:
        return None
    if isinstance(revision, bool) or not isinstance(revision, (int, str)):
        raise AccountChangeRecordError(
            "creative seller_revision must be a positive integer or a 1-255 character string",
            pointer="/resource_revision",
        )
    if isinstance(revision, int) and revision < 1:
        raise AccountChangeRecordError(
            "creative seller_revision must be a positive integer", pointer="/resource_revision"
        )
    if isinstance(revision, str) and not 1 <= len(revision) <= 255:
        raise AccountChangeRecordError(
            "creative seller_revision must be 1 to 255 characters", pointer="/resource_revision"
        )
    # A generated scalar subclass dumps as its base type; keep the JSON type.
    return int(revision) if isinstance(revision, int) else str(revision)


def mirror_creative_revision(change: _ChangeT, creative: Any) -> _ChangeT:
    """Return ``change`` with ``resource_revision`` mirroring the creative's ``seller_revision``.

    ``creative`` is the creative as the repair read (``list_creatives``)
    reports it after the change, as a model or a mapping. Its
    ``seller_revision`` is copied in the same JSON type; when the creative has
    none, ``resource_revision`` is removed. The input is not modified, and an
    :class:`AttributedAccountChange` keeps its originator.

    Raises :class:`AccountChangeRecordError` when the record's resource is not
    a creative.
    """
    resource_type = _field(_field(change, "resource"), "type")
    if resource_type != "creative":
        raise AccountChangeRecordError(
            f"{_describe(change)} is for resource type {resource_type!r}, not creative",
            pointer="/resource/type",
        )
    revision = _seller_revision(creative)
    if isinstance(change, AccountChange):
        return change.model_copy(update={"resource_revision": revision})
    updated = dict(change)
    if revision is None:
        updated.pop("resource_revision", None)
    else:
        updated["resource_revision"] = revision
    return updated  # type: ignore[return-value]
