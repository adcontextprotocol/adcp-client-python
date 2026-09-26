#!/usr/bin/env python3
"""Auditable lifecycle for the five Reliable Reporting storyboards.

The default invocation validates inputs and prints a plan.  It never starts a
process.  ``--execute`` is deliberately required before PostgreSQL, seller
processes, or the installed CLI can run.  Even a complete storyboard run is
not matrix or release acceptance; the aggregate gate owns that decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PINS = HERE / "pins.json"
STORYBOARD_PRIVATE_ENV = {
    "auth_token": "ADCP_INTEROP_TS_STORYBOARD_AUTH_TOKEN",
    "startup_proof": "ADCP_INTEROP_TS_STORYBOARD_STARTUP_PROOF",
}

REQUIRED_STORYBOARDS: tuple[tuple[str, int, int, str], ...] = (
    ("reliable_reporting_managed_delivery", 6, 5, "managed"),
    ("reliable_reporting_reconciled_billing", 16, 15, "billing"),
    ("reporting_consumer_status", 43, 41, "controlled"),
    ("reporting_core", 23, 23, "controlled"),
    ("reporting_core_declaration", 1, 0, "controlled"),
)
EXPECTED_TOTALS = {"storyboards": 5, "steps": 89, "stateful_steps": 84}


class OrchestrationError(RuntimeError):
    """A fail-closed preparation or execution error."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _positive_seconds(value: str) -> float:
    number = float(value)
    if not 0 < number <= 3_600:
        raise argparse.ArgumentTypeError("timeout must be greater than zero and at most 3600")
    return number


