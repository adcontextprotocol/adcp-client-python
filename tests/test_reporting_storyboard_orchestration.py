from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import signal
import subprocess
import sys
import tarfile
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/ci/reporting_interop/storyboard_orchestration.py"
SPEC = importlib.util.spec_from_file_location("reporting_storyboard_orchestration", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
orchestration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = orchestration
SPEC.loader.exec_module(orchestration)


def inventory() -> dict:
    rows = []
    for storyboard_id, steps, stateful, _server_kind in orchestration.REQUIRED_STORYBOARDS:
        rows.append(
            {
                "id": storyboard_id,
                "step_count": steps,
                "stateful_step_count": stateful,
                "steps": [
                    {"id": f"{storyboard_id}-{index}", "title": f"step {index}", "task": "task"}
                    for index in range(steps)
                ],
            }
        )
    return {"inventory": rows, "totals": dict(orchestration.EXPECTED_TOTALS)}


def test_inventory_requires_all_five_storyboards_in_exact_order() -> None:
    value = inventory()
    cases = orchestration.validate_inventory(value)
    assert [case.storyboard_id for case in cases] == [
        item[0] for item in orchestration.REQUIRED_STORYBOARDS
    ]

    value["inventory"].pop()
    with pytest.raises(orchestration.OrchestrationError, match="exactly 5 rows"):
        orchestration.validate_inventory(value)

    value = inventory()
    value["inventory"].append(dict(value["inventory"][-1]))
    with pytest.raises(orchestration.OrchestrationError, match="exactly 5 rows"):
        orchestration.validate_inventory(value)

    value = inventory()
    value["inventory"].reverse()
    with pytest.raises(orchestration.OrchestrationError, match="exact ordered denominator"):
        orchestration.validate_inventory(value)


def test_inventory_rejects_count_and_literal_identity_drift() -> None:
    value = inventory()
    value["inventory"][0]["steps"][1]["id"] = value["inventory"][0]["steps"][0]["id"]
    with pytest.raises(orchestration.OrchestrationError, match="missing or duplicate"):
        orchestration.validate_inventory(value)

    value = inventory()
    value["totals"]["steps"] -= 1
    with pytest.raises(orchestration.OrchestrationError, match="totals drifted"):
        orchestration.validate_inventory(value)


def test_external_dsn_is_explicit_and_loopback_by_default() -> None:
    orchestration.validate_external_dsn(
        "postgresql://127.0.0.1:5432/postgres", allow_non_loopback=False
    )
    with pytest.raises(orchestration.OrchestrationError, match="must be loopback"):
        orchestration.validate_external_dsn(
            "postgresql://database.example.test/postgres", allow_non_loopback=False
        )
    orchestration.validate_external_dsn(
        "postgresql://database.example.test/postgres", allow_non_loopback=True
    )


def test_wrong_node_or_package_identity_fails_before_execution(tmp_path: Path) -> None:
    node = tmp_path / "node"
    node.write_bytes(b"not-the-pinned-node")
    node.chmod(0o755)
    install = tmp_path / "install"
    (install / "node_modules/@adcp/sdk/bin").mkdir(parents=True)
    (install / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (install / "node_modules/@adcp/sdk/package.json").write_text(
        '{"version":"14.0.0-rc.45"}\n', encoding="utf-8"
    )
    (install / "node_modules/@adcp/sdk/bin/adcp.js").write_text("", encoding="utf-8")
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(inventory()), encoding="utf-8")
    args = SimpleNamespace(
        node_runtime=node,
        node_sha256="0" * 64,
        typescript_install=install,
        typescript_tarball=tmp_path / "sdk.tgz",
        typescript_role="candidate",
        inventory_json=inventory_path,
        external_pg_admin_dsn="postgresql://127.0.0.1/postgres",
        allow_non_loopback_external_dsn=False,
        local_pg_bin=None,
        service_port_base=55_500,
    )
    with pytest.raises(orchestration.OrchestrationError, match="Node runtime hash mismatch"):
        orchestration.validate_fixed_inputs(args)

    args.node_sha256 = orchestration.sha256(node)
    with pytest.raises(
        orchestration.OrchestrationError,
        match="rc.6 candidate identity.*no selected executable lock/member binding",
    ):
        orchestration.validate_fixed_inputs(args)


class FakeProcess:
    def __init__(self, pid: int = 4312, *, timeout_once: bool = False) -> None:
        self.pid = pid
        self.returncode = None
        self.wait_timeouts: list[float] = []
        self.timeout_once = timeout_once

    def poll(self):
        return self.returncode

    def wait(self, timeout: float):
        self.wait_timeouts.append(timeout)
        if self.timeout_once:
            self.timeout_once = False
            raise subprocess.TimeoutExpired("fake", timeout)
        self.returncode = 0
        return 0


def test_owned_process_never_signals_a_reused_pid() -> None:
    process = FakeProcess()
    signals: list[tuple[int, int]] = []
    original = orchestration.ProcessIdentity(process.pid, "S", process.pid, process.pid, "10")
    reused = orchestration.ProcessIdentity(process.pid, "S", process.pid, process.pid, "20")
    owned = orchestration.OwnedProcess(
        process,
        identity=original,
        identity_reader=lambda _pid: reused,
        member_reader=lambda _session, _start: (reused,),
        pidfd_open=lambda pid: pid,
        pidfd_signal=lambda pid, sig: signals.append((pid, sig)),
        pidfd_close=lambda _fd: None,
    )
    with pytest.raises(orchestration.OrchestrationError, match="unowned PID"):
        owned.stop(1)
    assert signals == []


def test_owned_process_escalates_only_exact_owned_session_members() -> None:
    class StubbornProcess(FakeProcess):
        def wait(self, timeout: float):
            self.wait_timeouts.append(timeout)
            if self.returncode is None:
                raise subprocess.TimeoutExpired("fake", timeout)
            return self.returncode

    process = StubbornProcess()
    signals: list[tuple[int, int]] = []
    leader = orchestration.ProcessIdentity(process.pid, "S", process.pid, process.pid, "10")
    child = orchestration.ProcessIdentity(process.pid + 1, "S", process.pid, process.pid, "11")

    def signal_member(pid: int, sig: int) -> None:
        signals.append((pid, sig))
        if sig == signal.SIGKILL and pid == process.pid:
            process.returncode = -signal.SIGKILL

    owned = orchestration.OwnedProcess(
        process,
        identity=leader,
        identity_reader=lambda pid: leader if pid == leader.pid else child,
        member_reader=lambda _session, _start: (
            () if process.returncode is not None else (child, leader)
        ),
        pidfd_open=lambda pid: pid,
        pidfd_signal=signal_member,
        pidfd_close=lambda _fd: None,
    )
    owned.stop(0)
    assert signals == [
        (child.pid, signal.SIGTERM),
        (leader.pid, signal.SIGTERM),
        (child.pid, signal.SIGKILL),
        (leader.pid, signal.SIGKILL),
    ]
    assert owned.death_proven is True


def test_owned_process_settles_descendant_after_direct_child_exit() -> None:
    process = FakeProcess()
    process.returncode = 0
    leader = orchestration.ProcessIdentity(process.pid, "Z", process.pid, process.pid, "10")
    child = orchestration.ProcessIdentity(process.pid + 1, "S", process.pid, process.pid, "11")
    child_alive = True
    signals: list[tuple[int, int]] = []

    def members(_session: int, _start: str):
        return (child,) if child_alive else ()

    def signal_member(pid: int, sig: int) -> None:
        nonlocal child_alive
        signals.append((pid, sig))
        child_alive = False

    owned = orchestration.OwnedProcess(
        process,
        identity=leader,
        identity_reader=lambda _pid: child,
        member_reader=members,
        pidfd_open=lambda pid: pid,
        pidfd_signal=signal_member,
        pidfd_close=lambda _fd: None,
    )
    owned.stop(1)
    assert signals == [(child.pid, signal.SIGTERM)]
    assert owned.death_proven is True


def test_owned_temp_cleanup_requires_the_original_marker(tmp_path: Path) -> None:
    owned = orchestration.OwnedTempRoot(tmp_path)
    retained = owned.path / "retained.txt"
    retained.write_text("evidence", encoding="utf-8")
    owned.marker.write_text('{"token":"different"}\n', encoding="utf-8")
    with pytest.raises(orchestration.OrchestrationError, match="mismatched owner"):
        owned.cleanup()
    assert retained.is_file()

    owned.marker.write_text(json.dumps({"token": owned.token}) + "\n", encoding="utf-8")
    owned.cleanup()
    assert not owned.path.exists()


def test_local_postgres_init_failure_removes_only_its_owned_temp_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestration, "_run_owned_to_files", lambda *args, **kwargs: 1)
    lifecycle = orchestration.LocalPostgres(
        pg_bin=tmp_path / "bin",
        port=55_439,
        output=tmp_path,
        startup_timeout=1,
        shutdown_timeout=1,
    )
    with pytest.raises(orchestration.OrchestrationError, match="initdb failed"):
        lifecycle.start()
    assert lifecycle.temp is not None
    assert not lifecycle.temp.path.exists()


