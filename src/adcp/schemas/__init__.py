"""Access bundled AdCP JSON schemas by name.

Schemas that ship with the SDK live as data files in this package
(``src/adcp/schemas/``).  They are committed to the repo and included
in the wheel via the ``adcp.schemas`` ``package-data`` entry in
``pyproject.toml``, so ``importlib.resources`` resolves them correctly
in both editable installs and installed wheels.

For per-tool request/response validation validators, see
:mod:`adcp.validation.schema_loader`.

Usage::

    from adcp.schemas import load_schema

    schema = load_schema("adcp-agents.json")
    jsonschema.validate(manifest, schema)
"""

from __future__ import annotations

import json
from importlib.resources import as_file, files
from typing import Any, cast

__all__ = ["load_schema", "ADCP_AGENTS"]

#: Known schema filenames shipped with the SDK.
ADCP_AGENTS = "adcp-agents.json"


def load_schema(name: str) -> dict[str, Any]:
    """Return the named AdCP JSON schema as a dict.

    Raises :class:`FileNotFoundError` if the schema is not bundled with
    the SDK.  Pass one of the ``adcp.schemas.<NAME>`` string constants to
    avoid typos (e.g. :data:`ADCP_AGENTS`).

    :param name: Filename of the schema, e.g. ``"adcp-agents.json"``.

    .. note::
        Returns the raw JSON Schema dict; pass it to
        ``jsonschema.validate(instance, schema)`` to validate a document.
        ``jsonschema`` is a required dependency of ``adcp``.
    """
    # Guard against path traversal: reject names that could escape the package.
    # Intentionally conservative — any name containing ".." is rejected even if
    # it wouldn't actually traverse (e.g. "..future.json"); prefer a clear
    # "invalid" error over a subtle escape.
    if "/" in name or "\\" in name or ".." in name:
        raise FileNotFoundError(
            f"AdCP schema name {name!r} is invalid. "
            "Use a bare filename, e.g. 'adcp-agents.json'."
        )

    try:
        pkg = files("adcp.schemas")
        with as_file(pkg / name) as p:
            # p.is_file() guards the read — as_file() hands back a path even
            # when the member is absent in the package.
            if p.is_file():
                return cast(dict[str, Any], json.loads(p.read_text(encoding="utf-8")))
    except (ModuleNotFoundError, FileNotFoundError, OSError, ValueError):
        # ValueError covers json.JSONDecodeError (a subclass) so a corrupted
        # bundled schema surfaces as FileNotFoundError rather than a raw parse
        # error.
        pass

    try:
        available = ", ".join(
            sorted(f.name for f in files("adcp.schemas").iterdir() if f.name.endswith(".json"))
        )
    except Exception:  # noqa: BLE001 — intentional: don't mask the real FileNotFoundError
        available = ADCP_AGENTS

    raise FileNotFoundError(
        f"AdCP schema {name!r} not bundled with this SDK release. "
        f"Available schemas: {available}. "
        "If you are developing against a source checkout, ensure "
        "`src/adcp/schemas/` contains the schema file."
    )
