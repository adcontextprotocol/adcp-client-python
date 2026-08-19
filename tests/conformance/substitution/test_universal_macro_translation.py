"""Execute the canonical AdCP universal-macro translation fixture."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator

import adcp
from adcp.substitution import (
    MacroMappingEntry,
    NativeMacroMapping,
    TranslateUniversalMacrosResult,
    UniversalMacroTranslationError,
    ValueMacroMapping,
    encode_unreserved,
    translate_universal_macros,
)

_VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "universal-macro-translation"
_FIXTURE_PATH = _VECTORS_DIR / "universal-macro-translation.json"
_SCHEMA_PATH = _VECTORS_DIR / "universal-macro-translation.schema.json"
_PINNED_SHA256 = {
    _FIXTURE_PATH.name: "f6c767a616b3564d6d96f035f396f35422d909f3506630d07dfea7c4575eeee4",
    _SCHEMA_PATH.name: "662e1bb8d7b324f22ef8f56d0729e32be67438c3bd3a35d0dd72b05400c3b08e",
}


def _load_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text())
    assert isinstance(document, dict)
    return document


_FIXTURE = _load_json(_FIXTURE_PATH)


def _typed_mapping(raw: dict[str, dict[str, str]]) -> dict[str, MacroMappingEntry]:
    return {
        macro: (
            NativeMacroMapping(native=entry["native"])
            if "native" in entry
            else ValueMacroMapping(value=entry["value"])
        )
        for macro, entry in raw.items()
    }


def test_vendored_fixture_is_exactly_pinned() -> None:
    for path in (_FIXTURE_PATH, _SCHEMA_PATH):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == _PINNED_SHA256[path.name]


def test_vendored_fixture_matches_its_canonical_schema() -> None:
    schema = _load_json(_SCHEMA_PATH)
    Draft7Validator.check_schema(schema)
    Draft7Validator(schema).validate(_FIXTURE)


@pytest.mark.parametrize(
    "vector",
    _FIXTURE["vectors"],
    ids=[vector["name"] for vector in _FIXTURE["vectors"]],
)
def test_canonical_universal_macro_translation_vector(vector: dict[str, Any]) -> None:
    mapping = _typed_mapping(vector["mapping"])
    if "expected_error" in vector:
        with pytest.raises(UniversalMacroTranslationError) as exc_info:
            translate_universal_macros(vector["input_pixel_url"], mapping)
        assert exc_info.value.code == vector["expected_error"]["code"]
        assert exc_info.value.macro == vector["expected_error"]["macro"]
        return

    result = translate_universal_macros(
        vector["input_pixel_url"],
        mapping,
    )
    assert asdict(result) == vector["expected"]


def test_unsafe_unused_native_mapping_rejects_before_emitting_url() -> None:
    with pytest.raises(UniversalMacroTranslationError) as exc_info:
        translate_universal_macros(
            "https://pixel.example/i?ok=1",
            {"{UNUSED}": NativeMacroMapping(native="bad\x00token")},
        )
    assert exc_info.value.code == "unsafe_native_mapping"
    assert exc_info.value.macro == "{UNUSED}"


@pytest.mark.parametrize(
    "codepoint",
    [*range(0x20), 0x7F],
    ids=lambda codepoint: f"U+{codepoint:04X}",
)
def test_native_mapping_rejects_every_forbidden_control_character(codepoint: int) -> None:
    with pytest.raises(UniversalMacroTranslationError) as exc_info:
        translate_universal_macros(
            "https://pixel.example/i?cb={CACHEBUSTER}",
            {"{CACHEBUSTER}": NativeMacroMapping(native=f"before{chr(codepoint)}after")},
        )
    assert exc_info.value.code == "unsafe_native_mapping"
    assert exc_info.value.macro == "{CACHEBUSTER}"


@pytest.mark.parametrize(
    "codepoint",
    [*range(0x80, 0xA0), 0x2028, 0x2029],
    ids=lambda codepoint: f"U+{codepoint:04X}",
)
def test_native_mapping_does_not_reject_c1_or_unicode_line_separators(codepoint: int) -> None:
    native = f"before{chr(codepoint)}after"
    result = translate_universal_macros(
        "https://pixel.example/i?cb={CACHEBUSTER}",
        {"{CACHEBUSTER}": NativeMacroMapping(native=native)},
    )
    assert result.url == f"https://pixel.example/i?cb={native}"


def test_mapping_is_not_reread_after_native_values_are_validated() -> None:
    class ChangingMapping(Mapping[str, MacroMappingEntry]):
        def __init__(self) -> None:
            self.reads = 0

        def __getitem__(self, key: str) -> MacroMappingEntry:
            if key != "{CACHEBUSTER}":
                raise KeyError(key)
            self.reads += 1
            if self.reads == 1:
                return NativeMacroMapping(native="%%SAFE%%")
            return NativeMacroMapping(native="%%X%%\r\nInjected: yes")

        def __iter__(self) -> Iterator[str]:
            return iter(("{CACHEBUSTER}",))

        def __len__(self) -> int:
            return 1

    mapping = ChangingMapping()
    result = translate_universal_macros(
        "https://pixel.example/i?cb={CACHEBUSTER}",
        mapping,
    )
    assert result.url == "https://pixel.example/i?cb=%%SAFE%%"
    assert mapping.reads == 1


def test_public_exports_resolve_to_substitution_models_and_helpers() -> None:
    assert adcp.NativeMacroMapping is NativeMacroMapping
    assert adcp.ValueMacroMapping is ValueMacroMapping
    assert adcp.TranslateUniversalMacrosResult is TranslateUniversalMacrosResult
    assert adcp.UniversalMacroTranslationError is UniversalMacroTranslationError
    assert adcp.encode_unreserved is encode_unreserved
    assert adcp.translate_universal_macros is translate_universal_macros
