"""Bounded, data-free progress for the isolated installed production gate.

Stdout remains the child's final JSON protocol. An atomically replaced sidecar
identifies the active phase; an append-only journal retains timings even when
the aggregate deadline interrupts pytest before its summary is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

PHASE_SECONDS = {
    "startup": 120,
    "identity": 120,
    "collection": 120,
    "setup": 120,
    "call": 300,
    "teardown": 60,
    "pytest_finish": 120,
    "typing": 120,
    "origins": 120,
    "complete": 30,
}


class InstalledProgress:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.journal = self.path.with_suffix(".jsonl").open("x", buffering=1)
        self.sequence = 0
        self.completed = 0
        self.case = {}
        self.start("identity")

    def start(self, phase, *, nodeid=None):
        assert phase in PHASE_SECONDS
        if nodeid is not None:
            file, _, name = nodeid.partition("::")
            # Parameter IDs can contain fixture/provider data. Retain only
            # repository identifiers and an opaque identity for the full case.
            file, name = Path(file).name, name.split("[")[0].split("::")[-1]
            self.case = {
                "file": file if re.fullmatch(r"test_[a-zA-Z0-9_]+\.py", file) else "test",
                "test": name if re.fullmatch(r"test_[a-zA-Z0-9_]+", name) else "test",
                "case": hashlib.sha256(nodeid.encode()).hexdigest()[:16],
            }
        elif phase not in {"setup", "call", "teardown"}:
            self.case = {}
        self.sequence += 1
        self.state = {
            "pid": os.getpid(),
            "supervisor_pid": int(os.environ.get("ADCP_INSTALLED_SUPERVISOR_PID", "0")),
            "sequence": self.sequence,
            "phase": phase,
            "phase_started": time.monotonic(),
            "last_progress_at": time.time(),
            "completed": self.completed,
            **self.case,
        }
        self._write("phase")

    def _write(self, event, **fields):
        self.journal.write(json.dumps({"event": event, **self.state, **fields}) + "\n")
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state) + "\n")
        temporary.replace(self.path)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        self.start("setup", nodeid=item.nodeid)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_call(self, item):
        self.start("call", nodeid=item.nodeid)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_teardown(self, item, nextitem):
        self.start("teardown", nodeid=item.nodeid)

    def pytest_runtest_logreport(self, report):
        if report.when == "teardown":
            self.completed += 1
        self.state["completed"] = self.completed
        self.state["last_progress_at"] = time.time()
        self._write("report", outcome=report.outcome, duration=report.duration)

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionfinish(self, session, exitstatus):
        self.start("pytest_finish")

    def close(self):
        self.journal.close()


def process_tree(pid):
    """Linux process metadata only: never argv, environment, or file contents."""
    entries = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            number = int(path.parent.name)
            entries[number] = {
                "pid": number,
                "ppid": int(fields[1]),
                "pgid": int(fields[2]),
                "state": fields[0],
                "start_ticks": int(fields[19]),
                "cpu_ticks": int(fields[11]) + int(fields[12]),
                "rss_pages": int(fields[21]),
            }
        except (OSError, ValueError, IndexError):
            continue
    owned = {pid} | {number for number, entry in entries.items() if entry["pgid"] == pid}
    while True:
        children = {number for number, entry in entries.items() if entry["ppid"] in owned}
        if children.issubset(owned):
            break
        owned.update(children)
    return [entries[number] for number in sorted(owned) if number in entries]


class ProgressMonitor:
    def __init__(self, path, *, phase_seconds=None, poll_seconds=1, heartbeat_seconds=30):
        self.path = Path(path)
        self.phase_seconds = {**PHASE_SECONDS, **(phase_seconds or {})}
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.application = "adcp-installed-" + uuid.uuid4().hex
        self.latest = None
        self.deadline = "aggregate"
        self.before_cleanup = None
        self.supervised = sys.platform == "linux"
        self.owned = {}

    def command(self, command):
        if self.supervised:
            return [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("_installed_supervisor.py")),
                *command,
            ]
        return command

    def _read(self, pid):
        try:
            with self.path.open() as stream:
                raw = stream.read(8193)
            if len(raw) > 8192:
                return
            value = json.loads(raw)
            # The parent prints only validated fields, even for corrupt files.
            owner = value.get("supervisor_pid") if self.supervised else value["pid"]
            if owner != pid or value["phase"] not in PHASE_SECONDS:
                return
            result = {"pid": int(value["pid"]), "phase": value["phase"]}
            for key in ("sequence", "completed"):
                result[key] = int(value[key])
            for key in ("phase_started", "last_progress_at"):
                result[key] = float(value[key])
            for key, pattern in (
                ("file", r"test_[a-zA-Z0-9_]+\.py|test"),
                ("test", r"test_[a-zA-Z0-9_]+|test"),
                ("case", r"[0-9a-f]{16}"),
            ):
                if key in value and re.fullmatch(pattern, value[key]):
                    result[key] = value[key]
            self.latest = result
        except (OSError, ValueError, TypeError, KeyError):
            return

    def communicate(self, process, value, timeout):
        self.process = process
        started = time.monotonic()
        self.latest = {
            "pid": process.pid,
            "sequence": 0,
            "phase": "startup",
            "phase_started": started,
            "last_progress_at": time.time(),
            "completed": 0,
        }
        next_heartbeat = started
        while True:
            now = time.monotonic()
            self._read(process.pid)
            state = self.latest or {"phase": "startup", "phase_started": started}
            phase_end = state["phase_started"] + self.phase_seconds[state["phase"]]
            remaining = min(started + timeout, phase_end) - now
            if remaining <= 0:
                self.deadline = "phase" if phase_end < started + timeout else "aggregate"
                raise subprocess.TimeoutExpired(process.args, timeout)
            if now >= next_heartbeat:
                print(
                    json.dumps(
                        {
                            "installed_progress": state,
                            "elapsed_seconds": round(now - started, 3),
                            "phase_seconds": round(now - state["phase_started"], 3),
                        }
                    ),
                    flush=True,
                )
                next_heartbeat = now + self.heartbeat_seconds
            try:
                return process.communicate(value, timeout=min(self.poll_seconds, remaining))
            except subprocess.TimeoutExpired:
                # communicate() retains its buffered output between polls.
                value = None

    def _database(self, *, terminate=False):
        url = os.environ.get("ADCP_PG_TEST_URL")
        if not url:
            return {"available": False, "reason": "not_configured"}
        try:
            import psycopg

            with psycopg.connect(
                url,
                autocommit=True,
                connect_timeout=2,
                options="-cstatement_timeout=1000 -cdefault_transaction_read_only=on",
            ) as connection:
                if terminate:
                    return self._terminate_database(connection)
                rows = connection.execute(
                    "SELECT pid,state,wait_event_type,wait_event,pg_blocking_pids(pid),"
                    "extract(epoch FROM clock_timestamp()-query_start)::float8"
                    " FROM pg_stat_activity WHERE datname=current_database()"
                    " AND application_name=%s ORDER BY pid LIMIT 64",
                    (self.application,),
                ).fetchall()
            return {"available": True, "sessions": rows}
        except ImportError:
            return {"available": False, "reason": "driver_absent"}
        except Exception:
            return {"available": False, "reason": "snapshot_failed"}

    def _terminate_database(self, connection):
        # Sending a signal does not prove exit. Signal without a per-backend
        # wait, retain each result, and verify disappearance within one budget.
        # Every signal rechecks the unique tag, including if a PID was reused.
        requested = {}
        result = {"available": True, "requested": requested, "complete": False}
        until = time.monotonic() + 3
        try:
            while time.monotonic() < until:
                rows = connection.execute(
                    "SELECT pid FROM pg_stat_activity WHERE datname=current_database()"
                    " AND application_name=%s AND pid<>pg_backend_pid() ORDER BY pid",
                    (self.application,),
                ).fetchall()
                if not rows:
                    result["complete"] = True
                    break
                for (pid,) in rows:
                    if pid in requested or time.monotonic() >= until:
                        continue
                    row = connection.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                        " WHERE datname=current_database() AND application_name=%s"
                        " AND pid=%s AND pid<>pg_backend_pid()",
                        (self.application, pid),
                    ).fetchone()
                    requested[pid] = bool(row and row[0])
                time.sleep(0.01)
        except Exception:
            result["reason"] = "termination_failed"
        return result

    def _observe_processes(self):
        entries = process_tree(self.process.pid)
        for entry in entries:
            self.owned[entry["pid"]] = entry
        return entries

    def capture(self, process):
        self._read(process.pid)
        entries = self._observe_processes()
        self.before_cleanup = {
            "deadline": self.deadline,
            "progress": self.latest,
            "captured_at": time.time(),
            "processes": entries[:64],
            "process_count": len(entries),
            "ownership": "subreaper" if self.supervised else "process_group",
            "database": self._database(),
        }
        print(json.dumps({"installed_timeout": self.before_cleanup}), flush=True)

    def _survivors(self):
        survivors = []
        for entry in self.owned.values():
            try:
                fields = Path(f"/proc/{entry['pid']}/stat").read_text().rsplit(")", 1)[1].split()
                # Never signal a reused PID. The dedicated owner reaps zombies.
                if int(fields[19]) == entry["start_ticks"] and fields[0] != "Z":
                    survivors.append(entry["pid"])
            except (OSError, ValueError, IndexError):
                continue
        return survivors

    def signal_owned(self, sig):
        self._observe_processes()
        for pid in self._survivors():
            # Keep the subreaper alive across cleanup, including while newly
            # orphaned descendants are being reparented to it.
            if self.supervised and pid == self.process.pid:
                continue
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    def cleaned(self, process, cleanup):
        until = time.monotonic() + 5
        while self._survivors() and time.monotonic() < until:
            self.signal_owned(signal.SIGKILL)
            process.poll()
            time.sleep(0.01)
        # A stopped supervisor cannot reap exited children. Once no live
        # descendant remains, resume it briefly so it can reap any zombies.
        # Kill only the remaining anchor if it still cannot exit. Keep the
        # anchor and explicit residual evidence for uninterruptible descendants.
        self._observe_processes()
        if self.supervised and self._survivors() == [process.pid]:
            try:
                os.kill(process.pid, signal.SIGCONT)
                process.wait(timeout=1)
                cleanup += "_resumed_supervisor"
            except subprocess.TimeoutExpired:
                try:
                    os.kill(process.pid, signal.SIGKILL)
                    cleanup += "_killed_supervisor"
                    process.wait(timeout=1)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
            except ProcessLookupError:
                pass
        remaining = self._observe_processes()
        database_cleanup = self._database(terminate=True)
        remaining_database = self._database()
        live = self._survivors()
        complete = (
            not cleanup.startswith("cleanup_deadline")
            and process.poll() is not None
            and not live
            and (
                remaining_database.get("sessions") == []
                or remaining_database.get("reason") == "not_configured"
            )
        )
        result = {
            **self.before_cleanup,
            "cleanup": cleanup,
            "exit": process.returncode,
            "remaining_processes": remaining[:64],
            "remaining_process_count": len(remaining),
            "owned_live_pids": live,
            "database_cleanup": database_cleanup,
            "remaining_database": remaining_database,
            "cleanup_complete": complete,
        }
        self.path.with_suffix(".timeout.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"installed_cleanup": result}), flush=True)
        state = self.latest or {"phase": "startup"}
        return (
            f" deadline_kind={self.deadline} phase={state['phase']}"
            f" cleanup_complete={complete}" + ("" if complete else " cleanup_deadline")
        )
