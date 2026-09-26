#!/usr/bin/env python3
"""Fail-closed input and evidence tooling for reporting interop issue #1199.

This module intentionally does not turn a checkout, a dist-tag, or a release PR
into an acceptance input.  It validates the fixed four-cell topology, verifies
the immutable inputs that are currently available, and monitors the corrected
TypeScript release without mutating the pin manifest.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MANIFEST = Path(__file__).with_name("reporting_interop_inputs.json")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
NPM_REGISTRY = "https://registry.npmjs.org/"
MUTABLE_VERSIONS = {"latest", "next", "rc", "beta", "alpha", "canary", "main", "master", "head"}
HEX_40 = re.compile(r"^[0-9a-f]{40}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
EXACT_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:(?:[-+.]?)[A-Za-z0-9][A-Za-z0-9.-]*)?$")
EXPECTED_CELLS = (
    ("python-stable__typescript-stable", "stable", "stable", "stable"),
    ("python-stable__typescript-candidate", "stable", "candidate", "stable"),
    ("python-candidate__typescript-stable", "candidate", "stable", "stable"),
    ("python-candidate__typescript-candidate", "candidate", "candidate", "candidate"),
)
REQUIRED_ASSERTIONS = (
    "installed_python_artifact",
    "installed_typescript_artifact",
    "real_mcp_http",
    "initial_state_empty",
    "deterministic_fixture_identity",
    "managed_delivery_status",
    "reconciled_billing_status",
    "semantic_reconcile_reporting",
    "receipt_recorded",
    "exact_revision_read",
    "signed_webhook_rejected_first_attempt",
    "signed_webhook_retry_succeeded",
    "signed_webhook_replay_idempotent",
    "account_activity_retry_visible",
    "final_state_expected",
)


class InputError(ValueError):
    """An input cannot qualify for the requested mode."""


@dataclass(frozen=True)
class Validation:
    ready: bool
    missing: tuple[str, ...]


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InputError(f"cannot read manifest {path}: {error}") from error
    if not isinstance(value, dict):
        raise InputError("manifest root must be an object")
    return value


def _object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise InputError(f"{field} must be an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InputError(f"{field} must be a non-empty string")
    return value


def _exact_version(value: Any, field: str) -> str:
    version = _text(value, field)
    if version.lower() in MUTABLE_VERSIONS or not EXACT_VERSION.fullmatch(version):
        raise InputError(f"{field} must be an exact package version, got {version!r}")
    return version


def _https(value: Any, field: str) -> str:
    url = _text(value, field)
    if not url.startswith("https://") or any(
        token in url.lower() for token in ("/latest", "/main/", "/master/")
    ):
        raise InputError(f"{field} must be an immutable HTTPS URL, got {url!r}")
    return url


def _digest(value: Any, field: str, pattern: re.Pattern[str]) -> str:
    digest = _text(value, field).lower()
    if not pattern.fullmatch(digest):
        raise InputError(f"{field} has the wrong digest shape")
    return digest


def _validate_protocol(name: str, raw: Any) -> None:
    item = _object(raw, f"protocols.{name}")
    version = _exact_version(item.get("version"), f"protocols.{name}.version")
    if version != ("3.2.0-rc.3" if name == "stable" else "3.2.0-rc.4"):
        raise InputError(f"protocols.{name}.version is not the controlling protocol pin")
    _digest(item.get("target_commit"), f"protocols.{name}.target_commit", HEX_40)
    for key in (
        "release_url",
        "source_url",
        "checksum_url",
        "signature_url",
        "certificate_url",
    ):
        _https(item.get(key), f"protocols.{name}.{key}")
    for key in ("sha256", "checksum_sha256", "signature_sha256", "certificate_sha256"):
        _digest(item.get(key), f"protocols.{name}.{key}", HEX_64)


def _validate_python(role: str, raw: Any, *, acceptance: bool = False) -> None:
    item = _object(raw, f"artifacts.python.{role}")
    if item.get("package") != "adcp":
        raise InputError(f"artifacts.python.{role}.package must be 'adcp'")
    version = _exact_version(item.get("version"), f"artifacts.python.{role}.version")
    registry = _https(item.get("registry"), f"artifacts.python.{role}.registry")
    if not registry.endswith("/"):
        raise InputError(f"artifacts.python.{role}.registry must end with '/'")
    url = _https(item.get("wheel_url"), f"artifacts.python.{role}.wheel_url")
    _digest(item.get("wheel_sha256"), f"artifacts.python.{role}.wheel_sha256", HEX_64)
    if not url.endswith(".whl") or version.replace("-", "_") not in url.replace("-", "_"):
        raise InputError(f"artifacts.python.{role} wheel URL does not bind version {version}")
    if item.get("source_kind") in {"checkout", "vcs", "pr", "source_tree"}:
        raise InputError(f"artifacts.python.{role} is a source-only input")
    protocol = _text(item.get("protocol"), f"artifacts.python.{role}.protocol")
    expected_protocol = "3.2.0-rc.3" if role == "stable" else "3.2.0-rc.4"
    if protocol != expected_protocol:
        raise InputError(
            f"artifacts.python.{role}.protocol must be {expected_protocol}, got {protocol}"
        )
    if acceptance:
        _exact_version(item.get("python_version"), f"artifacts.python.{role}.python_version")
        _https(
            item.get("requirements_lock_url"),
            f"artifacts.python.{role}.requirements_lock_url",
        )
        _digest(
            item.get("requirements_lock_sha256"),
            f"artifacts.python.{role}.requirements_lock_sha256",
            HEX_64,
        )


def _validate_typescript(role: str, raw: Any, *, acceptance: bool = False) -> None:
    item = _object(raw, f"artifacts.typescript.{role}")
    if item.get("package") != "@adcp/sdk":
        raise InputError(f"artifacts.typescript.{role}.package must be '@adcp/sdk'")
    version = _exact_version(item.get("version"), f"artifacts.typescript.{role}.version")
    if role == "candidate" and version == "14.0.0-rc.41":
        raise InputError("14.0.0-rc.41 cannot substitute for the corrected TypeScript candidate")
    if item.get("registry") != NPM_REGISTRY:
        raise InputError(f"artifacts.typescript.{role}.registry must be {NPM_REGISTRY}")
    url = _https(item.get("tarball_url"), f"artifacts.typescript.{role}.tarball_url")
    expected_url = f"https://registry.npmjs.org/@adcp/sdk/-/sdk-{version}.tgz"
    if url != expected_url:
        raise InputError(f"artifacts.typescript.{role}.tarball_url must be {expected_url}")
    integrity = _text(item.get("integrity"), f"artifacts.typescript.{role}.integrity")
    if not integrity.startswith("sha512-"):
        raise InputError(f"artifacts.typescript.{role}.integrity must be sha512")
    try:
        decoded = base64.b64decode(integrity.removeprefix("sha512-"), validate=True)
    except ValueError as error:
        raise InputError(f"artifacts.typescript.{role}.integrity is not valid base64") from error
    if len(decoded) != 64:
        raise InputError(f"artifacts.typescript.{role}.integrity is not a SHA-512 digest")
    _digest(item.get("shasum"), f"artifacts.typescript.{role}.shasum", HEX_40)
    if acceptance:
        _exact_version(item.get("node_version"), f"artifacts.typescript.{role}.node_version")
        _exact_version(item.get("npm_version"), f"artifacts.typescript.{role}.npm_version")
        _https(item.get("lock_url"), f"artifacts.typescript.{role}.lock_url")
        _digest(
            item.get("lock_sha256"),
            f"artifacts.typescript.{role}.lock_sha256",
            HEX_64,
        )


def _validate_execution(manifest: Mapping[str, Any], *, acceptance: bool) -> None:
    execution = _object(manifest.get("execution"), "execution")
    if execution.get("database_encoding") != "UTF8" or execution.get("database_collation") != "C":
        raise InputError("execution database identity must remain UTF8/C")
    fixtures = _object(execution.get("fixture_identity"), "execution.fixture_identity")
    expected_fixture_keys = {
        "account_id",
        "consumer_id",
        "media_buy_id",
        "delivery_config_id",
        "reporting_obligation_id",
        "reporting_revision_id",
        "reporting_materialization_id",
    }
    if set(fixtures) != expected_fixture_keys:
        raise InputError("execution.fixture_identity must contain every deterministic identifier")
    for key, value in fixtures.items():
        _text(value, f"execution.fixture_identity.{key}")
    if not acceptance:
        return
    postgres_version = _text(execution.get("postgresql_version"), "execution.postgresql_version")
    if not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", postgres_version):
        raise InputError("execution.postgresql_version must be exact")
    _digest(execution.get("os_release_sha256"), "execution.os_release_sha256", HEX_64)
    for name in ("seller", "buyer"):
        path = _text(execution.get(f"{name}_entrypoint"), f"execution.{name}_entrypoint")
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise InputError(f"execution.{name}_entrypoint must be a repository-relative path")
        expected = _digest(
            execution.get(f"{name}_entrypoint_sha256"),
            f"execution.{name}_entrypoint_sha256",
            HEX_64,
        )
        resolved = (REPOSITORY_ROOT / relative).resolve()
        if REPOSITORY_ROOT not in resolved.parents or not resolved.is_file():
            raise InputError(f"execution.{name}_entrypoint does not exist: {path}")
        actual = _sha(resolved.read_bytes(), "sha256")
        if actual != expected:
            raise InputError(f"execution.{name}_entrypoint_sha256 does not match {path}")


def validate_manifest(manifest: Mapping[str, Any], *, acceptance: bool) -> Validation:
    if manifest.get("schema_version") != 1:
        raise InputError("schema_version must be 1")
    protocols = _object(manifest.get("protocols"), "protocols")
    if set(protocols) != {"stable", "candidate"}:
        raise InputError("protocols must contain exactly stable and candidate")
    for name, value in protocols.items():
        _validate_protocol(name, value)
    _validate_execution(manifest, acceptance=False)

    artifacts = _object(manifest.get("artifacts"), "artifacts")
    missing: list[str] = []
    for language, validator in (("python", _validate_python), ("typescript", _validate_typescript)):
        roles = _object(artifacts.get(language), f"artifacts.{language}")
        if set(roles) != {"stable", "candidate"}:
            raise InputError(f"artifacts.{language} must contain exactly stable and candidate")
        for role in ("stable", "candidate"):
            value = roles[role]
            if value is None:
                missing.append(f"{language}.{role}")
            else:
                validator(role, value, acceptance=False)

    typescript_candidate = _object(artifacts["typescript"], "artifacts.typescript").get("candidate")
    if typescript_candidate is not None:
        pending_candidate = _object(
            _object(manifest.get("pending"), "pending").get("typescript_candidate"),
            "pending.typescript_candidate",
        )
        proposed = _exact_version(
            pending_candidate.get("proposed_version"),
            "pending.typescript_candidate.proposed_version",
        )
        observed = _exact_version(
            _object(typescript_candidate, "artifacts.typescript.candidate").get("version"),
            "artifacts.typescript.candidate.version",
        )
        if observed != proposed:
            raise InputError(
                "artifacts.typescript.candidate must match the exact release proposed by #2979"
            )

    raw_cells = manifest.get("cells")
    if not isinstance(raw_cells, list) or len(raw_cells) != 4:
        raise InputError("cells must contain exactly four entries")
    actual: list[tuple[str, str, str, str]] = []
    for index, raw in enumerate(raw_cells):
        cell = _object(raw, f"cells[{index}]")
        if cell.get("required") is not True:
            raise InputError(f"cells[{index}] must remain required")
        actual.append(
            (
                _text(cell.get("id"), f"cells[{index}].id"),
                _text(cell.get("python"), f"cells[{index}].python"),
                _text(cell.get("typescript"), f"cells[{index}].typescript"),
                _text(cell.get("protocol"), f"cells[{index}].protocol"),
            )
        )
    if tuple(actual) != EXPECTED_CELLS:
        raise InputError(
            "cells must be the ordered stable/candidate cross-product with rc.3 skew "
            "and rc.4 candidate/candidate"
        )

    if acceptance and missing:
        raise InputError("acceptance inputs are incomplete: " + ", ".join(missing))
    if acceptance:
        _validate_execution(manifest, acceptance=True)
        for language, validator in (
            ("python", _validate_python),
            ("typescript", _validate_typescript),
        ):
            roles = _object(artifacts.get(language), f"artifacts.{language}")
            for role in ("stable", "candidate"):
                validator(role, roles[role], acceptance=True)
    if acceptance and manifest.get("phase") != "acceptance":
        raise InputError("phase must be 'acceptance' before an acceptance run")
    return Validation(ready=not missing, missing=tuple(missing))


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "adcp-reporting-interop/1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def _sha(data: bytes, algorithm: str) -> str:
    return hashlib.new(algorithm, data).hexdigest()


def _verify_python(item: Mapping[str, Any], output: Path) -> dict[str, Any]:
    metadata_url = f"{str(item['registry']).rstrip('/')}/pypi/adcp/{item['version']}/json"
    try:
        metadata = json.loads(_download(metadata_url))
    except urllib.error.HTTPError as error:
        raise InputError(
            f"Python registry does not publish adcp=={item['version']}: HTTP {error.code}"
        ) from error
    files = metadata.get("urls")
    if not isinstance(files, list):
        raise InputError("Python registry metadata has no distribution list")
    distributions = [
        entry
        for entry in files
        if isinstance(entry, dict) and entry.get("url") == item["wheel_url"]
    ]
    if len(distributions) != 1:
        raise InputError("Python wheel URL is not uniquely present in exact registry metadata")
    distribution = distributions[0]
    digests = _object(distribution.get("digests"), "Python registry distribution digests")
    if (
        distribution.get("packagetype") != "bdist_wheel"
        or digests.get("sha256") != item["wheel_sha256"]
    ):
        raise InputError("Python registry wheel metadata does not match the manifest")
    data = _download(str(item["wheel_url"]))
    actual = _sha(data, "sha256")
    if actual != item["wheel_sha256"]:
        raise InputError(
            f"Python wheel SHA-256 mismatch: expected {item['wheel_sha256']}, got {actual}"
        )
    path = output / Path(str(item["wheel_url"])).name
    path.write_bytes(data)
    return {
        "path": str(path),
        "bytes": len(data),
        "sha256": actual,
        "url": item["wheel_url"],
        "registry_metadata": {
            "url": metadata_url,
            "filename": distribution.get("filename"),
            "packagetype": distribution.get("packagetype"),
            "sha256": digests.get("sha256"),
        },
    }


def _verify_typescript(item: Mapping[str, Any], output: Path) -> dict[str, Any]:
    metadata_url = f"{NPM_REGISTRY}@adcp%2fsdk/{item['version']}"
    try:
        metadata = json.loads(_download(metadata_url))
    except urllib.error.HTTPError as error:
        raise InputError(
            f"npm does not publish @adcp/sdk@{item['version']}: HTTP {error.code}"
        ) from error
    dist = _object(metadata.get("dist"), "npm metadata dist")
    observed = {
        "version": metadata.get("version"),
        "tarball_url": dist.get("tarball"),
        "integrity": dist.get("integrity"),
        "shasum": dist.get("shasum"),
    }
    expected = {
        "version": item["version"],
        "tarball_url": item["tarball_url"],
        "integrity": item["integrity"],
        "shasum": item["shasum"],
    }
    if observed != expected:
        raise InputError(f"npm metadata mismatch: expected {expected}, got {observed}")
    data = _download(str(item["tarball_url"]))
    sha512 = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    integrity = f"sha512-{sha512}"
    shasum = _sha(data, "sha1")
    if integrity != item["integrity"] or shasum != item["shasum"]:
        raise InputError("downloaded npm tarball does not match registry integrity and shasum")
    path = output / f"adcp-sdk-{item['version']}.tgz"
    path.write_bytes(data)
    return {
        "path": str(path),
        "bytes": len(data),
        "integrity": integrity,
        "shasum": shasum,
        "url": item["tarball_url"],
        "registry_metadata": observed,
    }


def _verify_dependency_lock(
    item: Mapping[str, Any],
    *,
    language: str,
    role: str,
    output: Path,
) -> dict[str, Any] | None:
    url_key = "requirements_lock_url" if language == "python" else "lock_url"
    digest_key = "requirements_lock_sha256" if language == "python" else "lock_sha256"
    if item.get(url_key) is None and item.get(digest_key) is None:
        return None
    url = _https(item.get(url_key), f"artifacts.{language}.{role}.{url_key}")
    expected = _digest(item.get(digest_key), f"artifacts.{language}.{role}.{digest_key}", HEX_64)
    data = _download(url)
    actual = _sha(data, "sha256")
    if actual != expected:
        raise InputError(
            f"{language}.{role} dependency lock SHA-256 mismatch: "
            f"expected {expected}, got {actual}"
        )
    path = output / f"{language}-{role}-{Path(url).name}"
    path.write_bytes(data)
    return {"path": str(path), "url": url, "bytes": len(data), "sha256": actual}


def _verify_protocol(role: str, item: Mapping[str, Any], output: Path) -> dict[str, Any]:
    release_api_url = (
        "https://api.github.com/repos/adcontextprotocol/adcp/releases/tags/" f"v{item['version']}"
    )
    release = _object(json.loads(_download(release_api_url)), f"protocol {role} release")
    if (
        release.get("tag_name") != f"v{item['version']}"
        or release.get("target_commitish") != item["target_commit"]
        or release.get("html_url") != item["release_url"]
    ):
        raise InputError(f"protocol {role} release identity does not match the manifest")
    release_assets = release.get("assets")
    if not isinstance(release_assets, list):
        raise InputError(f"protocol {role} release assets are missing")
    observed_assets = {
        asset.get("browser_download_url"): asset.get("digest")
        for asset in release_assets
        if isinstance(asset, dict)
    }
    for url_key, digest_key in (
        ("source_url", "sha256"),
        ("checksum_url", "checksum_sha256"),
        ("signature_url", "signature_sha256"),
        ("certificate_url", "certificate_sha256"),
    ):
        if observed_assets.get(item[url_key]) != f"sha256:{item[digest_key]}":
            raise InputError(f"protocol {role} release metadata differs for {url_key}")
    release_path = output / f"protocol-{role}-release.json"
    release_path.write_text(json.dumps(release, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assets: dict[str, Any] = {}
    paths: dict[str, Path] = {}
    for name, url_key, digest_key in (
        ("source", "source_url", "sha256"),
        ("checksum", "checksum_url", "checksum_sha256"),
        ("signature", "signature_url", "signature_sha256"),
        ("certificate", "certificate_url", "certificate_sha256"),
    ):
        data = _download(str(item[url_key]))
        actual = _sha(data, "sha256")
        if actual != item[digest_key]:
            raise InputError(
                f"protocol {role} {name} SHA-256 mismatch: "
                f"expected {item[digest_key]}, got {actual}"
            )
        path = output / Path(str(item[url_key])).name
        path.write_bytes(data)
        paths[name] = path
        assets[name] = {
            "path": str(path),
            "url": item[url_key],
            "bytes": len(data),
            "sha256": actual,
        }

    checksum_text = paths["checksum"].read_text(encoding="ascii").strip().split()[0]
    if checksum_text != item["sha256"]:
        raise InputError(f"protocol {role} checksum sidecar does not name the source digest")
    cosign = shutil.which("cosign")
    if not cosign:
        raise InputError("cosign is required to verify signed protocol inputs")
    cosign_version = subprocess.run(
        [cosign, "version", "--json"], check=True, text=True, capture_output=True
    ).stdout.strip()
    argv = [
        cosign,
        "verify-blob",
        "--signature",
        str(paths["signature"]),
        "--certificate",
        str(paths["certificate"]),
        "--certificate-identity",
        "https://github.com/adcontextprotocol/adcp/.github/workflows/release.yml@refs/heads/main",
        "--certificate-oidc-issuer",
        "https://token.actions.githubusercontent.com",
        str(paths["source"]),
    ]
    completed = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if completed.returncode != 0:
        raise InputError(f"cosign rejected protocol {role}: {completed.stdout.strip()}")
    cosign_output = output / f"protocol-{role}-cosign.log"
    cosign_output.write_text(completed.stdout, encoding="utf-8")
    return {
        "version": item["version"],
        "target_commit": item["target_commit"],
        "release_url": item["release_url"],
        "release_metadata": _retained(release_path),
        "assets": assets,
        "cosign": {
            "argv": argv,
            "version": json.loads(cosign_version),
            "exit_code": completed.returncode,
            "output_path": str(cosign_output),
            "output_sha256": _sha(completed.stdout.encode("utf-8"), "sha256"),
        },
    }


def _run_logged(argv: list[str], *, cwd: Path, log: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    log.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        raise InputError(
            f"installed baseline command failed ({completed.returncode}): {' '.join(argv)}; "
            f"see {log}"
        )
    return completed


def _retained(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": str(path), "bytes": len(data), "sha256": _sha(data, "sha256")}


def _installed_baseline(
    *,
    output: Path,
    python_item: Mapping[str, Any],
    python_wheel: Path,
    typescript_item: Mapping[str, Any],
    typescript_tarball: Path,
) -> dict[str, Any]:
    """Install current registry inputs in isolation as non-acceptance diagnostics."""
    output = output.resolve()
    python_wheel = python_wheel.resolve()
    typescript_tarball = typescript_tarball.resolve()
    install_root = output / "installed-baseline"
    install_root.mkdir()
    python_root = install_root / "python"
    node_root = install_root / "node"
    python_root.mkdir()
    node_root.mkdir()
    python_executable = shutil.which("python3.12")
    uv = shutil.which("uv")
    npm = shutil.which("npm")
    node = shutil.which("node")
    if not all((python_executable, uv, npm, node)):
        raise InputError("installed baseline requires python3.12, uv, node, and npm")

    venv = python_root / "venv"
    _run_logged(
        [str(uv), "venv", "--python", str(python_executable), str(venv)],
        cwd=install_root,
        log=install_root / "python-venv.log",
    )
    cell_python = venv / "bin" / "python"
    _run_logged(
        [str(uv), "pip", "install", "--python", str(cell_python), str(python_wheel)],
        cwd=install_root,
        log=install_root / "python-install.log",
    )
    python_probe = _run_logged(
        [
            str(cell_python),
            "-I",
            "-c",
            (
                "import adcp,json; from importlib.metadata import version; "
                "from pathlib import Path; "
                "print(json.dumps({'package_version':version('adcp'),'protocol_version':"
                "Path(adcp.__file__).with_name('ADCP_VERSION').read_text().strip(),"
                "'module_origin':str(Path(adcp.__file__).resolve())},sort_keys=True))"
            ),
        ],
        cwd=install_root,
        log=install_root / "python-probe.json",
    )
    python_probe_value = json.loads(python_probe.stdout)
    if (
        python_probe_value.get("package_version") != python_item["version"]
        or python_probe_value.get("protocol_version") != python_item["protocol"]
        or str(venv.resolve()) not in python_probe_value.get("module_origin", "")
    ):
        raise InputError(f"installed Python baseline identity mismatch: {python_probe_value}")
    freeze = _run_logged(
        [str(uv), "pip", "freeze", "--python", str(cell_python)],
        cwd=install_root,
        log=install_root / "python-freeze.txt",
    )

    _run_logged([str(npm), "init", "-y"], cwd=node_root, log=install_root / "npm-init.log")
    _run_logged(
        [
            str(npm),
            "install",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--save-exact",
            str(typescript_tarball),
        ],
        cwd=node_root,
        log=install_root / "npm-install.log",
    )
    node_probe = _run_logged(
        [
            str(node),
            "--input-type=module",
            "-e",
            (
                "import {createRequire} from 'node:module'; "
                "import {reconcileReporting} from '@adcp/sdk'; "
                "const require=createRequire(import.meta.url); "
                "const pkg=require('@adcp/sdk/package.json'); "
                "console.log(JSON.stringify({package_version:pkg.version,"
                "reconcile_reporting_type:typeof reconcileReporting,"
                "module_origin:require.resolve('@adcp/sdk/package.json')}));"
            ),
        ],
        cwd=node_root,
        log=install_root / "node-probe.json",
    )
    node_probe_value = json.loads(node_probe.stdout)
    if (
        node_probe_value.get("package_version") != typescript_item["version"]
        or node_probe_value.get("reconcile_reporting_type") != "function"
        or str(node_root.resolve()) not in node_probe_value.get("module_origin", "")
    ):
        raise InputError(f"installed TypeScript baseline identity mismatch: {node_probe_value}")
    npm_tree = _run_logged(
        [str(npm), "ls", "--all", "--json"],
        cwd=node_root,
        log=install_root / "npm-tree.json",
    )

    retained_paths = [
        install_root / "python-venv.log",
        install_root / "python-install.log",
        install_root / "python-probe.json",
        install_root / "python-freeze.txt",
        install_root / "npm-init.log",
        install_root / "npm-install.log",
        install_root / "node-probe.json",
        install_root / "npm-tree.json",
        node_root / "package.json",
        node_root / "package-lock.json",
    ]
    return {
        "acceptance": False,
        "reason": "surface/install diagnostic only; no PostgreSQL reporting service or matrix cell",
        "python": python_probe_value,
        "typescript": node_probe_value,
        "runtime": {
            "python": python_probe.args[0],
            "node": subprocess.run(
                [str(node), "--version"], check=True, text=True, capture_output=True
            ).stdout.strip(),
            "npm": subprocess.run(
                [str(npm), "--version"], check=True, text=True, capture_output=True
            ).stdout.strip(),
            "os_release_sha256": _sha(Path("/etc/os-release").read_bytes(), "sha256"),
        },
        "dependency_counts": {
            "python_lines": len(freeze.stdout.splitlines()),
            "npm_tree_bytes": len(npm_tree.stdout.encode("utf-8")),
        },
        "evidence": [_retained(path) for path in retained_paths],
    }


def verify_available(
    manifest: Mapping[str, Any],
    output: Path,
    *,
    include_baseline: bool,
    installed_baseline: bool,
) -> dict[str, Any]:
    validation = validate_manifest(manifest, acceptance=False)
    output.mkdir(parents=True, exist_ok=False)
    evidence: dict[str, Any] = {
        "acceptance": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "missing_acceptance_inputs": list(validation.missing),
        "artifacts": {},
        "protocols": {},
    }
    for role, item in _object(manifest["protocols"], "protocols").items():
        evidence["protocols"][role] = _verify_protocol(
            role, _object(item, f"protocols.{role}"), output
        )
    artifacts = _object(manifest["artifacts"], "artifacts")
    verified_paths: dict[str, Path] = {}
    for language in ("python", "typescript"):
        for role in ("stable", "candidate"):
            item = _object(artifacts[language], f"artifacts.{language}").get(role)
            if item is None:
                continue
            key = f"{language}.{role}"
            evidence["artifacts"][key] = (
                _verify_python(item, output)
                if language == "python"
                else _verify_typescript(item, output)
            )
            dependency_lock = _verify_dependency_lock(
                item, language=language, role=role, output=output
            )
            if dependency_lock is not None:
                evidence["artifacts"][key]["dependency_lock"] = dependency_lock
            verified_paths[key] = Path(evidence["artifacts"][key]["path"])
    if include_baseline:
        baseline = _object(
            _object(manifest.get("baseline_inputs"), "baseline_inputs").get("python_registry"),
            "baseline_inputs.python_registry",
        )
        if baseline.get("acceptance") is not False:
            raise InputError("baseline Python input must be explicitly non-acceptance")
        evidence["artifacts"]["python.registry-baseline"] = _verify_python(baseline, output)
        verified_paths["python.registry-baseline"] = Path(
            evidence["artifacts"]["python.registry-baseline"]["path"]
        )
    if installed_baseline:
        if not include_baseline:
            raise InputError("--installed-baseline requires --include-baseline")
        typescript_stable = _object(
            _object(artifacts.get("typescript"), "artifacts.typescript").get("stable"),
            "artifacts.typescript.stable",
        )
        evidence["installed_baseline"] = _installed_baseline(
            output=output,
            python_item=baseline,
            python_wheel=verified_paths["python.registry-baseline"],
            typescript_item=typescript_stable,
            typescript_tarball=verified_paths["typescript.stable"],
        )
    evidence_path = output / "evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return evidence


def _run_json(argv: Iterable[str]) -> Any:
    try:
        completed = subprocess.run(list(argv), check=True, text=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as error:
        detail = (
            error.stderr.strip()
            if isinstance(error, subprocess.CalledProcessError) and error.stderr
            else str(error)
        )
        raise InputError(f"command failed: {detail}") from error
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise InputError(f"command did not return JSON: {' '.join(argv)}") from error


def monitor_typescript(manifest: Mapping[str, Any]) -> dict[str, Any]:
    pending = _object(
        _object(manifest.get("pending"), "pending").get("typescript_candidate"),
        "pending.typescript_candidate",
    )
    repository = _text(pending.get("repository"), "pending.typescript_candidate.repository")
    release_pr = pending.get("release_pr")
    if not isinstance(release_pr, int):
        raise InputError("pending.typescript_candidate.release_pr must be an integer")
    expected_source = _digest(
        pending.get("source_merge_commit"),
        "pending.typescript_candidate.source_merge_commit",
        HEX_40,
    )
    proposed = _exact_version(
        pending.get("proposed_version"), "pending.typescript_candidate.proposed_version"
    )
    forbidden = _exact_version(
        pending.get("forbidden_substitute"), "pending.typescript_candidate.forbidden_substitute"
    )
    if proposed == forbidden:
        raise InputError("proposed TypeScript candidate equals the forbidden prior release")

    pr = _run_json(("gh", "api", f"repos/{repository}/pulls/{release_pr}"))
    body = str(pr.get("body") or "")
    body_version = re.search(r"@adcp/sdk@([0-9A-Za-z.-]+)", body)
    if not body_version or body_version.group(1) != proposed:
        raise InputError(f"release PR does not propose exactly @adcp/sdk@{proposed}")
    if (
        expected_source[:7] not in body
        or _object(pr.get("base"), "release PR base").get("ref") != "main"
    ):
        raise InputError("release PR is not bound to the expected source change on main")
    result: dict[str, Any] = {
        "ready": False,
        "acceptance": False,
        "repository": repository,
        "release_pr": release_pr,
        "release_pr_url": pr.get("html_url"),
        "release_pr_state": pr.get("state"),
        "release_pr_merged": bool(pr.get("merged")),
        "release_pr_merge_commit": pr.get("merge_commit_sha"),
        "source_merge_commit": expected_source,
        "proposed_version": proposed,
    }
    if not pr.get("merged"):
        result["reason"] = "release PR is not merged; registry lookup is intentionally not accepted"
        return result

    merge_commit = _digest(pr.get("merge_commit_sha"), "release PR merge_commit_sha", HEX_40)
    runs = _run_json(
        (
            "gh",
            "api",
            f"repos/{repository}/actions/runs?head_sha={merge_commit}&event=push&per_page=100",
        )
    )
    workflow_runs = _object(runs, "release workflow runs").get("workflow_runs")
    if not isinstance(workflow_runs, list):
        raise InputError("GitHub did not return release workflow runs")
    successful_release_runs = [
        run
        for run in workflow_runs
        if isinstance(run, dict)
        and "release" in str(run.get("name", "")).lower()
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("head_sha") == merge_commit
    ]
    if not successful_release_runs:
        result["reason"] = "release PR merged but no successful exact-merge release workflow exists"
        return result

    try:
        metadata = json.loads(_download(f"{NPM_REGISTRY}@adcp%2fsdk/{proposed}"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            result["reason"] = "release PR merged but exact npm package is not published"
            return result
        raise
    dist = _object(metadata.get("dist"), "npm metadata dist")
    if metadata.get("version") != proposed:
        raise InputError("npm returned a mismatched package version")
    integrity = _text(dist.get("integrity"), "npm dist.integrity")
    shasum = _digest(dist.get("shasum"), "npm dist.shasum", HEX_40)
    tarball_url = _https(dist.get("tarball"), "npm dist.tarball")
    package = {
        "package": "@adcp/sdk",
        "version": proposed,
        "registry": NPM_REGISTRY,
        "tarball_url": tarball_url,
        "integrity": integrity,
        "shasum": shasum,
    }
    _validate_typescript("candidate", package)
    with tempfile.TemporaryDirectory(prefix="adcp-ts-candidate-") as temporary:
        verified = _verify_typescript(package, Path(temporary))
    result.update(
        {
            "ready": True,
            "reason": "exact merged release workflow succeeded and registry bytes verify",
            "release_workflows": [
                {
                    "database_id": run.get("id"),
                    "name": run.get("name"),
                    "head_sha": run.get("head_sha"),
                    "conclusion": run.get("conclusion"),
                    "html_url": run.get("html_url"),
                }
                for run in successful_release_runs
            ],
            "candidate": package,
            "verification": {key: value for key, value in verified.items() if key != "path"},
        }
    )
    return result


def _expected_input(
    manifest: Mapping[str, Any], cell: Mapping[str, Any], language: str
) -> Mapping[str, Any]:
    artifacts = _object(manifest.get("artifacts"), "artifacts")
    role = _text(cell.get(language), f"cell.{language}")
    return _object(
        _object(artifacts.get(language), f"artifacts.{language}").get(role),
        f"artifacts.{language}.{role}",
    )


def _validate_evidence_files(raw: Any, *, run_root: Path, field: str) -> None:
    if not isinstance(raw, list) or not raw:
        raise InputError(f"{field} must retain at least one evidence file")
    root = run_root.resolve()
    for index, entry_raw in enumerate(raw):
        entry = _object(entry_raw, f"{field}[{index}]")
        relative = Path(_text(entry.get("path"), f"{field}[{index}].path"))
        if relative.is_absolute():
            raise InputError(f"{field}[{index}].path must be relative to the run root")
        path = (root / relative).resolve()
        if root not in path.parents:
            raise InputError(f"{field}[{index}].path escapes the run root")
        if not path.is_file():
            raise InputError(f"retained evidence is missing: {relative}")
        data = path.read_bytes()
        if entry.get("bytes") != len(data):
            raise InputError(f"retained evidence byte count changed: {relative}")
        expected = _digest(entry.get("sha256"), f"{field}[{index}].sha256", HEX_64)
        if _sha(data, "sha256") != expected:
            raise InputError(f"retained evidence digest changed: {relative}")


def validate_results(
    manifest: Mapping[str, Any], results: Mapping[str, Any], *, run_root: Path
) -> None:
    """Validate the retained legacy result shape without granting current acceptance."""

    validate_manifest(manifest, acceptance=True)
    if results.get("acceptance") is not True or results.get("status") != "passed":
        raise InputError("aggregate result is not a passed acceptance result")
    raw_cells = results.get("cells")
    if not isinstance(raw_cells, list) or len(raw_cells) != 4:
        raise InputError("aggregate result must contain exactly four cells")
    expected_cells = manifest["cells"]
    process_ids: set[int] = set()
    databases: set[str] = set()
    state_directories: set[str] = set()
    for index, (raw, expected_raw) in enumerate(zip(raw_cells, expected_cells, strict=True)):
        cell = _object(raw, f"results.cells[{index}]")
        expected = _object(expected_raw, f"cells[{index}]")
        if cell.get("id") != expected.get("id") or cell.get("status") != "passed":
            raise InputError(f"results.cells[{index}] is missing or not passed")
        if cell.get("acceptance") is not True:
            raise InputError(f"results.cells[{index}] is not acceptance evidence")
        pid = cell.get("seller_pid")
        if not isinstance(pid, int) or pid <= 1 or pid in process_ids:
            raise InputError("every cell must record a distinct seller process")
        process_ids.add(pid)
        database = _text(cell.get("database"), f"results.cells[{index}].database")
        state_directory = _text(
            cell.get("state_directory"), f"results.cells[{index}].state_directory"
        )
        if database in databases or state_directory in state_directories:
            raise InputError("every cell must use an isolated database and state directory")
        databases.add(database)
        state_directories.add(state_directory)

        assertions = _object(cell.get("assertions"), f"results.cells[{index}].assertions")
        if tuple(assertions) != REQUIRED_ASSERTIONS or not all(
            value is True for value in assertions.values()
        ):
            raise InputError(
                f"results.cells[{index}] does not pass every required semantic assertion"
            )

        observed_inputs = _object(cell.get("inputs"), f"results.cells[{index}].inputs")
        python_expected = _expected_input(manifest, expected, "python")
        typescript_expected = _expected_input(manifest, expected, "typescript")
        protocol_expected = _object(
            _object(manifest.get("protocols"), "protocols").get(expected["protocol"]),
            f"protocols.{expected['protocol']}",
        )
        expected_identity = {
            "python": {
                "version": python_expected["version"],
                "wheel_sha256": python_expected["wheel_sha256"],
                "requirements_lock_sha256": python_expected["requirements_lock_sha256"],
            },
            "typescript": {
                "version": typescript_expected["version"],
                "integrity": typescript_expected["integrity"],
                "lock_sha256": typescript_expected["lock_sha256"],
            },
            "protocol": {
                "version": protocol_expected["version"],
                "sha256": protocol_expected["sha256"],
            },
        }
        if observed_inputs != expected_identity:
            raise InputError(
                f"results.cells[{index}] artifact identity does not match the manifest"
            )
        observed_runtime = _object(cell.get("runtime"), f"results.cells[{index}].runtime")
        execution = _object(manifest.get("execution"), "execution")
        expected_runtime = {
            "python": python_expected["python_version"],
            "node": typescript_expected["node_version"],
            "npm": typescript_expected["npm_version"],
            "postgresql": execution["postgresql_version"],
            "os_release_sha256": execution["os_release_sha256"],
            "database_encoding": execution["database_encoding"],
            "database_collation": execution["database_collation"],
        }
        if observed_runtime != expected_runtime:
            raise InputError(f"results.cells[{index}] runtime identity does not match")
        _validate_evidence_files(
            cell.get("evidence"), run_root=run_root, field=f"results.cells[{index}].evidence"
        )

    _validate_evidence_files(results.get("evidence"), run_root=run_root, field="results.evidence")


def _write_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--acceptance", action="store_true")
    verify = subparsers.add_parser("verify-available")
    verify.add_argument("--output", type=Path, required=True)
    verify.add_argument("--include-baseline", action="store_true")
    verify.add_argument("--installed-baseline", action="store_true")
    subparsers.add_parser("monitor-typescript")
    results = subparsers.add_parser("validate-results")
    results.add_argument("--results", type=Path, required=True)
    results.add_argument("--run-root", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        manifest = _load(args.manifest)
        if args.command == "validate":
            result = validate_manifest(manifest, acceptance=args.acceptance)
            _write_json(
                {
                    "acceptance": args.acceptance,
                    "ready": result.ready,
                    "missing": list(result.missing),
                }
            )
            return 0
        if args.command == "verify-available":
            _write_json(
                verify_available(
                    manifest,
                    args.output,
                    include_baseline=args.include_baseline,
                    installed_baseline=args.installed_baseline,
                )
            )
            return 0
        if args.command == "monitor-typescript":
            result = monitor_typescript(manifest)
            _write_json(result)
            return 0 if result["ready"] else 3
        raw_results = _load(args.results)
        validate_results(manifest, raw_results, run_root=args.run_root)
        _write_json(
            {
                "acceptance": False,
                "blocking_acceptance": False,
                "status": "legacy_result_shape_validated_nonaccepting",
                "validated_results": str(args.results),
                "limitation": (
                    "obsolete four-cell topology and synthetic assertions cannot confer "
                    "current reporting matrix acceptance"
                ),
            }
        )
        return 0
    except InputError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
