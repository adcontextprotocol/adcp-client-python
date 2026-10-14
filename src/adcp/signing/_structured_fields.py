"""Reject Structured Fields duplicates before a parser overwrites their evidence.

This grammar-aware preflight preserves item and inner-list parameter scopes.
It does not decode values or replace the profile's existing value validation;
in particular, legacy Base64URL compatibility remains the decoder's choice.
"""

from __future__ import annotations


def reject_dictionary_duplicates(value: str) -> None:
    """Raise ValueError for duplicate labels/parameters or ambiguous syntax."""
    if not value.isascii():
        raise ValueError("Structured Fields must be ASCII")
    _DictionaryScanner(value).scan()


def dictionary_has_parameter(value: str, name: str, expected: str) -> bool:
    """Observe member parameters without resolving duplicates for profile choice.

    This is never a verification parser. An explicit marker observed before a
    syntax failure remains a marker; quoted text and component parameters do
    not become dictionary-member parameters.
    """
    scanner = _DictionaryScanner(value, reject_duplicates=False)
    try:
        scanner.scan()
    except ValueError:
        # A syntax error must not erase an explicit marker already observed.
        return _parameters_contain_marker(scanner.member_parameters, name, expected)
    return _parameters_contain_marker(scanner.member_parameters, name, expected)


def _parameters_contain_marker(parameters: list[tuple[str, str]], name: str, expected: str) -> bool:
    from http_sfv.item import Item

    for key, raw in parameters:
        if key != name:
            continue
        try:
            item = Item()
            item.parse(raw.encode("ascii"))
        except (ValueError, UnicodeError):
            continue
        if item.value == expected:
            return True
    return False


class _DictionaryScanner:
    def __init__(self, value: str, *, reject_duplicates: bool = True) -> None:
        self.value = value
        self.index = 0
        self.reject_duplicates = reject_duplicates
        self.member_parameters: list[tuple[str, str]] = []

    def _ows(self, *, inner: bool = False) -> None:
        whitespace = " " if inner else " \t"
        while self.index < len(self.value) and self.value[self.index] in whitespace:
            self.index += 1

    def _key(self) -> str:
        start = self.index
        if start == len(self.value) or self.value[start] not in "abcdefghijklmnopqrstuvwxyz*":
            raise ValueError("invalid Structured Field key")
        self.index += 1
        while self.index < len(self.value) and self.value[self.index] in (
            "abcdefghijklmnopqrstuvwxyz0123456789_.*-"
        ):
            self.index += 1
        return self.value[start : self.index]

    def _bare_item(self) -> None:
        if self.index == len(self.value):
            raise ValueError("missing Structured Field value")
        start = self.value[self.index]
        if start == "%" and self.value[self.index : self.index + 2] == '%"':
            # RFC 9651 Display Strings share quoted delimiter boundaries.
            # Their percent encoding is validated by the value parser.
            self.index += 1
            start = '"'
        if start == '"':
            self.index += 1
            while self.index < len(self.value):
                character = self.value[self.index]
                self.index += 1
                if character == '"':
                    return
                if character == "\\":
                    if self.index == len(self.value) or self.value[self.index] not in '\\"':
                        raise ValueError("invalid Structured Field string escape")
                    self.index += 1
                elif not 0x20 <= ord(character) <= 0x7E:
                    raise ValueError("invalid Structured Field string")
            raise ValueError("unterminated Structured Field string")
        if start == ":":
            self.index += 1
            while self.index < len(self.value):
                character = self.value[self.index]
                self.index += 1
                if character == ":":
                    return
                if character not in (
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_-"
                ):
                    raise ValueError("invalid Structured Field binary syntax")
            raise ValueError("unterminated Structured Field binary")
        initial = self.index
        while self.index < len(self.value) and self.value[self.index] not in " \t;,()":
            character = self.value[self.index]
            if character in '\\"' or not 0x21 <= ord(character) <= 0x7E:
                raise ValueError("invalid Structured Field value")
            self.index += 1
        if self.index == initial:
            raise ValueError("missing Structured Field item")

    def _parameters(self, *, member: bool = False) -> None:
        seen: set[str] = set()
        while self.index < len(self.value) and self.value[self.index] == ";":
            self.index += 1
            self._ows(inner=True)
            key = self._key()
            if key in seen and self.reject_duplicates:
                raise ValueError(f"duplicate Structured Field parameter {key!r}")
            seen.add(key)
            if self.index < len(self.value) and self.value[self.index] == "=":
                self.index += 1
                start = self.index
                self._bare_item()
                if member:
                    self.member_parameters.append((key, self.value[start : self.index]))

    def _item(self, *, member: bool = False) -> None:
        self._bare_item()
        self._parameters(member=member)

    def _member(self) -> None:
        if self.index < len(self.value) and self.value[self.index] == "(":
            self.index += 1
            while True:
                self._ows(inner=True)
                if self.index == len(self.value):
                    raise ValueError("unterminated Structured Field inner list")
                if self.value[self.index] == ")":
                    self.index += 1
                    break
                self._item()
                if self.index == len(self.value) or self.value[self.index] not in " )":
                    raise ValueError("invalid Structured Field inner-list delimiter")
            self._parameters(member=True)
        else:
            self._item(member=True)

    def scan(self) -> None:
        seen: set[str] = set()
        self._ows()
        if self.index == len(self.value):
            raise ValueError("empty Structured Field dictionary")
        while self.index < len(self.value):
            key = self._key()
            if key in seen and self.reject_duplicates:
                raise ValueError(f"duplicate Structured Field dictionary member {key!r}")
            seen.add(key)
            if self.index < len(self.value) and self.value[self.index] == "=":
                self.index += 1
                self._member()
            else:
                self._parameters(member=True)
            self._ows()
            if self.index == len(self.value):
                return
            if self.value[self.index] != ",":
                raise ValueError("invalid Structured Field dictionary delimiter")
            self.index += 1
            self._ows()
            if self.index == len(self.value):
                raise ValueError("trailing Structured Field dictionary comma")
