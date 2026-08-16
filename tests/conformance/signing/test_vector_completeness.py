"""Guard the vendored request-signing vector set against silent shrinkage.

``test_verifier_vectors`` and ``test_canonicalization`` grade whatever vectors
happen to be on disk. That makes them blind in one specific direction: a vector
the spec defines but the SDK never vendored is a rule nobody grades, and no
amount of green in those modules can tell you it is absent.

These tests supply the missing direction by pinning the vendored tree to a
manifest -- filenames *and* content hashes -- for the AdCP release named in
``VECTOR_SET_SPEC_VERSION``.
"""

from __future__ import annotations

import json

import pytest

from adcp import get_adcp_spec_version
from tests.conformance.signing.vectors import (
    VECTOR_MANIFEST,
    VECTOR_SET_SPEC_VERSION,
    VECTORS_DIR,
    load_canonicalization_cases,
    manifest_ids,
    on_disk_files,
)


def test_vendored_tree_matches_manifest_exactly() -> None:
    """Every vendored file is present, unmodified, and nothing extra is present."""
    on_disk = on_disk_files()

    missing = sorted(set(VECTOR_MANIFEST) - set(on_disk))
    unexpected = sorted(set(on_disk) - set(VECTOR_MANIFEST))
    drifted = sorted(
        name
        for name, digest in VECTOR_MANIFEST.items()
        if name in on_disk and on_disk[name] != digest
    )

    assert not missing, (
        f"vendored vectors missing from disk: {missing}\n"
        f"Re-vendor from the AdCP {VECTOR_SET_SPEC_VERSION} spec repo "
        f"(dist/compliance/{VECTOR_SET_SPEC_VERSION}/test-vectors/request-signing/)."
    )
    assert not unexpected, (
        f"files present under {VECTORS_DIR} that the manifest does not pin: {unexpected}\n"
        "Vectors are vendored verbatim from the spec; add the file upstream first, "
        "then re-vendor and regenerate the manifest."
    )
    assert not drifted, (
        f"vendored vectors differ from their pinned content: {drifted}\n"
        "Either the local copy was edited (revert it -- vectors are vendored verbatim) "
        "or the spec changed and the SDK must be re-graded before re-pinning."
    )


def test_manifest_pins_the_spec_version_the_sdk_targets() -> None:
    """A spec bump must re-vendor the vectors, not silently keep the old set."""
    assert get_adcp_spec_version() == VECTOR_SET_SPEC_VERSION, (
        f"SDK targets AdCP {get_adcp_spec_version()} but the vendored request-signing "
        f"vectors are pinned at {VECTOR_SET_SPEC_VERSION}. Re-vendor the vector tree "
        f"from the new spec release and regenerate the pin with "
        f"`python -m tests.conformance.signing.vectors --write`."
    )


@pytest.mark.parametrize(("subdir", "expected_count"), [("positive", 12), ("negative", 28)])
def test_vector_counts_match_spec(subdir: str, expected_count: int) -> None:
    """Counts are stated literally so a shrunken set is obvious in the diff."""
    ids = manifest_ids(subdir)
    assert len(ids) == expected_count, (
        f"AdCP {VECTOR_SET_SPEC_VERSION} defines {expected_count} {subdir} vectors, "
        f"manifest pins {len(ids)}"
    )


@pytest.mark.parametrize("subdir", ["positive", "negative"])
def test_vector_numbering_is_contiguous(subdir: str) -> None:
    """Vectors are numbered 001..NNN; a gap means one was dropped."""
    numbers = sorted(int(name.split("-", 1)[0]) for name in manifest_ids(subdir))
    assert numbers == list(
        range(1, len(numbers) + 1)
    ), f"{subdir} vector numbering has a gap: {numbers}"


def test_canonicalization_fixture_is_vendored_and_populated() -> None:
    """The standalone canonicalization fixture ships and carries both case kinds."""
    cases = load_canonicalization_cases()
    assert len(cases) == 31, (
        f"AdCP {VECTOR_SET_SPEC_VERSION} canonicalization.json defines 31 cases, "
        f"found {len(cases)}"
    )
    assert sum(1 for _, case in cases if case.get("reject")) == 6
    assert sum(1 for _, case in cases if not case.get("reject")) == 25


def test_every_key_referenced_by_a_vector_exists_in_keys_json() -> None:
    """``jwks_ref`` resolution is silent on a missing kid; catch it here instead."""
    available = {k["kid"] for k in json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]}
    for name in VECTOR_MANIFEST:
        if not name.startswith(("positive/", "negative/")):
            continue
        vector = json.loads((VECTORS_DIR / name).read_text())
        for kid in vector.get("jwks_ref", []):
            assert kid in available, f"{name} references kid {kid!r} absent from keys.json"
