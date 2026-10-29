"""Peak RSS of importing the SDK's entry modules stays under a budget (#1694).

Importing ``adcp.client`` or ``adcp.server`` realizes the generated type
surface through ``adcp.types._eager``. On 9.3.0 that cost about 520 MiB of
resident memory — 3.4x the 6.6 figure — because the import-time patchers
forced ``model_rebuild`` on two dozen request and response roots and every
generated ``RootModel`` built its validator at class creation. A seller that
ran in a 512 MiB container was OOM-killed at startup.

Each module is imported in a fresh ``python -I`` subprocess, as an adopter's
process would, and its own peak RSS is read back. The budget sits well above the
measured figure (about 115 MiB for ``adcp.types._eager`` and 185 MiB for
``adcp.server`` on Linux / CPython 3.12) so platform and interpreter variance
do not trip it, and well below the 512 MiB the issue was about, so a change
that reintroduces an import-time build of a large model subtree fails here
rather than in an adopter's container.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytest.importorskip("resource", reason="peak RSS needs a POSIX platform")

#: Peak-RSS budget for a bare ``import <module>``.
IMPORT_RSS_BUDGET_MIB = 320

ENTRY_MODULES = [
    "adcp.types._eager",
    "adcp.client",
    "adcp.server",
    "adcp.server.test_controller",
    "adcp.decisioning",
    "adcp.compat.versions",
]

_PROBE = """
import sys
import {module}
if sys.platform == "linux":
    # The address space's own high-water mark. ru_maxrss would not do: Linux
    # carries the forking parent's peak across exec, so a probe spawned from a
    # large pytest process would report pytest's peak rather than its own.
    with open("/proc/self/status") as status:
        line = next(line for line in status if line.startswith("VmHWM:"))
    print(int(line.split()[1]) / 1024)
else:
    import resource
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is KiB on most POSIX platforms and bytes on macOS.
    print(rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024)
"""


def _import_peak_rss_mib(module: str) -> float:
    result = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE.format(module=module)],
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )
    return float(result.stdout.strip().splitlines()[-1])


@pytest.mark.slow
@pytest.mark.parametrize("module", ENTRY_MODULES)
def test_import_peak_rss_within_budget(module: str) -> None:
    peak = _import_peak_rss_mib(module)
    assert peak < IMPORT_RSS_BUDGET_MIB, (
        f"import {module} peaked at {peak:.0f} MiB RSS, over the {IMPORT_RSS_BUDGET_MIB} MiB "
        "budget. Something now builds a large pydantic model subtree at import time: look "
        "for a model_rebuild(force=True) that should be defer_model_rebuild(), or a "
        "RootModel[...] that should parametrize adcp.types.base.AdCPRootModel (#1694)."
    )
