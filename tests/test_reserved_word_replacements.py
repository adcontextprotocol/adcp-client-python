"""Reserved-word-safe enum replacements reach only peers on 3.3 or later.

AdCP 3.3.0-rc.0 (adcontextprotocol/adcp#8791) adds ``passed`` for ``pass``,
``all`` for ``global`` and ``full_name`` for ``name``, and deprecates the
originals. A replacement is sent only when the release served is 3.3 or later;
an older release has only the original. The 3.3 schemas mark each original
``x-superseded-by`` its replacement, and conversion to an older peer writes
that original instead of dropping the value or failing.
"""

from __future__ import annotations

from typing import Any

from adcp._version import resolve_adcp_version
from adcp.compat.versions import convert_payload

PACKAGED = resolve_adcp_version(None)


def _artifacts(verdict: str) -> dict[str, Any]:
    return {
        "media_buy_id": "mb-1",
        "artifacts": [
            {
                "record_id": "r1",
                "artifact": {
                    "property_rid": "019372c4-7e2a-7f00-8000-000000000001",
                    "artifact_id": "a1",
                    "assets": [{"type": "text", "role": "title", "content": "Hello"}],
                },
                "local_verdict": verdict,
            }
        ],
    }


def _verdict(payload: dict[str, Any], target: str) -> tuple[str, tuple[Any, ...]]:
    result = convert_payload(
        "get_media_buy_artifacts",
        payload,
        direction="sync",
        source_version=PACKAGED,
        target_version=target,
    )
    return result.payload["artifacts"][0]["local_verdict"], result.losses


def test_a_replacement_reaches_an_older_peer_as_its_original() -> None:
    assert _verdict(_artifacts("passed"), "3.2") == ("pass", ())


def test_a_replacement_stays_for_a_peer_on_the_same_release() -> None:
    assert _verdict(_artifacts("passed"), PACKAGED) == ("passed", ())


def test_the_deprecated_original_is_unchanged_for_either_peer() -> None:
    assert _verdict(_artifacts("pass"), "3.2") == ("pass", ())
    assert _verdict(_artifacts("pass"), PACKAGED) == ("pass", ())
