"""Keep Linux installed-child descendants owned until every process is reaped.

This runs in a separate process, never making the concurrent pytest parent a
subreaper. Orphans (including helpers that call setsid) stay below this owner.
The owner keeps the output pipes open until its entire subtree has exited.
"""

import ctypes
import os
import signal
import subprocess
import sys


def main():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, ctypes.c_ulong(1), 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        return 125
    # The parent signals the group, then individually kills stubborn children.
    # Keep the reparenting anchor alive until those children have been reaped.
    signal.signal(signal.SIGTERM, lambda *_: None)
    child = subprocess.Popen(
        sys.argv[1:],
        env={**os.environ, "ADCP_INSTALLED_SUPERVISOR_PID": str(os.getpid())},
    )
    while True:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            break
        if pid == child.pid:
            child.returncode = os.waitstatus_to_exitcode(status)
    code = child.returncode
    if code < 0:
        if -code != signal.SIGKILL:
            signal.signal(-code, signal.SIG_DFL)
        os.kill(os.getpid(), -code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
