"""Release artifact size checks fail before a PyPI upload is attempted."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.ci.check_distribution_size import (
    MAX_BYTES,
    check_distribution_size,
    check_retired_schema_bundles,
)


def test_distribution_size_requires_both_artifacts(tmp_path: Path) -> None:
    (tmp_path / "adcp-1-py3-none-any.whl").write_bytes(b"wheel")
    with pytest.raises(ValueError, match=r"expected one \*\.tar\.gz"):
        check_distribution_size(tmp_path)


@pytest.mark.parametrize("suffix", ["py3-none-any.whl", "tar.gz"])
def test_distribution_size_rejects_oversized_artifact(tmp_path: Path, suffix: str) -> None:
    wheel = tmp_path / "adcp-1-py3-none-any.whl"
    source = tmp_path / "adcp-1.tar.gz"
    wheel.write_bytes(b"wheel")
    source.write_bytes(b"source")
    target = wheel if suffix.endswith(".whl") else source
    with target.open("r+b") as stream:
        stream.truncate(MAX_BYTES + 1)
    with pytest.raises(ValueError, match="limit is 90000000 bytes"):
        check_distribution_size(tmp_path)


def test_distribution_size_accepts_both_artifacts(tmp_path: Path) -> None:
    wheel = tmp_path / "adcp-1-py3-none-any.whl"
    source = tmp_path / "adcp-1.tar.gz"
    wheel.write_bytes(b"wheel")
    source.write_bytes(b"source")
    assert check_distribution_size(tmp_path) == (wheel, source)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "version", ["2.5", "3.2.0-beta.6", "3.2.0-rc.3", "3.2.0-rc.6", "3.2.0-rc.7"]
)
def test_distribution_rejects_retired_schema_members(
    tmp_path: Path, artifact: str, version: str
) -> None:
    wheel = tmp_path / "adcp-1-py3-none-any.whl"
    source = tmp_path / "adcp-1.tar.gz"
    wheel_name = f"adcp/_schemas/{version}/index.json" if artifact == "wheel" else "adcp/x.py"
    source_name = (
        f"adcp-1/schemas/cache/{version}/index.json" if artifact == "sdist" else "adcp-1/x.py"
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(wheel_name, "{}")
    with tarfile.open(source, "w:gz") as archive:
        body = b"{}"
        member = tarfile.TarInfo(source_name)
        member.size = len(body)
        archive.addfile(member, io.BytesIO(body))
    with pytest.raises(ValueError, match="retired schema bundle"):
        check_retired_schema_bundles(wheel, source)


def test_distribution_rejects_retired_release_metadata(tmp_path: Path) -> None:
    wheel = tmp_path / "adcp-1-py3-none-any.whl"
    source = tmp_path / "adcp-1.tar.gz"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("adcp/x.py", "")
    with tarfile.open(source, "w:gz") as archive:
        body = b"{}"
        member = tarfile.TarInfo("adcp-1/schemas/releases/3.2.0-rc.6.json")
        member.size = len(body)
        archive.addfile(member, io.BytesIO(body))
    with pytest.raises(ValueError, match="retired schema bundle 3.2.0-rc.6"):
        check_retired_schema_bundles(wheel, source)
