"""Skip SDK lanes only for a complete, verified documentation-only PR diff."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

DOCUMENTATION_FILES = {"AGENTS.md", "CHANGELOG.md", "CLAUDE.md", "CONTRIBUTING.md", "LADON.md"}
SHA = re.compile(r"[0-9a-f]{40}")


def requires_sdk_tests(paths: list[str]) -> bool:
    # README, LICENSE, migration guides, and unknown paths can be distribution
    # inputs. Keep the exemption narrow; renames must include their old paths.
    return not paths or any(
        path not in DOCUMENTATION_FILES and not (path.startswith("docs/") and path.endswith(".md"))
        for path in paths
    )


def select_scope(event_name: str, event: dict, *, cwd: Path | None = None) -> bool:
    if event_name != "pull_request":
        return True
    try:
        base = event["pull_request"]["base"]["sha"]
        head = event["pull_request"]["head"]["sha"]
        if not isinstance(base, str) or not isinstance(head, str):
            return True
        if not SHA.fullmatch(base) or not SHA.fullmatch(head):
            return True
        result = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "-z", base, head],
            cwd=cwd,
            capture_output=True,
            check=True,
            timeout=30,
        )
        paths = [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]
        return requires_sdk_tests(paths)
    except (KeyError, TypeError, OSError, subprocess.SubprocessError):
        return True  # Missing history or metadata must never suppress tests.


def main() -> None:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        run_tests = select_scope(os.environ["GITHUB_EVENT_NAME"], event)
    except (KeyError, OSError, ValueError):
        run_tests = True
    value = str(run_tests).lower()
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"run_tests={value}\n")
    print("Full SDK matrix required" if run_tests else "Verified documentation-only PR")


if __name__ == "__main__":
    main()
