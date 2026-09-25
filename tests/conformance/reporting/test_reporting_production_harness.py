"""Deliberate installed-child stalls retain phase evidence and drain ownership."""

import json
import os
import secrets
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from ._installed_progress import ProgressMonitor
from .test_reporting_notification_packaging import run_step

BOOT = """
import importlib.util, json, sys
from pathlib import Path
settings = json.load(sys.stdin)
spec = importlib.util.spec_from_file_location('progress_helper', settings['helper'])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
progress = module.InstalledProgress(settings['progress'])
"""


def settings(tmp_path):
    return {
        "helper": str(Path(__file__).with_name("_installed_progress.py")),
        "progress": str(tmp_path / "progress.json"),
    }


def test_installed_progress_preserves_json_protocol_and_real_pytest_phase_timings(tmp_path):
    case = tmp_path / "test_example.py"
    case.write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', ['private-parameter-canary'])\n"
        "def test_example(value):\n    assert value\n"
    )
    body = (
        BOOT
        + """
import contextlib, io, pytest
progress.start('collection')
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    code = pytest.main([str(Path.cwd() / 'test_example.py'), '-q', '-p', 'no:cacheprovider'],
                       plugins=[progress])
progress.start('typing')
progress.start('origins')
progress.start('complete')
progress.close()
print(json.dumps({'pytest_exit': int(code)}))
"""
    )
    value = settings(tmp_path)
    result = run_step(
        [sys.executable, "-I", "-c", body],
        label="phase_success",
        cwd=tmp_path,
        value=value,
        timeout=30,
        progress=ProgressMonitor(value["progress"], poll_seconds=0.05),
    )
    assert json.loads(result) == {"pytest_exit": 0}
    raw = (tmp_path / "progress.jsonl").read_text()
    assert "private-parameter-canary" not in raw
    events = [json.loads(line) for line in raw.splitlines()]
    assert {event["phase"] for event in events} >= {
        "identity",
        "collection",
        "setup",
        "call",
        "teardown",
        "typing",
        "origins",
        "complete",
    }
    assert [event["outcome"] for event in events if event["event"] == "report"] == [
        "passed",
        "passed",
        "passed",
    ]
    assert events[-1]["completed"] == 1


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux process ownership diagnostics")
@pytest.mark.parametrize("new_session", [False, True])
def test_installed_phase_deadline_reports_active_case_and_kills_descendant(
    tmp_path, monkeypatch, capsys, new_session
):
    monkeypatch.delenv("ADCP_PG_TEST_URL", raising=False)
    body = (
        BOOT
        + """
import subprocess, threading
child_code = (
    'import signal,time;'
    + ('signal.signal(signal.SIGTERM,signal.SIG_IGN);' if settings['new_session'] else '')
    + 'print("ready",flush=True);time.sleep(60)'
)
child = subprocess.Popen([sys.executable, '-c', child_code],
                         start_new_session=settings['new_session'], stdout=subprocess.PIPE)
assert child.stdout.readline() == b'ready\\n'
progress.start('call', nodeid='tests/test_stall.py::test_stall[private-parameter-canary]')
print('private-provider-canary', file=sys.stderr, flush=True)
threading.Event().wait()
"""
    )
    value = {**settings(tmp_path), "new_session": new_session}
    with pytest.raises(AssertionError, match="deadline_kind=phase phase=call"):
        run_step(
            [sys.executable, "-I", "-c", body],
            label="phase_stall",
            cwd=tmp_path,
            value=value,
            timeout=15,
            progress=ProgressMonitor(
                value["progress"], phase_seconds={"call": 0.2}, poll_seconds=0.02
            ),
        )
    evidence = json.loads((tmp_path / "progress.timeout.json").read_text())
    assert evidence["progress"]["phase"] == "call"
    assert evidence["progress"]["test"] == "test_stall"
    assert evidence["progress"]["last_progress_at"] <= evidence["captured_at"]
    assert len(evidence["processes"]) >= 2
    assert evidence["owned_live_pids"] == []
    assert evidence["exit"] < 0
    if new_session:
        assert "killed" in evidence["cleanup"]
    assert evidence["database"] == {"available": False, "reason": "not_configured"}
    assert "private-" not in json.dumps(evidence) + capsys.readouterr().out


def test_installed_aggregate_deadline_is_not_reset_by_continued_progress(tmp_path, monkeypatch):
    monkeypatch.delenv("ADCP_PG_TEST_URL", raising=False)
    value = settings(tmp_path)
    body = (
        BOOT
        + """
import time
while True:
    progress.start('call', nodeid='tests/test_busy.py::test_busy')
    time.sleep(0.02)
"""
    )
    with pytest.raises(AssertionError, match="deadline_kind=aggregate"):
        run_step(
            [sys.executable, "-I", "-c", body],
            label="aggregate_busy",
            cwd=tmp_path,
            value=value,
            timeout=3,
            progress=ProgressMonitor(value["progress"], poll_seconds=0.02),
        )
    evidence = json.loads((tmp_path / "progress.timeout.json").read_text())
    assert evidence["deadline"] == "aggregate"
    assert evidence["progress"]["sequence"] > 1
    assert not evidence["owned_live_pids"]


