"""Deterministic transport and integrity tests for immutable schema releases."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from scripts import generate_types, sync_schemas

_VERSION = "3.2.0-rc.4"
_ASSET_URL = f"https://adcontextprotocol.org/schemas/{_VERSION}/core/assets/video-asset.json"


def _http_error(url: str, code: int) -> HTTPError:
    return HTTPError(url, code, f"HTTP {code}", hdrs=None, fp=None)


def test_transient_reset_recovers_with_same_immutable_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    outcomes: list[bytes | Exception] = [URLError(ConnectionResetError(104, "reset")), b"{}"]
    sleeps: list[float] = []

    def fake_get(url: str) -> bytes:
        calls.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sync_schemas, "_http_get", fake_get)
    monkeypatch.setattr(sync_schemas.time, "sleep", sleeps.append)

    assert sync_schemas._http_get_with_retry(_ASSET_URL) == b"{}"
    assert calls == [_ASSET_URL, _ASSET_URL]
    assert sleeps == [1.0]
    log = capsys.readouterr().err
    assert "attempt=1/3" in log
    assert "attempt=2/3 classification=recovered" in log
    assert "classification=transport-exhausted" not in log


def test_http_503_recovers_within_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes: list[bytes | Exception] = [_http_error(_ASSET_URL, 503), b"schema"]
    sleeps: list[float] = []

    def fake_get(_url: str) -> bytes:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sync_schemas, "_http_get", fake_get)
    monkeypatch.setattr(sync_schemas.time, "sleep", sleeps.append)

    assert sync_schemas._http_get_with_retry(_ASSET_URL) == b"schema"
    assert sleeps == [1.0]


def test_transient_failure_exhausts_budget_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    def always_reset(url: str) -> bytes:
        calls.append(url)
        raise URLError(ConnectionResetError(104, "reset"))

    monkeypatch.setattr(sync_schemas, "_http_get", always_reset)
    monkeypatch.setattr(sync_schemas.time, "sleep", sleeps.append)

    with pytest.raises(URLError):
        sync_schemas._http_get_with_retry(_ASSET_URL)

    assert calls == [_ASSET_URL] * 3
    assert sleeps == [1.0, 2.0]
    assert "classification=transport-exhausted" in capsys.readouterr().err


def test_permanent_404_is_not_retried_or_logged_with_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_url = _ASSET_URL.replace("https://", "https://user:secret@")
    calls: list[str] = []

    def not_found(url: str) -> bytes:
        calls.append(url)
        raise _http_error(url, 404)

    monkeypatch.setattr(sync_schemas, "_http_get", not_found)
    monkeypatch.setattr(
        sync_schemas.time,
        "sleep",
        lambda _delay: pytest.fail("permanent failures must not sleep"),
    )

    with pytest.raises(HTTPError):
        sync_schemas._http_get_with_retry(secret_url)

    assert calls == [secret_url]
    log = capsys.readouterr().err
    assert "classification=permanent" in log
    assert "secret" not in log


def test_checksum_failure_is_not_retried_into_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    responses = iter([b"tampered bundle", ("0" * 64 + "  bundle.tgz\n").encode()])

    def fake_get(url: str) -> bytes:
        calls.append(url)
        return next(responses)

    monkeypatch.setattr(sync_schemas, "_http_get", fake_get)

    with pytest.raises(SystemExit):
        sync_schemas._sync_one(_VERSION, sync_skills=False)

    assert calls == [
        f"{sync_schemas.BUNDLE_BASE_URL}/{_VERSION}.tgz",
        f"{sync_schemas.BUNDLE_BASE_URL}/{_VERSION}.tgz.sha256",
    ]


def test_signature_failure_is_not_retried_into_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = b"bundle"
    digest = hashlib.sha256(bundle).hexdigest()
    calls: list[str] = []
    responses = iter([bundle, f"{digest}  bundle.tgz\n".encode(), b"sig", b"crt"])
    verifications: list[tuple[bytes, bytes, bytes]] = []

    def fake_get(url: str) -> bytes:
        calls.append(url)
        return next(responses)

    def reject_signature(data: bytes, sig: bytes, crt: bytes) -> None:
        verifications.append((data, sig, crt))
        raise RuntimeError("signature verification failed")

    monkeypatch.setattr(sync_schemas, "_http_get", fake_get)
    monkeypatch.setattr(sync_schemas, "verify_cosign_signature", reject_signature)

    with pytest.raises(SystemExit):
        sync_schemas._sync_one(_VERSION, sync_skills=False)

    assert len(calls) == 4
    assert verifications == [(bundle, b"sig", b"crt")]


def test_missing_release_never_falls_back_to_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def not_found(url: str) -> bytes:
        calls.append(url)
        raise _http_error(url, 404)

    monkeypatch.setattr(sync_schemas, "_http_get", not_found)

    with pytest.raises(SystemExit):
        sync_schemas._sync_one(_VERSION, sync_skills=False)

    assert calls == [f"{sync_schemas.BUNDLE_BASE_URL}/{_VERSION}.tgz"]
    assert all("latest" not in url for url in calls)


def test_release_redirect_cannot_change_selected_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class RedirectResponse:
        def __enter__(self) -> RedirectResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return "https://adcontextprotocol.org/schemas/latest/core/assets/video-asset.json"

        def read(self) -> bytes:
            pytest.fail("redirected content must not be read")

    monkeypatch.setattr(sync_schemas, "urlopen", lambda _request: RedirectResponse())
    monkeypatch.setattr(
        sync_schemas.time,
        "sleep",
        lambda _delay: pytest.fail("selection-policy failures must not sleep"),
    )

    with pytest.raises(sync_schemas.ReleaseAssetPolicyError):
        sync_schemas._http_get_with_retry(_ASSET_URL)

    log = capsys.readouterr().err
    assert "classification=permanent" in log
    assert "category=selection-policy" in log


def _write_prefetch_fixture(root: Path, *, version: str = _VERSION) -> tuple[Path, Path, Path]:
    schema_root = root / "cache"
    temp_root = root / "temp"
    target = Path("core/assets/video-asset.json")
    trusted = {"type": "object", "properties": {"url": {"type": "string"}}}
    (schema_root / target).parent.mkdir(parents=True)
    (schema_root / target).write_text(json.dumps(trusted), encoding="utf-8")
    prepared_target = temp_root / "core" / "assets" / "video_asset.json"
    prepared_target.parent.mkdir(parents=True)
    prepared_target.write_text(json.dumps(trusted), encoding="utf-8")
    caller = temp_root / "core" / "assets" / "asset_union.json"
    caller.write_text(
        json.dumps({"$ref": f"https://adcontextprotocol.org/schemas/{version}/{target}"}),
        encoding="utf-8",
    )
    return schema_root, temp_root, caller


def test_prefetch_verifies_and_mirrors_exact_release_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_root, temp_root, caller = _write_prefetch_fixture(tmp_path)
    remote = {
        "$id": _ASSET_URL,
        "type": "object",
        "properties": {"url": {"type": "string"}},
    }
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return json.dumps(remote).encode()

    monkeypatch.setattr(generate_types, "SCHEMAS_DIR", schema_root)
    monkeypatch.setattr(generate_types, "_ADCP_VERSION", _VERSION)
    monkeypatch.setattr(generate_types, "_http_get_with_retry", fetch)

    assert generate_types._prefetch_canonical_refs(temp_root) == 1
    assert calls == [_ASSET_URL]
    assert json.loads(caller.read_text())["$ref"] == _ASSET_URL
    mirror = (
        tmp_path
        / ".schema_http_refs"
        / "adcontextprotocol.org"
        / "schemas"
        / _VERSION
        / "core/assets/video-asset.json"
    )
    assert json.loads(mirror.read_text()) == remote


def test_prefetch_integrity_failure_is_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_root, temp_root, _caller = _write_prefetch_fixture(tmp_path)
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return b'{"type":"string"}'

    monkeypatch.setattr(generate_types, "SCHEMAS_DIR", schema_root)
    monkeypatch.setattr(generate_types, "_ADCP_VERSION", _VERSION)
    monkeypatch.setattr(generate_types, "_http_get_with_retry", fetch)

    with pytest.raises(RuntimeError, match="integrity mismatch"):
        generate_types._prefetch_canonical_refs(temp_root)

    assert calls == [_ASSET_URL]


@pytest.mark.parametrize("alternate", ["latest", "3.2.0-rc.3"])
def test_prefetch_prohibits_version_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alternate: str
) -> None:
    schema_root, temp_root, _caller = _write_prefetch_fixture(tmp_path, version=alternate)
    calls: list[str] = []
    monkeypatch.setattr(generate_types, "SCHEMAS_DIR", schema_root)
    monkeypatch.setattr(generate_types, "_ADCP_VERSION", _VERSION)
    monkeypatch.setattr(
        generate_types,
        "_http_get_with_retry",
        lambda url: calls.append(url) or b"{}",
    )

    with pytest.raises(RuntimeError, match="changed the selected release"):
        generate_types._prefetch_canonical_refs(temp_root)

    assert calls == []


def test_codegen_cannot_follow_remote_refs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(generate_types.subprocess, "run", fake_run)
    generate_types._run_datamodel_codegen(tmp_path / "input", tmp_path / "output")

    assert len(commands) == 1
    assert "--allow-remote-refs" not in commands[0]
    assert "--http-local-ref-path" in commands[0]
