"""Every public types package imports first, in a fresh interpreter (#1784).

``from adcp.types.domains.formats import Slot`` used to raise ``ImportError``
when it was the first adcp import in a process: the domain roots imported every
schema module eagerly, so ``formats/canonical/_base.py``'s ``from ...core import
<module>`` ran ``core/__init__``, which pulled in every response union, one of
which reached back into ``formats/canonical`` while ``_base`` was half built.
Whether a script failed depended on which unrelated import came first.

The domain roots are lazy now (PEP 562): importing one, or a schema module
under it, binds nothing until a name is read. These tests import each public
types package first in a fresh interpreter, which is the only way to grade an
import-order defect, and pin the behaviours a lazy root has to keep.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import adcp.types.domains as domains

_FRESH = [sys.executable, "-I", "-c"]


def _fresh_import(statement: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_FRESH, statement + "\nprint('imported')"],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


@pytest.mark.parametrize(
    "statement",
    [
        "from adcp.types.domains.formats import Slot",
        "import adcp.types.domains.formats.canonical",
        "import adcp.types; from adcp.types.domains.formats import Slot",
        "from adcp.types.domains.core import error; error.Error",
        "from adcp.types.domains.creative.list_creative_formats_response import "
        "ListCreativeFormatsResponseCreativeAgent",
    ],
)
def test_the_reported_entry_points_import_first(statement: str) -> None:
    result = _fresh_import(statement)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "imported"


@pytest.mark.parametrize("domain", sorted(domains.DOMAINS))
def test_each_domain_root_imports_first_and_binds_its_names(domain: str) -> None:
    statement = (
        f"import adcp.types.domains.{domain} as root\n"
        "for name in root.__all__:\n"
        "    getattr(root, name)\n"
        "assert not hasattr(root, 'NoSuchName')\n"
        "assert set(root.__all__) <= set(dir(root))"
    )
    result = _fresh_import(statement)
    assert result.returncode == 0, result.stderr[-2000:]


def test_a_domain_root_imports_nothing_until_a_name_is_read() -> None:
    statement = (
        "import sys\n"
        "import adcp.types.domains.core as core\n"
        "before = {m for m in sys.modules if m.startswith('adcp.types.domains.core.')}\n"
        "assert before == set(), sorted(before)[:5]\n"
        "core.AccountReference\n"
        "after = {m for m in sys.modules if m.startswith('adcp.types.domains.core.')}\n"
        "assert 'adcp.types.domains.core.account_ref' in after\n"
        "assert 'adcp.types.domains.core.format' not in after, "
        "'reading one name imported the domain'"
    )
    result = _fresh_import(statement)
    assert result.returncode == 0, result.stderr[-2000:]


def test_a_resolved_name_is_the_declaring_modules_object_and_is_cached() -> None:
    from adcp.types.domains import core
    from adcp.types.domains.core.account_ref import AccountReference

    assert core.AccountReference is AccountReference
    assert "AccountReference" in vars(core)
    with pytest.raises(AttributeError):
        core.NoSuchName  # noqa: B018
