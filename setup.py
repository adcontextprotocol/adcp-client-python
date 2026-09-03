"""Setuptools hooks needed when the SDK is built directly from a VCS checkout."""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

_REPOSITORY_ROOT = Path(__file__).resolve().parent
_PINNED_ADCP_VERSION = (_REPOSITORY_ROOT / "src" / "adcp" / "ADCP_VERSION").read_text().strip()
_CURRENT_SCHEMA_BUNDLE = (
    _PINNED_ADCP_VERSION
    if "-" in _PINNED_ADCP_VERSION
    else ".".join(_PINNED_ADCP_VERSION.split(".")[:2])
)
_BUNDLED_SCHEMA_VERSIONS = ("2.5", "3.0", "3.1", _CURRENT_SCHEMA_BUNDLE)


class BuildPy(_build_py):
    """Copy supported schema bundles into the wheel build directory.

    Release jobs pre-populate ``src/adcp/_schemas``. PEP 517 VCS installs do
    not run that release preparation step, so copying from the tracked cache
    here keeps direct Git installs functionally equivalent to released wheels.
    """

    def run(self) -> None:
        super().run()
        source_root = _REPOSITORY_ROOT / "schemas" / "cache"
        destination_root = Path(self.build_lib) / "adcp" / "_schemas"
        if destination_root.exists():
            shutil.rmtree(destination_root)
        for version in _BUNDLED_SCHEMA_VERSIONS:
            source = source_root / version
            if not source.is_dir():
                raise RuntimeError(f"required schema bundle is missing: {source}")
            shutil.copytree(
                source,
                destination_root / version,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("*.md", ".hashes.json"),
            )


setup(cmdclass={"build_py": BuildPy})
