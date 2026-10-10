"""Schema-derived root rules for the canonical product format declaration.

``core/product-format-declaration.json`` carries two kinds of root constraint
that a generated class cannot express on its own:

* six ``allOf`` clauses — ``if``/``then``/``not`` cross-field rules over
  ``required`` and ``const`` (for example ``publisher_domain`` is addressable
  only alongside ``format_option_id``);
* a sixteen-branch ``oneOf`` pairing each ``format_kind`` const with the
  canonical ``params`` schema for that kind.

:func:`check_declaration_rules` evaluates both against the bundled schema, so
the rules stay derived from the signed bundle rather than restated by hand.
:class:`adcp.types.canonical_creative.ProductFormatDeclaration` runs it as a
``model_validator``, which keeps one class — constructible, ``isinstance``-able
and ``model_validate``-able — as the public authoring type.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticCustomError

from adcp.validation.schema_loader import get_named_validator

PRODUCT_FORMAT_DECLARATION_SCHEMA = "core/product-format-declaration.json"


def check_declaration_rules(data: Any, *, schema_name: str) -> Any:
    """Return the document in ``data``, refusing it if a root rule fails.

    Raises :class:`pydantic_core.PydanticCustomError` keyed on the failing
    JSON Schema keyword, so the refusal carries the schema's own vocabulary
    (``required``, ``not``, ``oneOf``) rather than a hand-written sentence.

    A model arrives when a sibling generated branch or another ``Format`` is
    adopted. It is returned as its wire document so the declaration is built
    from the same keys the rules were graded against, and so an unset default
    stays absent rather than becoming an explicit null.
    """

    if isinstance(data, BaseModel):
        # Defaults that were never supplied are absent on the wire. Explicit
        # nulls retain their presence: root rules test required, not truthiness.
        document = data.model_dump(mode="json", exclude_unset=True, exclude_none=False)
    elif isinstance(data, dict):
        document = data
    else:
        return data

    validator = get_named_validator(schema_name)
    if validator is None:
        raise RuntimeError(f"Bundled {schema_name} schema is unavailable")
    # Each lookup supplies an independent resolver over cached schema data.
    # Evolving it preserves reference resolution and format checking, while
    # selecting only the normative root rules the declared fields cannot carry:
    # the cross-field allOf clauses and the format_kind/params oneOf pairing.
    # ``properties``, ``required`` and ``type`` are deliberately left out —
    # the model's own fields already enforce those, and including them would
    # report every field error twice.
    schema = validator.schema
    rules = {"allOf": schema.get("allOf", [])}
    branch = _select_branch(schema, document)
    if branch is None:
        # No branch claims this discriminator value. Reporting the whole oneOf
        # names the closed set of accepted kinds, which is the useful message.
        rules["oneOf"] = schema.get("oneOf", [])
    else:
        # Grading the one branch the discriminator selects — rather than the
        # whole oneOf — keeps the failing keyword and its instance path, so a
        # bad parameter reports as ``params.width`` instead of a root ``oneOf``.
        rules["allOf"] = [*rules["allOf"], branch]

    error = next(validator.evolve(schema=rules).iter_errors(document), None)
    if error is not None:
        raise PydanticCustomError(str(error.validator), _message_with_path(error))
    return document


def _select_branch(schema: dict[str, Any], document: Any) -> dict[str, Any] | None:
    """Return the ``oneOf`` branch the schema's discriminator selects."""

    property_name = schema.get("discriminator", {}).get("propertyName")
    if not property_name or not isinstance(document, dict):
        return None
    value = document.get(property_name)
    for branch in schema.get("oneOf", []):
        if branch.get("properties", {}).get(property_name, {}).get("const") == value:
            selected: dict[str, Any] = branch
            return selected
    return None


def _message_with_path(error: Any) -> str:
    """Prefix ``error``'s message with the instance path it failed at."""

    path = ".".join(str(part) for part in error.absolute_path)
    return f"{path}: {error.message}" if path else error.message
