#!/usr/bin/env python3
"""Fresh-process/fresh-database staged reporting foundation runner.

The current executable lane is the Python Core reference control.  The result
document remains non-acceptance until every blocking language quadrant and
supported-skew cell has an implemented launcher and a semantic pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tarfile
import time
import zipfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
PYTHON_SERVER = HERE / "python_core_server.py"
PYTHON_CLIENT = HERE / "python_core_client.py"
PYTHON_DEPENDENCY_FLOOR = HERE / "python_dependency_floor_probe.py"
TS_SERVER = HERE / "ts_core_server.cjs"
TS_CORE_BUYER = HERE / "ts_core_buyer.cjs"
TS_STORYBOARDS = HERE / "ts_storyboard_inventory.cjs"
STORYBOARD_ORCHESTRATION = HERE / "storyboard_orchestration.py"
OFFICIAL_PRECEDENCE = HERE / "official_precedence" / "repro-candidate.cjs"
OFFICIAL_CASES = HERE / "official_precedence" / "cases.json"
SIGNING_RUNNER = HERE / "signing" / "run-signing-vectors.cjs"
WEBHOOK_VECTORS_PYTHON = HERE / "signing" / "webhook_vectors_python.original.py.txt"
WEBHOOK_VECTORS_TYPESCRIPT = HERE / "signing" / "webhook_vectors_typescript.cjs"
WEBHOOK_REVOCATION_STATE = HERE / "signing" / "webhook_revocation_state.py"
WEBHOOK_SIGN_PYTHON = HERE / "signing" / "signer_roundtrip_python.original.py.txt"
WEBHOOK_VERIFY_TYPESCRIPT = HERE / "signing" / "crossverify_typescript.cjs"
WEBHOOK_SIGN_TYPESCRIPT = HERE / "signing" / "sign_webhook_typescript.cjs"
WEBHOOK_VERIFY_PYTHON = HERE / "signing" / "crossverify_python.py"
WEBHOOK_RECEIVER_SERVER = HERE / "signing" / "webhook_receiver_server.py"
WEBHOOK_RECEIVER_CLIENT = HERE / "signing" / "webhook_receiver_client.py"
WEBHOOK_VECTORS_AGGREGATE_SHA256 = (
    "b94b7597b3640d874406654a09296ffe4c6f333876bf9845919f133d49dfec52"
)
RESOURCE_SENTINELS = HERE / "security" / "resource_location_sentinels.json"
RESOURCE_PYTHON = HERE / "security" / "resource_location_python.original.py.txt"
RESOURCE_TYPESCRIPT = HERE / "security" / "resource_location_typescript.cjs"
APPLICABILITY = HERE / "applicability.json"
PINS = HERE / "pins.json"
TS_CORE_PRIVATE_ENV = {
    "token_a": "ADCP_INTEROP_TS_CORE_AUTH_TOKEN_A",
    "token_b": "ADCP_INTEROP_TS_CORE_AUTH_TOKEN_B",
    "startup_proof": "ADCP_INTEROP_TS_CORE_STARTUP_PROOF",
}
NARROW_RUNTIME_ENV_KEYS = frozenset(
    {"PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR"}
)


class HarnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class CoreSellerCredentials:
    token_a: str
    token_b: str
    startup_proof: str


@dataclass(frozen=True)
class PythonRuntime:
    route: str
    executable: Path


@dataclass(frozen=True)
class PythonArtifact:
    route: str
    path: Path


@dataclass(frozen=True)
class PythonDependencyRuntime:
    label: str
    executable: Path
    pydantic: str
    mcp: str
    artifact_route: str


@dataclass(frozen=True)
class PreviousPythonInput:
    executable: Path
    artifact: Path


@dataclass(frozen=True)
class TypeScriptInstall:
    role: str
    root: Path

    @property
    def lock(self) -> Path:
        return self.root / "package-lock.json"


@dataclass(frozen=True)
class TypeScriptArchive:
    role: str
    path: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _selected_python_build() -> dict[str, Any]:
    candidate = json.loads(PINS.read_text(encoding="utf-8"))["python"]["candidate"]
    for build in candidate["known_independent_builds"]:
        if build["builder"] == "reporting_interop_harness":
            return build
    raise HarnessError("pins omit the reporting-interop harness build identity")


def _python_wheel_sdk_identity(path: Path) -> dict[str, Any]:
    members: dict[str, str] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir() or not info.filename.startswith("adcp/"):
                    continue
                if info.filename in members:
                    raise HarnessError(f"duplicate SDK member in wheel: {info.filename}")
                members[info.filename] = hashlib.sha256(archive.read(info)).hexdigest()
    except zipfile.BadZipFile as error:
        raise HarnessError(f"previous Python artifact is not a valid wheel: {path}") from error
    if not members:
        raise HarnessError(f"Python wheel contains no SDK members: {path}")
    return {
        "sdk_member_count": len(members),
        "sdk_member_manifest_sha256": hashlib.sha256(
            json.dumps(members, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _expected_python_member_identity(
    runtime: PythonRuntime, artifact: PythonArtifact
) -> dict[str, Any]:
    if runtime.route != "previous":
        selected = _selected_python_build()
        return {
            "source": "selected_candidate_build_manifest",
            "sdk_member_count": selected["sdk_member_count"],
            "sdk_member_manifest_sha256": selected["sdk_member_manifest_sha256"],
        }
    previous = json.loads(PINS.read_text(encoding="utf-8"))["python"]["previous_released"]
    observed_hash = _sha256(artifact.path)
    if observed_hash != previous["wheel_sha256"]:
        raise HarnessError(
            f"previous Python artifact does not match its immutable wheel pin: {observed_hash}"
        )
    return {
        "source": "exact_previous_wheel_members",
        **_python_wheel_sdk_identity(artifact.path),
    }


def _validate_python_artifact_inputs(artifacts: dict[str, PythonArtifact]) -> None:
    build = _selected_python_build()
    expected = {
        "wheel": build["vcs_wheel_sha256"],
        "sdist": build["sdist_sha256"],
    }
    for route, artifact in artifacts.items():
        observed = _sha256(artifact.path)
        if observed != expected[route]:
            raise HarnessError(
                f"{route} artifact is not the selected independent harness build: {observed}"
            )


def _tree_aggregate_sha256(root: Path) -> tuple[str, dict[str, str]]:
    entries: dict[str, str] = {}
    aggregate = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = f"./{path.relative_to(root).as_posix()}"
        digest = _sha256(path)
        entries[relative] = digest
        aggregate.update(f"{digest}  {relative}\n".encode())
    return aggregate.hexdigest(), entries


def _parse_runtime(value: str) -> PythonRuntime:
    route, separator, raw_path = value.partition("=")
    if not separator or route not in {"wheel", "sdist"}:
        raise argparse.ArgumentTypeError("runtime must be wheel=/path or sdist=/path")
    executable = Path(os.path.abspath(raw_path))
    if not executable.is_file():
        raise argparse.ArgumentTypeError(f"runtime executable is unavailable: {executable}")
    return PythonRuntime(route, executable)


def _parse_artifact(value: str) -> PythonArtifact:
    route, separator, raw_path = value.partition("=")
    if not separator or route not in {"wheel", "sdist"}:
        raise argparse.ArgumentTypeError("artifact must be wheel=/path or sdist=/path")
    path = Path(os.path.abspath(raw_path))
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"artifact is unavailable: {path}")
    if route == "wheel" and path.suffix != ".whl":
        raise argparse.ArgumentTypeError("wheel artifact must end in .whl")
    if route == "sdist" and not path.name.endswith(".tar.gz"):
        raise argparse.ArgumentTypeError("sdist artifact must end in .tar.gz")
    return PythonArtifact(route, path)


def _parse_dependency_runtime(value: str) -> PythonDependencyRuntime:
    label, separator, raw = value.partition("=")
    parts = raw.split(",")
    if not separator or not label or len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "dependency runtime must be label=/python,PD_VERSION,MCP_VERSION,wheel|sdist"
        )
    raw_path, pydantic, mcp, artifact_route = parts
    executable = Path(os.path.abspath(raw_path))
    if not executable.is_file() or artifact_route not in {"wheel", "sdist"}:
        raise argparse.ArgumentTypeError("invalid dependency runtime executable or artifact route")
    return PythonDependencyRuntime(label, executable, pydantic, mcp, artifact_route)


def _parse_typescript_install(value: str) -> TypeScriptInstall:
    role, separator, raw_path = value.partition("=")
    if not separator or role not in {"candidate", "historical_candidate", "previous", "lead"}:
        raise argparse.ArgumentTypeError(
            "TypeScript install must be candidate=/path, historical_candidate=/path, "
            "previous=/path, or lead=/path"
        )
    root = Path(os.path.abspath(raw_path))
    if not (root / "package-lock.json").is_file():
        raise argparse.ArgumentTypeError(f"TypeScript package lock is unavailable: {root}")
    return TypeScriptInstall(role, root)


def _parse_typescript_archive(value: str) -> TypeScriptArchive:
    role, separator, raw_path = value.partition("=")
    if not separator or role not in {"candidate", "historical_candidate", "previous", "lead"}:
        raise argparse.ArgumentTypeError(
            "TypeScript archive must be candidate=/path, historical_candidate=/path, "
            "previous=/path, or lead=/path"
        )
    path = Path(os.path.abspath(raw_path))
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"TypeScript archive is unavailable: {path}")
    return TypeScriptArchive(role, path)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-runtime", action="append", type=_parse_runtime, required=True)
    parser.add_argument("--python-artifact", action="append", type=_parse_artifact, required=True)
    parser.add_argument("--previous-python-runtime", type=Path)
    parser.add_argument("--previous-python-artifact", type=Path)
    parser.add_argument(
        "--python-dependency-runtime",
        action="append",
        type=_parse_dependency_runtime,
        default=[],
    )
    parser.add_argument("--node-runtime", type=Path)
    parser.add_argument(
        "--typescript-install", action="append", type=_parse_typescript_install, default=[]
    )
    parser.add_argument(
        "--typescript-archive", action="append", type=_parse_typescript_archive, default=[]
    )
    parser.add_argument("--pg-admin-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--keep-databases", action="store_true")
    return parser.parse_args()


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _runtime_identity(runtime: PythonRuntime, artifact: PythonArtifact) -> dict[str, Any]:
    code = (
        "import hashlib,json,sys; "
        "from importlib.metadata import distribution,distributions,version; "
        "packages=sorted((d.metadata['Name'].lower(),d.version) for d in distributions() "
        "if d.metadata['Name']); "
        "dist=distribution('adcp'); direct_raw=dist.read_text('direct_url.json'); "
        "members={p.as_posix():hashlib.sha256(dist.locate_file(p).read_bytes()).hexdigest() "
        "for p in (dist.files or []) if p.as_posix().startswith('adcp/') "
        "and dist.locate_file(p).is_file()}; "
        "print(json.dumps({'python':sys.version.split()[0],'adcp':version('adcp'),"
        "'installed_distributions':packages,'direct_url':json.loads(direct_raw) "
        "if direct_raw else None,'sdk_member_count':len(members),"
        "'sdk_member_manifest_sha256':hashlib.sha256(json.dumps(members,sort_keys=True,"
        "separators=(',',':')).encode()).hexdigest()}))"
    )
    completed = subprocess.run(
        [str(runtime.executable), "-I", "-c", code],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    identity = json.loads(completed.stdout)
    if not str(identity["python"]).startswith("3.10."):
        raise HarnessError(f"{runtime.route} runtime is not Python 3.10: {identity}")
    expected_url = artifact.path.as_uri()
    if identity.get("direct_url", {}).get("url") != expected_url:
        raise HarnessError(
            f"{runtime.route} runtime was not installed from the supplied local artifact path"
        )
    expected_members = _expected_python_member_identity(runtime, artifact)
    if (
        identity.get("sdk_member_count") != expected_members["sdk_member_count"]
        or identity.get("sdk_member_manifest_sha256")
        != expected_members["sdk_member_manifest_sha256"]
    ):
        raise HarnessError(
            f"{runtime.route} installed SDK members differ from {expected_members['source']}"
        )
    return {
        **identity,
        "route": runtime.route,
        "executable": str(runtime.executable),
        "executable_sha256": _sha256(runtime.executable),
        "installed_member_expectation": expected_members,
        "artifact": {
            "path": str(artifact.path),
            "filename": artifact.path.name,
            "bytes": artifact.path.stat().st_size,
            "sha256": _sha256(artifact.path),
        },
    }


def _run_dependency_control(
    runtime: PythonDependencyRuntime,
    artifact: PythonArtifact,
    *,
    output: Path,
) -> dict[str, Any]:
    report = _run_json(
        [
            str(runtime.executable),
            "-I",
            str(PYTHON_DEPENDENCY_FLOOR),
            "--artifact",
            str(artifact.path),
            "--expected-pydantic",
            runtime.pydantic,
            "--expected-mcp",
            runtime.mcp,
        ],
        cwd=output,
        env=os.environ.copy(),
    )
    controls = report.get("reporting_model_controls", {})
    installed_members = report.get("installed_sdk_members", {})
    selected = _selected_python_build()
    if (
        report.get("python") != "3.10.21"
        or report.get("dependencies") != {"pydantic": runtime.pydantic, "mcp": runtime.mcp}
        or controls.get("py_public_001", {}).get("required_correct") is not True
        or controls.get("py_public_002", {}).get("required_correct") is not True
        or controls.get("rlx_py_002", {}).get("required_correct") is not True
        or installed_members.get("count") != selected["sdk_member_count"]
        or installed_members.get("manifest_sha256") != selected["sdk_member_manifest_sha256"]
    ):
        raise HarnessError(f"Python dependency control changed for {runtime.label}")
    path = output / f"python-dependency-{runtime.label}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "label": runtime.label,
        "status": "required_correct_controls_passed",
        "artifact_route": runtime.artifact_route,
        "result": report,
        "evidence": _retained(path, output),
    }


def _git_identity() -> dict[str, Any]:
    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
        "dirty": bool(git("status", "--porcelain", "--untracked-files=all")),
    }


def _database_url(admin_url: str, name: str) -> str:
    fields = conninfo_to_dict(admin_url)
    fields["dbname"] = name
    return make_conninfo(**fields)


def _node_database_url(admin_url: str, name: str) -> str:
    fields = conninfo_to_dict(admin_url)
    user = quote(fields.pop("user", ""), safe="")
    password = fields.pop("password", None)
    credentials = user
    if password is not None:
        credentials += f":{quote(password, safe='')}"
    if credentials:
        credentials += "@"
    host = fields.pop("host", "127.0.0.1")
    port = fields.pop("port", "5432")
    fields.pop("dbname", None)
    query = urlencode(fields)
    suffix = f"?{query}" if query else ""
    return f"postgresql://{credentials}{host}:{port}/{quote(name, safe='')}{suffix}"


def _create_database(admin_url: str, name: str) -> dict[str, str]:
    with psycopg.connect(admin_url, autocommit=True) as connection:
        statement = sql.SQL(
            "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'"
        ).format(sql.Identifier(name))
        connection.execute(statement)
    url = _database_url(admin_url, name)
    with psycopg.connect(url) as connection:
        row = connection.execute(
            "SELECT current_database(), pg_encoding_to_char(encoding), datcollate "
            "FROM pg_database WHERE datname=current_database()"
        ).fetchone()
        version = connection.execute("SHOW server_version").fetchone()
    if row != (name, "UTF8", "C") or version is None:
        raise HarnessError(f"database identity is not UTF8/C: {row}")
    return {"name": row[0], "encoding": row[1], "collation": row[2], "version": version[0]}


def _drop_database(admin_url: str, name: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _wait_port(process: subprocess.Popen[bytes], port: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise HarnessError(f"seller exited during startup with {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise HarnessError("seller did not bind its port before the deadline")


def _run_json(command: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise HarnessError(
            f"JSON probe emitted invalid output: {completed.stdout[:500]}"
        ) from error
    if completed.returncode != 0:
        raise HarnessError(
            f"JSON probe failed with {completed.returncode}: {result.get('error', result)}"
        )
    return result


def _narrow_child_environment(
    *,
    private_bindings: Mapping[str, str],
    configuration: Mapping[str, str] | None = None,
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an explicit child environment without forwarding ambient credentials."""

    source = os.environ if parent is None else parent
    environment = {key: value for key, value in source.items() if key in NARROW_RUNTIME_ENV_KEYS}
    if configuration is not None:
        environment.update(configuration)
    environment.update(private_bindings)
    return environment


