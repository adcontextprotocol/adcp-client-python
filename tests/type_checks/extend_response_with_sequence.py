"""Adopter pattern: extend a library response type with a more-specific element.

Critical Pattern #1 — subclass a library type, override a parent's
``list[X]`` field with ``list[ChildX]`` where ``ChildX`` carries
extra adopter-internal fields excluded from the wire.

Before #624 this required ``# type: ignore[assignment]`` on every override
because ``list[T]`` is invariant in T. After #624 the SDK ships the
parent annotation as ``Sequence[T]`` (covariant), so the override
typechecks under mypy --strict with zero ignores while keeping
``.append()`` ergonomics on the child class.

**Scope this test pins down:** Optional list fields where the parent's
element type matches the public alias (``adcp.types.X``). The widening
also applies to required-list fields, but a separate codegen issue
(``X`` emitted multiple times across response files; the public alias
resolves to one emission while ``ResponseFoo.bar: Sequence[X]``
references a *different* local emission with the same name) means
required-field overrides hit a type-identity mismatch *before* variance
matters. That's tracked separately — Sequence widening is necessary
but not sufficient for those cases.

**Documented limitation: Optional widening is rejected** — overriding
a required parent (``Sequence[X]``) with an optional child
(``list[Y] | None``) is a genuine Liskov-incompatibility (parent
promises present; child weakens to ``None``-allowed). Adopters must
keep optionality identical between parent and child.
"""

from __future__ import annotations

from pydantic import Field

from adcp.types import Package, UpdateMediaBuyResponse1

# --- Optional parent → Optional child (narrower element) ---


class _InternalPackage(Package):
    """Adopter extension — carries fields excluded from the wire envelope."""

    internal_state: str | None = Field(default=None, exclude=True)


class _ExtendedUpdateMediaBuyResponse(UpdateMediaBuyResponse1):
    """Adopter override — narrower element type on the response field.

    Library declares ``affected_packages: Sequence[Package] | None``.
    Adopter declares ``list[_InternalPackage] | None`` here, which is a
    valid subtype under Sequence's covariance — no ``# type: ignore``.
    """

    affected_packages: list[_InternalPackage] | None = None


# Construction + .append() prove runtime ergonomics survive the widening.
resp = _ExtendedUpdateMediaBuyResponse(
    media_buy_id="mb_1",
    status="completed",
    revision=2,
    affected_packages=[_InternalPackage(package_id="p1", internal_state="active")],
)
assert resp.affected_packages is not None
resp.affected_packages.append(_InternalPackage(package_id="p2"))
assert len(resp.affected_packages) == 2