def test_installed_startup_deadline_is_diagnosed_without_sidecar(tmp_path, monkeypatch):
    monkeypatch.delenv("ADCP_PG_TEST_URL", raising=False)
    path = tmp_path / "progress.json"
    with pytest.raises(AssertionError, match="deadline_kind=phase phase=startup"):
        run_step(
            [sys.executable, "-I", "-c", "import threading;threading.Event().wait()"],
            label="startup_stall",
            cwd=tmp_path,
            timeout=15,
            progress=ProgressMonitor(path, phase_seconds={"startup": 0.2}, poll_seconds=0.02),
        )
    evidence = json.loads(path.with_suffix(".timeout.json").read_text())
    assert evidence["progress"]["phase"] == "startup"
    assert evidence["progress"]["last_progress_at"] <= evidence["captured_at"]
    assert not evidence["owned_live_pids"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper ownership")
@pytest.mark.parametrize("inherit_pipes", [False, True])
def test_installed_cleanup_owns_escaped_descendant_after_parent_exits(
    tmp_path, monkeypatch, inherit_pipes
):
    monkeypatch.delenv("ADCP_PG_TEST_URL", raising=False)
    value = {**settings(tmp_path), "inherit_pipes": inherit_pipes}
    body = (
        BOOT
        + """
import os, subprocess, time
code = (
    'import json,os,signal,time;from pathlib import Path;'
    'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
    'fields=Path("/proc/self/stat").read_text().rsplit(")",1)[1].split();'
    'Path("owned.json").write_text(json.dumps({"pid":os.getpid(),'
    '"start_ticks":int(fields[19])}));time.sleep(60)'
)
subprocess.Popen([sys.executable, '-I', '-c', code], start_new_session=True,
                 stdout=None if settings['inherit_pipes'] else subprocess.DEVNULL,
                 stderr=None if settings['inherit_pipes'] else subprocess.DEVNULL)
until = time.monotonic() + 5
while not Path('owned.json').exists():
    assert time.monotonic() < until
    time.sleep(0.01)
progress.start('call', nodeid='tests/test_orphan.py::test_orphan')
progress.close()
os._exit(0)
"""
    )
    # A different session must remain alive while the owned orphan is killed.
    unrelated = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time;time.sleep(60)"], start_new_session=True
    )
    owned = None
    try:
        with pytest.raises(AssertionError, match="deadline_kind=phase phase=call"):
            run_step(
                [sys.executable, "-I", "-c", body],
                label="parent_exited",
                cwd=tmp_path,
                value=value,
                timeout=15,
                progress=ProgressMonitor(
                    value["progress"], phase_seconds={"call": 0.2}, poll_seconds=0.02
                ),
            )
        evidence = json.loads((tmp_path / "progress.timeout.json").read_text())
        owned = json.loads((tmp_path / "owned.json").read_text())
        pid = owned["pid"]
        owned = next(entry for entry in evidence["processes"] if entry["pid"] == pid)
        assert owned["pgid"] == pid  # setsid did not escape the subreaper.
        assert evidence["ownership"] == "subreaper"
        assert evidence["exit"] == 0  # Direct parent had already exited successfully.
        assert evidence["cleanup_complete"]
        assert not evidence["owned_live_pids"]
        assert not Path(f"/proc/{pid}").exists()  # Reaped, not merely a zombie.
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)
        if owned is None and (tmp_path / "owned.json").exists():
            owned = json.loads((tmp_path / "owned.json").read_text())
        if owned is not None:
            path = Path(f"/proc/{owned['pid']}/stat")
            if path.exists():
                fields = path.read_text().rsplit(")", 1)[1].split()
                if int(fields[19]) == owned["start_ticks"] and fields[0] != "Z":
                    os.kill(owned["pid"], signal.SIGKILL)


