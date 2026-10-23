"""The root-document capture instrument, graded on the one claim it makes.

``tests/capture_root_documents.py`` observes root constraint evaluation through
PEP 669 instead of rebinding the vocabulary's primitives, and every artifact it
writes carries ``instrument.subject_unchanged``. These tests exist because that
field is a SELF-ATTESTATION: the mechanism it replaced mutated the module under
test and still showed zero outcome transitions across 198 items once
``functools.wraps`` restored the one property anything asserted on, so a claim
of this shape is worth exactly what it costs to make it fail.
:func:`test_the_subject_verdict_loses_on_a_wraps_transparent_rebind` is that
cost.
"""

from __future__ import annotations

import functools
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# The instrument below observes through PEP 669 and REFUSES to import without
# ``sys.monitoring`` -- deliberately, since the mechanism it replaced rebound the
# module under test. That refusal is right for a programmatic caller and wrong as
# a collection-time error on the 3.10/3.11 matrix legs, where the capability does
# not exist to be tested. Gate the module here rather than soften the refusal.
if not hasattr(sys, "monitoring"):  # pragma: no cover - CPython < 3.12
    pytest.skip(
        "the root-document capture instrument needs PEP 669 (CPython 3.12+)",
        allow_module_level=True,
    )

# Through the package, which is the spelling the other constraint tests use and
# the only one that is order-independent: importing adcp.types.canonical_creative
# before the media_buy domain is a circular import.
from adcp.types import CreateMediaBuyRequest, GetReportingStatusRequest, _conditions
from tests import capture_root_documents as capture

#: A document each capture point sees, and the point it exercises.
#:
#: The flat one passes field validation, which is what lets the compiled
#: ``model_validator`` run at all; the split one satisfies no branch, so it is
#: refused -- and the split capture point still records it, because it reads the
#: document at the union wrapper's ENTRY rather than from its outcome.
_FLAT_DOCUMENT = {"account": {"account_id": "account-1"}, "view": "periods"}
_SPLIT_DOCUMENT = {"totally": "wrong"}


@pytest.fixture
def armed(tmp_path: Path) -> Iterator[Path]:
    """The instrument, armed for one test and released afterwards.

    Importing the module arms nothing on purpose, so a test can grade the
    instrument without instrumenting the rest of the session.
    """
    artifact = tmp_path / "rootdocs.json"
    capture.install(artifact)
    try:
        yield artifact
    finally:
        capture.uninstall()


def test_both_capture_points_record_their_document(armed: Path) -> None:
    """A flat root and a split root each land in the artifact, and the subject is intact.

    Both points in one test, because a capture mechanism that sees one of them
    is the defect this instrument's predecessor had: hooking only the union
    wrapper covered 11 roots of 103.
    """
    GetReportingStatusRequest.model_validate(_FLAT_DOCUMENT)
    with pytest.raises(Exception, match="at least one of these field groups"):
        CreateMediaBuyRequest.model_validate(_SPLIT_DOCUMENT)

    captured = {record["source"]: record for record in capture.records()}
    assert captured.keys() >= {
        "flat",
        "split",
    }, f"a capture point recorded nothing: got {sorted(captured)}"
    assert captured["flat"]["document"] == _FLAT_DOCUMENT
    assert captured["split"]["document"] == _SPLIT_DOCUMENT
    assert captured["split"]["names"], "a split record names the root's branch classes"

    report = capture.instrument_report()
    assert report["subject_unchanged"], report["subject_changes"]
    assert (report["frame_mismatches"], report["callback_faults"]) == (0, 0), report

    capture.flush()
    artifact = json.loads(armed.read_text())
    assert artifact["instrument"]["subject_unchanged"] is True
    assert artifact["documents"], "the artifact carries the documents, not just the verdict"
    assert artifact["by_source"]["flat"] and artifact["by_source"]["split"]


def test_the_subject_verdict_loses_on_a_wraps_transparent_rebind(armed: Path) -> None:
    """Rebinding ONE primitive must flip ``subject_unchanged`` to False.

    The fire probe. ``functools.wraps`` copies ``__module__`` and ``__name__``,
    which is every property ``tests/test_conditions_vocabulary.py`` reads, so a
    wrapped rebind is invisible there and was invisible to an outcome comparison
    over 198 items. The verdict therefore compares IDENTITY as well, and this is
    the test that would fail if it stopped doing so.
    """
    assert capture.instrument_report()["subject_unchanged"], "armed, nothing rebound yet"

    real = _conditions.sent

    @functools.wraps(real)
    def impostor(*args: object, **kwargs: object) -> object:
        return real(*args, **kwargs)

    assert impostor.__module__ == real.__module__, "the probe must be wraps-transparent"
    assert impostor.__name__ == real.__name__
    _conditions.sent = impostor  # type: ignore[assignment]
    try:
        report = capture.instrument_report()
    finally:
        _conditions.sent = real
    assert (
        report["subject_unchanged"] is False
    ), "a wraps-transparent rebind left the verdict True, so the verdict grades nothing"
    assert any("sent:" in change for change in report["subject_changes"]), report["subject_changes"]
    assert capture.instrument_report()["subject_unchanged"], "restored"


def test_an_unnamed_artifact_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """No destination means no run: a default path overwrites another lane's corpus."""
    monkeypatch.delenv("ROOT_DOCUMENT_CAPTURE", raising=False)
    with pytest.raises(pytest.UsageError, match="ROOT_DOCUMENT_CAPTURE is unset"):
        capture.pytest_configure(config=None)
