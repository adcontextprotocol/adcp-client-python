"""Release artifact size checks fail before a PyPI upload is attempted."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_distribution_size import MAX_BYTES, check_distribution_size


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