def test_installed_cleanup_still_writes_evidence_when_pipe_draining_expires(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("ADCP_PG_TEST_URL", raising=False)
    path = tmp_path / "progress.json"
    monitor = ProgressMonitor(path, phase_seconds={"startup": 0.2}, poll_seconds=0.02)
    capture = monitor.capture

    def fail_draining(process):
        capture(process)

        def failed(*args, **kwargs):
            raise subprocess.TimeoutExpired(
                process.args, 5, output=b"private-output-canary", stderr=b"private-error-canary"
            )

        monkeypatch.setattr(process, "communicate", failed)

    monkeypatch.setattr(monitor, "capture", fail_draining)
    with pytest.raises(AssertionError, match="pipe_drain: deadline pid=.*cleanup_deadline"):
        run_step(
            [sys.executable, "-I", "-c", "import time;time.sleep(60)"],
            label="pipe_drain",
            cwd=tmp_path,
            timeout=15,
            progress=monitor,
        )
    evidence = json.loads(path.with_suffix(".timeout.json").read_text())
    assert evidence["progress"]["phase"] == "startup"
    assert not evidence["owned_live_pids"]
    assert not evidence["cleanup_complete"]
    assert evidence["database_cleanup"] == {"available": False, "reason": "not_configured"}
    assert "private-" not in json.dumps(evidence) + capsys.readouterr().out


def test_installed_phase_deadline_captures_database_wait_and_cleans_owned_session(tmp_path):
    psycopg = pytest.importorskip("psycopg")
    url = os.environ.get("ADCP_PG_TEST_URL")
    if not url:
        pytest.skip("ADCP_PG_TEST_URL supplies the isolated test database")
    key = secrets.randbits(63)
    value = {**settings(tmp_path), "lock_key": key, "sessions": 4}
    body = (
        BOOT
        + """
import os, psycopg, threading, time
connections = [psycopg.connect(os.environ['ADCP_PG_TEST_URL'], autocommit=True)
               for _ in range(settings['sessions'])]
for connection in connections:
    threading.Thread(target=connection.execute,
                     args=('SELECT pg_advisory_xact_lock(%s)', (settings['lock_key'],)),
                     daemon=True).start()
with psycopg.connect(os.environ['ADCP_PG_TEST_URL'], autocommit=True) as observer:
    for _ in range(500):
        rows = observer.execute(
            'SELECT wait_event_type,wait_event FROM pg_stat_activity WHERE pid=ANY(%s)',
            ([c.info.backend_pid for c in connections],)).fetchall()
        if rows == [('Lock', 'advisory')] * settings['sessions']:
            progress.start('call', nodeid='tests/test_wait.py::test_wait[private-canary]')
            break
        time.sleep(0.01)
    else:
        raise AssertionError('database wait barrier')
threading.Event().wait()
"""
    )
    with psycopg.connect(url, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (key,))
        with pytest.raises(AssertionError, match="deadline_kind=phase phase=call"):
            run_step(
                [sys.executable, "-I", "-c", body],
                label="database_stall",
                cwd=tmp_path,
                value=value,
                timeout=15,
                progress=ProgressMonitor(
                    value["progress"], phase_seconds={"call": 0.2}, poll_seconds=0.02
                ),
            )
        evidence = json.loads((tmp_path / "progress.timeout.json").read_text())
        assert evidence["database"]["available"]
        assert any(
            session[2:4] == ["Lock", "advisory"] and holder.info.backend_pid in session[4]
            for session in evidence["database"]["sessions"]
        )
        assert not evidence["owned_live_pids"]
        assert len(evidence["database"]["sessions"]) >= value["sessions"]
        assert evidence["database_cleanup"]["complete"]
        assert evidence["cleanup_complete"]
        assert evidence["remaining_database"] == {"available": True, "sessions": []}
        # The observer must leave the independent lock holder connected.
        assert holder.execute("SELECT 1").fetchone() == (1,)
        assert "private-canary" not in json.dumps(evidence)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper ownership")
def test_installed_cleanup_resumes_stopped_supervisor_and_reaps_descendants(tmp_path, monkeypatch):
    monkeypatch.delenv("ADCP_PG_TEST_URL", raising=False)
    helper = Path(__file__).with_name("_installed_progress.py")
    monitor = ProgressMonitor(
        tmp_path / "progress.json", phase_seconds={"call": 0.2}, poll_seconds=0.02
    )
    body = """
import importlib.util,json,os,signal,sys
settings=json.load(sys.stdin)
spec=importlib.util.spec_from_file_location('progress',settings['helper'])
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
progress=module.InstalledProgress(settings['progress'])
progress.start('call',nodeid='tests/test_stopped.py::test_stopped_supervisor')
progress.close()
os.kill(os.getppid(),signal.SIGSTOP)
os._exit(0)
"""
    try:
        with pytest.raises(AssertionError, match="stopped_supervisor: deadline pid=.*phase=call"):
            run_step(
                [sys.executable, "-I", "-c", body],
                label="stopped_supervisor",
                cwd=tmp_path,
                value={"helper": str(helper), "progress": str(monitor.path)},
                timeout=20,
                progress=monitor,
            )
        evidence = json.loads(monitor.path.with_suffix(".timeout.json").read_text())
        assert any(entry["state"] == "T" for entry in evidence["processes"])
        assert evidence["owned_live_pids"] == []
        assert evidence["exit"] == 0
        assert evidence["cleanup"].endswith("_resumed_supervisor")
        assert all(not Path(f"/proc/{entry['pid']}").exists() for entry in evidence["processes"])
    finally:
        # Bound even a regression's deliberately stopped, test-owned anchor.
        if hasattr(monitor, "process") and monitor.process.poll() is None:
            monitor.process.kill()
            monitor.process.wait(timeout=3)
