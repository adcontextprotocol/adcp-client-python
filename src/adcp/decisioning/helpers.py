"""Small mechanical helpers for adopter platform method bodies.

These exist because the same boilerplate appears in every adopter
platform implementation we've reviewed. They are kept in their own
module so adopters can import precisely what they need without
pulling in the full :mod:`adcp.decisioning` surface.
"""

from __future__ import annotations

from typing import Any

from adcp.types import AccountReference

__all__ = ["ref_account_id"]


def ref_account_id(
    ref: AccountReference | dict[str, Any] | None,
) -> str | None:
    """Extract ``account_id`` from an :class:`AccountReference`.

    :class:`AccountReference` is a discriminated union of two shapes:

    * :class:`AccountReferenceById` — ``{account_id: ...}``
    * :class:`AccountReferenceByNaturalKey` — ``{brand: ..., operator: ...}``

    Adopters routinely write the same null-safe access pattern (open-coded
    ``ref.account_id if hasattr(ref, 'account_id') else None``); this helper
    centralises it. Returns ``None`` if ``ref`` is ``None`` or has the
    natural-key shape.

    Both Pydantic models and raw dicts are accepted, since legacy code
    paths may pass dicts straight through from JSON deserialisation.

    :param ref: An :class:`AccountReference`, a raw dict matching either
        shape, or ``None``.
    :returns: The ``account_id`` string when present; ``None`` otherwise.
    """
    if ref is None:
        return None

    if isinstance(ref, dict):
        value = ref.get("account_id")
        return value if isinstance(value, str) else None

    # AccountReference is a RootModel wrapping AccountReference1 |
    # AccountReference2. Its __getattr__ proxies to .root, so a direct
    # ``ref.account_id`` raises AttributeError on the natural-key arm.
    # getattr() with a default is the cleanest cross-arm read.
    value = getattr(ref, "account_id", None)
    return value if isinstance(value, str) else None
