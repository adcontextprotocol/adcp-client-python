"""A dependency-free PEP 517 probe for isolated archive-to-wheel builds."""

import base64
import hashlib
import io
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

METADATA = "Metadata-Version: 2.4\nName: probe\nVersion: 1.0.0\n"


def record(hook):
    with Path(os.environ["ADCP_BUILD_POLICY_TRACE"]).open("a") as trace:
        trace.write(json.dumps({"hook": hook, "prefix": sys.prefix, "cwd": str(Path.cwd())}) + "\n")


def build_sdist(sdist_directory, config_settings=None):
    record("sdist")
    if os.environ.get("ADCP_BUILD_POLICY_FAIL") == "1":
        raise RuntimeError("intentional build probe failure")
    name = "probe-1.0.0.tar.gz"
    with tarfile.open(Path(sdist_directory) / name, "w:gz") as archive:
        for source in ("pyproject.toml", "backend.py"):
            archive.add(source, arcname="probe-1.0.0/" + source)
        marker = tarfile.TarInfo("probe-1.0.0/archive-marker")
        marker.size = len(b"from-sdist")
        archive.addfile(marker, io.BytesIO(b"from-sdist"))
        metadata = tarfile.TarInfo("probe-1.0.0/PKG-INFO")
        metadata.size = len(METADATA.encode())
        archive.addfile(metadata, io.BytesIO(METADATA.encode()))
    return name


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    record("wheel")
    marker = Path("archive-marker").read_bytes()
    assert marker == b"from-sdist"
    name = "probe-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(Path(wheel_directory) / name, "w") as archive:
        archive.writestr("probe.py", f"ARCHIVE_MARKER = {marker.decode()!r}\n")
        archive.writestr("probe-1.0.0.dist-info/METADATA", METADATA)
        archive.writestr(
            "probe-1.0.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: build-policy-probe\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        records = []
        for member in archive.namelist():
            content = archive.read(member)
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
            records.append(f"{member},sha256={digest.decode()},{len(content)}\n")
        records.append("probe-1.0.0.dist-info/RECORD,,\n")
        archive.writestr("probe-1.0.0.dist-info/RECORD", "".join(records))
    return name
