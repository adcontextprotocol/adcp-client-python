"""Byte-for-byte canonicalization check against AdCP request-signing vectors.

The committed positive-vector signatures are trustworthy only if our canonical
signature base agrees with `expected_signature_base` in every vector. This test
proves that — independent of crypto, HTTP, JWKS, or replay logic — before any
other stage is built on top.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from adcp.signing.canonical import (
    build_signature_base,
    canonicalize_authority,
    canonicalize_target_uri,
    parse_signature_input_header,
)
from tests.conformance.signing.vectors import (
    VECTORS_DIR,
    load_canonicalization_cases,
    load_vector_set,
)

# Cases from canonicalization.json that the SDK does not yet satisfy, mapped to
# the issue tracking each gap. Marked strict so a fix XPASSes and forces the
# entry to be retired instead of lingering.
KNOWN_CANONICALIZATION_GAPS: dict[str, str] = {}


def _vectors_with_expected_base() -> list[tuple[str, Path]]:
    all_vectors = load_vector_set("positive") + load_vector_set("negative")
    return [
        (name, path)
        for name, path in all_vectors
        if "expected_signature_base" in json.loads(path.read_text())
    ]


def _canonicalization_params() -> list[Any]:
    params = []
    for name, case in load_canonicalization_cases():
        marks = []
        if name in KNOWN_CANONICALIZATION_GAPS:
            marks.append(pytest.mark.xfail(strict=True, reason=KNOWN_CANONICALIZATION_GAPS[name]))
        params.append(pytest.param(name, case, marks=marks))
    return params


@pytest.mark.parametrize(
    ("name", "path"),
    _vectors_with_expected_base(),
    ids=lambda v: v if isinstance(v, str) else v.name,
)
def test_signature_base_matches_expected(name: str, path: Path) -> None:
    vector = json.loads(path.read_text())
    request = vector["request"]
    sig_input_header = request["headers"]["Signature-Input"]
    labels = parse_signature_input_header(sig_input_header)
    assert "sig1" in labels, f"{name}: no sig1 label in Signature-Input"

    computed = build_signature_base(
        method=request["method"],
        url=request["url"],
        headers=request["headers"],
        parsed=labels["sig1"],
    )
    assert computed == vector["expected_signature_base"], (
        f"{name}: signature base mismatch\n"
        f"  expected: {vector['expected_signature_base']!r}\n"
        f"  computed: {computed!r}"
    )


@pytest.mark.parametrize(("name", "case"), _canonicalization_params())
def test_canonicalization_case(name: str, case: dict[str, Any]) -> None:
    """Grade `canonicalization.json` -- pure URL canonicalization, no crypto.

    The per-vector `expected_signature_base` path only exercises the URL shapes
    that happen to appear in a signed vector. This fixture is the exhaustive
    set: IDN, IPv6, userinfo, empty-query and the six malformed-authority
    rejections, several of which no signed vector covers at all.
    """
    url = case["input_url"]

    if case.get("reject"):
        # Assert the code the vector ships, not merely that something raised.
        # `https://[::1/p` is refused inside urlsplit() with a bare ValueError
        # carrying no code, so a bare `pytest.raises(ValueError)` passes for the
        # wrong reason and grades nothing -- the refusal has to be ours, and it
        # has to name which rule fired.
        with pytest.raises(ValueError) as excinfo:
            canonicalize_target_uri(url)
        actual_code = getattr(excinfo.value, "code", None)
        assert actual_code == case["expected_error_code"], (
            f"{name}: expected error code {case['expected_error_code']!r}, got "
            f"{actual_code!r} from {type(excinfo.value).__name__} ({case['rule']})"
        )
        return

    assert (
        canonicalize_target_uri(url) == case["expected_target_uri"]
    ), f"{name}: @target-uri mismatch for {url!r} ({case['rule']})"
    assert (
        canonicalize_authority(url) == case["expected_authority"]
    ), f"{name}: @authority mismatch for {url!r} ({case['rule']})"


# ---- trailing FQDN-root dot: the two host branches must stay symmetric ----

# `bücher.example` is the U-label form; `xn--bcher-kva.example` is its A-label
# form. Same host, two spellings -- the canonical authority must not depend on
# which one arrived.
_U_LABEL_HOST = "bücher.example"
_A_LABEL_HOST = "xn--bcher-kva.example"


@pytest.mark.parametrize(
    ("url", "expected_authority"),
    [
        # ASCII branch: already an A-label, root dot present.
        (f"https://{_A_LABEL_HOST}./p", _A_LABEL_HOST),
        # Non-ASCII branch: U-label, root dot present.
        (f"https://{_U_LABEL_HOST}./p", _A_LABEL_HOST),
        # Root dot in front of a non-default port -- the dot belongs to the
        # host, not to the netloc, so stripping it must not eat the port.
        (f"https://{_U_LABEL_HOST}.:8443/p", f"{_A_LABEL_HOST}:8443"),
        (f"https://{_A_LABEL_HOST}.:8443/p", f"{_A_LABEL_HOST}:8443"),
    ],
    ids=["ascii-root-dot", "u-label-root-dot", "u-label-root-dot-port", "ascii-root-dot-port"],
)
def test_trailing_root_dot_is_stripped_on_both_host_branches(
    url: str, expected_authority: str
) -> None:
    """A trailing FQDN-root dot is stripped identically on both host branches.

    The beta.3 fixture now pins representative root-dot cases. These focused
    relationships matter because the two branches disagree by construction: the
    ASCII path lowercases in place, while the IDNA helper
    (`_idna_canonicalize.canonicalize_host`) strips one trailing dot as part of
    its UTS-46 preparation. Left unguarded, `https://example.com./p` would keep
    its dot while `https://bücher.example./p` lost one -- a signer emitting the
    A-label form and a verifier reading a Host-header U-label would then compute
    different `@authority` values for the same host, and every signature between
    them would fail to verify.

    The rule this pins: strip the root dot ONCE, before choosing a branch, so
    both branches agree -- and agree with `canonicalize_host`, the package's
    designated host normalizer. Handling the dot inside `canonical.py` instead
    would make it the seventh host normalizer in the tree, which is the very
    duplication this change exists to remove.

    This is deliberately wire-visible: `https://example.com./p` now derives
    `example.com` where it derived `example.com.` before, so a signer on this
    SDK and a verifier on an older one disagree for FQDN-root URLs. The AdCP
    beta.3 profile defines root-dot handling and ships conformance vectors for it.
    """
    assert canonicalize_authority(url) == expected_authority
    assert canonicalize_target_uri(url) == f"https://{expected_authority}/p"


def test_root_dot_is_not_observable_in_the_canonical_authority() -> None:
    """Dotted and undotted forms converge, on either branch.

    Stated as a relationship rather than a literal so it keeps holding if the
    A-label spelling ever changes, and so it catches a fix that special-cases
    the dotted form instead of handling the dot uniformly.
    """
    for host in (_U_LABEL_HOST, _A_LABEL_HOST):
        undotted = canonicalize_authority(f"https://{host}/p")
        dotted = canonicalize_authority(f"https://{host}./p")
        assert dotted == undotted, f"root-dot handling diverges for {host!r}"

    # ...and both spellings converge on the same canonical authority.
    assert canonicalize_authority(f"https://{_U_LABEL_HOST}./p") == canonicalize_authority(
        f"https://{_A_LABEL_HOST}./p"
    )


def test_multi_label_signature_input_selects_sig1() -> None:
    """Per spec, verifiers process exactly sig1 and ignore additional labels."""
    vector = json.loads(
        (VECTORS_DIR / "positive" / "004-multiple-signature-labels.json").read_text()
    )
    labels = parse_signature_input_header(vector["request"]["headers"]["Signature-Input"])
    assert set(labels) == {"sig1", "sig2"}
    assert labels["sig1"].components == (
        "@method",
        "@target-uri",
        "@authority",
        "content-type",
    )
    assert labels["sig2"].components == ("@method", "@target-uri")
    assert labels["sig1"].params["nonce"] == "KXYnfEfJ0PBRZXQyVXfVQA"
    assert labels["sig2"].params["nonce"] == "DIFFERENT-NONCE-FOR-SIG2____"


@pytest.mark.parametrize(
    "url",
    ["https://./p", "https://../p", "https://.:443/p", "https://a..b/p"],
    ids=["root-dot-only", "double-dot", "root-dot-with-port", "interior-empty-label"],
)
def test_authority_that_empties_after_normalization_is_rejected(url: str) -> None:
    """A host must still be a host once the root dot comes off.

    ``malformed_authority_reason`` judges the raw netloc, where ``.`` and
    ``..`` are non-empty and pass as hosts. Stripping the root dot is what
    empties them, so the check has to run again afterwards. Without it
    ``https://./p`` canonicalized to ``https:///p`` -- the empty authority
    ``malformed-empty-authority`` rejects, reached by a path that skipped the
    gate.

    ``a..b`` is here because the rule is "no empty label", not merely
    "not empty".
    """
    with pytest.raises(ValueError) as excinfo:
        canonicalize_target_uri(url)
    assert getattr(excinfo.value, "code", None) == "request_target_uri_malformed"
    with pytest.raises(ValueError):
        canonicalize_authority(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # RFC 3986 §3.2.3: `port = *DIGIT`, so an EMPTY port is legal and means
        # "default" -- normalizers SHOULD drop it and its colon. Rejecting it
        # would refuse a valid URI.
        ("https://host:/p", "host"),
        ("https://[::1]:/p", "[::1]"),
        # ...and a real port still survives.
        ("https://host:8443/p", "host:8443"),
    ],
    ids=["empty-port", "empty-port-ipv6", "real-port"],
)
def test_empty_port_is_normalized_away_not_rejected(url: str, expected: str) -> None:
    assert canonicalize_authority(url) == expected


@pytest.mark.parametrize(
    "url",
    ["https://host:-80/p", "https://host:8_0/p", "https://host:٨٠/p", "https://host:8a/p"],
    ids=["negative", "underscore-separator", "arabic-indic-digits", "alphanumeric"],
)
def test_non_digit_port_is_rejected_with_the_spec_code(url: str) -> None:
    """The port was never validated -- it went straight into `int()`.

    Three distinct failures came out of that. `int("-80")` yielded the
    authority ``host:-80``, which is not a valid authority at all. `int("8_0")`
    is 80, because Python accepts underscore digit separators. And
    `int("٨٠")` is also 80, because `int()` accepts non-ASCII digits -- so
    ``host:٨٠`` and ``host:80`` collapsed to the SAME canonical authority.
    That last one is a raw-vs-canonical differential: a peer that does not
    fold Arabic-Indic digits computes a different `@authority` for the same
    bytes, and the signature fails for a reason neither side can see.

    Note `str.isdigit()` alone does not close this -- `"٨٠".isdigit()` is
    True. The gate has to be ASCII digits specifically.
    """
    with pytest.raises(ValueError) as excinfo:
        canonicalize_authority(url)
    assert getattr(excinfo.value, "code", None) == "request_target_uri_malformed"


# ---- step 5: empty path + authority -> "/", whatever the query does ----


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # The canonicalization.json case, kept here so the no-query rows below
        # are read against the one spelling the vector set already pins.
        ("https://seller.example.com?x=1", "https://seller.example.com/?x=1"),
        # No query at all -- the rows the vector set does not reach.
        ("https://seller.example.com", "https://seller.example.com/"),
        ("https://seller.example.com:8443", "https://seller.example.com:8443/"),
        ("https://[2001:db8::1]", "https://[2001:db8::1]/"),
        ("http://seller.example.com:80", "http://seller.example.com/"),
        # Empty query, i.e. a bare trailing "?". Step 5 supplies the slash and
        # step 7 keeps the "?"; the two rules compose rather than compete.
        ("https://seller.example.com?", "https://seller.example.com/?"),
    ],
    ids=[
        "query",
        "no-query",
        "no-query-port",
        "no-query-ipv6",
        "no-query-default-port",
        "empty-query",
    ],
)
def test_empty_path_with_authority_becomes_slash_regardless_of_query(
    url: str, expected: str
) -> None:
    """Step 5 keys on the path and the authority, never on the query.

    The spec's step 5 reads "If the path is empty AND an authority is present,
    substitute `/`"; the query appears only inside its illustrative example
    (`https://host?x=1` -> `https://host/?x=1`). RFC 3986 §6.2.3 is the same --
    scheme-based normalization gives an authority-bearing hierarchical URI the
    `/` path whether or not a query follows.

    This was implemented as `if not path and parts.query`, so `https://host`
    canonicalized to `https://host` and only the queried spelling got its
    slash. `canonicalization.json` could not catch it: of its 37 cases exactly
    one has an empty path, and that one carries `?x=1`. The empty-query row is
    the same gate failing on its own terms -- `https://host?` has a `?` in the
    raw URL but `urlsplit` reports `query == ""`, so the condition read False
    and the output was `https://host?`, a path-less URI no RFC 3986 normalizer
    produces.

    Both consequences are silent until they are not. On the signing path a
    producer applying step 5 and a comparer skipping it derive different
    `@target-uri` bytes for one URL, and every signature between them fails
    verification with nothing in either log naming the cause. Off the signing
    path the same function is the identifier comparer for `adagents.json`
    `authorized_agents[].url`, `brand.json` `agents[].url`, TMP
    `seller_agent.agent_url` and the governance issuer, which is what
    `test_bare_authority_and_trailing_slash_are_one_identifier` pins.
    """
    assert canonicalize_target_uri(url) == expected


def test_bare_authority_and_trailing_slash_are_one_identifier() -> None:
    """`https://host` and `https://host/` are the same URL, so one canonical form.

    Stated as a relationship rather than literals: what matters is that the two
    spellings converge, not which string they converge on. Before step 5 ran
    unconditionally they did not, and because this canonicalizer is shared by
    every URL-identifier surface in the SDK -- not just `@target-uri` -- a
    publisher who registered an agent as `https://agent.example.com` failed to
    match a caller arriving as `https://agent.example.com/`, and the rejection
    named authorization rather than canonicalization.
    """
    for bare in (
        "https://agent.example.com",
        "https://agent.example.com:8443",
        "https://[2001:db8::1]",
    ):
        assert canonicalize_target_uri(bare) == canonicalize_target_uri(
            f"{bare}/"
        ), f"bare authority and trailing slash diverge for {bare!r}"