def test_spawn_preflight_fails_before_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    invoked: list[bool] = []
    monkeypatch.setattr(
        orchestration,
        "assert_process_ownership_supported",
        lambda: (_ for _ in ()).throw(orchestration.OrchestrationError("unsupported")),
    )
    with pytest.raises(orchestration.OrchestrationError, match="unsupported"):
        orchestration.OwnedProcess.spawn(
            ["never"], ownership_timeout=1, popen=lambda *_args, **_kwargs: invoked.append(True)
        )
    assert invoked == []


def test_spawn_proc_identity_failure_settles_child_through_pidfd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestration, "assert_process_ownership_supported", lambda: None)
    process = FakeProcess()
    read_fd, write_fd = orchestration.os.pipe()
    signals: list[int] = []
    try:
        with pytest.raises(orchestration.OrchestrationError, match="safely settled"):
            orchestration.OwnedProcess.spawn(
                ["fake"],
                ownership_timeout=1,
                popen=lambda *_args, **_kwargs: process,
                pidfd_open=lambda _pid: read_fd,
                pidfd_signal=lambda _fd, sig: signals.append(sig),
                identity_reader=lambda _pid: None,
            )
        assert signals == [signal.SIGKILL]
        assert process.returncode == 0
    finally:
        orchestration.os.close(write_fd)


def test_spawn_uses_a_gated_launcher_before_releasing_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestration, "assert_process_ownership_supported", lambda: None)
    process = FakeProcess()
    identity = orchestration.ProcessIdentity(process.pid, "S", process.pid, process.pid, "10")
    captured: dict[str, object] = {}
    inherited_gate: list[int] = []

    def popen(command, **kwargs):
        captured["command"] = command
        captured["pass_fds"] = kwargs["pass_fds"]
        inherited_gate.append(orchestration.os.dup(kwargs["pass_fds"][0]))
        return process

    owned = orchestration.OwnedProcess.spawn(
        ["target", "--flag"],
        ownership_timeout=1,
        popen=popen,
        identity_reader=lambda _pid: identity,
        member_reader=lambda _session, _start: (),
        pidfd_open=lambda _pid: 77,
        pidfd_signal=lambda _fd, _sig: None,
        pidfd_close=lambda _fd: None,
        start_new_session=True,
    )
    assert captured["command"][:3] == [sys.executable, "-I", "-c"]
    assert "os.execvpe" in captured["command"][3]
    assert json.loads(captured["command"][5]) == ["target", "--flag"]
    assert len(captured["pass_fds"]) == 1
    orchestration.os.close(inherited_gate.pop())
    process.returncode = 0
    owned.stop(1)


def test_postgres_stop_failure_retains_owned_data(tmp_path: Path) -> None:
    lifecycle = orchestration.LocalPostgres(
        pg_bin=tmp_path / "bin",
        port=55_439,
        output=tmp_path,
        startup_timeout=1,
        shutdown_timeout=1,
    )
    lifecycle.temp = orchestration.OwnedTempRoot(tmp_path)
    retained = lifecycle.temp.path
    running = FakeProcess()

    class FailedStop:
        process = running

        def stop(self, _timeout):
            raise orchestration.OrchestrationError("death unproven")

    lifecycle.process = FailedStop()
    with pytest.raises(orchestration.OrchestrationError, match=str(retained)):
        lifecycle.close()
    assert retained.is_dir()


def test_postgres_stop_failure_retains_data_even_if_direct_child_exited(tmp_path: Path) -> None:
    lifecycle = orchestration.LocalPostgres(
        pg_bin=tmp_path / "bin",
        port=55_439,
        output=tmp_path,
        startup_timeout=1,
        shutdown_timeout=1,
    )
    lifecycle.temp = orchestration.OwnedTempRoot(tmp_path)
    retained = lifecycle.temp.path
    exited = FakeProcess()
    exited.returncode = 0

    class FailedDescendantProof:
        process = exited

        def stop(self, _timeout):
            raise orchestration.OrchestrationError("descendant death unproven")

    lifecycle.process = FailedDescendantProof()
    with pytest.raises(orchestration.OrchestrationError, match=str(retained)):
        lifecycle.close()
    assert retained.is_dir()


def test_failure_propagates_stops_later_cases_and_cleanup_always_runs() -> None:
    cases = [
        orchestration.Case("one", 1, 1, "controlled"),
        orchestration.Case("two", 1, 1, "controlled"),
        orchestration.Case("three", 1, 1, "controlled"),
    ]
    invoked: list[str] = []
    cleaned: list[bool] = []

    def runner(_index, case):
        invoked.append(case.storyboard_id)
        return {"id": case.storyboard_id, "fully_executed": case.storyboard_id == "one"}

    rows, errors = orchestration.execute_case_sequence(cases, runner, lambda: cleaned.append(True))
    assert [row["id"] for row in rows] == ["one", "two"]
    assert invoked == ["one", "two"]
    assert cleaned == [True]
    assert errors == ["two: storyboard did not fully execute"]


def test_cleanup_failure_is_blocking_even_after_success() -> None:
    case = orchestration.Case("one", 1, 1, "controlled")

    def cleanup():
        raise RuntimeError("drop failed")

    rows, errors = orchestration.execute_case_sequence(
        [case], lambda _index, _case: {"id": "one", "fully_executed": True}, cleanup
    )
    assert rows == [{"id": "one", "fully_executed": True}]
    assert errors == ["cleanup: drop failed"]