def _buyer_environment(token: str, *, parent: Mapping[str, str] | None = None) -> dict[str, str]:
    return _narrow_child_environment(
        private_bindings={"ADCP_INTEROP_BUYER_TOKEN": token},
        parent=parent,
    )


def _typescript_pin(install: TypeScriptInstall) -> dict[str, Any]:
    pins = json.loads(PINS.read_text(encoding="utf-8"))["typescript"]
    key = {
        "candidate": "development_candidate_rc47",
        "historical_candidate": "historical_candidate_rc45",
        "previous": "previous_compatible",
        "lead": "newer_rc_lead",
    }[install.role]
    pin = pins[key]
    if install.role == "candidate" and pin.get("executable_harness_input") is not True:
        raise HarnessError(
            "the rc.6 candidate is not selected as an executable lock/member binding; "
            "historical rc.45 cannot substitute for required Q-cell execution"
        )
    return pin


def _typescript_archive_identity(
    install: TypeScriptInstall, archive_input: TypeScriptArchive
) -> dict[str, Any]:
    """Bind the complete installed SDK tree to its exact immutable archive before use."""

    if archive_input.role != install.role:
        raise HarnessError("TypeScript archive and install roles differ")
    pin = _typescript_pin(install)
    archive_hash = _sha256(archive_input.path)
    if archive_hash != pin["tarball_sha256"]:
        raise HarnessError(f"{install.role} TypeScript archive hash differs from its immutable pin")
    lock_hash = _sha256(install.lock)
    if lock_hash != pin["package_lock_sha256"]:
        raise HarnessError(f"{install.role} TypeScript lock differs from its immutable pin")
    package_root = install.root / "node_modules/@adcp/sdk"
    package_path = package_root / "package.json"
    lock = json.loads(install.lock.read_text(encoding="utf-8"))
    package = json.loads(package_path.read_text(encoding="utf-8"))
    locked = lock.get("packages", {}).get("node_modules/@adcp/sdk", {})
    if (
        package.get("version") != pin["version"]
        or locked.get("version") != pin["version"]
        or locked.get("integrity") != pin["integrity"]
    ):
        raise HarnessError(f"{install.role} TypeScript install metadata differs from its pin")
    archived: dict[str, str] = {}
    try:
        with tarfile.open(archive_input.path, "r:gz") as stream:
            for member in stream.getmembers():
                if member.isdir():
                    continue
                if not member.isfile() or not member.name.startswith("package/"):
                    raise HarnessError(f"unsupported TypeScript archive member {member.name!r}")
                relative = member.name.removeprefix("package/")
                if (
                    not relative
                    or relative.startswith("/")
                    or ".." in Path(relative).parts
                    or relative in archived
                ):
                    raise HarnessError(f"unsafe or duplicate archive member {member.name!r}")
                extracted = stream.extractfile(member)
                if extracted is None:
                    raise HarnessError(f"cannot read archive member {member.name!r}")
                archived[relative] = hashlib.sha256(extracted.read()).hexdigest()
    except tarfile.TarError as error:
        raise HarnessError(f"invalid TypeScript archive {archive_input.path}: {error}") from error
    expected_members = pin.get("tarball_members")
    if expected_members is not None and len(archived) != expected_members:
        raise HarnessError(f"{install.role} archive member count differs from its immutable pin")
    installed = {
        path.relative_to(package_root).as_posix(): _sha256(path)
        for path in sorted(package_root.rglob("*"))
        if path.is_file()
    }
    if archived != installed:
        missing = sorted(set(archived) - set(installed))
        unexpected = sorted(set(installed) - set(archived))
        changed = sorted(
            name for name in set(archived) & set(installed) if archived[name] != installed[name]
        )
        raise HarnessError(
            f"{install.role} installed SDK differs from its archive: "
            f"missing={missing[:5]} unexpected={unexpected[:5]} changed={changed[:5]}"
        )
    manifest = hashlib.sha256(
        json.dumps(archived, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "role": install.role,
        "version": pin["version"],
        "integrity": pin["integrity"],
        "archive": str(archive_input.path),
        "archive_bytes": archive_input.path.stat().st_size,
        "archive_sha256": archive_hash,
        "package_lock": str(install.lock),
        "package_lock_sha256": lock_hash,
        "installed_member_count": len(installed),
        "installed_member_manifest_sha256": manifest,
    }


def _node_identity(node: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            str(node),
            "-p",
            "JSON.stringify({node:process.version,platform:process.platform,arch:process.arch})",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result = json.loads(completed.stdout)
    if result["node"] != "v22.12.0":
        raise HarnessError(f"interoperability floor requires Node v22.12.0: {result}")
    return {**result, "executable": str(node), "executable_sha256": _sha256(node)}


def _ts_core_buyer_command(
    node: Path,
    install: TypeScriptInstall,
    *,
    endpoint: str,
    expect_missing: bool,
) -> list[str]:
    pin = _typescript_pin(install)
    command = [
        str(node),
        str(TS_CORE_BUYER),
        "--package-lock",
        str(install.lock),
        "--expected-version",
        pin["version"],
        "--expected-integrity",
        pin["integrity"],
        "--adcp-version",
        "3.2.0-rc.4",
        "--endpoint",
        endpoint,
        "--auth-env",
        "ADCP_INTEROP_BUYER_TOKEN",
        "--account",
        "interop-account-a",
    ]
    if expect_missing:
        command.append("--expect-missing-core-api")
    return command


def _storyboard_inventory_command(node: Path, install: TypeScriptInstall) -> list[str]:
    pin = _typescript_pin(install)
    return [
        str(node),
        str(TS_STORYBOARDS),
        "--package-lock",
        str(install.lock),
        "--expected-version",
        pin["version"],
        "--expected-integrity",
        pin["integrity"],
    ]


def _ts_server_command(
    node: Path,
    install: TypeScriptInstall,
    *,
    port: int,
    database_url: str,
    ready_file: Path,
) -> list[str]:
    pin = _typescript_pin(install)
    return [
        str(node),
        str(TS_SERVER),
        "--package-lock",
        str(install.lock),
        "--expected-version",
        pin["version"],
        "--expected-integrity",
        pin["integrity"],
        "--adcp-version",
        "3.2.0-rc.4",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--database-url",
        database_url,
        "--ready-file",
        str(ready_file),
        "--seller-role",
        install.role,
    ]


def _ts_server_environment(credentials: CoreSellerCredentials) -> dict[str, str]:
    """Pass private inputs outside argv; this is not a same-user secrecy boundary."""

    return _narrow_child_environment(
        private_bindings={
            TS_CORE_PRIVATE_ENV["token_a"]: credentials.token_a,
            TS_CORE_PRIVATE_ENV["token_b"]: credentials.token_b,
            TS_CORE_PRIVATE_ENV["startup_proof"]: credentials.startup_proof,
        },
    )


def _start_ts_core_server(
    node: Path,
    install: TypeScriptInstall,
    *,
    port: int,
    database_url: str,
    credentials: CoreSellerCredentials,
    ready_file: Path,
    cwd: Path,
    stdout: Any,
    stderr: Any,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        _ts_server_command(
            node,
            install,
            port=port,
            database_url=database_url,
            ready_file=ready_file,
        ),
        cwd=cwd,
        env=_ts_server_environment(credentials),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )


def _new_core_seller_credentials() -> CoreSellerCredentials:
    """Generate execution credentials; readiness records retain digests only."""

    credentials = CoreSellerCredentials(
        token_a=secrets.token_urlsafe(32),
        token_b=secrets.token_urlsafe(32),
        startup_proof=secrets.token_hex(32),
    )
    if (
        min(len(credentials.token_a), len(credentials.token_b)) < 32
        or len(credentials.startup_proof) < 32
        or len({credentials.token_a, credentials.token_b, credentials.startup_proof}) != 3
    ):
        raise HarnessError("seller credential generator returned low-entropy or repeated values")
    return credentials


def _linux_process_start_token(pid: int) -> str:
    if not sys.platform.startswith("linux"):
        raise HarnessError("Core seller owned-start proof currently requires Linux /proc")
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError as error:
        raise HarnessError("could not read Core seller process identity") from error
    command_end = stat.rfind(")")
    fields = stat[command_end + 2 :].split() if command_end >= 0 else []
    if len(fields) <= 19 or not fields[19].isdigit():
        raise HarnessError("Core seller process identity is malformed")
    return fields[19]


def _wait_core_seller_ready(
    process: subprocess.Popen[bytes],
    ready_file: Path,
    *,
    node: Path,
    install: TypeScriptInstall,
    credentials: CoreSellerCredentials,
    port: int,
    timeout: float = 60,
) -> dict[str, Any]:
    expected_pin = _typescript_pin(install)
    expected_start = _linux_process_start_token(process.pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise HarnessError(
                f"TypeScript Core seller exited before readiness: {process.returncode}"
            )
        if ready_file.is_file():
            try:
                ready = json.loads(ready_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise HarnessError(
                    "TypeScript Core seller readiness record is malformed"
                ) from error
            expected_auth = {
                "interop-account-a": hashlib.sha256(credentials.token_a.encode()).hexdigest(),
                "interop-account-b": hashlib.sha256(credentials.token_b.encode()).hexdigest(),
            }
            package = ready.get("seller_package", {})
            runtime = ready.get("node", {})
            if (
                ready.get("kind") != "reporting_interop_ts_core_server_ready"
                or ready.get("pid") != process.pid
                or ready.get("port") != port
                or ready.get("process_start_token") != expected_start
                or ready.get("startup_proof_sha256")
                != hashlib.sha256(credentials.startup_proof.encode()).hexdigest()
                or ready.get("auth_binding_sha256") != expected_auth
                or Path(runtime.get("executable", "")).resolve() != node.resolve()
                or runtime.get("version") != "v22.12.0"
                or package.get("name") != expected_pin.get("package", "@adcp/sdk")
                or package.get("language") != "typescript"
                or package.get("package_role") != install.role
                or package.get("version") != expected_pin["version"]
                or package.get("integrity") != expected_pin["integrity"]
                or package.get("protocol") != expected_pin["protocol"]
            ):
                raise HarnessError(
                    "TypeScript Core seller readiness identity did not match the selected seller"
                )
            return ready
        time.sleep(0.05)
    raise HarnessError("TypeScript Core seller did not publish owned readiness before timeout")


def _python_core_client_command(
    executable: Path,
    *,
    endpoint: str,
    expect_unsupported: bool = False,
) -> list[str]:
    command = [
        str(executable),
        "-I",
        str(PYTHON_CLIENT),
        "--url",
        endpoint,
        "--auth-env",
        "ADCP_INTEROP_BUYER_TOKEN",
        "--account",
        "interop-account-a",
        "--adcp-version",
        "3.2.0-rc.4",
    ]
    if expect_unsupported:
        command.append("--expect-unsupported")
    return command


def _run_official_precedence(
    node: Path, install: TypeScriptInstall, output: Path
) -> dict[str, Any]:
    if install.role != "candidate":
        raise HarnessError("official-precedence blocking probe must use the candidate TS pin")
    result_path = output / "official-precedence-candidate.json"
    stderr_path = output / "official-precedence-candidate.stderr.log"
    completed = subprocess.run(
        [str(node), str(OFFICIAL_PRECEDENCE), str(result_path)],
        cwd=output,
        env={**os.environ, "NODE_PATH": str(install.root / "node_modules")},
        text=True,
        capture_output=True,
        timeout=60,
    )
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0 or not result_path.is_file():
        raise HarnessError(
            f"official-precedence probe did not produce its required semantic pass: "
            f"exit={completed.returncode}"
        )
    report = json.loads(result_path.read_text(encoding="utf-8"))
    totals = report.get("totals", {})
    if (
        report.get("allInputsValid") is not True
        or report.get("semanticCompatibilityPassed") is not True
        or totals.get("semanticPass") != 17
        or totals.get("semanticFail") != 0
    ):
        raise HarnessError("official-precedence candidate result is not required-correct")
    manifest = json.loads(OFFICIAL_CASES.read_text(encoding="utf-8"))
    expected_cases = manifest.get("cases", [])
    observed_cases = [
        {"id": row.get("caseId"), "required": row.get("required")}
        for row in report.get("cases", [])
    ]
    if (
        manifest.get("protocol_owned") is not False
        or manifest.get("scope") != "supplemental_shared_neutral_regression"
        or observed_cases != expected_cases
    ):
        raise HarnessError("official-precedence case identities or expectations changed")
    return {
        "status": "passed",
        "corpus": {
            "protocol_owned": False,
            "manifest": _retained(OFFICIAL_CASES, ROOT),
        },
        "result": report,
        "evidence": [
            _retained(result_path, output),
            _retained(stderr_path, output),
        ],
    }


def _run_signing_vectors(
    node: Path,
    install: TypeScriptInstall,
    *,
    admin_url: str,
    output: Path,
    database: str,
) -> dict[str, Any]:
    database_identity = _create_database(admin_url, database)
    database_url = _database_url(admin_url, database)
    node_path = str(install.root / "node_modules")
    migration = subprocess.run(
        [
            str(node),
            "-e",
            "process.stdout.write(require('@adcp/sdk/signing/server').REPLAY_CACHE_MIGRATION)",
        ],
        env={**os.environ, "NODE_PATH": node_path},
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(migration)
    result_path = output / "signing-vectors.json"
    stderr_path = output / "signing-vectors.stderr.log"
    completed = subprocess.run(
        [str(node), str(SIGNING_RUNNER), str(result_path)],
        cwd=output,
        env={
            **os.environ,
            "NODE_PATH": node_path,
            "PROBE_PORT": str(_free_port()),
            "PROBE_PG_URL": _node_database_url(admin_url, database),
        },
        text=True,
        capture_output=True,
        timeout=60,
    )
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0 or not result_path.is_file():
        raise HarnessError(f"signing vectors failed with exit {completed.returncode}")
    report = json.loads(result_path.read_text(encoding="utf-8"))
    if report.get("totals") != {"vectors": 13, "passed": 13, "failed": 0}:
        raise HarnessError(f"signing vector totals changed: {report.get('totals')}")
    return {
        "status": "passed",
        "database": database_identity,
        "result": report,
        "evidence": [
            _retained(result_path, output),
            _retained(stderr_path, output),
        ],
    }


def _python_webhook_vector_dir(runtime: PythonRuntime) -> Path:
    completed = subprocess.run(
        [
            str(runtime.executable),
            "-I",
            "-c",
            "import adcp,pathlib; print(pathlib.Path(adcp.__file__).parent / "
            "'_compliance/3.2.0-rc.4/test-vectors/webhook-signing')",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    path = Path(completed.stdout.strip())
    if not path.is_dir():
        raise HarnessError(f"Python rc.4 webhook vectors are unavailable: {path}")
    return path


def _run_protocol_webhook_vectors(
    runtimes: list[PythonRuntime],
    artifacts: dict[str, PythonArtifact],
    *,
    node: Path,
    install: TypeScriptInstall,
    output: Path,
) -> dict[str, Any]:
    python_results: list[dict[str, Any]] = []
    reference_entries: dict[str, str] | None = None
    for runtime in runtimes:
        vector_dir = _python_webhook_vector_dir(runtime)
        aggregate, entries = _tree_aggregate_sha256(vector_dir)
        if aggregate != WEBHOOK_VECTORS_AGGREGATE_SHA256:
            raise HarnessError(f"Python webhook vector aggregate changed for {runtime.route}")
        if reference_entries is None:
            reference_entries = entries
        elif entries != reference_entries:
            raise HarnessError("Python webhook vector trees differ across artifact routes")
        rows = _run_json(
            [str(runtime.executable), "-I", str(WEBHOOK_VECTORS_PYTHON), str(vector_dir)],
            cwd=output,
            env=os.environ.copy(),
        )
        mismatches = [row["vector"] for row in rows if row.get("match") is not True]
        if len(rows) != 29 or mismatches != ["negative/019-revocation-stale.json"]:
            raise HarnessError(
                f"Python webhook conformance changed for {runtime.route}: {mismatches}"
            )
        result_path = output / f"webhook-vectors-{runtime.route}.json"
        result_path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        revocation_state = _run_json(
            [str(runtime.executable), "-I", str(WEBHOOK_REVOCATION_STATE), str(vector_dir)],
            cwd=output,
            env=os.environ.copy(),
        )
        if revocation_state.get("fresh") != {"success": True} or revocation_state.get("stale") != {
            "error_code": "webhook_signature_revocation_stale",
            "step": 9,
            "success": False,
        }:
            raise HarnessError(
                f"Python public revocation-list state control failed for {runtime.route}"
            )
        revocation_path = output / f"webhook-revocation-state-{runtime.route}.json"
        revocation_path.write_text(
            json.dumps(revocation_state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        python_results.append(
            {
                "route": runtime.route,
                "runtime": _runtime_identity(runtime, artifacts[runtime.route]),
                "totals": {"vectors": 29, "matched": 28, "mismatched": 1},
                "mismatch_ids": mismatches,
                "rows": rows,
                "revocation_state": revocation_state,
                "evidence": [
                    _retained(result_path, output),
                    _retained(revocation_path, output),
                ],
            }
        )

    if any(item["rows"] != python_results[0]["rows"] for item in python_results[1:]):
        raise HarnessError("Python webhook results differ across artifact routes")

    ts_vector_dir = (
        install.root
        / "node_modules/@adcp/sdk/compliance/cache/3.2.0-rc.4/test-vectors/webhook-signing"
    )
    if not ts_vector_dir.is_dir():
        raise HarnessError("TypeScript rc.4 webhook vectors are unavailable")
    ts_aggregate, ts_entries = _tree_aggregate_sha256(ts_vector_dir)
    if (
        ts_aggregate != WEBHOOK_VECTORS_AGGREGATE_SHA256
        or reference_entries is None
        or ts_entries != reference_entries
    ):
        raise HarnessError("installed Python and TypeScript webhook vector trees differ")
    ts_rows = _run_json(
        [
            str(node),
            str(WEBHOOK_VECTORS_TYPESCRIPT),
            str(install.root),
            str(ts_vector_dir),
        ],
        cwd=output,
        env=os.environ.copy(),
    )
    ts_mismatches = [row["vector"] for row in ts_rows if row.get("match") is not True]
    if len(ts_rows) != 29 or ts_mismatches != ["negative/019-revocation-stale.json"]:
        raise HarnessError(f"TypeScript webhook conformance changed: {ts_mismatches}")

    ts_path = output / "webhook-vectors-typescript.json"
    ts_path.write_text(json.dumps(ts_rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    python_by_id = {row["vector"]: row["got"] for row in python_results[0]["rows"]}
    ts_by_id = {row["vector"]: row["got"] for row in ts_rows}
    disagreements = sorted(
        vector for vector in python_by_id if python_by_id[vector] != ts_by_id[vector]
    )
    if disagreements:
        raise HarnessError(f"cross-SDK webhook outcomes changed: {disagreements}")
    return {
        "status": "verifier_corpus_28_of_29_receiver_contract_required",
        "corpus": {
            "owner": "protocol_3.2.0_rc4",
            "aggregate_sha256": WEBHOOK_VECTORS_AGGREGATE_SHA256,
            "files": len(ts_entries),
            "trees_byte_identical": True,
        },
        "inputs": {
            "python_probe": _retained(WEBHOOK_VECTORS_PYTHON, ROOT),
            "typescript_probe": _retained(WEBHOOK_VECTORS_TYPESCRIPT, ROOT),
            "revocation_state_probe": _retained(WEBHOOK_REVOCATION_STATE, ROOT),
        },
        "python": python_results,
        "typescript": {
            "runtime": _node_identity(node),
            "pin": _typescript_pin(install),
            "totals": {"vectors": 29, "matched": 28, "mismatched": 1},
            "mismatch_ids": ts_mismatches,
            "rows": ts_rows,
            "evidence": _retained(ts_path, output),
        },
        "cross_sdk": {
            "agreed": 29,
            "disagreed": 0,
            "disagreement_ids": disagreements,
            "bare_verifier_normative_acceptance": False,
        },
        "receiver_contract": {
            "status": "incomplete",
            "required_vector": "negative/019-revocation-stale.json",
            "requires_contract": "webhook_receiver_runner",
            "fault": "simulate_stale_revocation_fetch",
            "configured_public_revocation_list_control": "passed_both_python_artifact_routes",
            "remaining": [
                "satisfied_only_by_the_separate_live_receiver_finding",
            ],
        },
        "limitations": [
            "in_process_public_verifiers",
            "not_socket_callback_evidence",
            "not_retry_or_activity_evidence",
        ],
    }


def _run_cross_language_webhook_signing(
    runtimes: list[PythonRuntime],
    artifacts: dict[str, PythonArtifact],
    *,
    node: Path,
    install: TypeScriptInstall,
    output: Path,
) -> dict[str, Any]:
    ts_vector_dir = (
        install.root
        / "node_modules/@adcp/sdk/compliance/cache/3.2.0-rc.4/test-vectors/webhook-signing"
    )
    python_rows: list[dict[str, Any]] = []
    for runtime in runtimes:
        vector_dir = _python_webhook_vector_dir(runtime)
        sample = _run_json(
            [str(runtime.executable), "-I", str(WEBHOOK_SIGN_PYTHON), str(vector_dir)],
            cwd=output,
            env=os.environ.copy(),
        )
        if sample.get(
            "signature_alphabet"
        ) != "url(-_)" or "_private_d_for_test_only" in json.dumps(sample):
            raise HarnessError(f"Python webhook signer output changed for {runtime.route}")
        sample_path = output / f"webhook-signed-python-{runtime.route}.json"
        sample_path.write_text(
            json.dumps(sample, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        python_self = _run_json(
            [
                str(runtime.executable),
                "-I",
                str(WEBHOOK_VERIFY_PYTHON),
                str(sample_path),
                str(vector_dir),
            ],
            cwd=output,
            env=os.environ.copy(),
        )
        typescript_cross = _run_json(
            [
                str(node),
                str(WEBHOOK_VERIFY_TYPESCRIPT),
                str(install.root),
                str(sample_path),
                str(ts_vector_dir),
            ],
            cwd=output,
            env=os.environ.copy(),
        )
        if python_self != {"accepted": True} or (
            typescript_cross.get("ts_accepts_python_signed_webhook") is not True
        ):
            raise HarnessError(f"Python-signed webhook 2x2 outcome changed for {runtime.route}")
        python_rows.append(
            {
                "route": runtime.route,
                "runtime": _runtime_identity(runtime, artifacts[runtime.route]),
                "emitted": sample["emitted_headers"],
                "signature_alphabet": sample["signature_alphabet"],
                "content_digest_alphabet": sample["content_digest_alphabet"],
                "python_verifier": python_self,
                "typescript_verifier": typescript_cross,
                "evidence": _retained(sample_path, output),
            }
        )
    if any(row["emitted"] != python_rows[0]["emitted"] for row in python_rows[1:]):
        raise HarnessError("Python webhook signer output differs across artifact routes")

    ts_sample = _run_json(
        [str(node), str(WEBHOOK_SIGN_TYPESCRIPT), str(install.root), str(ts_vector_dir)],
        cwd=output,
        env=os.environ.copy(),
    )
    expected_ts_signature = (
        "sig1=:iaoTUFbGl4Ka6O3G2H3mOxvFDhJNXSrlFNBsdDFKmvlNz1vr9jilnh6LBDFuCD4wY1g1rC9HxI9"
        "lgRy_-04EBw:"
    )
    if ts_sample.get("emitted", {}).get(
        "Signature"
    ) != expected_ts_signature or "_private_d_for_test_only" in json.dumps(ts_sample):
        raise HarnessError("TypeScript webhook signer output changed")
    ts_sample_path = output / "webhook-signed-typescript.json"
    ts_sample_path.write_text(
        json.dumps(ts_sample, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    ts_self = _run_json(
        [
            str(node),
            str(WEBHOOK_VERIFY_TYPESCRIPT),
            str(install.root),
            str(ts_sample_path),
            str(ts_vector_dir),
        ],
        cwd=output,
        env=os.environ.copy(),
    )
    python_cross: list[dict[str, Any]] = []
    for runtime in runtimes:
        result = _run_json(
            [
                str(runtime.executable),
                "-I",
                str(WEBHOOK_VERIFY_PYTHON),
                str(ts_sample_path),
                str(_python_webhook_vector_dir(runtime)),
            ],
            cwd=output,
            env=os.environ.copy(),
        )
        python_cross.append({"route": runtime.route, "result": result})
    if ts_self.get("ts_accepts_python_signed_webhook") is not True or any(
        row["result"] != {"accepted": True} for row in python_cross
    ):
        raise HarnessError("TypeScript-signed webhook 2x2 outcome changed")

    return {
        "status": "passed",
        "protocol_adjudication": {
            "source_commit": "94976657c8456e5ad6de55d9793a883542a4fc5f",
            "signature_profile": "unpadded_base64url",
            "content_digest": "separately_bounded_wording_vector_discrepancy",
        },
        "inputs": {
            "python_signer_probe": _retained(WEBHOOK_SIGN_PYTHON, ROOT),
            "python_verifier_probe": _retained(WEBHOOK_VERIFY_PYTHON, ROOT),
            "typescript_signer_probe": _retained(WEBHOOK_SIGN_TYPESCRIPT, ROOT),
            "typescript_verifier_probe": _retained(WEBHOOK_VERIFY_TYPESCRIPT, ROOT),
        },
        "python_signer": python_rows,
        "typescript_signer": {
            "runtime": _node_identity(node),
            "pin": _typescript_pin(install),
            "emitted": ts_sample["emitted"],
            "typescript_verifier": ts_self,
            "python_verifiers": python_cross,
            "evidence": _retained(ts_sample_path, output),
        },
        "directions": {
            "python_to_python": "accepted",
            "python_to_typescript": "accepted",
            "typescript_to_python": "accepted",
            "typescript_to_typescript": "accepted",
        },
        "semantic_compatibility": True,
        "limitations": [
            "in_process_public_signers_and_verifiers",
            "published_protocol_test_key_only",
            "not_real_socket_callback_evidence",
        ],
    }


def _run_resource_location_comparison(
    runtimes: list[PythonRuntime],
    artifacts: dict[str, PythonArtifact],
    *,
    node: Path,
    install: TypeScriptInstall,
    output: Path,
) -> dict[str, Any]:
    python_results: list[dict[str, Any]] = []
    for runtime in runtimes:
        result = _run_json(
            [str(runtime.executable), "-I", str(RESOURCE_PYTHON), str(RESOURCE_SENTINELS)],
            cwd=output,
            env=os.environ.copy(),
        )
        if not isinstance(result, list) or len(result) != 17:
            raise HarnessError(
                f"resource-location Python sentinel result changed for {runtime.route}"
            )
        result_path = output / f"resource-location-{runtime.route}.json"
        result_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        python_results.append(
            {
                "route": runtime.route,
                "runtime": _runtime_identity(runtime, artifacts[runtime.route]),
                "rows": result,
                "evidence": _retained(result_path, output),
            }
        )

    typescript = _run_json(
        [str(node), str(RESOURCE_TYPESCRIPT), str(install.root), str(RESOURCE_SENTINELS)],
        cwd=output,
        env=os.environ.copy(),
    )
    if typescript.get("available") != "function" or len(typescript.get("rows", [])) != 17:
        raise HarnessError("resource-location TypeScript sentinel guard is unavailable or changed")
    typescript_path = output / "resource-location-typescript.json"
    typescript_path.write_text(
        json.dumps(typescript, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    canonical_python = python_results[0]["rows"]
    if any(item["rows"] != canonical_python for item in python_results[1:]):
        raise HarnessError("resource-location Python artifact routes disagree")
    python_by_id = {row["id"]: row["python"] for row in canonical_python}
    ts_by_id = {row["id"]: row["ts"] for row in typescript["rows"]}
    if set(python_by_id) != set(ts_by_id):
        raise HarnessError("resource-location sentinel identities disagree")
    divergences = sorted(
        row_id for row_id in python_by_id if python_by_id[row_id] != ts_by_id[row_id]
    )
    expected_divergences = sorted(
        [
            "aws-key-id",
            "bare-jwt",
            "double-pct-encoded",
            "http-plain",
            "https-clean",
            "pct-encoded-query",
            "protocol-relative",
        ]
    )
    if divergences != expected_divergences:
        raise HarnessError(
            "resource-location native policy comparison changed and requires re-adjudication"
        )
    return {
        "id": "typescript_candidate_resource_location_guard_advisory",
        "status": "advisory_observed",
        "blocking": False,
        "acceptance_effect": "none",
        "evidence_scope": "in_process_installed_seller_persistence_guards_only",
        "does_not_override_secret_leak_failure": True,
        "pin": _typescript_pin(install),
        "typescript_install": {
            "package_lock": str(install.lock),
            "package_lock_sha256": _sha256(install.lock),
        },
        "inputs": {
            "corpus": _retained(RESOURCE_SENTINELS, ROOT),
            "python_probe": _retained(RESOURCE_PYTHON, ROOT),
            "typescript_probe": _retained(RESOURCE_TYPESCRIPT, ROOT),
        },
        "totals": {"sentinels": 17, "agree": 10, "diverge": 7},
        "divergence_ids": divergences,
        "python": python_results,
        "typescript": {
            "runtime": _node_identity(node),
            "rows": typescript["rows"],
            "evidence": _retained(typescript_path, output),
        },
        "limitations": [
            "not_real_http",
            "not_mcp_quadrant_evidence",
            "seller_persistence_guards_only",
            "corpus_expect_field_is_not_a_pass_criterion",
        ],
    }


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def _retained(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(root)),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _run_python_control(
    runtime: PythonRuntime,
    artifact: PythonArtifact,
    *,
    admin_url: str,
    output: Path,
    database: str,
    node: Path | None,
    candidate_ts: TypeScriptInstall | None,
) -> list[dict[str, Any]]:
    database_identity = _create_database(admin_url, database)
    database_url = _node_database_url(admin_url, database)
    port = _free_port()
    stdout_path = output / "seller.stdout.log"
    stderr_path = output / "seller.stderr.log"
    client_path = output / "client.json"
    with stdout_path.open("wb") as seller_stdout, stderr_path.open("wb") as seller_stderr:
        process = subprocess.Popen(
            [
                str(runtime.executable),
                "-I",
                str(PYTHON_SERVER),
                "--port",
                str(port),
                "--database-url",
                database_url,
            ],
            cwd=output,
            stdin=subprocess.DEVNULL,
            stdout=seller_stdout,
            stderr=seller_stderr,
            start_new_session=True,
        )
        try:
            _wait_port(process, port)
            completed = subprocess.run(
                [
                    str(runtime.executable),
                    "-I",
                    str(PYTHON_CLIENT),
                    "--url",
                    f"http://127.0.0.1:{port}/mcp",
                    "--auth-env",
                    "ADCP_INTEROP_BUYER_TOKEN",
                    "--account",
                    "interop-account-a",
                    "--adcp-version",
                    "3.2.0-rc.4",
                ],
                cwd=output,
                text=True,
                capture_output=True,
                timeout=60,
                env=_buyer_environment("interop-token-a"),
            )
            client_path.write_text(completed.stdout, encoding="utf-8")
            if completed.stderr:
                (output / "client.stderr.log").write_text(completed.stderr, encoding="utf-8")
            if completed.returncode != 0:
                raise HarnessError(f"Python semantic buyer failed with {completed.returncode}")
            result = json.loads(completed.stdout)
            if result.get("definitive") is not True or result.get("submitted_receipts") != 0:
                raise HarnessError(f"Python semantic result is not definitive Core: {result}")
            ts_gap = None
            if node is not None and candidate_ts is not None:
                ts_gap = _run_json(
                    _ts_core_buyer_command(
                        node,
                        candidate_ts,
                        endpoint=f"http://127.0.0.1:{port}/mcp",
                        expect_missing=False,
                    ),
                    cwd=output,
                    env=_buyer_environment("interop-token-a"),
                )
        finally:
            _stop(process)
    cells = [
        {
            "id": "supplemental_shared_rc4__candidate_py_client__candidate_py_server",
            "target_contract_id": "Q1",
            "target_cell_id": "candidate_py_client__candidate_py_server",
            "control_classification": "historical_rc4_shared_seller_control",
            "required_cell_credit": False,
            "execution_attempted": True,
            "observed_protocol": "3.2.0-rc.4",
            "shared_seller_database": True,
            "status": "partial_pass",
            "cell_complete": False,
            "acceptance": False,
            "reason": (
                "Core control passed; the cell's full declared scenario set is not implemented"
            ),
            "database": database_identity,
            "runtime": _runtime_identity(runtime, artifact),
            "result": result,
            "entrypoints": {
                "server": {
                    "path": str(PYTHON_SERVER.relative_to(ROOT)),
                    "sha256": _sha256(PYTHON_SERVER),
                },
                "client": {
                    "path": str(PYTHON_CLIENT.relative_to(ROOT)),
                    "sha256": _sha256(PYTHON_CLIENT),
                },
            },
            "evidence": [
                _retained(stdout_path, output),
                _retained(stderr_path, output),
                _retained(client_path, output),
            ],
        }
    ]
    if ts_gap is not None:
        cells.append(
            {
                "id": "supplemental_shared_rc4__candidate_ts_client__candidate_py_server",
                "target_contract_id": "Q2",
                "target_cell_id": "candidate_ts_client__candidate_py_server",
                "control_classification": "historical_rc4_shared_seller_control",
                "required_cell_credit": False,
                "execution_attempted": True,
                "observed_protocol": "3.2.0-rc.4",
                "shared_seller_database": True,
                "status": "partial_pass",
                "cell_complete": False,
                "acceptance": False,
                "reason": (
                    "Candidate TypeScript Core reconciliation passed; the cell's full declared "
                    "scenario set remains incomplete"
                ),
                "result": ts_gap,
            }
        )
    return cells


def _run_ts_core_control(
    runtime: PythonRuntime,
    artifact: PythonArtifact,
    *,
    node: Path,
    install: TypeScriptInstall,
    admin_url: str,
    output: Path,
    database: str,
) -> list[dict[str, Any]]:
    database_identity = _create_database(admin_url, database)
    database_url = _node_database_url(admin_url, database)
    pin = _typescript_pin(install)
    port = _free_port()
    stdout_path = output / "seller.stdout.log"
    stderr_path = output / "seller.stderr.log"
    client_path = output / "python-client.json"
    ts_path = output / "typescript-client.json"
    ready_path = output / "seller.ready.json"
    if ready_path.exists():
        raise HarnessError("refusing to reuse a pre-existing Core seller readiness record")
    credentials = _new_core_seller_credentials()
    with stdout_path.open("wb") as seller_stdout, stderr_path.open("wb") as seller_stderr:
        process = _start_ts_core_server(
            node,
            install,
            port=port,
            database_url=database_url,
            credentials=credentials,
            ready_file=ready_path,
            cwd=output,
            stdout=seller_stdout,
            stderr=seller_stderr,
        )
        try:
            ready = _wait_core_seller_ready(
                process,
                ready_path,
                node=node,
                install=install,
                credentials=credentials,
                port=port,
            )
            endpoint = f"http://127.0.0.1:{port}/mcp"
            python_result = _run_json(
                _python_core_client_command(runtime.executable, endpoint=endpoint),
                cwd=output,
                env=_buyer_environment(credentials.token_a),
            )
            client_path.write_text(
                json.dumps(python_result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            if python_result.get("definitive") is not True:
                raise HarnessError(
                    "Python Core reconciliation against TypeScript was not definitive"
                )
            ts_result = _run_json(
                _ts_core_buyer_command(node, install, endpoint=endpoint, expect_missing=False),
                cwd=output,
                env=_buyer_environment(credentials.token_a),
            )
            ts_path.write_text(
                json.dumps(ts_result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        finally:
            _stop(process)
    evidence = [
        _retained(stdout_path, output),
        _retained(stderr_path, output),
        _retained(client_path, output),
        _retained(ts_path, output),
        _retained(ready_path, output),
    ]
    seller_process_identity = {
        "pid": ready["pid"],
        "start_token": ready["process_start_token"],
        "startup_proof_sha256": ready["startup_proof_sha256"],
    }
    seller_artifact_identity = ready["seller_package"]
    py_cell = {
        "id": "supplemental_shared_rc4__candidate_py_client__candidate_ts_server",
        "target_contract_id": "Q3",
        "target_cell_id": "candidate_py_client__candidate_ts_server",
        "control_classification": "historical_rc4_shared_seller_control",
        "required_cell_credit": False,
        "execution_attempted": True,
        "observed_protocol": pin["protocol"],
        "shared_seller_database": True,
        "status": "partial_pass",
        "cell_complete": False,
        "acceptance": False,
        "reason": "Core control passed; Managed Delivery and the full scenario set remain open",
        "database": database_identity,
        "seller_process_identity": seller_process_identity,
        "seller_artifact_identity": seller_artifact_identity,
        "python_runtime": _runtime_identity(runtime, artifact),
        "typescript": {
            "role": install.role,
            "package_lock": str(install.lock),
            "package_lock_sha256": _sha256(install.lock),
            "pin": pin,
        },
        "result": python_result,
        "evidence": evidence,
    }
    ts_cell = {
        "id": (
            "supplemental_shared_rc4__candidate_ts_client__candidate_ts_server"
            if install.role == "candidate"
            else "supplemental_lead_shared__candidate_ts_client__candidate_ts_server"
        ),
        "target_contract_id": "Q4",
        "target_cell_id": "candidate_ts_client__candidate_ts_server",
        "control_classification": (
            "historical_rc4_shared_seller_control"
            if install.role == "candidate"
            else "supplemental_lead_shared_seller_control"
        ),
        "required_cell_credit": False,
        "execution_attempted": True,
        "observed_protocol": pin.get("protocol"),
        "shared_seller_database": True,
        "status": "partial_pass" if install.role == "candidate" else "supplemental_pass",
        "cell_complete": False,
        "acceptance": False,
        "reason": (
            "Candidate Core HTTP and native Core reconciliation passed; full scenarios remain open"
            if install.role == "candidate"
            else "rc.44 supplemental Core lane only; it is not the blocking pin"
        ),
        "seller_process_identity": seller_process_identity,
        "seller_artifact_identity": seller_artifact_identity,
        "result": ts_result,
        "evidence": evidence,
    }
    return [py_cell, ts_cell]


def _run_storyboard_server(
    node: Path,
    install: TypeScriptInstall,
    archive: TypeScriptArchive,
    *,
    admin_url: str,
    output: Path,
    inventory: dict[str, Any],
) -> dict[str, Any]:
    inventory_path = output / "inventory-input.json"
    inventory_path.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    orchestration_output = output / "orchestration"
    stdout_path = output / "orchestration.stdout.log"
    stderr_path = output / "orchestration.stderr.log"
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(STORYBOARD_ORCHESTRATION),
            "--node-runtime",
            str(node),
            "--node-sha256",
            _sha256(node),
            "--typescript-install",
            str(install.root),
            "--typescript-tarball",
            str(archive.path),
            "--typescript-role",
            install.role,
            "--inventory-json",
            str(inventory_path),
            "--external-pg-admin-dsn",
            admin_url,
            "--output",
            str(orchestration_output),
            "--execute",
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=1_500,
    )
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    result_path = orchestration_output / "results.json"
    if completed.returncode == 2 or not result_path.is_file():
        raise HarnessError(
            f"storyboard orchestration failed before producing a result: "
            f"exit={completed.returncode} stderr={completed.stderr[:500]}"
        )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("acceptance") is not False or result.get("blocking_acceptance") is not False:
        raise HarnessError("storyboard-only orchestration claimed aggregate acceptance")
    return {
        **result,
        "role": install.role,
        "evidence": [
            _retained(inventory_path, output),
            _retained(stdout_path, output),
            _retained(stderr_path, output),
            _retained(result_path, output),
        ],
    }


def _run_webhook_receiver_refresh(
    runtimes: list[PythonRuntime],
    artifacts: dict[str, PythonArtifact],
    *,
    output: Path,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for runtime in runtimes:
        route_root = output / f"webhook-receiver-{runtime.route}"
        route_root.mkdir()
        receiver_port = _free_port()
        issuer_port = _free_port()
        vector_dir = _python_webhook_vector_dir(runtime)
        stdout_path = route_root / "receiver.stdout.log"
        stderr_path = route_root / "receiver.stderr.log"
        result_path = route_root / "result.json"
        with stdout_path.open("wb") as receiver_stdout, stderr_path.open("wb") as receiver_stderr:
            process = subprocess.Popen(
                [
                    str(runtime.executable),
                    "-I",
                    str(WEBHOOK_RECEIVER_SERVER),
                    "--receiver-port",
                    str(receiver_port),
                    "--issuer-port",
                    str(issuer_port),
                    "--vector-dir",
                    str(vector_dir),
                ],
                cwd=route_root,
                stdin=subprocess.DEVNULL,
                stdout=receiver_stdout,
                stderr=receiver_stderr,
                start_new_session=True,
            )
            try:
                _wait_port(process, receiver_port)
                completed = subprocess.run(
                    [
                        str(runtime.executable),
                        "-I",
                        str(WEBHOOK_RECEIVER_CLIENT),
                        "--receiver-url",
                        f"http://127.0.0.1:{receiver_port}/webhook",
                        "--vector-dir",
                        str(vector_dir),
                    ],
                    cwd=route_root,
                    env=os.environ.copy(),
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
            finally:
                _stop(process)
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise HarnessError(
                f"webhook receiver probe emitted invalid output: {completed.stdout[:500]}"
            ) from error
        result_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        normative_match = result.get("normative_019_match") is True
        receiver_http_mapping_match = (
            result.get("vector_019", {}).get("receiver_http_mapping_match") is True
        )
        blocking_acceptance = normative_match and receiver_http_mapping_match
        if result.get("blocking_acceptance") is not blocking_acceptance:
            raise HarnessError(
                f"webhook receiver result contract is inconsistent for {runtime.route}"
            )
        expected_exit = 0 if blocking_acceptance else 1
        expected_status = "passed" if blocking_acceptance else "blocking_red"
        if (
            completed.returncode != expected_exit
            or result.get("status") != expected_status
            or result.get("execution_completed") is not True
            or result.get("historical_reproducer_mode") is not False
            or len(result.get("rows", [])) != 3
        ):
            raise HarnessError(f"webhook receiver refresh control failed for {runtime.route}")
        results.append(
            {
                "route": runtime.route,
                "runtime": _runtime_identity(runtime, artifacts[runtime.route]),
                "result": result,
                "evidence": [
                    _retained(stdout_path, route_root),
                    _retained(stderr_path, route_root),
                    _retained(result_path, route_root),
                ],
            }
        )
    classifications = {item["result"].get("stale_failure_classification") for item in results}
    if len(classifications) != 1:
        raise HarnessError("webhook receiver artifact routes disagree on stale classification")
    normative = all(item["result"].get("normative_019_match") is True for item in results)
    receiver_http_mapping = all(
        item["result"].get("vector_019", {}).get("receiver_http_mapping_match") is True
        for item in results
    )
    blocking_acceptance = normative and receiver_http_mapping
    return {
        "id": "python_webhook_receiver_live_revocation",
        "status": "passed" if blocking_acceptance else "blocking_red",
        "blocking_acceptance": blocking_acceptance,
        "normative_019_match": normative,
        "receiver_http_mapping_match": receiver_http_mapping,
        "stale_failure_classification": classifications.pop(),
        "receiver_contract": {
            "actual_socket_callback": True,
            "live_revocation_fetch": True,
            "stale_outage_fail_closed": True,
            "refresh_recovery": True,
            "raw_body_verification": True,
            "replay_persistence_exercised": False,
        },
        "inputs": {
            "server": _retained(WEBHOOK_RECEIVER_SERVER, ROOT),
            "client": _retained(WEBHOOK_RECEIVER_CLIENT, ROOT),
        },
        "routes": results,
        "limitations": [
            "deterministic_public_test_key",
            "loopback_http_test_transport",
            "does_not_close_cross_language_signer_interop",
            "does_not_cover_reporting_delivery_retry_or_account_activity",
        ],
    }


def _run_candidate_python_previous_ts(
    runtime: PythonRuntime,
    artifact: PythonArtifact,
    *,
    node: Path,
    install: TypeScriptInstall,
    admin_url: str,
    output: Path,
    database: str,
) -> dict[str, Any]:
    if install.role != "previous":
        raise HarnessError("previous-TypeScript skew requires the previous install role")
    database_identity = _create_database(admin_url, database)
    port = _free_port()
    stdout_path = output / "seller.stdout.log"
    stderr_path = output / "seller.stderr.log"
    result_path = output / "python-client.json"
    ready_path = output / "seller.ready.json"
    if ready_path.exists():
        raise HarnessError("refusing to reuse a pre-existing Core seller readiness record")
    credentials = _new_core_seller_credentials()
    with stdout_path.open("wb") as seller_stdout, stderr_path.open("wb") as seller_stderr:
        process = _start_ts_core_server(
            node,
            install,
            port=port,
            database_url=_node_database_url(admin_url, database),
            credentials=credentials,
            ready_file=ready_path,
            cwd=output,
            stdout=seller_stdout,
            stderr=seller_stderr,
        )
        try:
            ready = _wait_core_seller_ready(
                process,
                ready_path,
                node=node,
                install=install,
                credentials=credentials,
                port=port,
            )
            result = _run_json(
                _python_core_client_command(
                    runtime.executable,
                    endpoint=f"http://127.0.0.1:{port}/mcp",
                ),
                cwd=output,
                env=_buyer_environment(credentials.token_a),
            )
        finally:
            _stop(process)
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if result.get("definitive") is not True or result.get("submitted_receipts") != 0:
        raise HarnessError("candidate Python did not reconcile the previous TS Core seller")
    return {
        "id": "supplemental_rc4_skew__candidate_py_client__previous_ts_server",
        "target_contract_id": "S2",
        "target_cell_id": "candidate_py_client__previous_ts_server",
        "control_classification": "historical_rc4_positive_core_only",
        "required_cell_credit": False,
        "execution_attempted": True,
        "observed_protocol": _typescript_pin(install)["protocol"],
        "polarity": "positive_core_control",
        "status": "partial_pass",
        "cell_complete": False,
        "acceptance": False,
        "reason": "Core skew passed; the complete declared scenario set remains required",
        "scenario_results": {
            "core_reporting": "passed",
            "actionable_unsupported": "not_triggered_for_supported_core_contract",
        },
        "database": database_identity,
        "seller_process_identity": {
            "pid": ready["pid"],
            "start_token": ready["process_start_token"],
            "startup_proof_sha256": ready["startup_proof_sha256"],
        },
        "seller_artifact_identity": ready["seller_package"],
        "python_runtime": _runtime_identity(runtime, artifact),
        "typescript": {
            "pin": _typescript_pin(install),
            "package_lock": str(install.lock),
            "package_lock_sha256": _sha256(install.lock),
        },
        "result": result,
        "evidence": [
            _retained(stdout_path, output),
            _retained(stderr_path, output),
            _retained(result_path, output),
            _retained(ready_path, output),
        ],
    }


def _run_previous_python_candidate_ts(
    previous: PreviousPythonInput,
    *,
    node: Path,
    install: TypeScriptInstall,
    admin_url: str,
    output: Path,
    database: str,
) -> dict[str, Any]:
    if install.role != "candidate":
        raise HarnessError("previous-Python skew requires the candidate TS install role")
    pins = json.loads(PINS.read_text(encoding="utf-8"))["python"]["previous_released"]
    if _sha256(previous.artifact) != pins["wheel_sha256"]:
        raise HarnessError("previous Python wheel bytes do not match the immutable released pin")
    runtime = PythonRuntime("previous", previous.executable)
    artifact = PythonArtifact("previous", previous.artifact)
    database_identity = _create_database(admin_url, database)
    port = _free_port()
    stdout_path = output / "seller.stdout.log"
    stderr_path = output / "seller.stderr.log"
    result_path = output / "python-client.json"
    ready_path = output / "seller.ready.json"
    if ready_path.exists():
        raise HarnessError("refusing to reuse a pre-existing Core seller readiness record")
    credentials = _new_core_seller_credentials()
    with stdout_path.open("wb") as seller_stdout, stderr_path.open("wb") as seller_stderr:
        process = _start_ts_core_server(
            node,
            install,
            port=port,
            database_url=_node_database_url(admin_url, database),
            credentials=credentials,
            ready_file=ready_path,
            cwd=output,
            stdout=seller_stdout,
            stderr=seller_stderr,
        )
        try:
            ready = _wait_core_seller_ready(
                process,
                ready_path,
                node=node,
                install=install,
                credentials=credentials,
                port=port,
            )
            result = _run_json(
                _python_core_client_command(
                    previous.executable,
                    endpoint=f"http://127.0.0.1:{port}/mcp",
                    expect_unsupported=True,
                ),
                cwd=output,
                env=_buyer_environment(credentials.token_a),
            )
        finally:
            _stop(process)
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if (
        result.get("status") != "actionable_unsupported"
        or result.get("mutation_attempted") is not False
        or result.get("phase") != "client_construction"
    ):
        raise HarnessError("previous Python skew did not fail closed before transport mutation")
    return {
        "id": "negative_skew_refusal__previous_py_client__candidate_ts_server",
        "target_contract_id": "S1",
        "target_cell_id": "previous_py_client__candidate_ts_server",
        "control_classification": "negative_refusal_only",
        "required_cell_credit": False,
        "execution_attempted": True,
        "observed_protocol": _typescript_pin(install)["protocol"],
        "polarity": "negative_refusal",
        "status": "actionable_unsupported",
        "cell_complete": False,
        "scenario_complete": True,
        "acceptance": False,
        "reason": (
            "released Python rc.3 rejects explicit rc.4 before transport; remaining applicable "
            "cell scenarios still require execution"
        ),
        "database": database_identity,
        "seller_process_identity": {
            "pid": ready["pid"],
            "start_token": ready["process_start_token"],
            "startup_proof_sha256": ready["startup_proof_sha256"],
        },
        "seller_artifact_identity": ready["seller_package"],
        "python_runtime": _runtime_identity(runtime, artifact),
        "typescript": {
            "pin": _typescript_pin(install),
            "package_lock": str(install.lock),
            "package_lock_sha256": _sha256(install.lock),
        },
        "result": result,
        "evidence": [
            _retained(stdout_path, output),
            _retained(stderr_path, output),
            _retained(result_path, output),
            _retained(ready_path, output),
        ],
    }


def _execution_credit_errors(contract: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    """Explain why one observed control cannot count as a declared cell execution."""

    errors: list[str] = []
    if observed.get("target_cell_id", observed.get("id")) != contract.get("id"):
        errors.append("cell_id_mismatch")
    if observed.get("target_contract_id") != contract.get("contract_id"):
        errors.append("contract_id_mismatch")
    if observed.get("execution_attempted") is not True:
        errors.append("execution_not_attempted")
    if observed.get("required_cell_credit") is not True:
        errors.append("observed_control_disclaims_required_credit")
    seller_identity = observed.get("seller_process_identity")
    if not isinstance(seller_identity, dict) or not {
        "pid",
        "start_token",
        "startup_proof_sha256",
    }.issubset(seller_identity):
        errors.append("seller_process_identity_missing")
    seller_artifact = observed.get("seller_artifact_identity")
    seller_contract = contract.get("server")
    if not isinstance(seller_artifact, dict):
        errors.append("seller_artifact_identity_missing")
    elif isinstance(seller_contract, dict):
        if seller_artifact.get("language") != seller_contract.get("language"):
            errors.append("seller_language_does_not_match_cell_server")
        if seller_artifact.get("package_role") != seller_contract.get("package_role"):
            errors.append("seller_package_role_does_not_match_cell_server")
        if not isinstance(seller_artifact.get("runtime_identity_sha256"), str):
            errors.append("seller_runtime_identity_missing")
        if not isinstance(seller_artifact.get("artifact_identity_sha256"), str):
            errors.append("seller_archive_identity_missing")
        expected_seller_archive = seller_contract.get("artifact_identity_sha256")
        if expected_seller_archive is not None and (
            seller_artifact.get("artifact_identity_sha256") != expected_seller_archive
        ):
            errors.append("seller_archive_identity_does_not_match_cell_server")
        expected_seller_runtime = seller_contract.get("runtime_identity_sha256")
        if expected_seller_runtime is not None and (
            seller_artifact.get("runtime_identity_sha256") != expected_seller_runtime
        ):
            errors.append("seller_runtime_identity_does_not_match_cell_server")
    database_identity = observed.get("database_identity")
    if not isinstance(database_identity, dict) or not {
        "name",
        "cluster_identity",
    }.issubset(database_identity):
        errors.append("database_identity_missing")

    protocol = contract.get("protocol_contract")
    if protocol is not None:
        if protocol.get("selected_artifacts") is not True:
            errors.append("contract_artifacts_not_selected")
        expected_artifacts = protocol.get("artifact_identity_sha256")
        if not isinstance(expected_artifacts, str) or len(expected_artifacts) != 64:
            errors.append("contract_artifact_identity_missing")
        elif observed.get("artifact_identity_sha256") != expected_artifacts:
            errors.append("observed_artifact_identity_does_not_match_required")
        if protocol.get("alignment") != "exact":
            errors.append("contract_protocol_alignment_not_exact")
        if observed.get("observed_protocol") != protocol.get("required"):
            errors.append("observed_protocol_does_not_match_required")
        if observed.get("polarity") != "positive_semantic":
            errors.append("positive_semantic_polarity_not_proven")

    positive = contract.get("positive_requirement")
    if positive is not None:
        if positive.get("status") != "selected_supported_pair":
            errors.append("positive_skew_pair_not_selected")
        expected_artifacts = positive.get("artifact_identity_sha256")
        if not isinstance(expected_artifacts, str) or len(expected_artifacts) != 64:
            errors.append("positive_skew_artifact_identity_missing")
        elif observed.get("artifact_identity_sha256") != expected_artifacts:
            errors.append("observed_artifact_identity_does_not_match_positive_skew_pair")
        if observed.get("polarity") != "positive_supported_skew":
            errors.append("negative_or_partial_skew_cannot_satisfy_positive_requirement")

    if contract.get("family") == "latest_canary":
        if observed.get("control_classification") != "visible_latest_canary":
            errors.append("visible_canary_classification_missing")
        expected_artifacts = contract.get("artifact_identity_sha256")
        if not isinstance(expected_artifacts, str) or len(expected_artifacts) != 64:
            errors.append("visible_canary_artifact_identity_missing")
        elif observed.get("artifact_identity_sha256") != expected_artifacts:
            errors.append("visible_canary_artifact_identity_mismatch")
    return errors


def _execution_accounting(
    applicability: dict[str, Any], cells: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Account for required and visible cells without trusting a returned ID alone."""

    prepared: list[tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, Any]]]]] = []
    for contract in applicability["cells"]:
        observed = [
            cell for cell in cells if cell.get("target_cell_id", cell.get("id")) == contract["id"]
        ]
        evaluations = [
            {"observed_id": cell.get("id"), "errors": _execution_credit_errors(contract, cell)}
            for cell in observed
        ]
        prepared.append((contract, list(zip(observed, evaluations, strict=True))))

    potentially_credited = [
        cell
        for _contract, pairs in prepared
        for cell, evaluation in pairs
        if not evaluation["errors"]
    ]

    def identity_key(value: dict[str, Any]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    seller_counts = Counter(
        identity_key(cell["seller_process_identity"]) for cell in potentially_credited
    )
    database_counts = Counter(
        identity_key(cell["database_identity"]) for cell in potentially_credited
    )
    for _contract, pairs in prepared:
        for cell, evaluation in pairs:
            if evaluation["errors"]:
                continue
            if seller_counts[identity_key(cell["seller_process_identity"])] != 1:
                evaluation["errors"].append("seller_process_identity_not_unique")
            if database_counts[identity_key(cell["database_identity"])] != 1:
                evaluation["errors"].append("database_identity_not_unique")

    rows: list[dict[str, Any]] = []
    for contract, pairs in prepared:
        credited = [cell for cell, evaluation in pairs if not evaluation["errors"]]
        rows.append(
            {
                "id": contract["id"],
                "contract_id": contract.get("contract_id"),
                "family": contract["family"],
                "blocking": contract["blocking"],
                "observed_controls": [evaluation for _cell, evaluation in pairs],
                "executed_with_required_credit": bool(credited),
                "completed_with_required_credit": any(
                    cell.get("cell_complete") is True for cell in credited
                ),
            }
        )
    return rows


def main() -> None:
    args = _arguments()
    args.output = args.output.absolute()
    routes = [runtime.route for runtime in args.python_runtime]
    if len(routes) != len(set(routes)):
        raise SystemExit("each Python artifact route may be supplied only once")
    artifacts = {artifact.route: artifact for artifact in args.python_artifact}
    if len(artifacts) != len(args.python_artifact):
        raise SystemExit("each Python artifact route may be supplied only once")
    if set(routes) != set(artifacts):
        raise SystemExit("Python runtime and artifact routes must match exactly")
    _validate_python_artifact_inputs(artifacts)
    if bool(args.previous_python_runtime) != bool(args.previous_python_artifact):
        raise SystemExit(
            "--previous-python-runtime and --previous-python-artifact must be supplied together"
        )
    previous_python = None
    if args.previous_python_runtime and args.previous_python_artifact:
        previous_executable = args.previous_python_runtime.absolute()
        previous_artifact = args.previous_python_artifact.absolute()
        if not previous_executable.is_file() or not previous_artifact.is_file():
            raise SystemExit("previous Python runtime or artifact is unavailable")
        previous_python = PreviousPythonInput(previous_executable, previous_artifact)
    dependency_labels = [runtime.label for runtime in args.python_dependency_runtime]
    if len(dependency_labels) != len(set(dependency_labels)):
        raise SystemExit("each Python dependency runtime label may be supplied only once")
    installs = {install.role: install for install in args.typescript_install}
    if len(installs) != len(args.typescript_install):
        raise SystemExit("each TypeScript install role may be supplied only once")
    archives = {archive.role: archive for archive in args.typescript_archive}
    if len(archives) != len(args.typescript_archive):
        raise SystemExit("each TypeScript archive role may be supplied only once")
    if set(archives) != set(installs):
        raise SystemExit("TypeScript install and archive roles must match exactly")
    node = args.node_runtime.absolute() if args.node_runtime else None
    if installs and (node is None or not node.is_file()):
        raise SystemExit("--node-runtime is required with TypeScript installs")
    node_identity = _node_identity(node) if node is not None else None
    typescript_archive_identities = {
        role: _typescript_archive_identity(install, archives[role])
        for role, install in sorted(installs.items())
    }
    args.output.mkdir(parents=True, exist_ok=False)
    run_id = secrets.token_hex(6)
    cells: list[dict[str, Any]] = []
    storyboard_inventories: list[dict[str, Any]] = []
    storyboard_executions: list[dict[str, Any]] = []
    inventory_by_role: dict[str, dict[str, Any]] = {}
    blocking_findings: list[dict[str, Any]] = []
    advisory_findings: list[dict[str, Any]] = []
    dependency_controls: list[dict[str, Any]] = []
    created: list[str] = []
    try:
        for runtime in args.python_dependency_runtime:
            dependency_controls.append(
                _run_dependency_control(
                    runtime,
                    artifacts[runtime.artifact_route],
                    output=args.output,
                )
            )
        if node is not None:
            for role, install in sorted(installs.items()):
                inventory = _run_json(
                    _storyboard_inventory_command(node, install),
                    cwd=args.output,
                    env=os.environ.copy(),
                )
                inventory_path = args.output / f"storyboards-{role}.json"
                inventory_path.write_text(
                    json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                storyboard_inventories.append(
                    {
                        "role": role,
                        "result": inventory,
                        "evidence": _retained(inventory_path, args.output),
                        "execution_complete": False,
                    }
                )
                inventory_by_role[role] = inventory
            if "candidate" in installs:
                advisory_findings.append(
                    _run_resource_location_comparison(
                        args.python_runtime,
                        artifacts,
                        node=node,
                        install=installs["candidate"],
                        output=args.output,
                    )
                )
                advisory_findings.extend(
                    [
                        {
                            "id": "typescript_response_headers_metadata",
                            "status": "supplied_red_not_reexecuted_by_this_matrix",
                            "blocking": False,
                            "applicability": "visible_nonblocking",
                            "typescript_pin": _typescript_pin(installs["candidate"]),
                            "observed": (
                                "responseHeaders omission affects diagnostic response headers"
                            ),
                            "boundaries": [
                                "independently reproduced at Undici 6.28.0 and 6.28.1",
                                "not inferred green from candidate publication",
                                "not reporting-semantic acceptance evidence",
                            ],
                        },
                        {
                            "id": "typescript_specialism_timeout_fragility",
                            "status": "supplied_advisory_not_reexecuted_by_this_matrix",
                            "blocking": False,
                            "applicability": "visible_nonblocking",
                            "typescript_pin": _typescript_pin(installs["candidate"]),
                            "boundaries": [
                                "no retry or test-budget expansion applied",
                                "separate from response-header metadata",
                            ],
                        },
                    ]
                )
                blocking_findings.append(
                    {
                        "id": "typescript_candidate_official_precedence",
                        **_run_official_precedence(node, installs["candidate"], args.output),
                    }
                )
                blocking_findings.append(
                    _run_webhook_receiver_refresh(
                        args.python_runtime,
                        artifacts,
                        output=args.output,
                    )
                )
                blocking_findings.append(
                    {
                        "id": "protocol_rc4_webhook_signing_conformance",
                        **_run_protocol_webhook_vectors(
                            args.python_runtime,
                            artifacts,
                            node=node,
                            install=installs["candidate"],
                            output=args.output,
                        ),
                    }
                )
                blocking_findings.append(
                    {
                        "id": "python_typescript_webhook_signer_interop",
                        **_run_cross_language_webhook_signing(
                            args.python_runtime,
                            artifacts,
                            node=node,
                            install=installs["candidate"],
                            output=args.output,
                        ),
                    }
                )
                signing_database = f"adcp_i_{run_id}_signing"
                created.append(signing_database)
                blocking_findings.append(
                    {
                        "id": "typescript_candidate_signing_and_hmac_controls",
                        **_run_signing_vectors(
                            node,
                            installs["candidate"],
                            admin_url=args.pg_admin_url,
                            output=args.output,
                            database=signing_database,
                        ),
                    }
                )
        for index, runtime in enumerate(args.python_runtime):
            cell_root = args.output / runtime.route / "candidate_py_client__candidate_py_server"
            cell_root.mkdir(parents=True)
            database = f"adcp_i_{run_id}_{index}"
            created.append(database)
            cells.extend(
                _run_python_control(
                    runtime,
                    artifacts[runtime.route],
                    admin_url=args.pg_admin_url,
                    output=cell_root,
                    database=database,
                    node=node,
                    candidate_ts=installs.get("candidate"),
                )
            )
            candidate_ts = installs.get("candidate")
            if candidate_ts is not None and node is not None:
                ts_root = args.output / runtime.route / "candidate_ts_server_core"
                ts_root.mkdir(parents=True)
                ts_database = f"adcp_i_{run_id}_ts_{index}"
                created.append(ts_database)
                cells.extend(
                    _run_ts_core_control(
                        runtime,
                        artifacts[runtime.route],
                        node=node,
                        install=candidate_ts,
                        admin_url=args.pg_admin_url,
                        output=ts_root,
                        database=ts_database,
                    )
                )
        lead = installs.get("lead")
        if lead is not None and node is not None:
            runtime = args.python_runtime[0]
            lead_root = args.output / "supplemental" / "rc44_core"
            lead_root.mkdir(parents=True)
            lead_database = f"adcp_i_{run_id}_lead"
            created.append(lead_database)
            lead_cells = _run_ts_core_control(
                runtime,
                artifacts[runtime.route],
                node=node,
                install=lead,
                admin_url=args.pg_admin_url,
                output=lead_root,
                database=lead_database,
            )
            cells.extend(lead_cells)
        previous_ts = installs.get("previous")
        if previous_ts is not None and node is not None:
            for index, runtime in enumerate(args.python_runtime):
                skew_root = args.output / runtime.route / "candidate_py_client__previous_ts_server"
                skew_root.mkdir(parents=True)
                skew_database = f"adcp_i_{run_id}_skew_ts_{index}"
                created.append(skew_database)
                cells.append(
                    _run_candidate_python_previous_ts(
                        runtime,
                        artifacts[runtime.route],
                        node=node,
                        install=previous_ts,
                        admin_url=args.pg_admin_url,
                        output=skew_root,
                        database=skew_database,
                    )
                )
        candidate_ts = installs.get("candidate")
        if previous_python is not None and candidate_ts is not None and node is not None:
            skew_root = args.output / "previous" / "previous_py_client__candidate_ts_server"
            skew_root.mkdir(parents=True)
            skew_database = f"adcp_i_{run_id}_skew_py"
            created.append(skew_database)
            cells.append(
                _run_previous_python_candidate_ts(
                    previous_python,
                    node=node,
                    install=candidate_ts,
                    admin_url=args.pg_admin_url,
                    output=skew_root,
                    database=skew_database,
                )
            )
        if node is not None:
            for role, install in sorted(installs.items()):
                storyboard_root = args.output / "storyboards" / role
                storyboard_root.mkdir(parents=True)
                storyboard_executions.append(
                    _run_storyboard_server(
                        node,
                        install,
                        archives[role],
                        admin_url=args.pg_admin_url,
                        output=storyboard_root,
                        inventory=inventory_by_role[role],
                    )
                )
    finally:
        if not args.keep_databases:
            for database in reversed(created):
                _drop_database(args.pg_admin_url, database)

    applicability = json.loads(APPLICABILITY.read_text(encoding="utf-8"))
    execution_accounting = _execution_accounting(applicability, cells)
    required_rows = [row for row in execution_accounting if row["blocking"]]
    visible_rows = [row for row in execution_accounting if not row["blocking"]]
    required = {row["id"] for row in required_rows}
    executed = {row["id"] for row in required_rows if row["executed_with_required_credit"] is True}
    completed = {
        row["id"] for row in required_rows if row["completed_with_required_credit"] is True
    }
    executed_visible = {
        row["id"] for row in visible_rows if row["executed_with_required_credit"] is True
    }
    required_visible = {row["id"] for row in visible_rows}
    report = {
        "schema_version": 1,
        "phase": "foundation_qualification_preflight",
        "acceptance": False,
        "status": "incomplete",
        "run_id": run_id,
        "executed_blocking_cells": sorted(executed),
        "missing_blocking_cells": sorted(required - executed),
        "incomplete_blocking_cells": sorted(required - completed),
        "executed_visible_canaries": sorted(executed_visible),
        "missing_visible_canaries": sorted(required_visible - executed_visible),
        "cell_execution_accounting": execution_accounting,
        "artifact_routes": routes,
        "harness": {
            **_git_identity(),
            "inputs": {
                path.name: {
                    "path": str(path.relative_to(ROOT)),
                    "sha256": _sha256(path),
                }
                for path in [APPLICABILITY, HERE / "corpus.json", HERE / "pins.json"]
            },
        },
        "cells": cells,
        "node_runtime": node_identity,
        "typescript_archive_identities": typescript_archive_identities,
        "storyboard_inventories": storyboard_inventories,
        "storyboard_executions": storyboard_executions,
        "blocking_findings": blocking_findings,
        "advisory_findings": advisory_findings,
        "dependency_controls": dependency_controls,
    }
    report_path = args.output / "results.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
