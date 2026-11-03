"""``row_binding_id`` and ``credential_ref`` accept 1 to 255 characters (adcp-client-python #1498).

Both were capped at 128, tighter than every comparable AdCP identifier (``reporting_revision_id``,
``resource_ref`` and ``destination_ref`` use ``^[A-Za-z0-9_.:-]{1,255}$``). Ported from the
JavaScript SDK (adcp-client #3595, ``reporting-row-binding-bounds``). No database needed: this is
the configuration check and the provider ``clients`` map.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from adcp_reporting_storage.ledger.row_storage import (
    ReportingObjectRowBindingV1,
    ReportingPostgresRowBindingV1,
    ReportingRowStorageOptionsV1,
    ReportingRowStorageV1,
)
from adcp_reporting_storage.ledger.row_storage_object import (
    REPORTING_ROW_BINDING_ID_MAX_LENGTH,
    REPORTING_ROW_CREDENTIAL_REF_MAX_LENGTH,
    create_filesystem_reporting_row_object_provider_v1,
)
from adcp_reporting_storage.ledger.row_storage_provider_io import client_map, is_row_identifier

from adcp.reporting.ledger.row_storage_errors import ReportingRowStoreError

HOSTILE = (
    "",
    "has space",
    "tab\there",
    "trailing-newline\n",
    "nul\x00byte",
    "ａfullwidth",  # fullwidth "a"
    "аcyrillic",  # Cyrillic "a" next to the Latin one
    "٣arabic-indic-digit",
    "semi;colon",
    "slash/er",
)


def test_the_bounds_are_the_adcp_identifier_bound() -> None:
    assert REPORTING_ROW_BINDING_ID_MAX_LENGTH == 255
    assert REPORTING_ROW_CREDENTIAL_REF_MAX_LENGTH == 255


@pytest.mark.parametrize("length", [1, 128, 129, 254, 255])
def test_an_identifier_up_to_255_characters_is_accepted(length: int) -> None:
    assert is_row_identifier("a" * length)


@pytest.mark.parametrize("length", [256, 300, 5000])
def test_an_identifier_beyond_255_characters_is_refused(length: int) -> None:
    assert not is_row_identifier("a" * length)


def test_every_character_of_the_identifier_alphabet_is_accepted() -> None:
    assert is_row_identifier("Az09_.:-")


@pytest.mark.parametrize("value", [*HOSTILE, None, 7, b"abc", ["a"]])
def test_hostile_and_non_string_identifiers_are_refused(value: Any) -> None:
    assert not is_row_identifier(value)


def storage(tmp_path: Path, binding_id: str = "reports", **binding: Any) -> ReportingRowStorageV1:
    providers = {
        "filesystem": create_filesystem_reporting_row_object_provider_v1(
            roots={"rows": tmp_path / "rows"}
        )
    }
    defaults: dict[str, Any] = {
        "provider": "filesystem",
        "location": {"root": "rows"},
        "prefix": "adcp-rows",
    }
    return ReportingRowStorageV1(
        ReportingRowStorageOptionsV1(
            bindings={binding_id: ReportingObjectRowBindingV1(**{**defaults, **binding})},
            providers=providers,
        )
    )


@pytest.mark.parametrize("length", [128, 129, 255])
def test_a_binding_id_up_to_255_characters_passes_the_configuration_check(
    tmp_path: Path, length: int
) -> None:
    storage(tmp_path, "b" * length)
    ReportingRowStorageV1(
        ReportingRowStorageOptionsV1(bindings={"p" * length: ReportingPostgresRowBindingV1()})
    )


def test_a_binding_id_beyond_255_characters_is_refused_and_not_echoed(tmp_path: Path) -> None:
    with pytest.raises(ReportingRowStoreError) as failure:
        storage(tmp_path, "x" * 5000)
    assert failure.value.code == "INVALID_INPUT"
    message = str(failure.value)
    assert "1 to 255 characters of A-Za-z0-9_.:-" in message
    assert len(message) < 300, "a hostile identifier is not echoed whole"
    assert "x" * 100 not in message


def test_a_short_invalid_binding_id_is_quoted_with_the_bound(tmp_path: Path) -> None:
    with pytest.raises(ReportingRowStoreError) as failure:
        storage(tmp_path, "bad id")
    assert '"bad id"' in str(failure.value)
    assert "1 to 255 characters" in str(failure.value)


@pytest.mark.parametrize("binding_id", HOSTILE)
def test_hostile_binding_ids_are_refused(tmp_path: Path, binding_id: str) -> None:
    with pytest.raises(ReportingRowStoreError) as failure:
        storage(tmp_path, binding_id)
    assert failure.value.code == "INVALID_INPUT"


def test_a_non_string_binding_id_is_refused_not_a_type_error() -> None:
    with pytest.raises(ReportingRowStoreError) as failure:
        ReportingRowStorageV1(
            ReportingRowStorageOptionsV1(
                bindings={7: ReportingPostgresRowBindingV1()}  # type: ignore[dict-item]
            )
        )
    assert failure.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("length", [128, 129, 255])
def test_a_credential_ref_up_to_255_characters_passes_the_configuration_check(
    tmp_path: Path, length: int
) -> None:
    storage(tmp_path, credential_ref="c" * length)


def test_a_credential_ref_beyond_255_characters_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ReportingRowStoreError) as failure:
        storage(tmp_path, credential_ref="c" * 256)
    assert failure.value.code == "INVALID_INPUT"
    assert "1 to 255 characters of A-Za-z0-9_.:-" in str(failure.value)
    assert "c" * 100 not in str(failure.value)


@pytest.mark.parametrize("credential_ref", HOSTILE)
def test_hostile_credential_refs_are_refused(tmp_path: Path, credential_ref: str) -> None:
    with pytest.raises(ReportingRowStoreError) as failure:
        storage(tmp_path, credential_ref=credential_ref)
    assert failure.value.code == "INVALID_INPUT"


def test_a_provider_clients_map_holds_every_reference_a_binding_can_name() -> None:
    client = object()
    for length in (128, 129, 255):
        assert client_map({"r" * length: client}) == {"r" * length: client}
    for bad in ("r" * 256, *HOSTILE):
        with pytest.raises(ReportingRowStoreError) as failure:
            client_map({bad: client})
        assert failure.value.code == "INVALID_INPUT"
    with pytest.raises(ReportingRowStoreError):
        client_map({"ok": None})
