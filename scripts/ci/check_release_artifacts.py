"""Verify the candidate version in the wheel and source distribution."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path

from scripts.normalize_pyproject_prerelease import pep440_prerelease


def check_metadata(data: bytes, version: str) -> None:
    metadata = BytesParser(policy=policy.default).parsebytes(data)
    if metadata.get_all("Name") != ["adcp"] or metadata.get_all("Version") != [version]:
        raise ValueError("distribution metadata does not match the candidate SDK version")


def check_artifacts(directory: Path, version: str) -> None:
    wheels, sdists = list(directory.glob("*.whl")), list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("expected exactly one candidate wheel and source distribution")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("wheel must contain exactly one METADATA record")
        check_metadata(archive.read(names[0]), version)
    with tarfile.open(sdists[0]) as archive:
        records = [
            member
            for member in archive.getmembers()
            if member.name.endswith("/PKG-INFO") and member.name.count("/") == 1
        ]
        if len(records) != 1 or not records[0].isfile():
            raise ValueError("source distribution must contain a regular root PKG-INFO record")
        stream = archive.extractfile(records[0])
        assert stream is not None
        with stream:
            check_metadata(stream.read(), version)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    directory = parser.parse_args().directory
    version = pep440_prerelease(json.loads(Path(".release-please-manifest.json").read_text())["."])
    check_artifacts(directory, version)
    wheel = next(directory.glob("*.whl")).resolve()
    with tempfile.TemporaryDirectory(prefix="adcp-release-install-") as temporary:
        root = Path(temporary)
        environment = root / "installed"
        subprocess.run(
            ["uv", "venv", "--python", sys.executable, str(environment)],
            check=True,
            timeout=30,
        )
        python = environment / "bin/python"
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
            check=True,
            timeout=30,
        )
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "import sys; from pathlib import Path; import adcp; "
                "assert Path(adcp.__file__).is_relative_to(Path(sys.prefix)); "
                "assert adcp.__version__ == sys.argv[1]",
                version,
            ],
            cwd=root,
            check=True,
            timeout=30,
        )
    print(f"Wheel, source distribution, and installed SDK all report {version}")


if __name__ == "__main__":
    main()