def _port(value: str) -> int:
    number = int(value)
    if not 1_024 <= number <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return number


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-runtime", type=Path, required=True)
    parser.add_argument("--node-sha256", required=True)
    parser.add_argument("--typescript-install", type=Path, required=True)
    parser.add_argument("--typescript-tarball", type=Path, required=True)
    parser.add_argument(
        "--typescript-role",
        choices=("candidate", "historical_candidate", "previous", "lead"),
        required=True,
    )
    parser.add_argument("--inventory-json", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    database = parser.add_mutually_exclusive_group(required=True)
    database.add_argument("--external-pg-admin-dsn")
    database.add_argument("--local-pg-bin", type=Path)
    parser.add_argument("--allow-non-loopback-external-dsn", action="store_true")
    parser.add_argument("--local-pg-port", type=_port, default=55439)
    parser.add_argument("--service-port-base", type=_port, default=55500)
    parser.add_argument("--startup-timeout", type=_positive_seconds, default=60.0)
    parser.add_argument("--storyboard-timeout", type=_positive_seconds, default=240.0)
    parser.add_argument("--shutdown-timeout", type=_positive_seconds, default=10.0)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


@dataclass(frozen=True)
class Case:
    storyboard_id: str
    step_count: int
    stateful_step_count: int
    server_kind: str


@dataclass(frozen=True)
class FixedInputs:
    role: str
    node: Path
    node_sha256: str
    install: Path
    tarball: Path
    tarball_sha256: str
    package_member_count: int
    package_member_manifest_sha256: str
    lock: Path
    lock_sha256: str
    version: str
    integrity: str
    adcp_version: str
    inventory: Mapping[str, Any]
    cases: tuple[Case, ...]


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OrchestrationError(f"cannot read valid JSON from {path}: {error}") from error


def _inventory_rows(inventory: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = inventory.get("inventory")
    if not isinstance(rows, list):
        raise OrchestrationError("inventory JSON omitted its inventory rows")
    return rows


def validate_inventory(inventory: Mapping[str, Any]) -> tuple[Case, ...]:
    rows = _inventory_rows(inventory)
    if len(rows) != len(REQUIRED_STORYBOARDS) or any(not isinstance(row, dict) for row in rows):
        raise OrchestrationError(
            f"storyboard inventory must contain exactly {len(REQUIRED_STORYBOARDS)} rows"
        )
    by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
    required_ids = [item[0] for item in REQUIRED_STORYBOARDS]
    if list(by_id) != required_ids:
        raise OrchestrationError(
            f"storyboard inventory must contain the exact ordered denominator {required_ids}; "
            f"got {list(by_id)}"
        )
    cases: list[Case] = []
    for storyboard_id, steps, stateful, server_kind in REQUIRED_STORYBOARDS:
        row = by_id[storyboard_id]
        identities = row.get("steps")
        if not isinstance(identities, list) or len(identities) != steps:
            raise OrchestrationError(f"{storyboard_id} must retain {steps} literal step identities")
        observed_ids = [identity.get("id") for identity in identities if isinstance(identity, dict)]
        if (
            len(observed_ids) != steps
            or len(set(observed_ids)) != steps
            or any(not isinstance(value, str) or not value for value in observed_ids)
        ):
            raise OrchestrationError(f"{storyboard_id} has missing or duplicate literal step IDs")
        if row.get("step_count") != steps or row.get("stateful_step_count") != stateful:
            raise OrchestrationError(
                f"{storyboard_id} count drift: expected {steps}/{stateful}, "
                f"got {row.get('step_count')}/{row.get('stateful_step_count')}"
            )
        cases.append(Case(storyboard_id, steps, stateful, server_kind))
    totals = inventory.get("totals")
    if totals != EXPECTED_TOTALS:
        raise OrchestrationError(
            f"storyboard totals drifted: expected {EXPECTED_TOTALS}, got {totals}"
        )
    return tuple(cases)


def _is_loopback(hostname: str | None) -> bool:
    return hostname in {"127.0.0.1", "::1", "localhost"}


def validate_external_dsn(dsn: str, *, allow_non_loopback: bool) -> None:
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise OrchestrationError("external PostgreSQL DSN must be a postgresql:// URL")
    if not allow_non_loopback and not _is_loopback(parsed.hostname):
        raise OrchestrationError(
            "external PostgreSQL DSN must be loopback unless explicitly authorized"
        )
    if parsed.path in {"", "/"}:
        raise OrchestrationError("external PostgreSQL DSN must name an administrative database")


def _typescript_pin(role: str) -> Mapping[str, Any]:
    pins = _load_json(PINS)
    key = {
        "candidate": "development_candidate_rc47",
        "historical_candidate": "historical_candidate_rc45",
        "previous": "previous_compatible",
        "lead": "newer_rc_lead",
    }[role]
    try:
        return pins["typescript"][key]
    except (KeyError, TypeError) as error:
        raise OrchestrationError(f"pins.json omits the TypeScript {role} pin") from error


def _manifest_sha256(entries: Mapping[str, str]) -> str:
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_package_archive(
    tarball: Path, install: Path, pin: Mapping[str, Any]
) -> tuple[int, str]:
    """Bind every published package member to the installed package bytes."""

    if not tarball.is_file():
        raise OrchestrationError(f"TypeScript registry tarball is unavailable: {tarball}")
    observed_tarball_sha256 = sha256(tarball)
    if observed_tarball_sha256 != pin["tarball_sha256"]:
        raise OrchestrationError(
            "TypeScript tarball hash mismatch: "
            f"expected {pin['tarball_sha256']}, got {observed_tarball_sha256}"
        )
    archive: dict[str, str] = {}
    with tarfile.open(tarball, "r:gz") as stream:
        members = stream.getmembers()
        for member in members:
            if member.isdir():
                continue
            if not member.isfile() or not member.name.startswith("package/"):
                raise OrchestrationError(
                    f"TypeScript tarball contains unsupported member {member.name!r}"
                )
            name = member.name.removeprefix("package/")
            if not name or name.startswith("/") or ".." in Path(name).parts or name in archive:
                raise OrchestrationError(f"unsafe or duplicate TypeScript member {member.name!r}")
            extracted = stream.extractfile(member)
            if extracted is None:
                raise OrchestrationError(f"cannot read TypeScript member {member.name!r}")
            archive[name] = hashlib.sha256(extracted.read()).hexdigest()
    expected_count = pin.get("tarball_members")
    if expected_count is not None and len(archive) != expected_count:
        raise OrchestrationError(
            "TypeScript archive member count mismatch: "
            f"expected {expected_count}, got {len(archive)}"
        )
    package_root = install / "node_modules/@adcp/sdk"
    installed = {
        path.relative_to(package_root).as_posix(): sha256(path)
        for path in sorted(package_root.rglob("*"))
        if path.is_file()
    }
    if archive != installed:
        missing = sorted(set(archive) - set(installed))
        unexpected = sorted(set(installed) - set(archive))
        changed = sorted(
            name for name in set(archive) & set(installed) if archive[name] != installed[name]
        )
        raise OrchestrationError(
            "installed TypeScript package differs from registry archive: "
            f"missing={missing[:5]} unexpected={unexpected[:5]} changed={changed[:5]}"
        )
    return len(archive), _manifest_sha256(archive)


def validate_fixed_inputs(args: argparse.Namespace) -> FixedInputs:
    node = args.node_runtime.resolve()
    install = args.typescript_install.resolve()
    tarball = args.typescript_tarball.resolve()
    inventory_path = args.inventory_json.resolve()
    if not node.is_file():
        raise OrchestrationError(f"Node runtime is unavailable: {node}")
    actual_node_hash = sha256(node)
    if actual_node_hash != args.node_sha256:
        raise OrchestrationError(
            f"Node runtime hash mismatch: expected {args.node_sha256}, got {actual_node_hash}"
        )
    pin = _typescript_pin(args.typescript_role)
    if args.typescript_role == "candidate" and pin.get("executable_harness_input") is not True:
        raise OrchestrationError(
            "the rc.6 candidate identity is recorded but has no selected executable lock/member "
            "binding; historical rc.45 cannot substitute for required Q-cell execution"
        )
    lock = install / "package-lock.json"
    package = install / "node_modules/@adcp/sdk/package.json"
    cli = install / "node_modules/@adcp/sdk/bin/adcp.js"
    for path in (lock, package, cli):
        if not path.is_file():
            raise OrchestrationError(f"pinned TypeScript install is incomplete: {path}")
    lock_hash = sha256(lock)
    if lock_hash != pin["package_lock_sha256"]:
        raise OrchestrationError(
            f"TypeScript lock hash mismatch: expected {pin['package_lock_sha256']}, got {lock_hash}"
        )
    lock_data = _load_json(lock)
    package_data = _load_json(package)
    entry = lock_data.get("packages", {}).get("node_modules/@adcp/sdk", {})
    if (
        entry.get("version") != pin["version"]
        or entry.get("integrity") != pin["integrity"]
        or package_data.get("version") != pin["version"]
    ):
        raise OrchestrationError("installed TypeScript package does not match the exact candidate")
    member_count, member_manifest = validate_package_archive(tarball, install, pin)
    if args.external_pg_admin_dsn:
        validate_external_dsn(
            args.external_pg_admin_dsn,
            allow_non_loopback=args.allow_non_loopback_external_dsn,
        )
    else:
        pg_bin = args.local_pg_bin.resolve()
        for name in ("initdb", "postgres"):
            executable = pg_bin / name
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise OrchestrationError(f"local PostgreSQL binary is unavailable: {executable}")
    inventory = _load_json(inventory_path)
    cases = validate_inventory(inventory)
    if args.service_port_base + len(cases) - 1 > 65_535:
        raise OrchestrationError("service port range exceeds 65535")
    return FixedInputs(
        role=args.typescript_role,
        node=node,
        node_sha256=actual_node_hash,
        install=install,
        tarball=tarball,
        tarball_sha256=sha256(tarball),
        package_member_count=member_count,
        package_member_manifest_sha256=member_manifest,
        lock=lock,
        lock_sha256=lock_hash,
        version=pin["version"],
        integrity=pin["integrity"],
        adcp_version=pin["protocol"],
        inventory=inventory,
        cases=cases,
    )


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    state: str
    process_group: int
    session: int
    start_token: str


def _process_identity(pid: int) -> ProcessIdentity | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    closing = stat.rfind(")")
    fields = stat[closing + 2 :].split()
    if closing < 0 or len(fields) <= 19:
        return None
    try:
        return ProcessIdentity(
            pid=pid,
            state=fields[0],
            process_group=int(fields[2]),
            session=int(fields[3]),
            start_token=fields[19],
        )
    except ValueError:
        return None


def _process_start_token(pid: int) -> str | None:
    identity = _process_identity(pid)
    return identity.start_token if identity is not None else None


def _same_process(left: ProcessIdentity | None, right: ProcessIdentity) -> bool:
    return left is not None and (
        left.pid,
        left.process_group,
        left.session,
        left.start_token,
    ) == (
        right.pid,
        right.process_group,
        right.session,
        right.start_token,
    )


def _owned_session_members(session: int, leader_start_token: str) -> tuple[ProcessIdentity, ...]:
    """Return live members created in the owned, newly-created session."""

    try:
        earliest = int(leader_start_token)
    except ValueError as error:
        raise OrchestrationError("owned process start token is not numeric") from error
    members: list[ProcessIdentity] = []
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as error:
        raise OrchestrationError(f"cannot inspect owned process session: {error}") from error
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        identity = _process_identity(int(entry.name))
        if (
            identity is not None
            and identity.state != "Z"
            and identity.session == session
            and int(identity.start_token) >= earliest
        ):
            members.append(identity)
    return tuple(sorted(members, key=lambda item: (item.pid == session, item.pid)))


def assert_process_ownership_supported() -> None:
    """Fail before spawn unless Linux supplies both /proc identity and pidfds."""

    if sys.platform != "linux" or not Path("/proc/self/stat").is_file():
        raise OrchestrationError("owned process execution requires Linux /proc")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise OrchestrationError("owned process execution requires Linux pidfd support")
    try:
        descriptor = os.pidfd_open(os.getpid())
    except OSError as error:
        raise OrchestrationError(f"cannot establish pidfd ownership support: {error}") from error
    os.close(descriptor)


class OwnedProcess:
    """A gated process session guarded by exact pidfd and /proc identities."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        identity: ProcessIdentity | None = None,
        pidfd: int | None = None,
        identity_reader: Callable[[int], ProcessIdentity | None] = _process_identity,
        member_reader: Callable[[int, str], tuple[ProcessIdentity, ...]] = _owned_session_members,
        pidfd_open: Callable[[int], int] | None = None,
        pidfd_signal: Callable[[int, int], None] | None = None,
        pidfd_close: Callable[[int], None] = os.close,
    ) -> None:
        self.process = process
        self.pid = process.pid
        self._identity_reader = identity_reader
        self._member_reader = member_reader
        self._pidfd_open = pidfd_open or os.pidfd_open
        self._pidfd_signal = pidfd_signal or signal.pidfd_send_signal
        self._pidfd_close = pidfd_close
        self.pidfd = pidfd
        self.death_proven = process.poll() is not None
        self.identity = identity if identity is not None else identity_reader(self.pid)
        if self.identity is None:
            raise OrchestrationError(f"cannot establish process ownership for PID {self.pid}")
        if self.identity.process_group != self.pid or self.identity.session != self.pid:
            raise OrchestrationError(
                f"PID {self.pid} does not own the required process group and session"
            )
        self.start_token = self.identity.start_token
        self.session = self.identity.session

    @classmethod
    def spawn(
        cls,
        command: Sequence[str],
        *,
        ownership_timeout: float,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        identity_reader: Callable[[int], ProcessIdentity | None] = _process_identity,
        member_reader: Callable[[int, str], tuple[ProcessIdentity, ...]] = _owned_session_members,
        pidfd_open: Callable[[int], int] | None = None,
        pidfd_signal: Callable[[int, int], None] | None = None,
        pidfd_close: Callable[[int], None] = os.close,
        **kwargs: Any,
    ) -> OwnedProcess:
        """Claim a blocked launcher before allowing the requested program to exec."""

        assert_process_ownership_supported()
        open_pidfd = pidfd_open or os.pidfd_open
        send_pidfd = pidfd_signal or signal.pidfd_send_signal
        gate_read, gate_write = os.pipe()
        inherited = tuple(kwargs.pop("pass_fds", ()))
        launcher = (
            "import json,os,sys;"
            "fd=int(sys.argv[1]);allowed=os.read(fd,1);os.close(fd);"
            "sys.exit(125) if allowed!=b'1' else None;"
            "argv=json.loads(sys.argv[2]);os.execvpe(argv[0],argv,os.environ)"
        )
        wrapped = [
            sys.executable,
            "-I",
            "-c",
            launcher,
            str(gate_read),
            json.dumps([os.fspath(item) for item in command]),
        ]
        try:
            process = popen(wrapped, pass_fds=(*inherited, gate_read), **kwargs)
        except (Exception, KeyboardInterrupt, SystemExit):
            os.close(gate_read)
            os.close(gate_write)
            raise
        os.close(gate_read)
        descriptor: int | None = None
        try:
            descriptor = open_pidfd(process.pid)
            identity = identity_reader(process.pid)
            if identity is None:
                raise OrchestrationError(
                    f"cannot establish /proc start identity for spawned PID {process.pid}"
                )
            if identity.process_group != process.pid or identity.session != process.pid:
                raise OrchestrationError(
                    f"spawned PID {process.pid} does not own its process group and session"
                )
            owned = cls(
                process,
                identity=identity,
                pidfd=descriptor,
                identity_reader=identity_reader,
                member_reader=member_reader,
                pidfd_open=open_pidfd,
                pidfd_signal=send_pidfd,
                pidfd_close=pidfd_close,
            )
            os.write(gate_write, b"1")
            os.close(gate_write)
            return owned
        except Exception as primary:
            cleanup_error: Exception | None = None
            try:
                os.close(gate_write)
            except OSError:
                # Best-effort descriptor cleanup must not replace the primary spawn error.
                pass
            try:
                if process.poll() is None:
                    if descriptor is None:
                        raise OrchestrationError(
                            f"spawned PID {process.pid} has no safe pidfd cleanup handle"
                        )
                    send_pidfd(descriptor, signal.SIGKILL)
                process.wait(timeout=ownership_timeout)
            except Exception as error:  # noqa: BLE001 - preserve both failures
                cleanup_error = error
            if descriptor is not None and process.poll() is not None:
                pidfd_close(descriptor)
            if cleanup_error is not None:
                raise OrchestrationError(
                    f"{primary}; spawned PID {process.pid} death is unproven: {cleanup_error}"
                ) from primary
            raise OrchestrationError(f"{primary}; spawned child was safely settled") from primary

    def stop(
        self,
        timeout: float,
    ) -> None:
        if self.process.poll() is None:
            current = self._identity_reader(self.pid)
            if not _same_process(current, self.identity):
                raise OrchestrationError(f"refusing to signal reused or unowned PID {self.pid}")
        self._signal_owned_members(signal.SIGTERM)
        if not self._wait_until_settled(timeout):
            self._signal_owned_members(signal.SIGKILL)
            if not self._wait_until_settled(min(timeout, 5.0)):
                raise OrchestrationError(
                    f"PID {self.pid} and owned session descendants did not exit after SIGKILL"
                )
        self.death_proven = self.process.poll() is not None and not self._live_members()
        if not self.death_proven:
            raise OrchestrationError(f"PID {self.pid} or owned descendants' death is unproven")
        self._close_pidfd()

    def _live_members(self) -> tuple[ProcessIdentity, ...]:
        members = self._member_reader(self.session, self.start_token)
        if self.process.poll() is None and not any(
            _same_process(member, self.identity) for member in members
        ):
            raise OrchestrationError(
                f"live PID {self.pid} disappeared from its owned session inventory"
            )
        return members

    def _signal_owned_members(self, sig: int) -> None:
        for member in self._live_members():
            descriptor: int | None = None
            try:
                descriptor = self._pidfd_open(member.pid)
                if not _same_process(self._identity_reader(member.pid), member):
                    continue
                self._pidfd_signal(descriptor, sig)
            except ProcessLookupError:
                continue
            finally:
                if descriptor is not None:
                    self._pidfd_close(descriptor)

    def _wait_until_settled(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.process.wait(timeout=0)
            except subprocess.TimeoutExpired:
                # A live child is expected during this zero-timeout polling probe.
                pass
            if self.process.poll() is not None and not self._live_members():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _close_pidfd(self) -> None:
        if self.pidfd is not None:
            self._pidfd_close(self.pidfd)
            self.pidfd = None


class OwnedTempRoot:
    """Temporary directory deleted only when its unpredictable owner marker matches."""

    def __init__(self, parent: Path | None = None) -> None:
        parent_value = str(parent.resolve()) if parent else None
        self.path = Path(tempfile.mkdtemp(prefix="adcp-storyboard-pg-", dir=parent_value)).resolve()
        self.token = secrets.token_hex(24)
        self.marker = self.path / ".adcp-storyboard-owner.json"
        self.marker.write_text(json.dumps({"token": self.token}) + "\n", encoding="utf-8")

    def cleanup(self) -> None:
        if not self.path.exists():
            return
        try:
            marker = json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise OrchestrationError(f"refusing to clean unmarked temp root {self.path}") from error
        if marker != {"token": self.token}:
            raise OrchestrationError(
                f"refusing to clean temp root with mismatched owner {self.path}"
            )
        shutil.rmtree(self.path)


class LocalPostgres:
    def __init__(
        self,
        *,
        pg_bin: Path,
        port: int,
        output: Path,
        startup_timeout: float,
        shutdown_timeout: float,
    ) -> None:
        self.pg_bin = pg_bin.resolve()
        self.port = port
        self.output = output
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.temp: OwnedTempRoot | None = None
        self.process: OwnedProcess | None = None
        self.log_stream: Any = None
        self.identity: dict[str, Any] | None = None

    def _dsn(self, socket_directory: Path) -> str:
        from psycopg.conninfo import make_conninfo

        fields: dict[str, Any] = {
            "host": str(socket_directory),
            "port": self.port,
            "dbname": "postgres",
        }
        if os.environ.get("USER"):
            fields["user"] = os.environ["USER"]
        return make_conninfo(**fields)

    def _wait_owned_ready(
        self, dsn: str, data: Path, process: subprocess.Popen[bytes]
    ) -> dict[str, Any]:
        import psycopg

        deadline = time.monotonic() + self.startup_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise OrchestrationError(
                    f"owned PostgreSQL exited before readiness with {process.returncode}"
                )
            try:
                with psycopg.connect(
                    dsn,
                    connect_timeout=max(1, math.ceil(min(self.startup_timeout, 5))),
                    options="-c statement_timeout=5000 -c lock_timeout=5000",
                ) as connection:
                    row = connection.execute(
                        "SELECT current_setting('data_directory'), "
                        "pg_postmaster_start_time()::text, pg_backend_pid()"
                    ).fetchone()
                if row is None or Path(row[0]).resolve() != data.resolve():
                    raise OrchestrationError(
                        f"PostgreSQL data_directory is not the owned path: {row}"
                    )
                return {
                    "data_directory": str(data.resolve()),
                    "postmaster_started_at": row[1],
                    "readiness_backend_pid": row[2],
                    "postmaster_pid": process.pid,
                    "postmaster_start_token": self.process.start_token if self.process else None,
                }
            except (psycopg.Error, OSError) as error:
                last_error = error
                time.sleep(0.05)
        raise OrchestrationError(f"owned PostgreSQL readiness timed out: {last_error}")

    def start(self) -> str:
        try:
            self.temp = OwnedTempRoot()
            data = self.temp.path / "data"
            socket_directory = self.temp.path / "socket"
            socket_directory.mkdir(mode=0o700)
            init_exit = _run_owned_to_files(
                [
                    str(self.pg_bin / "initdb"),
                    "-D",
                    str(data),
                    "--no-locale",
                    "--encoding=UTF8",
                ],
                cwd=self.output,
                env=_minimal_environment(),
                stdout_path=self.output / "postgres-init.stdout.log",
                stderr_path=self.output / "postgres-init.stderr.log",
                timeout=self.startup_timeout,
                shutdown_timeout=self.shutdown_timeout,
            )
            if init_exit != 0:
                raise OrchestrationError(f"initdb failed with exit {init_exit}")
            self.log_stream = (self.output / "postgres.log").open("wb")
            self.process = OwnedProcess.spawn(
                [
                    str(self.pg_bin / "postgres"),
                    "-D",
                    str(data),
                    "-c",
                    "listen_addresses=",
                    "-k",
                    str(socket_directory),
                    "-p",
                    str(self.port),
                ],
                ownership_timeout=self.shutdown_timeout,
                stdin=subprocess.DEVNULL,
                stdout=self.log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            dsn = self._dsn(socket_directory)
            self.identity = self._wait_owned_ready(dsn, data, self.process.process)
            return dsn
        except Exception as primary:
            try:
                self.close()
            except Exception as cleanup:
                raise OrchestrationError(
                    f"{primary}; startup cleanup failed: {cleanup}"
                ) from primary
            raise

    def close(self) -> None:
        errors: list[str] = []
        death_proven = self.process is None
        if self.process:
            try:
                self.process.stop(self.shutdown_timeout)
                death_proven = self.process.death_proven
            except Exception as error:  # noqa: BLE001 - aggregate cleanup failures
                errors.append(str(error))
                # A dead direct child does not prove that every member of its
                # owned session exited. Preserve the data root on any stop error.
                death_proven = False
        if self.log_stream:
            self.log_stream.close()
        if self.temp and death_proven:
            try:
                self.temp.cleanup()
            except Exception as error:  # noqa: BLE001 - aggregate cleanup failures
                errors.append(str(error))
        elif self.temp:
            errors.append(f"retained live or unproven PostgreSQL data at {self.temp.path}")
        if errors:
            raise OrchestrationError("PostgreSQL cleanup failed: " + "; ".join(errors))


def _database_url(admin_dsn: str, database: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    fields = conninfo_to_dict(admin_dsn)
    fields["dbname"] = database
    return make_conninfo(**fields)


class DatabaseOwner:
    def __init__(self, admin_dsn: str, run_id: str, timeout: float) -> None:
        self.admin_dsn = admin_dsn
        self.run_id = run_id
        self.timeout = timeout
        self.created: list[str] = []

    def _connect(self, dsn: str, *, autocommit: bool):
        import psycopg

        milliseconds = max(1, math.ceil(self.timeout * 1_000))
        return psycopg.connect(
            dsn,
            autocommit=autocommit,
            connect_timeout=max(1, math.ceil(self.timeout)),
            options=f"-c statement_timeout={milliseconds} -c lock_timeout={milliseconds}",
        )

    def create(self, index: int) -> str:
        from psycopg import sql

        name = f"adcp_story_{self.run_id}_{index}"
        if name in self.created:
            raise OrchestrationError(f"database name was already claimed: {name}")
        # Retain the exact possible-effect name before sending CREATE. Cleanup
        # can then address only this name even if transport failure obscures the result.
        self.created.append(name)
        with self._connect(self.admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL(
                    "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' "
                    "LC_COLLATE 'C' LC_CTYPE 'C'"
                ).format(sql.Identifier(name))
            )
        database_dsn = _database_url(self.admin_dsn, name)
        with self._connect(database_dsn, autocommit=False) as connection:
            row = connection.execute("SELECT current_database()").fetchone()
        if row != (name,):
            raise OrchestrationError(f"created database identity mismatch: {row}")
        return database_dsn

    def close(self) -> None:
        from psycopg import sql

        failures: list[str] = []
        for name in reversed(self.created):
            try:
                with self._connect(self.admin_dsn, autocommit=True) as connection:
                    connection.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
                    )
            except Exception as error:  # noqa: BLE001 - retain every cleanup failure
                failures.append(f"{name}: {error}")
        if failures:
            raise OrchestrationError("database cleanup failed: " + "; ".join(failures))


def _server_command(
    inputs: FixedInputs,
    case: Case,
    *,
    port: int,
    database_url: str | None,
    ready_file: Path,
) -> list[str]:
    common = [
        "--package-lock",
        str(inputs.lock),
        "--expected-version",
        inputs.version,
        "--expected-integrity",
        inputs.integrity,
        "--adcp-version",
        inputs.adcp_version,
        "--port",
        str(port),
        "--host",
        "127.0.0.1",
        "--ready-file",
        str(ready_file),
    ]
    if case.server_kind == "controlled":
        return [
            str(inputs.node),
            str(HERE / "ts_controlled_reporting_server.cjs"),
            "--storyboard-id",
            case.storyboard_id,
            *common,
        ]
    if database_url is None:
        raise OrchestrationError(f"{case.storyboard_id} requires an isolated PostgreSQL database")
    mode = "managed" if case.server_kind == "managed" else "billing"
    return [
        str(inputs.node),
        str(HERE / "ts_managed_reporting_server.cjs"),
        "--mode",
        mode,
        "--pg-url",
        database_url,
        *common,
    ]


def _server_environment(auth_token: str, startup_proof: str) -> dict[str, str]:
    """Supply private inputs outside argv, without claiming same-user secrecy."""

    return {
        **_minimal_environment(),
        STORYBOARD_PRIVATE_ENV["auth_token"]: auth_token,
        STORYBOARD_PRIVATE_ENV["startup_proof"]: startup_proof,
    }


def _storyboard_command(
    inputs: FixedInputs,
    case: Case,
    *,
    endpoint: str,
) -> list[str]:
    return [
        str(inputs.node),
        str(inputs.install / "node_modules/@adcp/sdk/bin/adcp.js"),
        "storyboard",
        "run",
        endpoint,
        case.storyboard_id,
        "--json",
        "--protocol",
        "mcp",
        "--allow-http",
        "--sandbox",
        "--timeout",
        "180",
    ]


def _step_rows(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        step
        for track in result.get("tracks", [])
        for scenario in track.get("scenarios", [])
        for step in scenario.get("steps", [])
    ]


def _wait_owned_server(
    process: OwnedProcess,
    ready_file: Path,
    expected: Mapping[str, Any],
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.process.poll() is not None:
            raise OrchestrationError(
                f"owned seller exited before readiness with {process.process.returncode}"
            )
        if ready_file.is_file():
            try:
                ready = _load_json(ready_file)
                _validate_seller_readiness(ready, expected)
                with socket.create_connection(("127.0.0.1", expected["port"]), timeout=0.2):
                    return dict(ready)
            except (OSError, OrchestrationError) as error:
                last_error = error
        time.sleep(0.05)
    raise OrchestrationError(f"owned seller readiness timed out: {last_error}")


def _validate_seller_readiness(ready: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    """Bind a ready record to the seller process, package, runtime, and run secret."""

    for field in ("pid", "port", "startup_proof_sha256", "auth_binding_sha256"):
        if ready.get(field) != expected.get(field):
            raise OrchestrationError(f"seller readiness {field} mismatch")
    if ready.get("seller_package") != expected.get("seller_package"):
        raise OrchestrationError("seller readiness package identity mismatch")
    observed_node = ready.get("node")
    expected_node = expected.get("node")
    if not isinstance(observed_node, dict) or not isinstance(expected_node, dict):
        raise OrchestrationError("seller readiness runtime identity missing")
    if observed_node.get("executable") != expected_node.get("executable"):
        raise OrchestrationError("seller readiness runtime executable mismatch")
    version = observed_node.get("version")
    if not isinstance(version, str) or not version.startswith("v"):
        raise OrchestrationError("seller readiness runtime version missing")


def evaluate_case(
    case: Case,
    inventory_row: Mapping[str, Any],
    result: Mapping[str, Any],
    exit_code: int,
) -> dict[str, Any]:
    if result.get("storyboards_executed") != [case.storyboard_id]:
        raise OrchestrationError(f"storyboard selection drifted for {case.storyboard_id}")
    expected = inventory_row["steps"]
    observed = _step_rows(result)
    expected_counts = Counter((item.get("title"), item.get("task")) for item in expected)
    observed_counts = Counter((item.get("step"), item.get("task")) for item in observed)
    buckets: dict[tuple[Any, Any], list[Mapping[str, Any]]] = {}
    for step in observed:
        buckets.setdefault((step.get("step"), step.get("task")), []).append(step)
    executed: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for identity in expected:
        bucket = buckets.get((identity.get("title"), identity.get("task")), [])
        if bucket:
            executed.append((identity, bucket.pop(0)))
    executed_ids = [identity["id"] for identity, _ in executed]
    missing_ids = [identity["id"] for identity in expected if identity["id"] not in executed_ids]
    unexpected_rows = [
        {
            "step": step.get("step"),
            "task": step.get("task"),
            "passed": step.get("passed"),
            "skipped": step.get("skipped"),
        }
        for key, count in (observed_counts - expected_counts).items()
        for step in [item for item in observed if (item.get("step"), item.get("task")) == key][
            :count
        ]
    ]
    passed = (
        exit_code == 0
        and not missing_ids
        and not unexpected_rows
        and observed_counts == expected_counts
        and len(observed) == case.step_count
        and len(executed) == case.step_count
        and all(
            step.get("passed") is True and step.get("skipped") is not True for _, step in executed
        )
    )
    return {
        "id": case.storyboard_id,
        "exit_code": exit_code,
        "resolved_steps": case.step_count,
        "resolved_stateful_steps": case.stateful_step_count,
        "executed_step_ids": executed_ids,
        "unexecuted_step_ids": missing_ids,
        "observed_result_row_count": len(observed),
        "observed_identity_counts": [
            {"step": key[0], "task": key[1], "count": count}
            for key, count in sorted(observed_counts.items(), key=lambda item: repr(item[0]))
        ],
        "unexpected_result_rows": unexpected_rows,
        "fully_executed": passed,
        "summary": result.get("summary"),
    }


def _minimal_environment() -> dict[str, str]:
    allowed = {"PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "SYSTEMROOT", "WINDIR"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def _storyboard_environment(auth_token: str) -> dict[str, str]:
    """Supply the CLI bearer through its supported environment fallback."""

    return {
        **_minimal_environment(),
        "ADCP_ALLOW_INTERNAL_PROBES": "1",
        "ADCP_AUTH_TOKEN": auth_token,
    }


def _run_owned_to_files(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdout_path: Path,
    stderr_path: Path,
    timeout: float,
    shutdown_timeout: float,
) -> int:
    """Run one command with owned descendants and retain output on every exit path."""

    primary: BaseException | None = None
    cleanup: BaseException | None = None
    owned: OwnedProcess | None = None
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            owned = OwnedProcess.spawn(
                command,
                ownership_timeout=shutdown_timeout,
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            owned.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            primary = OrchestrationError(
                f"command timed out after {timeout}s; partial output retained at "
                f"{stdout_path} and {stderr_path}"
            )
            primary.__cause__ = error
        except (Exception, KeyboardInterrupt, SystemExit) as error:
            primary = error
        finally:
            if owned is not None:
                try:
                    owned.stop(shutdown_timeout)
                except (Exception, KeyboardInterrupt, SystemExit) as error:
                    cleanup = error
    if primary is not None and cleanup is not None:
        raise OrchestrationError(
            f"execution failed: {primary}; cleanup failed: {cleanup}"
        ) from primary
    if cleanup is not None:
        raise OrchestrationError(f"command cleanup failed: {cleanup}") from cleanup
    if primary is not None:
        raise primary
    if owned is None or owned.process.returncode is None:
        raise OrchestrationError("owned command completed without a settled return code")
    return owned.process.returncode


def _run_case(
    inputs: FixedInputs,
    case: Case,
    inventory_row: Mapping[str, Any],
    *,
    index: int,
    port: int,
    database_url: str | None,
    output: Path,
    startup_timeout: float,
    storyboard_timeout: float,
    shutdown_timeout: float,
) -> dict[str, Any]:
    case_output = output / case.storyboard_id
    case_output.mkdir()
    server_stdout_path = case_output / "server.stdout.log"
    server_stderr_path = case_output / "server.stderr.log"
    auth_token = secrets.token_urlsafe(32)
    startup_proof = secrets.token_hex(32)
    ready_file = case_output / "seller.ready.json"
    server_command = _server_command(
        inputs,
        case,
        port=port,
        database_url=database_url,
        ready_file=ready_file,
    )
    primary: BaseException | None = None
    cleanup: BaseException | None = None
    result: dict[str, Any] | None = None
    process: OwnedProcess | None = None
    with server_stdout_path.open("wb") as stdout, server_stderr_path.open("wb") as stderr:
        try:
            process = OwnedProcess.spawn(
                server_command,
                ownership_timeout=shutdown_timeout,
                cwd=ROOT,
                env=_server_environment(auth_token, startup_proof),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            readiness = _wait_owned_server(
                process,
                ready_file,
                {
                    "pid": process.pid,
                    "port": port,
                    "startup_proof_sha256": hashlib.sha256(startup_proof.encode()).hexdigest(),
                    "auth_binding_sha256": hashlib.sha256(auth_token.encode()).hexdigest(),
                    "seller_package": {
                        "name": "@adcp/sdk",
                        "version": inputs.version,
                        "integrity": inputs.integrity,
                        "adcp_version": inputs.adcp_version,
                    },
                    "node": {"executable": str(inputs.node.resolve())},
                },
                startup_timeout,
            )
            command = _storyboard_command(
                inputs,
                case,
                endpoint=f"http://127.0.0.1:{port}/mcp",
            )
            storyboard_stdout = case_output / "storyboard.stdout.json"
            storyboard_stderr = case_output / "storyboard.stderr.log"
            exit_code = _run_owned_to_files(
                command,
                cwd=case_output,
                env=_storyboard_environment(auth_token),
                timeout=storyboard_timeout,
                shutdown_timeout=shutdown_timeout,
                stdout_path=storyboard_stdout,
                stderr_path=storyboard_stderr,
            )
            try:
                observed = json.loads(storyboard_stdout.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise OrchestrationError(
                    f"{case.storyboard_id} emitted invalid JSON with exit {exit_code}"
                ) from error
            result = evaluate_case(case, inventory_row, observed, exit_code)
            result["seller_readiness"] = readiness
            result["seller_process_identity"] = {
                "pid": process.pid,
                "start_token": process.identity.start_token,
                "startup_proof_sha256": hashlib.sha256(startup_proof.encode()).hexdigest(),
            }
            result["seller_artifact_identity"] = {
                "language": "typescript",
                "package_role": inputs.role,
                "package": "@adcp/sdk",
                "version": inputs.version,
                "integrity": inputs.integrity,
                "adcp_version": inputs.adcp_version,
                "tarball_sha256": inputs.tarball_sha256,
                "package_member_manifest_sha256": inputs.package_member_manifest_sha256,
                "node_sha256": inputs.node_sha256,
                "artifact_identity_sha256": inputs.tarball_sha256,
                "runtime_identity_sha256": _manifest_sha256(
                    {"node_path": str(inputs.node), "node_sha256": inputs.node_sha256}
                ),
            }
        except (Exception, KeyboardInterrupt, SystemExit) as error:
            primary = error
        finally:
            if process is not None:
                try:
                    process.stop(shutdown_timeout)
                except (Exception, KeyboardInterrupt, SystemExit) as error:
                    cleanup = error
    if primary is not None and cleanup is not None:
        raise OrchestrationError(
            f"{case.storyboard_id} execution failed: {primary}; seller cleanup failed: {cleanup}"
        ) from primary
    if cleanup is not None:
        raise OrchestrationError(
            f"{case.storyboard_id} seller cleanup failed: {cleanup}"
        ) from cleanup
    if primary is not None:
        raise primary
    if result is None:
        raise OrchestrationError(f"{case.storyboard_id} produced no result")
    return result


def execute_case_sequence(
    cases: Iterable[Case],
    runner: Callable[[int, Case], dict[str, Any]],
    cleanup: Callable[[], None],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run in order, stop on the first failure, and always surface cleanup errors."""

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        for index, case in enumerate(cases):
            try:
                row = runner(index, case)
                rows.append(row)
            except Exception as error:  # noqa: BLE001 - convert to retained result
                errors.append(f"{case.storyboard_id}: {error}")
                break
            if row.get("fully_executed") is not True:
                errors.append(f"{case.storyboard_id}: storyboard did not fully execute")
                break
    finally:
        try:
            cleanup()
        except Exception as error:  # noqa: BLE001 - cleanup failure is blocking
            errors.append(f"cleanup: {error}")
    return rows, errors


def _plan(inputs: FixedInputs, args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": "execute" if args.execute else "validate_only",
        "acceptance": False,
        "blocking_acceptance": False,
        "typescript": {
            "role": inputs.role,
            "version": inputs.version,
            "integrity": inputs.integrity,
            "registry_tarball": str(inputs.tarball),
            "registry_tarball_sha256": inputs.tarball_sha256,
            "package_lock": str(inputs.lock),
            "package_lock_sha256": inputs.lock_sha256,
            "package_member_count": inputs.package_member_count,
            "package_member_manifest_sha256": inputs.package_member_manifest_sha256,
        },
        "node": {"path": str(inputs.node), "sha256": inputs.node_sha256},
        "database_mode": "external" if args.external_pg_admin_dsn else "owned_local",
        "cases": [case.__dict__ for case in inputs.cases],
        "totals": EXPECTED_TOTALS,
        "timeouts": {
            "startup": args.startup_timeout,
            "storyboard": args.storyboard_timeout,
            "shutdown": args.shutdown_timeout,
        },
        "limitations": [
            "storyboard_execution_is_not_six_cell_matrix_acceptance",
            "probe_scheduler_dst_remains_mandatory_and_uncredited_until_it_executes",
            "python_final_artifacts_and_1172_composition_are_not_selected_here",
            "prior_cyberPolicy_reason_is_not_available_in_retained_tool_output",
        ],
    }


def execute(inputs: FixedInputs, args: argparse.Namespace) -> dict[str, Any]:
    if args.output is None:
        raise OrchestrationError("--output is required with --execute")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    plan = _plan(inputs, args)
    (output / "plan.json").write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    local: LocalPostgres | None = None
    owner: DatabaseOwner | None = None
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    primary: BaseException | None = None
    cleanup_errors: list[str] = []
    admin_dsn: str | None = None
    inventory_by_id = {row["id"]: row for row in _inventory_rows(inputs.inventory)}

    def run(index: int, case: Case) -> dict[str, Any]:
        if owner is None:
            raise OrchestrationError("database owner was not initialized")
        database_url = owner.create(index) if case.server_kind != "controlled" else None
        return _run_case(
            inputs,
            case,
            inventory_by_id[case.storyboard_id],
            index=index,
            port=args.service_port_base + index,
            database_url=database_url,
            output=output,
            startup_timeout=args.startup_timeout,
            storyboard_timeout=args.storyboard_timeout,
            shutdown_timeout=args.shutdown_timeout,
        )

    try:
        if args.external_pg_admin_dsn:
            admin_dsn = args.external_pg_admin_dsn
        else:
            local = LocalPostgres(
                pg_bin=args.local_pg_bin,
                port=args.local_pg_port,
                output=output,
                startup_timeout=args.startup_timeout,
                shutdown_timeout=args.shutdown_timeout,
            )
            admin_dsn = local.start()
        owner = DatabaseOwner(admin_dsn, secrets.token_hex(6), args.startup_timeout)
        rows, errors = execute_case_sequence(inputs.cases, run, lambda: None)
    except (Exception, KeyboardInterrupt, SystemExit) as error:
        primary = error
    finally:
        if owner is not None:
            try:
                owner.close()
            except (Exception, KeyboardInterrupt, SystemExit) as error:
                cleanup_errors.append(f"database cleanup: {error}")
        if local is not None:
            try:
                local.close()
            except (Exception, KeyboardInterrupt, SystemExit) as error:
                cleanup_errors.append(f"PostgreSQL cleanup: {error}")
    if primary is not None and cleanup_errors:
        raise OrchestrationError(
            f"execution failed: {primary}; " + "; ".join(cleanup_errors)
        ) from primary
    if primary is not None:
        raise primary
    errors.extend(cleanup_errors)
    if local is not None:
        plan["owned_postgresql_identity"] = local.identity
    completed = (
        len(rows) == len(inputs.cases)
        and all(row.get("fully_executed") is True for row in rows)
        and not errors
    )
    result = {
        **plan,
        "status": "storyboard_complete_nonaccepting" if completed else "incomplete",
        "execution_complete": completed,
        "acceptance": False,
        "blocking_acceptance": False,
        "rows": rows,
        "errors": errors,
    }
    (output / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        inputs = validate_fixed_inputs(args)
        if not args.execute:
            print(json.dumps(_plan(inputs, args), indent=2, sort_keys=True))
            return 0
        result = execute(inputs, args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["execution_complete"] else 1
    except Exception as error:  # noqa: BLE001 - command boundary must fail closed
        print(
            json.dumps(
                {
                    "status": "harness_error",
                    "acceptance": False,
                    "blocking_acceptance": False,
                    "error": str(error),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
