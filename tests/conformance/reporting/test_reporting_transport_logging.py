"""Deterministic overlapping transport protection across tasks and threads."""

from __future__ import annotations

import ast
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import httpcore
import httpx
import pytest

from adcp.reporting.outbox._transport_logging import (
    _LOGGER_NAMES,
    protected_transport_logs,
)


def logger_filters():
    return {name: tuple(logging.getLogger(name).filters) for name in _LOGGER_NAMES}


def test_installed_transport_loggers_are_all_protected():
    discovered = set()
    for module in (httpcore, httpx):
        for path in Path(module.__file__).parent.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "logging"
                    and node.func.attr == "getLogger"
                ):
                    continue
                assert len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                name = node.args[0].value
                assert isinstance(name, str) and name.startswith(("httpx", "httpcore"))
                discovered.add(name)
    assert discovered and discovered <= set(_LOGGER_NAMES)


@pytest.mark.parametrize("logger_name", _LOGGER_NAMES)
async def test_overlapping_tasks_keep_secrets_protected_after_first_exit(caplog, logger_name):
    caplog.set_level(logging.DEBUG)
    logger = logging.getLogger(logger_name)
    before = logger_filters()
    first_entered, second_entered = asyncio.Event(), asyncio.Event()
    first_exited, normal_logged = asyncio.Event(), asyncio.Event()

    async def first():
        with protected_transport_logs():
            first_entered.set()
            await second_entered.wait()
        first_exited.set()
        logger.info("first-task-normal-record")

    async def second():
        await first_entered.wait()
        with protected_transport_logs():
            second_entered.set()
            await first_exited.wait()
            logger.warning("OVERLAPPING_TASK_SECRET")
            logging.getLogger("adcp.test.application").info("protected-task-application-record")
            await normal_logged.wait()

    async def application():
        await first_exited.wait()
        logger.info("concurrent-task-normal-record")
        normal_logged.set()

    await asyncio.wait_for(asyncio.gather(first(), second(), application()), timeout=5)
    logger.info("after-tasks-normal-record")
    assert "OVERLAPPING_TASK_SECRET" not in caplog.text
    for message in (
        "first-task-normal-record",
        "concurrent-task-normal-record",
        "protected-task-application-record",
        "after-tasks-normal-record",
    ):
        assert message in caplog.text
    assert logger_filters() == before


@pytest.mark.parametrize("logger_name", _LOGGER_NAMES)
def test_overlapping_threads_keep_secrets_protected_after_first_exit(caplog, logger_name):
    caplog.set_level(logging.DEBUG)
    logger = logging.getLogger(logger_name)
    before = logger_filters()
    first_entered, second_entered = Event(), Event()
    first_exited, protected_logged, normal_logged = Event(), Event(), Event()

    def first():
        with protected_transport_logs():
            first_entered.set()
            assert second_entered.wait(5), "second thread entry deadline"
        first_exited.set()
        logger.info("first-thread-normal-record")

    def second():
        assert first_entered.wait(5), "first thread entry deadline"
        with protected_transport_logs():
            second_entered.set()
            assert first_exited.wait(5), "first thread exit deadline"
            logger.warning("OVERLAPPING_THREAD_SECRET")
            logging.getLogger("adcp.test.application").info("protected-thread-application-record")
            protected_logged.set()
            assert normal_logged.wait(5), "application record deadline"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(first), executor.submit(second)]
        try:
            assert protected_logged.wait(5), "protected record deadline"
            logger.info("concurrent-thread-normal-record")
        finally:
            normal_logged.set()
        for future in futures:
            future.result(timeout=5)
    logger.info("after-threads-normal-record")
    assert "OVERLAPPING_THREAD_SECRET" not in caplog.text
    for message in (
        "first-thread-normal-record",
        "concurrent-thread-normal-record",
        "protected-thread-application-record",
        "after-threads-normal-record",
    ):
        assert message in caplog.text
    assert logger_filters() == before


@pytest.mark.parametrize("exception_exit", [False, True])
def test_nested_contexts_restore_filters_and_context_after_exit(caplog, exception_exit):
    caplog.set_level(logging.DEBUG)
    before = logger_filters()

    class ContextExitError(Exception):
        pass

    try:
        with protected_transport_logs():
            try:
                with protected_transport_logs():
                    for name in _LOGGER_NAMES:
                        logging.getLogger(name).warning("NESTED_CONTEXT_SECRET")
                    if exception_exit:
                        raise ContextExitError
            except ContextExitError:
                pass
            for name in _LOGGER_NAMES:
                logging.getLogger(name).warning("OUTER_CONTEXT_SECRET")
            if exception_exit:
                raise ContextExitError
    except ContextExitError:
        pass

    for name in _LOGGER_NAMES:
        logging.getLogger(name).info("restored-normal-record:%s", name)
    assert "NESTED_CONTEXT_SECRET" not in caplog.text
    assert "OUTER_CONTEXT_SECRET" not in caplog.text
    assert all(f"restored-normal-record:{name}" in caplog.text for name in _LOGGER_NAMES)
    assert logger_filters() == before


async def test_cancelled_task_restores_transport_filters(caplog):
    caplog.set_level(logging.DEBUG)
    before = logger_filters()
    entered = asyncio.Event()

    async def protected():
        with protected_transport_logs():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(protected())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    for name in _LOGGER_NAMES:
        logging.getLogger(name).info("after-cancellation:%s", name)
    assert all(f"after-cancellation:{name}" in caplog.text for name in _LOGGER_NAMES)
    assert logger_filters() == before
