"""Cold-process regressions: no schema state may leak in from another test."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.mark.parametrize("scenario", ["cold", "prewarm", "reverse"])
def test_freezegun_in_fresh_process(scenario: str) -> None:
    program = r"""
import datetime
import importlib
import sys

from freezegun import freeze_time
import freezegun

real_date, real_datetime = datetime.date, datetime.datetime
scenario = sys.argv[1]
from adcp.types import AcquireRightsRequest

if scenario == 'cold':
    # Starting the freezer resolves top-level lazy exports with datetime patched.
    # Merely importing those modules must not construct their adapters.
    with freeze_time('2026-01-01'):
        assert datetime.date.today().isoformat() == '2026-01-01'
    assert datetime.date is real_date
    assert datetime.datetime is real_datetime
else:
    from adcp.types.base import AdCPBaseModel
    from pydantic import ConfigDict
    class Parent(AdCPBaseModel):
        model_config = ConfigDict(defer_build=True)
    class Child(Parent):
        day: real_date
    class Grandchild(Child):
        pass
    assert not Grandchild.__pydantic_complete__
    modules = [
        'adcp.reporting.receipts.records',
        'adcp.reporting.projection.history',
        'adcp.reporting.ledger.notification_models',
        'adcp.types.legacy',
    ]
    if scenario == 'reverse':
        modules.reverse()
    for module in modules:
        importlib.import_module(module)
    freezegun.configure(extend_ignore_list=['adcp'])
    from adcp.testing import build_all_models
    build_all_models()
    assert AcquireRightsRequest.__pydantic_complete__
    assert Grandchild.__pydantic_complete__
    serializer = Grandchild.__pydantic_serializer__
    build_all_models()
    assert Grandchild.__pydantic_serializer__ is serializer
    with freeze_time('2026-01-01'):
        # First user validation and serialization of these models happen frozen.
        AcquireRightsRequest.model_json_schema()
        request = AcquireRightsRequest.model_validate({
            'rights_id': 'rights-1',
            'pricing_option_id': 'fixed',
            'buyer': {'domain': 'buyer.example'},
            'campaign': {'description': 'Campaign', 'uses': ['commercial'], 'start_date': '2026-04-05'},
            'revocation_webhook': {'url': 'https://buyer.example/revoke'},
            'idempotency_key': 'rights-acquire-0001',
        })
        assert request.model_dump(mode='json')['campaign']['start_date'] == '2026-04-05'
        assert '2026-04-05' in request.model_dump_json()
        from adcp._deferred_adapters import _FACTORIES
        for factory in _FACTORIES:
            assert factory().pydantic_complete

        model = Grandchild.model_validate({'day': '2026-04-05'})
        assert model.model_dump(mode='json') == {'day': '2026-04-05'}
        assert model.model_dump_json() == '{"day":"2026-04-05"}'
        from adcp import RefineProposalsRequest
        assert RefineProposalsRequest.__pydantic_complete__
        from adcp.types.legacy import LegacyCreativeAsset, LegacyFormatId
        from pydantic import ValidationError
        try:
            LegacyCreativeAsset(creative_id='c', name='c', format_id={'agent_url': 'https://creative.example', 'id': 'display'}, assets={'headline': {'content': 'Hello'}})
        except ValidationError:
            pass
        else:
            raise AssertionError('prewarming changed strict construction defaults')
        assert LegacyFormatId(agent_url='https://creative.example', id='display').agent_url == 'https://creative.example'
    assert datetime.date is real_date
    assert datetime.datetime is real_datetime
    # Cleanup also holds when user code raises inside a successfully started freeze.
    try:
        with freeze_time('2026-01-02'):
            raise RuntimeError('user failure')
    except RuntimeError:
        pass
    assert datetime.date is real_date
    assert datetime.datetime is real_datetime
print('ok', scenario)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, scenario],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"ok {scenario}" in result.stdout


def test_sdk_has_no_module_level_type_adapter_construction() -> None:
    spec = importlib.util.find_spec("adcp")
    assert spec and spec.origin
    root = Path(spec.origin).parent
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "TypeAdapter"
                ):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders


def test_build_all_models_returns_after_a_generated_poc_alias_import() -> None:
    # #1619: the deprecated alias registers the canonical module object under a
    # second sys.modules key, which used to make the lazy-export walk spin
    # forever. A subprocess with a timeout turns a regression into a failure
    # instead of a hung CI job.
    program = r"""
import sys
import warnings

warnings.simplefilter("ignore", DeprecationWarning)
import adcp.types.generated_poc.core.format_id as alias
import adcp.types.domains.core.format_id as canonical
assert alias is canonical
# A split name is served by its own shim module rather than the canonical object.
from adcp.types.generated_poc.brand import Brand  # noqa: F401
assert "adcp.types.generated_poc.brand" in sys.modules

from adcp.testing import build_all_models
build_all_models()
build_all_models()
print("ok")
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("build_all_models() did not return after a generated_poc alias import")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_lazy_export_walk_that_never_stabilizes_raises_naming_the_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adcp.testing import models

    created: list[str] = []

    def facade(index: int) -> ModuleType:
        name = f"adcp._test_endless_facade_{index}"
        module = ModuleType(name)

        def realize(attr: str) -> object:
            # Every realized facade imports a brand-new facade.
            child = facade(index + 1)
            monkeypatch.setitem(sys.modules, child.__name__, child)
            created.append(child.__name__)
            return None

        module.__getattr__ = realize  # type: ignore[method-assign]
        module.__dir__ = lambda: ["export"]  # type: ignore[method-assign]
        return module

    monkeypatch.setattr(models, "_MAX_REALIZE_PASSES", 5)
    first = facade(0)
    monkeypatch.setitem(sys.modules, first.__name__, first)

    with pytest.raises(RuntimeError, match="did not stabilize") as excinfo:
        models._realize_lazy_exports()
    assert created[-1] in str(excinfo.value)
