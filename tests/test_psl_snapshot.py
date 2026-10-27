"""The eTLD+1 helpers use the SDK's vendored, pinned Public Suffix List (#1562)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import tldextract

from adcp.signing import PSL_SNAPSHOT, PublicSuffixListSnapshot, etld
from adcp.signing.etld import quota_key, registrable_domain


def test_vendored_file_matches_the_recorded_snapshot() -> None:
    data = etld._PSL_PATH.read_bytes()
    assert hashlib.sha256(data).hexdigest() == PSL_SNAPSHOT.sha256
    header = data[:1024].decode()
    assert f"// VERSION: {PSL_SNAPSHOT.version}\n" in header
    assert f"// COMMIT: {PSL_SNAPSHOT.commit}\n" in header
    assert "===BEGIN PRIVATE DOMAINS===" in data.decode()


def test_snapshot_is_exported_as_provenance() -> None:
    assert isinstance(PSL_SNAPSHOT, PublicSuffixListSnapshot)
    assert PSL_SNAPSHOT.source_url == "https://publicsuffix.org/list/public_suffix_list.dat"


def test_extractors_read_only_the_vendored_file(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    real = tldextract.TLDExtract

    def spy(**kwargs: object) -> tldextract.TLDExtract:
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(etld.tldextract, "TLDExtract", spy)
    etld._extractor.cache_clear()
    etld._icann_extractor.cache_clear()
    try:
        assert registrable_domain("ads.brand.co.uk") == "brand.co.uk"
        assert quota_key("alice.github.io") == "github.io"
    finally:
        etld._extractor.cache_clear()
        etld._icann_extractor.cache_clear()
    assert calls, "extractors were not rebuilt"
    for kwargs in calls:
        assert kwargs["suffix_list_urls"] == (etld._PSL_PATH.resolve().as_uri(),)
        assert kwargs["cache_dir"] is None
        assert kwargs["fallback_to_snapshot"] is False


def test_a_tampered_snapshot_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tampered = tmp_path / "public_suffix_list.dat"
    tampered.write_bytes(etld._PSL_PATH.read_bytes() + b"\nevil.example\n")
    monkeypatch.setattr(etld, "_PSL_PATH", tampered)
    etld._pinned_psl_uri.cache_clear()
    etld._extractor.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="does not match the pinned snapshot"):
            registrable_domain("a.evil.example")
    finally:
        monkeypatch.undo()
        etld._pinned_psl_uri.cache_clear()
        etld._extractor.cache_clear()


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        # ICANN section, single- and multi-label suffixes.
        ("ads.brand.com", "brand.com"),
        ("a.b.example.co.uk", "example.co.uk"),
        ("co.uk", None),
        # PRIVATE section: each hosted tenant is its own registrable domain.
        ("alice.github.io", "alice.github.io"),
        ("github.io", None),
        ("x.y.vercel.app", "y.vercel.app"),
        # Wildcard rule *.ck, with the exception rule !www.ck.
        ("foo.bar.ck", "foo.bar.ck"),
        ("bar.ck", None),
        ("www.ck", "www.ck"),
        # IDN, compared in A-label form.
        ("shop.straße.de", "xn--strae-oqa.de"),
        ("www.例え.jp", "xn--r8jz45g.jp"),
        # A Unicode ICANN suffix (東京.jp) is itself not registrable.
        ("東京.jp", None),
        ("a.東京.jp", "a.xn--1lqs71d.jp"),
    ],
)
def test_registrable_domain_under_the_pinned_snapshot(host: str, expected: str | None) -> None:
    assert registrable_domain(host) == expected


def test_private_tenants_share_the_providers_quota_group() -> None:
    assert quota_key("alice.github.io") == quota_key("bob.github.io") == "github.io"
    assert quota_key("ads.brand.co.uk") == "brand.co.uk"
