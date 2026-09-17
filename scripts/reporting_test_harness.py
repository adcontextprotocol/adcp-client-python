"""Run a reporting pytest gate in an audited private temporary directory.

Usage: python scripts/reporting_test_harness.py pytest tests/... -v -s -ra
The child inherits the normal process umask. Failed diagnostics remain under the
printed root until the runner/workspace is removed; no live artifact is deleted.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("supply the pytest executable and gate arguments")
    root = Path(tempfile.mkdtemp(prefix="adcp-reporting-gate-")).resolve()
    root.chmod(0o700)
    parent = root / "suite"
    parent.mkdir(mode=0o700)
    ancestors = []
    for path in (parent, root, *root.parents):
        metadata = path.stat()
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & 0o022 and not mode & stat.S_ISVTX:
            raise SystemExit(f"unsafe reporting harness ancestor: {path}")
        ancestors.append({"path": str(path), "mode": oct(mode), "uid": metadata.st_uid})
    mask = os.umask(0)
    os.umask(mask)
    command = [*sys.argv[1:], "--basetemp", str(parent / "run")]
    print(
        json.dumps(
            {
                "reporting_harness": str(root),
                "umask": oct(mask),
                "ancestors": ancestors,
                "command": command,
            }
        ),
        flush=True,
    )
    started = time.monotonic()
    result = subprocess.run(command, check=False)
    print(
        json.dumps(
            {
                "reporting_harness": str(root),
                "exit_status": result.returncode,
                "runtime_seconds": round(time.monotonic() - started, 3),
            }
        ),
        flush=True,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