def test_storyboard_timeout_retains_partial_output_and_settles_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeProcess(timeout_once=True)
    stopped: list[float] = []

    class Owned:
        def __init__(self, stdout):
            self.process = process
            stdout.write(b"partial-json")
            stdout.flush()

        def stop(self, timeout):
            stopped.append(timeout)
            process.wait(timeout)

    monkeypatch.setattr(
        orchestration.OwnedProcess,
        "spawn",
        lambda *_args, **kwargs: Owned(kwargs["stdout"]),
    )
    stdout = tmp_path / "stdout.json"
    stderr = tmp_path / "stderr.log"
    with pytest.raises(orchestration.OrchestrationError, match="partial output retained"):
        orchestration._run_owned_to_files(
            ["fake"],
            cwd=tmp_path,
            env={},
            stdout_path=stdout,
            stderr_path=stderr,
            timeout=1,
            shutdown_timeout=2,
        )
    assert stdout.read_bytes() == b"partial-json"
    assert stopped == [2]
    assert process.returncode == 0


def test_storyboard_timeout_reports_execution_and_cleanup_failures_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeProcess(timeout_once=True)

    class Owned:
        def __init__(self):
            self.process = process

        def stop(self, _timeout):
            raise orchestration.OrchestrationError("descendants remain")

    monkeypatch.setattr(orchestration.OwnedProcess, "spawn", lambda *_args, **_kwargs: Owned())
    with pytest.raises(
        orchestration.OrchestrationError,
        match="execution failed: command timed out.*cleanup failed: descendants remain",
    ):
        orchestration._run_owned_to_files(
            ["fake"],
            cwd=tmp_path,
            env={},
            stdout_path=tmp_path / "stdout.json",
            stderr_path=tmp_path / "stderr.log",
            timeout=1,
            shutdown_timeout=2,
        )


def test_stale_ready_file_cannot_prove_owned_seller(tmp_path: Path) -> None:
    process = FakeProcess(pid=7182)
    owned = SimpleNamespace(process=process, pid=process.pid)
    ready = tmp_path / "ready.json"
    ready.write_text(
        json.dumps({"startup_proof_sha256": "stale", "pid": process.pid, "port": 55_500}),
        encoding="utf-8",
    )
    with pytest.raises(orchestration.OrchestrationError, match="startup_proof_sha256 mismatch"):
        orchestration._wait_owned_server(
            owned,
            ready,
            {
                "startup_proof_sha256": "fresh",
                "pid": process.pid,
                "port": 55_500,
                "auth_binding_sha256": "a" * 64,
                "seller_package": {},
                "node": {"executable": "/node"},
            },
            0.01,
        )


def test_seller_readiness_rejects_misrouted_package_runtime_and_auth() -> None:
    expected = {
        "pid": 101,
        "port": 55_500,
        "startup_proof_sha256": "a" * 64,
        "auth_binding_sha256": "a" * 64,
        "seller_package": {
            "name": "@adcp/sdk",
            "version": "14.0.0-rc.47",
            "integrity": "sha512-expected",
            "adcp_version": "3.2.0-rc.6",
        },
        "node": {"executable": "/owned/node"},
    }
    ready = {
        **expected,
        "node": {"executable": "/owned/node", "version": "v22.12.0"},
    }
    orchestration._validate_seller_readiness(ready, expected)

    for replacement, message in [
        ({"auth_binding_sha256": "b" * 64}, "auth_binding_sha256 mismatch"),
        (
            {"seller_package": {**expected["seller_package"], "integrity": "sha512-stale"}},
            "package identity mismatch",
        ),
        (
            {"node": {"executable": "/stale/node", "version": "v22.12.0"}},
            "runtime executable mismatch",
        ),
    ]:
        with pytest.raises(orchestration.OrchestrationError, match=message):
            orchestration._validate_seller_readiness({**ready, **replacement}, expected)


def test_storyboard_private_inputs_are_supplied_outside_every_server_argv(
    tmp_path: Path,
) -> None:
    inputs = orchestration.FixedInputs(
        role="candidate",
        node=tmp_path / "node",
        node_sha256="a" * 64,
        install=tmp_path / "install",
        tarball=tmp_path / "sdk.tgz",
        tarball_sha256="b" * 64,
        package_member_count=1,
        package_member_manifest_sha256="c" * 64,
        lock=tmp_path / "package-lock.json",
        lock_sha256="d" * 64,
        version="14.0.0-rc.47",
        integrity="sha512-example",
        adcp_version="3.2.0-rc.6",
        inventory=inventory(),
        cases=orchestration.validate_inventory(inventory()),
    )
    token = "a" * 43
    proof = "b" * 64
    for case, database_url in [
        (orchestration.Case("reporting_core", 23, 23, "controlled"), None),
        (
            orchestration.Case("reliable_reporting_managed_delivery", 6, 5, "managed"),
            "postgresql://fixture/db",
        ),
        (
            orchestration.Case("reliable_reporting_reconciled_billing", 16, 15, "billing"),
            "postgresql://fixture/db",
        ),
    ]:
        command = orchestration._server_command(
            inputs,
            case,
            port=55_500,
            database_url=database_url,
            ready_file=tmp_path / f"{case.storyboard_id}.ready.json",
        )
        assert token not in command and proof not in command
        assert "--auth-token" not in command and "--startup-proof" not in command
    environment = orchestration._server_environment(token, proof)
    assert environment[orchestration.STORYBOARD_PRIVATE_ENV["auth_token"]] == token
    assert environment[orchestration.STORYBOARD_PRIVATE_ENV["startup_proof"]] == proof


