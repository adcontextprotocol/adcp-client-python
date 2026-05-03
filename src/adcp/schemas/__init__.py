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

__all__ = ["load_schema"]

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
    try:
        pkg = files("adcp.schemas")
        with as_file(pkg / name) as p:
            # as_file() does not raise when the file is absent; is_file() guards the read.
            if p.is_file():
                return cast(dict[str, Any], json.loads(p.read_text()))
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        pass

    raise FileNotFoundError(
        f"AdCP schema {name!r} not bundled with this SDK release. "
        "Available schemas: adcp-agents.json. "
        "If you are developing against a source checkout, ensure "
        "`src/adcp/schemas/` contains the schema file."
    )
