"""A stuck child is a failing bounded contract, never successful evidence."""

import asyncio
import sys

import pytest

from ._reliable_support import ServiceProcess


async def test_parent_deadline_reports_exact_child_and_only_kills_its_owned_process():
    async def child():
        return await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                "-c",
                'import sys; print(\'{"point":"blocked"}\', flush=True); sys.stdin.read()',
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            10,
        )

    blocked = ServiceProcess(await child(), "deadline-probe")
    independent = ServiceProcess(await child(), "independent-probe")
    try:
        await blocked.event("blocked")
        await independent.event("blocked")
        with pytest.raises(
            AssertionError, match=r"role=deadline-probe .*deadline:never .*last_point=blocked"
        ):
            await blocked.event("never", timeout_seconds=0)
        await blocked.kill()
        assert blocked.process.returncode is not None
        assert independent.process.returncode is None
    finally:
        await blocked.kill()
        await independent.kill()