def test_storyboard_cli_handoff_uses_supported_auth_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = orchestration.FixedInputs(
        role="candidate",
        node=tmp_path / "node",
        node_sha256="a" * 64,
        install=tmp_path / "install",
        tarball=tmp_path / "sdk.tgz",
        tarball_sha256="b" * 64,
        package_member_count=1,
        package_member_manifest_sha256="c" * 64,
        lock=tmp_path / "package-lock.json",
        lock_sha256="d" * 64,
        version="14.0.0-rc.47",
        integrity="sha512-example",
        adcp_version="3.2.0-rc.6",
        inventory=inventory(),
        cases=orchestration.validate_inventory(inventory()),
    )
    case = orchestration.Case("unit_storyboard", 1, 1, "controlled")
    inventory_row = {
        "id": case.storyboard_id,
        "steps": [{"id": "step-1", "title": "step one", "task": "unit_task"}],
    }
    output = tmp_path / "run"
    output.mkdir()
    captured: dict[str, object] = {}
    server_environment: dict[str, str] = {}
    stopped: list[bool] = []

    class FakeOwned:
        pid = 4242
        identity = SimpleNamespace(start_token="123")

        def stop(self, _timeout: float) -> None:
            stopped.append(True)

    def record_server(_command, **kwargs):
        server_environment.update(kwargs["env"])
        return FakeOwned()

    monkeypatch.setattr(orchestration.OwnedProcess, "spawn", record_server)
    monkeypatch.setattr(
        orchestration,
        "_wait_owned_server",
        lambda *_args, **_kwargs: {"kind": "owned-ready"},
    )
    monkeypatch.setattr(orchestration, "_minimal_environment", lambda: {"PATH": "/runtime"})

    def record_cli(command, **kwargs):
        captured["command"] = list(command)
        captured["environment"] = dict(kwargs["env"])
        kwargs["stdout_path"].write_text(
            json.dumps(
                {
                    "storyboards_executed": [case.storyboard_id],
                    "tracks": [
                        {
                            "scenarios": [
                                {
                                    "steps": [
                                        {
                                            "step": "step one",
                                            "task": "unit_task",
                                            "passed": True,
                                        }
                                    ]
                                }
                            ]
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(orchestration, "_run_owned_to_files", record_cli)
    result = orchestration._run_case(
        inputs,
        case,
        inventory_row,
        index=0,
        port=55_500,
        database_url=None,
        output=output,
        startup_timeout=1,
        storyboard_timeout=1,
        shutdown_timeout=1,
    )

    command = captured["command"]
    environment = captured["environment"]
    assert isinstance(command, list) and "--auth" not in command
    assert isinstance(environment, dict)
    assert environment == {
        "PATH": "/runtime",
        "ADCP_ALLOW_INTERNAL_PROBES": "1",
        "ADCP_AUTH_TOKEN": environment["ADCP_AUTH_TOKEN"],
    }
    assert (
        environment["ADCP_AUTH_TOKEN"]
        == server_environment[orchestration.STORYBOARD_PRIVATE_ENV["auth_token"]]
    )
    assert environment["ADCP_AUTH_TOKEN"] not in command
    assert result["fully_executed"] is True
    assert stopped == [True]


def test_uncertain_create_is_retained_for_exact_name_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = orchestration.DatabaseOwner("postgresql://127.0.0.1/postgres", "abc123", timeout=1)
    statements: list[object] = []
    create_attempts = 0

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement):
            nonlocal create_attempts
            statements.append(statement)
            if create_attempts == 0:
                create_attempts += 1
                raise RuntimeError("transport outcome unknown")
            return SimpleNamespace(fetchone=lambda: None)

    class SQL:
        def __init__(self, value):
            self.value = value

        def format(self, *_args):
            return self

    fake_sql = SimpleNamespace(SQL=SQL, Identifier=lambda value: value)
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(sql=fake_sql))

    monkeypatch.setattr(owner, "_connect", lambda *_args, **_kwargs: Connection())
    with pytest.raises(RuntimeError, match="outcome unknown"):
        owner.create(4)
    assert owner.created == ["adcp_story_abc123_4"]
    owner.close()
    assert len(statements) == 2


def test_postgres_started_then_setup_failed_still_runs_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    class Local:
        identity = {"data_directory": "/owned"}

        def __init__(self, **_kwargs):
            pass

        def start(self):
            return "postgresql://127.0.0.1/postgres"

        def close(self):
            closed.append(True)

    class Owner:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("subsequent setup failed")

    monkeypatch.setattr(orchestration, "LocalPostgres", Local)
    monkeypatch.setattr(orchestration, "DatabaseOwner", Owner)
    inputs = orchestration.FixedInputs(
        role="candidate",
        node=tmp_path / "node",
        node_sha256="a" * 64,
        install=tmp_path / "install",
        tarball=tmp_path / "sdk.tgz",
        tarball_sha256="b" * 64,
        package_member_count=1,
        package_member_manifest_sha256="c" * 64,
        lock=tmp_path / "package-lock.json",
        lock_sha256="d" * 64,
        version="14.0.0-rc.45",
        integrity="sha512-example",
        adcp_version="3.2.0-rc.4",
        inventory=inventory(),
        cases=orchestration.validate_inventory(inventory()),
    )
    args = SimpleNamespace(
        output=tmp_path / "out",
        execute=True,
        external_pg_admin_dsn=None,
        local_pg_bin=tmp_path / "bin",
        local_pg_port=55_439,
        service_port_base=55_500,
        startup_timeout=1,
        storyboard_timeout=1,
        shutdown_timeout=1,
    )
    with pytest.raises(RuntimeError, match="subsequent setup failed"):
        orchestration.execute(inputs, args)
    assert closed == [True]


def test_case_result_requires_exit_success_all_identities_and_no_skips() -> None:
    case = orchestration.Case("story", 2, 2, "controlled")
    row = {
        "id": "story",
        "steps": [
            {"id": "a", "title": "A", "task": "tool"},
            {"id": "b", "title": "B", "task": "tool"},
        ],
    }
    result = {
        "storyboards_executed": ["story"],
        "summary": {"steps_passed": 2},
        "tracks": [
            {
                "scenarios": [
                    {
                        "steps": [
                            {"step": "A", "task": "tool", "passed": True},
                            {"step": "B", "task": "tool", "passed": True},
                        ]
                    }
                ]
            }
        ],
    }
    assert orchestration.evaluate_case(case, row, result, 0)["fully_executed"] is True
    result["tracks"][0]["scenarios"][0]["steps"][1]["skipped"] = True
    assert orchestration.evaluate_case(case, row, result, 0)["fully_executed"] is False
    assert orchestration.evaluate_case(case, row, result, 1)["fully_executed"] is False

    result["tracks"][0]["scenarios"][0]["steps"][1].pop("skipped")
    result["tracks"][0]["scenarios"][0]["steps"].append(
        {"step": "unexpected", "task": "tool", "passed": False}
    )
    evaluated = orchestration.evaluate_case(case, row, result, 0)
    assert evaluated["fully_executed"] is False
    assert evaluated["observed_result_row_count"] == 3
    assert evaluated["unexpected_result_rows"] == [
        {"step": "unexpected", "task": "tool", "passed": False, "skipped": None}
    ]


def test_plan_never_claims_matrix_or_release_acceptance(tmp_path: Path) -> None:
    inputs = orchestration.FixedInputs(
        role="candidate",
        node=tmp_path / "node",
        node_sha256="a" * 64,
        install=tmp_path / "install",
        tarball=tmp_path / "sdk.tgz",
        tarball_sha256="c" * 64,
        package_member_count=1,
        package_member_manifest_sha256="d" * 64,
        lock=tmp_path / "package-lock.json",
        lock_sha256="b" * 64,
        version="14.0.0-rc.45",
        integrity="sha512-example",
        adcp_version="3.2.0-rc.4",
        inventory=inventory(),
        cases=orchestration.validate_inventory(inventory()),
    )
    args = SimpleNamespace(
        execute=False,
        external_pg_admin_dsn="postgresql://127.0.0.1/postgres",
        startup_timeout=60,
        storyboard_timeout=240,
        shutdown_timeout=10,
    )
    plan = orchestration._plan(inputs, args)
    assert plan["acceptance"] is False
    assert plan["blocking_acceptance"] is False
    assert "probe_scheduler_dst_remains_mandatory" in " ".join(plan["limitations"])


def test_execution_source_uses_no_pg_ctl_or_shell_process_wrapper() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "pg_ctl" not in source
    assert "shell=True" not in source
    assert "start_new_session=True" in source
    assert '"--execute"' in source
    assert '"--sandbox"' in source


def test_archive_manifest_binds_every_installed_member(tmp_path: Path) -> None:
    package = tmp_path / "install/node_modules/@adcp/sdk"
    package.mkdir(parents=True)
    (package / "package.json").write_bytes(b'{"name":"@adcp/sdk"}\n')
    tarball = tmp_path / "sdk.tgz"
    with tarfile.open(tarball, "w:gz") as stream:
        payload = b'{"name":"@adcp/sdk"}\n'
        member = tarfile.TarInfo("package/package.json")
        member.size = len(payload)
        stream.addfile(member, io.BytesIO(payload))
    pin = {
        "tarball_sha256": orchestration.sha256(tarball),
        "tarball_members": 1,
    }
    count, manifest = orchestration.validate_package_archive(tarball, tmp_path / "install", pin)
    assert count == 1
    assert len(manifest) == 64
    (package / "package.json").write_bytes(b"changed")
    with pytest.raises(orchestration.OrchestrationError, match="differs from registry archive"):
        orchestration.validate_package_archive(tarball, tmp_path / "install", pin)


def test_previous_and_candidate_python_members_use_distinct_immutable_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.sql = SimpleNamespace()
    fake_conninfo = types.ModuleType("psycopg.conninfo")
    fake_conninfo.conninfo_to_dict = lambda _value: {}
    fake_conninfo.make_conninfo = lambda **_value: ""
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.conninfo", fake_conninfo)
    runner_path = ROOT / "scripts/ci/reporting_interop/run_foundation_matrix.py"
    spec = importlib.util.spec_from_file_location("reporting_foundation_identity_test", runner_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)

    previous_wheel = tmp_path / "previous.whl"
    with zipfile.ZipFile(previous_wheel, "w") as archive:
        archive.writestr("adcp/__init__.py", b"previous")
    pins = tmp_path / "pins.json"
    pins.write_text(
        json.dumps(
            {
                "python": {
                    "previous_released": {
                        "wheel_sha256": runner._sha256(previous_wheel),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "PINS", pins)
    monkeypatch.setattr(
        runner,
        "_selected_python_build",
        lambda: {"sdk_member_count": 9, "sdk_member_manifest_sha256": "c" * 64},
    )
    candidate = runner._expected_python_member_identity(
        runner.PythonRuntime("wheel", tmp_path / "python"),
        runner.PythonArtifact("wheel", tmp_path / "candidate.whl"),
    )
    previous = runner._expected_python_member_identity(
        runner.PythonRuntime("previous", tmp_path / "python"),
        runner.PythonArtifact("previous", previous_wheel),
    )
    assert candidate == {
        "source": "selected_candidate_build_manifest",
        "sdk_member_count": 9,
        "sdk_member_manifest_sha256": "c" * 64,
    }
    assert previous["source"] == "exact_previous_wheel_members"
    assert previous["sdk_member_count"] == 1
    assert previous["sdk_member_manifest_sha256"] != candidate["sdk_member_manifest_sha256"]


def test_matrix_binds_complete_typescript_install_to_its_role_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.sql = SimpleNamespace()
    fake_conninfo = types.ModuleType("psycopg.conninfo")
    fake_conninfo.conninfo_to_dict = lambda _value: {}
    fake_conninfo.make_conninfo = lambda **_value: ""
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.conninfo", fake_conninfo)
    runner_path = ROOT / "scripts/ci/reporting_interop/run_foundation_matrix.py"
    spec = importlib.util.spec_from_file_location("reporting_matrix_archive_test", runner_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)

    install = tmp_path / "install"
    package_root = install / "node_modules/@adcp/sdk"
    (package_root / "dist").mkdir(parents=True)
    members = {
        "package/package.json": b'{"name":"@adcp/sdk","version":"14.0.0-test"}\n',
        "package/dist/index.js": b"module.exports = {};\n",
    }
    (package_root / "package.json").write_bytes(members["package/package.json"])
    (package_root / "dist/index.js").write_bytes(members["package/dist/index.js"])
    integrity = "sha512-test"
    lock = {
        "packages": {
            "node_modules/@adcp/sdk": {
                "version": "14.0.0-test",
                "integrity": integrity,
            }
        }
    }
    (install / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    archive = tmp_path / "sdk.tgz"
    with tarfile.open(archive, "w:gz") as stream:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            stream.addfile(info, io.BytesIO(payload))
    pins = tmp_path / "pins.json"
    pins.write_text(
        json.dumps(
            {
                "typescript": {
                    "development_candidate_rc47": {
                        "version": "14.0.0-test",
                        "integrity": integrity,
                        "tarball_sha256": runner._sha256(archive),
                        "tarball_members": 2,
                        "package_lock_sha256": runner._sha256(install / "package-lock.json"),
                        "executable_harness_input": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "PINS", pins)
    identity = runner._typescript_archive_identity(
        runner.TypeScriptInstall("candidate", install),
        runner.TypeScriptArchive("candidate", archive),
    )
    assert identity["installed_member_count"] == 2

    (package_root / "dist/index.js").write_text("changed\n", encoding="utf-8")
    with pytest.raises(runner.HarnessError, match="installed SDK differs"):
        runner._typescript_archive_identity(
            runner.TypeScriptInstall("candidate", install),
            runner.TypeScriptArchive("candidate", archive),
        )


def test_storyboard_role_map_uses_the_declared_lead_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = tmp_path / "pins.json"
    pins.write_text(
        json.dumps({"typescript": {"newer_rc_lead": {"version": "lead"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestration, "PINS", pins)
    assert orchestration._typescript_pin("lead") == {"version": "lead"}


def test_storyboard_candidate_map_refuses_historical_rc45_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = tmp_path / "pins.json"
    pins.write_text(
        json.dumps(
            {
                "typescript": {
                    "development_candidate_rc47": {
                        "version": "14.0.0-rc.47",
                        "protocol": "3.2.0-rc.6",
                        "executable_harness_input": False,
                    },
                    "historical_candidate_rc45": {
                        "version": "14.0.0-rc.45",
                        "protocol": "3.2.0-rc.4",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestration, "PINS", pins)
    assert orchestration._typescript_pin("candidate")["version"] == "14.0.0-rc.47"
    assert orchestration._typescript_pin("historical_candidate")["version"] == "14.0.0-rc.45"


def test_foundation_lead_core_control_retains_real_rc44_protocol_in_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.sql = SimpleNamespace()
    fake_conninfo = types.ModuleType("psycopg.conninfo")
    fake_conninfo.conninfo_to_dict = lambda _value: {}
    fake_conninfo.make_conninfo = lambda **_value: ""
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.conninfo", fake_conninfo)
    runner_path = ROOT / "scripts/ci/reporting_interop/run_foundation_matrix.py"
    spec = importlib.util.spec_from_file_location("reporting_matrix_lead_result", runner_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)

    install = runner.TypeScriptInstall("lead", ROOT / "scripts/ci/reporting_interop/npm/rc44")
    pin = runner._typescript_pin(install)
    assert pin["version"] == "14.0.0-rc.44"
    assert pin["protocol"] == "3.2.0-rc.4"

    output = tmp_path / "result"
    output.mkdir()
    calls = iter([{"definitive": True}, {"status": "passed"}])
    monkeypatch.setattr(
        runner,
        "_create_database",
        lambda _admin, name: {"name": name, "cluster_identity": "fake-cluster"},
    )
    monkeypatch.setattr(runner, "_node_database_url", lambda *_args: "postgresql://fake/db")
    monkeypatch.setattr(runner, "_free_port", lambda: 55_555)
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *_args, **_kwargs: SimpleNamespace(pid=4242),
    )

    def fake_ready(_process, ready_file, **_kwargs):
        ready_file.write_text("{}\n", encoding="utf-8")
        return {
            "pid": 4242,
            "process_start_token": "123",
            "startup_proof_sha256": "f" * 64,
            "seller_package": {
                "integrity": pin["integrity"],
                "language": "typescript",
                "name": "@adcp/sdk",
                "package_role": "lead",
                "protocol": pin["protocol"],
                "version": pin["version"],
            },
        }

    monkeypatch.setattr(runner, "_wait_core_seller_ready", fake_ready)
    monkeypatch.setattr(runner, "_run_json", lambda *_args, **_kwargs: next(calls))
    monkeypatch.setattr(runner, "_stop", lambda _process: None)
    monkeypatch.setattr(
        runner,
        "_runtime_identity",
        lambda *_args: {"route": "wheel", "identity": "fake-candidate"},
    )

    rows = runner._run_ts_core_control(
        runner.PythonRuntime("wheel", tmp_path / "python"),
        runner.PythonArtifact("wheel", tmp_path / "candidate.whl"),
        node=tmp_path / "node",
        install=install,
        admin_url="postgresql://fake/postgres",
        output=output,
        database="lead-control",
    )

    assert len(rows) == 2
    assert {row["observed_protocol"] for row in rows} == {"3.2.0-rc.4"}
    assert all(row["required_cell_credit"] is False for row in rows)
    assert rows[1]["id"].startswith("supplemental_lead_shared__")
    assert rows[1]["control_classification"] == "supplemental_lead_shared_seller_control"
    assert rows[1]["acceptance"] is False


def test_core_seller_credentials_fail_closed_on_low_entropy_or_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.sql = SimpleNamespace()
    fake_conninfo = types.ModuleType("psycopg.conninfo")
    fake_conninfo.conninfo_to_dict = lambda _value: {}
    fake_conninfo.make_conninfo = lambda **_value: ""
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.conninfo", fake_conninfo)
    runner_path = ROOT / "scripts/ci/reporting_interop/run_foundation_matrix.py"
    spec = importlib.util.spec_from_file_location("reporting_matrix_secret_test", runner_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)

    monkeypatch.setattr(runner.secrets, "token_urlsafe", lambda _size: "fixed")
    monkeypatch.setattr(runner.secrets, "token_hex", lambda _size: "fixed")
    with pytest.raises(runner.HarnessError, match="low-entropy or repeated"):
        runner._new_core_seller_credentials()

    values = iter(["a" * 43, "b" * 43])
    monkeypatch.setattr(runner.secrets, "token_urlsafe", lambda _size: next(values))
    monkeypatch.setattr(runner.secrets, "token_hex", lambda _size: "c" * 64)
    credentials = runner._new_core_seller_credentials()
    assert credentials.token_a != credentials.token_b
    command = runner._ts_server_command(
        tmp_path / "node",
        runner.TypeScriptInstall("lead", ROOT / "scripts/ci/reporting_interop/npm/rc44"),
        port=55_555,
        database_url="postgresql://fixture/db",
        ready_file=ROOT / "seller.ready.unit.json",
    )
    assert "interop-token-a" not in command
    assert credentials.token_a not in command and credentials.token_b not in command
    assert credentials.startup_proof not in command
    assert "--auth-token-a" not in command and "--auth-token-b" not in command
    assert "--startup-proof" not in command
    environment = runner._ts_server_environment(credentials)
    assert environment[runner.TS_CORE_PRIVATE_ENV["token_a"]] == credentials.token_a
    assert environment[runner.TS_CORE_PRIVATE_ENV["token_b"]] == credentials.token_b
    assert environment[runner.TS_CORE_PRIVATE_ENV["startup_proof"]] == credentials.startup_proof
    assert str(tmp_path / "node") == command[0]
    launches: list[tuple[list[str], dict[str, object]]] = []

    def capture_launch(argv, **kwargs):
        launches.append((argv, kwargs))
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(runner.subprocess, "Popen", capture_launch)
    monkeypatch.setattr(
        runner,
        "_typescript_pin",
        lambda install: {
            "package": "@adcp/sdk",
            "version": f"14.0.0-{install.role}",
            "integrity": f"sha512-{install.role}",
            "protocol": "3.2.0-rc.6",
        },
    )
    for role in ("candidate", "previous", "lead"):
        runner._start_ts_core_server(
            tmp_path / "node",
            runner.TypeScriptInstall(role, ROOT / "scripts/ci/reporting_interop/npm/rc44"),
            port=55_555,
            database_url="postgresql://fixture/db",
            credentials=credentials,
            ready_file=tmp_path / f"{role}.ready.json",
            cwd=tmp_path,
            stdout=io.BytesIO(),
            stderr=io.BytesIO(),
        )
    assert len(launches) == 3
    for argv, kwargs in launches:
        assert credentials.token_a not in argv
        assert credentials.token_b not in argv
        assert credentials.startup_proof not in argv
        child_environment = kwargs["env"]
        assert child_environment[runner.TS_CORE_PRIVATE_ENV["token_a"]] == credentials.token_a
        assert child_environment[runner.TS_CORE_PRIVATE_ENV["token_b"]] == credentials.token_b
        assert (
            child_environment[runner.TS_CORE_PRIVATE_ENV["startup_proof"]]
            == credentials.startup_proof
        )


def test_foundation_handoffs_use_narrow_environments_for_every_scoped_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.sql = SimpleNamespace()
    fake_conninfo = types.ModuleType("psycopg.conninfo")
    fake_conninfo.conninfo_to_dict = lambda _value: {}
    fake_conninfo.make_conninfo = lambda **_value: ""
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.conninfo", fake_conninfo)
    runner_path = ROOT / "scripts/ci/reporting_interop/run_foundation_matrix.py"
    spec = importlib.util.spec_from_file_location("reporting_matrix_environment_test", runner_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)

    parent = {
        "PATH": "/runtime/bin",
        "LANG": "C.UTF-8",
        "TZ": "Etc/UTC",
        "AMBIENT_TOKEN": "ambient-token",
        "AWS_SECRET_ACCESS_KEY": "ambient-cloud-secret",
        "UNRELATED_PASSWORD": "ambient-password",
        "DATABASE_URL": "postgresql://ambient/unsafe",
        "ADCP_AUTH_TOKEN": "stale-cli-token",
        runner.TS_CORE_PRIVATE_ENV["token_a"]: "stale-a",
        runner.TS_CORE_PRIVATE_ENV["token_b"]: "stale-b",
        runner.TS_CORE_PRIVATE_ENV["startup_proof"]: "stale-proof",
    }
    original_parent = dict(parent)
    monkeypatch.setattr(runner.os, "environ", parent)
    monkeypatch.setattr(
        runner,
        "_typescript_pin",
        lambda install: {
            "package": "@adcp/sdk",
            "version": f"14.0.0-{install.role}",
            "integrity": f"sha512-{install.role}",
            "protocol": "3.2.0-rc.4",
        },
    )
    monkeypatch.setattr(
        runner,
        "_create_database",
        lambda _admin, name: {"name": name, "cluster_identity": "fixture-cluster"},
    )
    monkeypatch.setattr(runner, "_node_database_url", lambda *_args: "postgresql://owned/db")
    monkeypatch.setattr(runner, "_free_port", lambda: 55_555)
    monkeypatch.setattr(runner, "_wait_port", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_stop", lambda _process: None)
    monkeypatch.setattr(runner, "_runtime_identity", lambda *_args: {"route": "fixture"})
    monkeypatch.setattr(
        runner,
        "_sha256",
        lambda path: "previous-wheel-hash" if path.name == "previous.whl" else "fixture-hash",
    )

    pins = tmp_path / "pins.json"
    pins.write_text(
        json.dumps({"python": {"previous_released": {"wheel_sha256": "previous-wheel-hash"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "PINS", pins)

    buyer_handoffs: list[tuple[list[str], dict[str, str]]] = []
    seller_handoffs: list[tuple[list[str], dict[str, str]]] = []

    def record_popen(command, **kwargs):
        if "env" in kwargs:
            seller_handoffs.append((list(command), dict(kwargs["env"])))
        return SimpleNamespace(pid=4242)

    def record_direct_buyer(command, **kwargs):
        buyer_handoffs.append((list(command), dict(kwargs["env"])))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"definitive": True, "submitted_receipts": 0}),
            stderr="",
        )

    def record_json_buyer(command, *, cwd, env):
        del cwd
        buyer_handoffs.append((list(command), dict(env)))
        if "--expect-unsupported" in command:
            return {
                "status": "actionable_unsupported",
                "mutation_attempted": False,
                "phase": "client_construction",
            }
        return {"definitive": True, "submitted_receipts": 0, "status": "passed"}

    def record_ready(process, ready_file, *, install, credentials, **_kwargs):
        pin = runner._typescript_pin(install)
        ready_file.write_text("{}\n", encoding="utf-8")
        return {
            "pid": process.pid,
            "process_start_token": "123",
            "startup_proof_sha256": hashlib.sha256(credentials.startup_proof.encode()).hexdigest(),
            "seller_package": {
                "name": "@adcp/sdk",
                "language": "typescript",
                "package_role": install.role,
                "version": pin["version"],
                "integrity": pin["integrity"],
                "protocol": pin["protocol"],
            },
        }

    monkeypatch.setattr(runner.subprocess, "Popen", record_popen)
    monkeypatch.setattr(runner.subprocess, "run", record_direct_buyer)
    monkeypatch.setattr(runner, "_run_json", record_json_buyer)
    monkeypatch.setattr(runner, "_wait_core_seller_ready", record_ready)

    runtime = runner.PythonRuntime("wheel", tmp_path / "python")
    artifact = runner.PythonArtifact("wheel", tmp_path / "candidate.whl")
    node = tmp_path / "node"
    candidate = runner.TypeScriptInstall("candidate", tmp_path / "candidate-ts")
    previous = runner.TypeScriptInstall("previous", tmp_path / "previous-ts")
    for install in (candidate, previous):
        install.root.mkdir()

    route_outputs = [tmp_path / f"route-{index}" for index in range(4)]
    for output in route_outputs:
        output.mkdir()
    runner._run_python_control(
        runtime,
        artifact,
        admin_url="postgresql://owned/postgres",
        output=route_outputs[0],
        database="python-control",
        node=node,
        candidate_ts=candidate,
    )
    runner._run_ts_core_control(
        runtime,
        artifact,
        node=node,
        install=candidate,
        admin_url="postgresql://owned/postgres",
        output=route_outputs[1],
        database="typescript-control",
    )
    runner._run_candidate_python_previous_ts(
        runtime,
        artifact,
        node=node,
        install=previous,
        admin_url="postgresql://owned/postgres",
        output=route_outputs[2],
        database="positive-skew",
    )
    runner._run_previous_python_candidate_ts(
        runner.PreviousPythonInput(tmp_path / "previous-python", tmp_path / "previous.whl"),
        node=node,
        install=candidate,
        admin_url="postgresql://owned/postgres",
        output=route_outputs[3],
        database="negative-skew",
    )

    assert len(buyer_handoffs) == 6
    assert len(seller_handoffs) == 3
    allowed_runtime = {"PATH": "/runtime/bin", "LANG": "C.UTF-8", "TZ": "Etc/UTC"}
    excluded = {
        "AMBIENT_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "UNRELATED_PASSWORD",
        "DATABASE_URL",
        "ADCP_AUTH_TOKEN",
        *runner.TS_CORE_PRIVATE_ENV.values(),
    }
    for command, environment in buyer_handoffs:
        assert {key: environment[key] for key in allowed_runtime} == allowed_runtime
        assert excluded.isdisjoint(environment)
        assert set(environment) == {*allowed_runtime, "ADCP_INTEROP_BUYER_TOKEN"}
        assert environment["ADCP_INTEROP_BUYER_TOKEN"] not in command
    for command, environment in seller_handoffs:
        assert {key: environment[key] for key in allowed_runtime} == allowed_runtime
        assert excluded.isdisjoint(set(environment) - set(runner.TS_CORE_PRIVATE_ENV.values()))
        assert set(environment) == {*allowed_runtime, *runner.TS_CORE_PRIVATE_ENV.values()}
        for key in runner.TS_CORE_PRIVATE_ENV.values():
            assert environment[key] not in command

    assert [env["ADCP_INTEROP_BUYER_TOKEN"] for _, env in buyer_handoffs[:2]] == [
        "interop-token-a",
        "interop-token-a",
    ]
    assert [env["ADCP_INTEROP_BUYER_TOKEN"] for _, env in buyer_handoffs[2:4]] == [
        seller_handoffs[0][1][runner.TS_CORE_PRIVATE_ENV["token_a"]],
        seller_handoffs[0][1][runner.TS_CORE_PRIVATE_ENV["token_a"]],
    ]
    assert (
        buyer_handoffs[4][1]["ADCP_INTEROP_BUYER_TOKEN"]
        == seller_handoffs[1][1][runner.TS_CORE_PRIVATE_ENV["token_a"]]
    )
    assert (
        buyer_handoffs[5][1]["ADCP_INTEROP_BUYER_TOKEN"]
        == seller_handoffs[2][1][runner.TS_CORE_PRIVATE_ENV["token_a"]]
    )
    assert parent == original_parent


def test_core_seller_readiness_rejects_a_misrouted_seller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.sql = SimpleNamespace()
    fake_conninfo = types.ModuleType("psycopg.conninfo")
    fake_conninfo.conninfo_to_dict = lambda _value: {}
    fake_conninfo.make_conninfo = lambda **_value: ""
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.conninfo", fake_conninfo)
    runner_path = ROOT / "scripts/ci/reporting_interop/run_foundation_matrix.py"
    spec = importlib.util.spec_from_file_location("reporting_matrix_ready_test", runner_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)

    install = runner.TypeScriptInstall("lead", ROOT / "scripts/ci/reporting_interop/npm/rc44")
    pin = runner._typescript_pin(install)
    credentials = runner.CoreSellerCredentials("a" * 43, "b" * 43, "c" * 64)
    node = tmp_path / "node"
    node.write_text("fixture", encoding="utf-8")
    ready_file = tmp_path / "ready.json"
    process = SimpleNamespace(pid=4242, poll=lambda: None)
    monkeypatch.setattr(runner, "_linux_process_start_token", lambda _pid: "123")
    ready = {
        "kind": "reporting_interop_ts_core_server_ready",
        "pid": 4242,
        "port": 55_555,
        "process_start_token": "123",
        "startup_proof_sha256": hashlib.sha256(credentials.startup_proof.encode()).hexdigest(),
        "auth_binding_sha256": {
            "interop-account-a": hashlib.sha256(credentials.token_a.encode()).hexdigest(),
            "interop-account-b": hashlib.sha256(credentials.token_b.encode()).hexdigest(),
        },
        "node": {"executable": str(node.resolve()), "version": "v22.12.0"},
        "seller_package": {
            "name": "@adcp/sdk",
            "language": "typescript",
            "package_role": "candidate",
            "version": pin["version"],
            "integrity": pin["integrity"],
            "protocol": pin["protocol"],
        },
    }
    ready_file.write_text(json.dumps(ready), encoding="utf-8")
    with pytest.raises(runner.HarnessError, match="selected seller"):
        runner._wait_core_seller_ready(
            process,
            ready_file,
            node=node,
            install=install,
            credentials=credentials,
            port=55_555,
            timeout=0.1,
        )

    ready["seller_package"]["package_role"] = "lead"
    ready_file.write_text(json.dumps(ready), encoding="utf-8")
    observed = runner._wait_core_seller_ready(
        process,
        ready_file,
        node=node,
        install=install,
        credentials=credentials,
        port=55_555,
        timeout=0.1,
    )
    assert observed["seller_package"]["package_role"] == "lead"


def test_foundation_accounting_rejects_id_only_rc4_and_negative_skew_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.sql = SimpleNamespace()
    fake_conninfo = types.ModuleType("psycopg.conninfo")
    fake_conninfo.conninfo_to_dict = lambda _value: {}
    fake_conninfo.make_conninfo = lambda **_value: ""
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.conninfo", fake_conninfo)
    runner_path = ROOT / "scripts/ci/reporting_interop/run_foundation_matrix.py"
    spec = importlib.util.spec_from_file_location("reporting_matrix_accounting_test", runner_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)

    applicability = {
        "cells": [
            {
                "id": "q1",
                "contract_id": "Q1",
                "family": "candidate_quadrant",
                "blocking": True,
                "protocol_contract": {
                    "required": "3.2.0-rc.6",
                    "alignment": "exact",
                    "selected_artifacts": True,
                    "artifact_identity_sha256": "a" * 64,
                },
            },
            {
                "id": "s1",
                "contract_id": "S1",
                "family": "supported_skew",
                "blocking": True,
                "positive_requirement": {
                    "status": "selected_supported_pair",
                    "blocking": True,
                    "artifact_identity_sha256": "b" * 64,
                },
            },
            {
                "id": "latest",
                "family": "latest_canary",
                "blocking": False,
            },
        ]
    }
    base = {
        "execution_attempted": True,
        "required_cell_credit": True,
        "artifact_identity_sha256": "a" * 64,
        "seller_process_identity": {
            "pid": 101,
            "start_token": "10",
            "startup_proof_sha256": "e" * 64,
        },
        "seller_artifact_identity": {
            "language": "typescript",
            "package_role": "candidate",
            "runtime_identity_sha256": "c" * 64,
            "artifact_identity_sha256": "d" * 64,
        },
        "database_identity": {"name": "database-1", "cluster_identity": "cluster-1"},
        "cell_complete": True,
    }
    rows = runner._execution_accounting(
        applicability,
        [
            {
                **base,
                "id": "q1",
                "target_contract_id": "Q1",
                "observed_protocol": "3.2.0-rc.4",
                "polarity": "positive_semantic",
            },
            {
                **base,
                "id": "negative_skew_refusal__s1",
                "target_cell_id": "s1",
                "target_contract_id": "S1",
                "artifact_identity_sha256": "b" * 64,
                "polarity": "negative_refusal",
            },
        ],
    )
    by_id = {row["id"]: row for row in rows}
    assert by_id["q1"]["executed_with_required_credit"] is False
    assert by_id["s1"]["executed_with_required_credit"] is False
    assert by_id["latest"]["executed_with_required_credit"] is False
    assert (
        "observed_protocol_does_not_match_required" in by_id["q1"]["observed_controls"][0]["errors"]
    )
    assert (
        "negative_or_partial_skew_cannot_satisfy_positive_requirement"
        in by_id["s1"]["observed_controls"][0]["errors"]
    )


def test_foundation_accounting_requires_fresh_identity_and_positive_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.sql = SimpleNamespace()
    fake_conninfo = types.ModuleType("psycopg.conninfo")
    fake_conninfo.conninfo_to_dict = lambda _value: {}
    fake_conninfo.make_conninfo = lambda **_value: ""
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.conninfo", fake_conninfo)
    runner_path = ROOT / "scripts/ci/reporting_interop/run_foundation_matrix.py"
    spec = importlib.util.spec_from_file_location(
        "reporting_matrix_positive_accounting", runner_path
    )
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)

    applicability = {
        "cells": [
            {
                "id": "q1",
                "contract_id": "Q1",
                "family": "candidate_quadrant",
                "blocking": True,
                "server": {
                    "language": "typescript",
                    "package_role": "candidate",
                    "artifact_identity_sha256": "d" * 64,
                    "runtime_identity_sha256": "c" * 64,
                },
                "protocol_contract": {
                    "required": "3.2.0-rc.6",
                    "alignment": "exact",
                    "selected_artifacts": True,
                    "artifact_identity_sha256": "a" * 64,
                },
            }
        ]
    }
    observed = {
        "id": "q1",
        "target_contract_id": "Q1",
        "execution_attempted": True,
        "required_cell_credit": True,
        "artifact_identity_sha256": "a" * 64,
        "seller_process_identity": {
            "pid": 101,
            "start_token": "10",
            "startup_proof_sha256": "e" * 64,
        },
        "seller_artifact_identity": {
            "language": "typescript",
            "package_role": "candidate",
            "runtime_identity_sha256": "c" * 64,
            "artifact_identity_sha256": "d" * 64,
        },
        "database_identity": {"name": "database-1", "cluster_identity": "cluster-1"},
        "observed_protocol": "3.2.0-rc.6",
        "polarity": "positive_semantic",
        "cell_complete": True,
    }
    [row] = runner._execution_accounting(applicability, [observed])
    assert row["executed_with_required_credit"] is True
    assert row["completed_with_required_credit"] is True

    misrouted = {
        **observed,
        "seller_artifact_identity": {
            **observed["seller_artifact_identity"],
            "artifact_identity_sha256": "e" * 64,
        },
        # A legitimate cross-language buyer identity must not be compared to
        # the seller archive identity.
        "buyer_artifact_identity_sha256": "f" * 64,
    }
    [row] = runner._execution_accounting(applicability, [misrouted])
    assert row["executed_with_required_credit"] is False
    assert (
        "seller_archive_identity_does_not_match_cell_server"
        in row["observed_controls"][0]["errors"]
    )

    second_contract = {
        **applicability["cells"][0],
        "id": "q2",
        "contract_id": "Q2",
    }
    second_observed = {
        **observed,
        "id": "q2",
        "target_contract_id": "Q2",
    }
    duplicate_rows = runner._execution_accounting(
        {"cells": [applicability["cells"][0], second_contract]},
        [observed, second_observed],
    )
    assert all(row["executed_with_required_credit"] is False for row in duplicate_rows)
    assert all(
        "seller_process_identity_not_unique" in row["observed_controls"][0]["errors"]
        and "database_identity_not_unique" in row["observed_controls"][0]["errors"]
        for row in duplicate_rows
    )

    del observed["database_identity"]
    [row] = runner._execution_accounting(applicability, [observed])
    assert row["executed_with_required_credit"] is False
    assert "database_identity_missing" in row["observed_controls"][0]["errors"]


def test_controlled_server_routes_only_declared_storyboard_accounts() -> None:
    server = (ROOT / "scripts/ci/reporting_interop/ts_controlled_reporting_server.cjs").read_text(
        encoding="utf-8"
    )
    fixture = (ROOT / "scripts/ci/reporting_interop/ts_controlled_reporting_fixture.cjs").read_text(
        encoding="utf-8"
    )

    assert "function createAccountRouter(storyboardId)" in server
    assert "request?.account?.operator !== STORYBOARD_OPERATOR" in server
    assert "request?.account?.sandbox !== true" in server
    assert "consumerPrepareCount >= consumerAccounts.length" in server
    assert "corePrepareCount++ === 0" in server
    assert "--storyboard-id" in server
    assert "reporting_consumer_content_lab" in fixture
