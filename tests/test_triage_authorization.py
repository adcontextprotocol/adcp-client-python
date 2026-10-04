"""Exercise the shipped authorization shell with a local GitHub API stub."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


@pytest.fixture
def authorize(tmp_path: Path):
    step = next(
        step
        for step in workflow("claude-issue-triage.yml")["jobs"]["fire-routine"]["steps"]
        if step.get("name") == "Authorize mutation-capable trigger"
    )
    stub = tmp_path / "gh"
    stub.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$API_CALLS"
[[ "$GH_TOKEN" == fixture-token ]]
[[ "$1" == api && "$2" == repos/fixture/repository/collaborators/maintainer/permission ]]
[[ "$3" == --jq ]]
if [[ "$API_STATUS" != 200 ]]; then
  echo "gh: API failure (HTTP $API_STATUS)" >&2
  # Even plausible stdout from a failed lookup must not authorize the caller.
  echo admin
  exit 1
fi
printf '%s' "$API_RESPONSE" | jq -r "$4"
"""
    )
    stub.chmod(0o755)
    calls = tmp_path / "calls"

    def run(
        response: Any, *, status: int = 200, commenter: str = "maintainer"
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        result = subprocess.run(
            ["bash", "-c", step["run"]],
            env={
                **os.environ,
                "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
                "GH_TOKEN": "fixture-token",
                "REPO": "fixture/repository",
                "COMMENTER": commenter,
                "API_CALLS": str(calls),
                "API_RESPONSE": json.dumps(response),
                "API_STATUS": str(status),
                # An event's claimed role cannot rescue a failed API lookup.
                "AUTHOR_ASSOCIATION": "OWNER",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        return result, calls.read_text().splitlines() if calls.exists() else []

    return run


@pytest.mark.parametrize(
    ("permission", "role"),
    [("write", "write"), ("write", "maintain"), ("admin", "admin"), ("write", "custom")],
)
def test_current_write_permission_authorizes(authorize, permission: str, role: str) -> None:
    # The documented base permission works even without user.permissions.
    result, calls = authorize({"permission": permission, "role_name": role})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Authorized @maintainer" in result.stdout
    assert len(calls) == 1


@pytest.mark.parametrize("permission", ["read", "triage", "none", None, True, "", "ADMIN"])
def test_non_write_or_malformed_permission_denies(authorize, permission: Any) -> None:
    result, calls = authorize({"permission": permission, "role_name": "admin"})
    assert result.returncode != 0
    assert "Refusing mutation-capable triage" in result.stdout
    assert "Authorized @" not in result.stdout
    assert len(calls) == 1


def test_missing_permission_denies_even_with_claimed_push_access(authorize) -> None:
    result, _ = authorize({"role_name": "admin", "user": {"permissions": {"push": True}}})
    assert result.returncode != 0
    assert "Refusing mutation-capable triage" in result.stdout


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_api_failure_never_authorizes(authorize, status: int) -> None:
    result, calls = authorize({"permission": "admin"}, status=status)
    assert result.returncode != 0
    assert "Permission lookup for @maintainer failed" in result.stdout
    assert "Authorized @" not in result.stdout
    assert len(calls) == 1


def test_empty_commenter_is_denied_before_lookup(authorize) -> None:
    result, calls = authorize({"permission": "admin"}, commenter="")
    assert result.returncode != 0
    assert "Cannot authorize an empty commenter" in result.stdout
    assert calls == []


def test_triage_authorization_and_reactions_use_job_token() -> None:
    config = workflow("claude-issue-triage.yml")
    steps = config["jobs"]["fire-routine"]["steps"]
    gate = next(step for step in steps if step.get("name") == "Authorize mutation-capable trigger")
    assert gate["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "github.event_name == 'repository_dispatch'" in gate["if"]
    assert "github.event_name == 'issue_comment' && github.event.issue.pull_request" in gate["if"]
    fire = next(step for step in steps if step.get("id") == "fire")
    assert steps.index(gate) < steps.index(fire)
    assert "if" not in fire  # Default success() prevents firing after failed authorization.
    reactions = [step for step in steps if step.get("name", "").startswith("React ")]
    assert len(reactions) == 2
    assert all(step["with"]["token"] == "${{ github.token }}" for step in reactions)
    assert config["permissions"] == {"contents": "read", "issues": "write"}
    assert "TRIAGE_DISPATCH_PAT" not in json.dumps(config)


def test_dispatch_uses_repository_scoped_app_and_job_reaction_token() -> None:
    config = workflow("slash-command-dispatch.yml")
    job = config["jobs"]["dispatch"]
    mint, dispatch = job["steps"]
    assert mint["id"] == "app-token"
    assert mint["with"] == {
        "app-id": "${{ secrets.IPR_APP_ID }}",
        "private-key": "${{ secrets.IPR_APP_PRIVATE_KEY }}",
        "owner": "${{ github.repository_owner }}",
        "repositories": "adcp-client-python",
        "permission-contents": "write",
        "permission-pull-requests": "read",
    }
    assert dispatch["with"]["token"] == "${{ steps.app-token.outputs.token }}"
    assert dispatch["with"]["reaction-token"] == "${{ github.token }}"
    assert dispatch["with"]["permission"] == "write"
    assert dispatch["with"]["issue-type"] == "both"
    assert config["permissions"] == {"issues": "write"}
    assert "startsWith(github.event.comment.body, '/triage')" in job["if"]
    assert "TRIAGE_DISPATCH_PAT" not in json.dumps(config)
