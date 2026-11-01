"""A minimal in-process runner for AdCP storyboard YAML (test helper).

It executes the checks the reporting-subscriptions storyboard uses
(``response_schema``, ``field_value``, ``field_present``, ``field_absent``,
``field_contains``, ``field_equals_context``, ``error_code``), with
``context_outputs`` and ``$generate:uuid_v4#name``. The authoritative runner
is the TypeScript ``adcp storyboard run``; this one lets Python run the same
file in process, so the reference seller is checked on every test run.
"""

from __future__ import annotations

import copy
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_MISSING = object()
_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+|\*)\]")

#: ``(task, arguments) -> (structured response, adcp_error or None)``
Call = Callable[[str, dict[str, Any]], Awaitable[tuple[dict[str, Any], dict[str, Any] | None]]]


@dataclass
class ValidationResult:
    check: str
    passed: bool
    path: str | None
    message: str


@dataclass
class StepResult:
    step_id: str
    passed: bool
    validations: list[ValidationResult] = field(default_factory=list)


@dataclass
class StoryboardResult:
    steps: list[StepResult]

    @property
    def passed(self) -> bool:
        return all(step.passed for step in self.steps)

    @property
    def failed_steps(self) -> list[str]:
        return [step.step_id for step in self.steps if not step.passed]


def load_storyboard(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text())
    assert isinstance(document, dict)
    return document


def resolve_path(document: Any, path: str) -> Any:
    """``a.b[0].c`` or ``a[*].b``; a ``[*]`` returns the list of matches."""
    values = [document]
    fanned = False
    for name, index in _TOKEN.findall(path):
        next_values = []
        for value in values:
            if name:
                if isinstance(value, Mapping) and name in value:
                    next_values.append(value[name])
            elif index == "*":
                fanned = True
                if isinstance(value, list):
                    next_values.extend(value)
            elif isinstance(value, list) and int(index) < len(value):
                next_values.append(value[int(index)])
        values = next_values
    if fanned:
        return values
    return values[0] if values else _MISSING


def _generate(value: Any, generated: dict[str, str]) -> Any:
    if isinstance(value, str) and value.startswith("$generate:uuid_v4"):
        name = value.partition("#")[2] or str(uuid.uuid4())
        return generated.setdefault(name, str(uuid.uuid4()))
    if isinstance(value, Mapping):
        return {key: _generate(item, generated) for key, item in value.items()}
    if isinstance(value, list):
        return [_generate(item, generated) for item in value]
    return value


def _error_code(structured: Mapping[str, Any], error: Mapping[str, Any] | None) -> str | None:
    if error is not None:
        return str(error.get("code"))
    errors = resolve_path(structured, "result.errors")
    if isinstance(errors, list) and errors:
        return str(errors[0].get("code"))
    errors = structured.get("errors")
    if isinstance(errors, list) and errors:
        return str(errors[0].get("code"))
    return None


def _validate(
    check: Mapping[str, Any],
    step: Mapping[str, Any],
    structured: dict[str, Any],
    error: dict[str, Any] | None,
    context: dict[str, Any],
    schema: Callable[[str], Any] | None,
) -> ValidationResult:
    kind = str(check["check"])
    path = check.get("path")
    actual = resolve_path(structured, path) if path else _MISSING
    if kind == "response_schema":
        ref = step.get("response_schema_ref")
        if not ref or schema is None:
            return ValidationResult(kind, True, None, "no response schema")
        validator = schema(ref)
        if validator is None:
            return ValidationResult(kind, False, None, f"no validator for {ref}")
        problems = [item.message for item in validator.iter_errors(structured)]
        return ValidationResult(kind, not problems, None, "; ".join(problems[:3]))
    if kind == "field_value":
        ok = actual is not _MISSING and actual == check["value"]
        return ValidationResult(kind, ok, path, f"{actual!r} != {check['value']!r}")
    if kind == "field_present":
        return ValidationResult(kind, actual is not _MISSING, path, "absent")
    if kind == "field_absent":
        return ValidationResult(kind, actual is _MISSING, path, f"present: {actual!r}")
    if kind == "field_contains":
        ok = isinstance(actual, list) and check["value"] in actual
        return ValidationResult(kind, ok, path, f"{check['value']!r} not in {actual!r}")
    if kind == "field_equals_context":
        expected = context.get(check["context_key"], _MISSING)
        ok = actual is not _MISSING and actual == expected
        return ValidationResult(kind, ok, path, f"{actual!r} != context {expected!r}")
    if kind == "error_code":
        code = _error_code(structured, error)
        ok = code in set(check.get("allowed_values") or [check.get("value")])
        return ValidationResult(kind, ok, None, f"error code {code!r}")
    return ValidationResult(kind, False, path, f"unsupported check {kind!r}")


async def run_storyboard(
    call: Call,
    storyboard: Mapping[str, Any],
    *,
    schema: Callable[[str], Any] | None = None,
    adcp_version: str | None = None,
) -> StoryboardResult:
    """Run every phase and step in order against ``call``.

    ``adcp_version`` is sent on every request, as the TypeScript runner sends
    the negotiated release.
    """
    generated: dict[str, str] = {}
    context: dict[str, Any] = {}
    results: list[StepResult] = []
    for phase in storyboard["phases"]:
        for step in phase["steps"]:
            payload = _generate(copy.deepcopy(step.get("sample_request") or {}), generated)
            if adcp_version is not None:
                payload.setdefault("adcp_version", adcp_version)
            structured, error = await call(step["task"], payload)
            validations = [
                _validate(check, step, structured, error, context, schema)
                for check in step.get("validations") or ()
            ]
            if error is not None and not step.get("expect_error"):
                validations.append(
                    ValidationResult("transport", False, None, f"unexpected error {error}")
                )
            for output in step.get("context_outputs") or ():
                value = resolve_path(structured, output["path"])
                if value is not _MISSING:
                    context[output["name"]] = value
            results.append(
                StepResult(step["id"], all(item.passed for item in validations), validations)
            )
    return StoryboardResult(results)


def mcp_call(server: Any) -> Call:
    """A :data:`Call` over an in-process FastMCP server (``server.call_tool``)."""

    async def call(
        task: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        from mcp.types import CallToolResult

        try:
            raw = await server.call_tool(task, payload)
        except Exception as exc:  # noqa: BLE001 - a tool error is a step outcome
            data = getattr(exc, "args", [None])[0]
            return {}, {"code": "TOOL_ERROR", "message": str(data)}
        if isinstance(raw, CallToolResult):
            structured: dict[str, Any] = dict(raw.structured_content or {})
        elif isinstance(raw, (tuple, list)) and len(raw) == 2:
            structured = dict(raw[1] or {})
        else:
            structured = dict(raw)
        error = structured.get("adcp_error")
        return structured, error

    return call
