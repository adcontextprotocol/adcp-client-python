"""Published release pins must fail before any cache/fixture extraction."""

from __future__ import annotations

import hashlib
import json
from urllib.error import HTTPError

import pytest

from tests.test_sync_schemas import _mod

VERSION = "3.2.0-rc.4"


@pytest.fixture
def published_release(monkeypatch, tmp_path):
    bundle = b"immutable signed bundle"
    files = {
        VERSION + ".tgz": bundle,
        VERSION + ".tgz.sha256": hashlib.sha256(bundle).hexdigest().encode() + b"  bundle.tgz\n",
        VERSION + ".tgz.sig": b"signature",
        VERSION + ".tgz.crt": b"certificate",
    }
    pin = {
        "version": VERSION,
        "source_commit": "94976657c8456e5ad6de55d9793a883542a4fc5f",
        "bundle_url": f"https://adcontextprotocol.org/protocol/{VERSION}.tgz",
        "certificate_identity": (
            "https://github.com/adcontextprotocol/adcp/"
            ".github/workflows/release.yml@refs/heads/main"
        ),
        "certificate_oidc_issuer": "https://token.actions.githubusercontent.com",
        "artifacts": {
            name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in files.items()
        },
    }
    pins = tmp_path / "releases"
    pins.mkdir()
    (pins / (VERSION + ".json")).write_text(json.dumps(pin))
    monkeypatch.setattr(_mod, "RELEASE_PINS_DIR", pins, raising=False)
    monkeypatch.delenv("ADCP_SKIP_SIGNATURE", raising=False)
    monkeypatch.setattr(_mod, "BUNDLE_BASE_URL", "https://adcontextprotocol.org/protocol")
    fetched = []
    verified = []

    def fetch(url):
        fetched.append(url)
        name = url.rsplit("/", 1)[-1]
        if name not in files:
            raise HTTPError(url, 404, "not found", {}, None)
        return files[name]

    monkeypatch.setattr(_mod, "_http_get", fetch)
    monkeypatch.setattr(_mod, "verify_cosign_signature", lambda *a, **kw: verified.append((a, kw)))
    return files, pin, fetched, verified


def test_audited_pin_uses_exact_source_and_certificate(published_release):
    files, pin, fetched, verified = published_release
    assert _mod.fetch_signed_release(VERSION, pin) == files[VERSION + ".tgz"]
    assert fetched == [pin["bundle_url"] + suffix for suffix in ("", ".sha256", ".sig", ".crt")]
    assert len(verified) == 1
    assert verified[0][1] == {"certificate_identity": pin["certificate_identity"]}


@pytest.mark.parametrize("suffix", [".tgz", ".tgz.sha256", ".tgz.sig", ".tgz.crt"])
def test_missing_published_artifact_never_fetches_latest(published_release, suffix):
    files, pin, fetched, verified = published_release
    del files[VERSION + suffix]
    with pytest.raises(HTTPError):
        _mod.fetch_signed_release(VERSION, pin)
    assert all("latest" not in url for url in fetched)
    assert verified == []


@pytest.mark.parametrize("suffix", [".tgz", ".tgz.sha256", ".tgz.sig", ".tgz.crt"])
def test_changed_artifact_fails_immutable_pin(published_release, suffix):
    files, pin, _, verified = published_release
    files[VERSION + suffix] += b"altered"
    with pytest.raises(RuntimeError, match="audited release pin"):
        _mod.fetch_signed_release(VERSION, pin)
    assert verified == []


def test_strict_release_rejects_signature_bypass(published_release, monkeypatch):
    _, pin, fetched, _ = published_release
    monkeypatch.setenv("ADCP_SKIP_SIGNATURE", "1")
    with pytest.raises(RuntimeError, match="ADCP_SKIP_SIGNATURE"):
        _mod.fetch_signed_release(VERSION, pin)
    assert fetched == []


def test_strict_release_rejects_source_override(published_release, monkeypatch):
    _, pin, fetched, _ = published_release
    monkeypatch.setattr(_mod, "BUNDLE_BASE_URL", "https://untrusted.example/protocol")
    with pytest.raises(RuntimeError, match="official protocol source"):
        _mod.fetch_signed_release(VERSION, pin)
    assert fetched == []


def test_known_pin_is_strict_even_without_cli_option(published_release, monkeypatch):
    files, _, fetched, _ = published_release
    del files[VERSION + ".tgz.sig"]
    monkeypatch.setattr(
        _mod, "_extract_bundle", lambda *a: pytest.fail("unverified bytes reached extraction")
    )
    with pytest.raises(SystemExit) as raised:
        _mod._sync_one(VERSION, sync_skills=False)
    assert raised.value.code == 1
    assert all("latest" not in url for url in fetched)


def test_explicit_strict_mode_requires_an_audited_pin(published_release):
    with pytest.raises(SystemExit) as raised:
        _mod._sync_one("3.2.0-rc.999", sync_skills=False, require_signed_release=True)
    assert raised.value.code == 1


def test_cosign_exact_identity_retains_normal_transparency_checks(monkeypatch):
    from types import SimpleNamespace

    seen = []
    monkeypatch.setattr(_mod.shutil, "which", lambda _: "/usr/bin/cosign")

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout="Verified OK", stderr="")

    monkeypatch.setattr(_mod.subprocess, "run", run)
    identity = (
        "https://github.com/adcontextprotocol/adcp/.github/workflows/release.yml@refs/heads/main"
    )
    _mod.verify_cosign_signature(b"bundle", b"sig", b"cert", certificate_identity=identity)
    argv = seen[0]
    assert argv[argv.index("--certificate-identity") + 1] == identity
    assert "--certificate-identity-regexp" not in argv
    assert not any("insecure" in value or "ignore" in value for value in argv)
