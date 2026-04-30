"""Cross-tenant security boundary regression suite for
:class:`adcp.decisioning.task_registry.InMemoryTaskRegistry`.

The wire ``tasks/get`` path passes the authenticated principal's
account_id as ``expected_account_id``. The registry MUST return None
on mismatch — returning the raw record enables principal-enumeration
via task_id probing (an attacker with one valid task_id can confirm
its existence regardless of which account they're authenticated as).

This is a separate file (vs. ``test_decisioning_task_registry.py``)
because the security boundary deserves explicit, prominently-named
tests. If a future implementer regresses the cross-tenant check, the
test name on the failure should be unambiguous about what broke.

Round-3 dispatch design D7: "cross-tenant ``get`` returns None."
Emma TS-side review #11 (Round 4): same regression caught on the JS
port; mirror the test surface here.
"""

from __future__ import annotations

import pytest

from adcp.decisioning.task_registry import InMemoryTaskRegistry


@pytest.mark.asyncio
async def test_cross_tenant_get_on_submitted_task_returns_none() -> None:
    """Account A creates a task; account B probes it. B gets None,
    NOT A's task data."""
    reg = InMemoryTaskRegistry()
    tid_a = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    cross = await reg.get(tid_a, expected_account_id="acct_b")
    assert cross is None, (
        "Cross-tenant probe must return None; returning A's record to B "
        "leaks task existence and enables principal-enumeration"
    )


@pytest.mark.asyncio
async def test_cross_tenant_get_on_working_task_returns_none() -> None:
    """Same regression after the task has been touched by
    update_progress (state=working)."""
    reg = InMemoryTaskRegistry()
    tid_a = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    await reg.update_progress(tid_a, {"step": 1})
    cross = await reg.get(tid_a, expected_account_id="acct_b")
    assert cross is None


@pytest.mark.asyncio
async def test_cross_tenant_get_on_completed_task_returns_none() -> None:
    """After the task is completed, the cross-tenant check still
    holds — the result payload is just as sensitive (probably more
    so) than the existence signal."""
    reg = InMemoryTaskRegistry()
    tid_a = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    await reg.complete(tid_a, {"media_buy_id": "mb_1"})
    cross = await reg.get(tid_a, expected_account_id="acct_b")
    assert cross is None, (
        "Completed-task cross-tenant probe must return None; the result "
        "payload is the very thing the attacker wants to read"
    )


@pytest.mark.asyncio
async def test_cross_tenant_get_on_failed_task_returns_none() -> None:
    """Failure-state probe is also blocked — the error payload may
    reveal seller-side validation rules or business logic that
    shouldn't leak across tenants."""
    reg = InMemoryTaskRegistry()
    tid_a = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    await reg.fail(
        tid_a,
        {
            "code": "POLICY_VIOLATION",
            "message": "buyer fails fraud heuristic 3.2",
            "recovery": "terminal",
        },
    )
    cross = await reg.get(tid_a, expected_account_id="acct_b")
    assert cross is None


@pytest.mark.asyncio
async def test_owner_can_read_their_own_task_after_state_transitions() -> None:
    """Sanity: the cross-tenant block doesn't break the legitimate
    same-tenant read path. Account A reads its own task at every
    state."""
    reg = InMemoryTaskRegistry()
    tid_a = await reg.issue(account_id="acct_a", task_type="create_media_buy")

    # submitted
    rec = await reg.get(tid_a, expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "submitted"

    # working
    await reg.update_progress(tid_a, {"step": 1})
    rec = await reg.get(tid_a, expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "working"

    # completed
    await reg.complete(tid_a, {"media_buy_id": "mb_1"})
    rec = await reg.get(tid_a, expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "completed"


@pytest.mark.asyncio
async def test_cross_tenant_probe_with_unknown_id_also_returns_none() -> None:
    """Hostile-probe variant: attacker guesses a task_id that doesn't
    exist at all. Must return None just like the existence-check
    case — distinguishing 'no such task' from 'task exists but wrong
    tenant' would itself be a side-channel."""
    reg = InMemoryTaskRegistry()
    cross = await reg.get("task_definitely_not_real", expected_account_id="acct_b")
    assert cross is None


@pytest.mark.asyncio
async def test_cross_tenant_probe_does_not_match_on_substring() -> None:
    """Edge case: account A is "acct" and account B is "acct_b". A
    naive prefix or substring check would let A see B's tasks. The
    registry must use exact equality."""
    reg = InMemoryTaskRegistry()
    tid_b = await reg.issue(account_id="acct_b", task_type="create_media_buy")
    # "acct" is a prefix of "acct_b" — exact-match check rejects.
    cross = await reg.get(tid_b, expected_account_id="acct")
    assert cross is None


@pytest.mark.asyncio
async def test_cross_tenant_probe_with_empty_string_account_returns_none() -> None:
    """Empty-string account_id is not a valid principal; must be
    treated as a mismatch rather than as "no scoping"."""
    reg = InMemoryTaskRegistry()
    tid_a = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    cross = await reg.get(tid_a, expected_account_id="")
    assert cross is None
