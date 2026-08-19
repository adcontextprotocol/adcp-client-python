"""Producer-side translation of AdCP universal macros in pixel URLs.

``native`` mappings are a deliberate raw-token escape hatch for downstream ad
servers. They bypass percent-encoding, so this module validates every native
entry before translating the URL, including entries the URL does not use.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias

_UNIVERSAL_MACRO = re.compile(r"\{[A-Z][A-Z0-9_]*\}")
_NATIVE_TOKEN_SHAPE = re.compile(
    r"(?:%%[^\n\r\u2028\u2029]+%%|"
    r"\{\{[^\n\r\u2028\u2029]+\}\}|"
    r"\$\{[^\n\r\u2028\u2029]+\}|"
    r"\[[A-Z][A-Z0-9_]*\])"
)
_CONSENT_MACROS = frozenset(
    {
        "{GDPR}",
        "{GDPR_CONSENT}",
        "{US_PRIVACY}",
        "{GPP_STRING}",
        "{GPP_SID}",
        "{LIMIT_AD_TRACKING}",
    }
)
_UNRESERVED_BYTES = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


@dataclass(frozen=True, slots=True)
class NativeMacroMapping:
    """A downstream ad-server token inserted without percent-encoding."""

    native: str


@dataclass(frozen=True, slots=True)
class ValueMacroMapping:
    """A literal value encoded with the RFC 3986 unreserved whitelist."""

    value: str


MacroMappingEntry: TypeAlias = NativeMacroMapping | ValueMacroMapping
MacroMapping: TypeAlias = Mapping[str, MacroMappingEntry]
UniversalMacroTranslationErrorCode: TypeAlias = Literal["unsafe_native_mapping"]


@dataclass(slots=True)
class TranslateUniversalMacrosResult:
    """Translated URL and deterministic diagnostics.

    ``dropped_params`` preserves query-parameter occurrence order and may
    contain duplicate keys. All macro diagnostic lists are deduplicated.
    URL-scoped diagnostics use first query occurrence order; mapping-scoped
    diagnostics preserve mapping iteration order.
    """

    url: str
    dropped_params: list[str]
    unmapped_macros: list[str]
    dropped_consent_macros: list[str]
    frozen_consent_macros: list[str]
    suspect_native_values: list[str]


class UniversalMacroTranslationError(ValueError):
    """Typed rejection raised before an unsafe native token can be emitted."""

    code: UniversalMacroTranslationErrorCode
    macro: str

    def __init__(self, macro: str) -> None:
        self.code = "unsafe_native_mapping"
        self.macro = macro
        super().__init__(f"native mapping for {macro!r} contains an unsafe control character")


def encode_unreserved(raw: str) -> str:
    """UTF-8 encode ``raw``, escaping every byte outside RFC 3986 unreserved.

    Percent escapes use uppercase hexadecimal. Unlike
    :func:`urllib.parse.quote`, this helper does not preserve ``/``.
    """

    return "".join(
        chr(byte) if byte in _UNRESERVED_BYTES else f"%{byte:02X}" for byte in raw.encode("utf-8")
    )


def _has_unsafe_native_character(value: str) -> bool:
    return any(ord(character) <= 0x1F or ord(character) == 0x7F for character in value)


def _append_once(items: list[str], seen: set[str], value: str) -> None:
    if value not in seen:
        seen.add(value)
        items.append(value)


def translate_universal_macros(
    pixel_url: str,
    mapping: MacroMapping,
) -> TranslateUniversalMacrosResult:
    """Translate universal macros in query-parameter values.

    ``ValueMacroMapping`` values are UTF-8 percent-encoded with
    :func:`encode_unreserved`; ``NativeMacroMapping`` values are inserted
    verbatim after the full mapping passes the control-character guard. If a
    parameter value contains any unmapped universal macro, that whole parameter
    is dropped. Query keys, the path, the fragment, and parameters without
    universal macros pass through byte-for-byte. Replacement is single-pass.

    Consent macros supplied through ``ValueMacroMapping`` are translated but
    reported in ``frozen_consent_macros`` because freezing impression-time
    consent at producer time can create a privacy defect. Callers should also
    inspect ``dropped_consent_macros`` and ``suspect_native_values`` before
    publishing a tracker.

    Raises:
        UniversalMacroTranslationError: A native mapping, used or unused,
            contains U+0000-U+001F or U+007F. No URL is emitted.
        TypeError: A mapping entry is not a supported typed mapping model.
    """

    frozen_consent_macros: list[str] = []
    frozen_seen: set[str] = set()
    suspect_native_values: list[str] = []
    suspect_seen: set[str] = set()
    validated_mapping: dict[str, MacroMappingEntry] = {}

    # Validate the entire raw-token trust boundary before doing any URL work.
    # An unused unsafe entry must reject just like an entry present in the URL.
    for macro, entry in mapping.items():
        if isinstance(entry, NativeMacroMapping):
            native = entry.native
            if _has_unsafe_native_character(native):
                raise UniversalMacroTranslationError(macro)
            validated_mapping[macro] = NativeMacroMapping(native=native)
        elif isinstance(entry, ValueMacroMapping):
            value = entry.value
            if macro in _CONSENT_MACROS:
                _append_once(frozen_consent_macros, frozen_seen, macro)
            if _NATIVE_TOKEN_SHAPE.fullmatch(value):
                _append_once(suspect_native_values, suspect_seen, macro)
            validated_mapping[macro] = ValueMacroMapping(value=value)
        else:
            raise TypeError(
                f"mapping entry for {macro!r} must be NativeMacroMapping " "or ValueMacroMapping"
            )

    fragment_index = pixel_url.find("#")
    if fragment_index == -1:
        without_fragment = pixel_url
        fragment = ""
    else:
        without_fragment = pixel_url[:fragment_index]
        fragment = pixel_url[fragment_index:]

    query_index = without_fragment.find("?")
    if query_index == -1:
        return TranslateUniversalMacrosResult(
            url=pixel_url,
            dropped_params=[],
            unmapped_macros=[],
            dropped_consent_macros=[],
            frozen_consent_macros=frozen_consent_macros,
            suspect_native_values=suspect_native_values,
        )

    base = without_fragment[:query_index]
    raw_query = without_fragment[query_index + 1 :]

    dropped_params: list[str] = []
    unmapped_macros: list[str] = []
    unmapped_seen: set[str] = set()
    dropped_consent_macros: list[str] = []
    dropped_consent_seen: set[str] = set()
    output_parts: list[str] = []

    for raw_param in raw_query.split("&"):
        key, separator, value = raw_param.partition("=")
        tokens = _UNIVERSAL_MACRO.findall(value)
        if not tokens:
            output_parts.append(raw_param)
            continue

        missing = [token for token in tokens if token not in validated_mapping]
        if missing:
            dropped_params.append(key)
            for macro in missing:
                _append_once(unmapped_macros, unmapped_seen, macro)
                if macro in _CONSENT_MACROS:
                    _append_once(
                        dropped_consent_macros,
                        dropped_consent_seen,
                        macro,
                    )
            continue

        def replace(match: re.Match[str]) -> str:
            macro = match.group(0)
            entry = validated_mapping[macro]
            if isinstance(entry, NativeMacroMapping):
                return entry.native
            if isinstance(entry, ValueMacroMapping):
                return encode_unreserved(entry.value)
            # The mapping-wide validation above makes this unreachable even
            # for mutable custom Mapping implementations under normal use.
            raise TypeError(f"unsupported mapping entry for {macro!r}")

        translated = _UNIVERSAL_MACRO.sub(replace, value)
        output_parts.append(f"{key}{separator}{translated}")

    new_query = "&".join(output_parts)
    url = f"{base}?{new_query}{fragment}" if new_query else f"{base}{fragment}"
    return TranslateUniversalMacrosResult(
        url=url,
        dropped_params=dropped_params,
        unmapped_macros=unmapped_macros,
        dropped_consent_macros=dropped_consent_macros,
        frozen_consent_macros=frozen_consent_macros,
        suspect_native_values=suspect_native_values,
    )


__all__ = [
    "MacroMapping",
    "MacroMappingEntry",
    "NativeMacroMapping",
    "TranslateUniversalMacrosResult",
    "UniversalMacroTranslationError",
    "UniversalMacroTranslationErrorCode",
    "ValueMacroMapping",
    "encode_unreserved",
    "translate_universal_macros",
]
