"""Cached schemas must not share mutable reference-resolution state (#1434)."""

from __future__ import annotations

import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any

import pytest

from adcp.validation import schema_loader


@pytest.fixture(params=["named", "task", "modular_task"])
def validator_case(request: pytest.FixtureRequest) -> tuple[Any, dict[str, Any]]:
    if request.param == "named":
        return (
            lambda: schema_loader.get_named_validator(
                "core/targeting-overlay-support.json", version="3.2"
            ),
            {
                "geo_countries": True,
                "geo_metros": {"systems": ["nielsen_dma"]},
                "geo_postal_areas": {"US": ["zip"]},
                "frequency_cap": True,
            },
        )
    if request.param == "modular_task":
        return (
            lambda: schema_loader.get_validator("get_products", "submitted", version="3.2"),
            {"status": "submitted", "task_id": "task_1", "context": {"campaign": "sports"}},
        )
    return (
        lambda: schema_loader.get_validator("get_products", "request", version="3.2"),
        {"buying_mode": "brief", "brief": "Sports campaign"},
    )


def test_cached_lookups_return_independent_resolvers_without_schema_io(
    validator_case: tuple[Any, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    get_validator, payload = validator_case
    first = get_validator()
    assert first is not None

    def unexpected_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("cached validator lookup reloaded schemas")

    monkeypatch.setattr(Path, "read_text", unexpected_read)

    def unexpected_warning_context(*args: Any, **kwargs: Any) -> None:
        pytest.fail("cached validator lookup changed process warning filters")

    monkeypatch.setattr(
        schema_loader, "warnings", SimpleNamespace(catch_warnings=unexpected_warning_context)
    )
    second = get_validator()
    assert second is not None
    assert first is not second
    assert first._ref_resolver is not second._ref_resolver
    assert first._ref_resolver.store is not second._ref_resolver.store
    assert not list(first.iter_errors(payload))
    assert not list(second.iter_errors(payload))
    assert list(second.iter_errors({"geo_countries": False}))


def test_concurrent_validation_uses_independent_resolvers(
    validator_case: tuple[Any, dict[str, Any]],
) -> None:
    get_validator, payload = validator_case
    workers = 8
    start = Barrier(workers)

    def validate() -> None:
        validator = get_validator()
        assert validator is not None
        start.wait(timeout=30)
        for _ in range(150):
            assert not list(validator.iter_errors(payload))
            validator = get_validator()
            assert validator is not None

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(validate) for _ in range(workers)]
            for future in futures:
                future.result(timeout=60)
