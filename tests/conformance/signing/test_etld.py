"""IDNA conformance tests for :mod:`adcp.signing.etld`.

Complements the behavioral suite in ``tests/test_etld.py`` (eTLD+1
derivation, PSL private section, failure-closed IP/single-label cases)
with the host-canonicalization contract the brand-authorization binding
depends on.

Behavior under test:

* A host spelled as a U-label (``straße.de``) and the same host spelled
  as an A-label (``xn--strae-oqa.de``) are ONE host and must bind.
* :func:`registrable_domain` returns canonical A-label form.
* The single-trailing-dot rule is symmetric across the URL branch and
  the bare-host branch of :func:`host_from`.
* Failure-closed convention extends to hosts that are not IDNA-encodable
  (underscore labels, labels over 63 bytes, leading-hyphen labels).
"""

from __future__ import annotations

import pytest

from adcp.signing.etld import host_from, registrable_domain, same_registrable_domain

# ----- host_from -----


def test_host_from_trailing_dot_symmetric_across_url_and_bare_forms() -> None:
    # The documented single-trailing-dot rule applies to BOTH branches.
    # The URL branch previously trimmed nothing and the bare-host branch
    # trimmed every trailing dot, so the two spellings of one host
    # normalized differently.
    assert host_from("https://Example.COM./") == "example.com"
    assert host_from("Example.COM.") == "example.com"
    assert host_from("https://x.example.com..") == host_from("x.example.com..")


def test_host_from_returns_a_label_for_idn() -> None:
    # host_from is the single normalization point for the binding, so it
    # owns UTS-46 / IDNA-2008 encoding, not just case folding.
    assert host_from("https://shop.straße.de/") == "shop.xn--strae-oqa.de"
    assert host_from("STRASSE.straße.DE") == "strasse.xn--strae-oqa.de"
    assert host_from("xn--strae-oqa.de") == "xn--strae-oqa.de"


def test_host_from_ip_literals_pass_through() -> None:
    # IDNA-2008 rejects purely-numeric labels; IP literals must survive
    # normalization unchanged so the downstream eTLD+1 lookup can fail
    # them closed for the right reason.
    assert host_from("192.0.2.1") == "192.0.2.1"
    assert host_from("https://[2001:0db8::0001]/") == "2001:db8::1"


def test_host_from_idna_invalid_host_raises_value_error() -> None:
    # ``idna.IDNAError`` subclasses ``UnicodeError`` subclasses
    # ``ValueError``, so the documented "raises ValueError" contract is
    # unchanged and brand_authz's ``except ValueError`` around host_from
    # still catches it.
    with pytest.raises(ValueError):
        host_from("under_score.brand.com")


def test_host_from_empty_and_dot_only_still_raise() -> None:
    # The pre-existing emptiness contract survives the delegation.
    for value in ("", ".", "..", "   "):
        with pytest.raises(ValueError):
            host_from(value)


# ----- registrable_domain -----


def test_registrable_domain_returns_a_label_for_idn() -> None:
    assert registrable_domain("straße.de") == "xn--strae-oqa.de"
    assert registrable_domain("ADS.Straße.DE") == "xn--strae-oqa.de"
    assert registrable_domain("xn--strae-oqa.de") == "xn--strae-oqa.de"


def test_registrable_domain_idna_invalid_host_fails_closed() -> None:
    # Not encodable as a hostname -> no derivable eTLD+1 -> None, the
    # same failure-closed category as IP literals and single-label hosts.
    # Failing open here would let ``under_score.brand.com`` reduce to
    # ``brand.com`` and satisfy the binding on a string IDNA rejects.
    assert registrable_domain("under_score.brand.com") is None
    assert registrable_domain("a" * 64 + ".brand.com") is None
    assert registrable_domain("-lead.brand.com") is None
    assert same_registrable_domain("under_score.brand.com", "brand.com") is False


def test_registrable_domain_ip_literals_still_none() -> None:
    # Guard for the IP short-circuit inside the canonicalizer: routing
    # host_from through IDNA must not turn these into raises.
    assert registrable_domain("192.0.2.1") is None
    assert registrable_domain("https://[2001:db8::1]/") is None


# ----- same_registrable_domain -----


def test_same_registrable_domain_idna_u_label_binds_to_a_label() -> None:
    # A brand publishing brand.json under the U-label host and listing
    # its agent under the A-label form (or the reverse) is one host and
    # MUST bind. Uses a real delegated TLD (.de) — ``.example`` is RFC
    # 2606 reserved, not in the PSL, and returns None for both spellings
    # regardless, which would make this assertion vacuous.
    assert same_registrable_domain("https://shop.straße.de/", "xn--strae-oqa.de") is True
    assert same_registrable_domain("xn--strae-oqa.de", "https://shop.straße.de/") is True


def test_same_registrable_domain_idn_cross_domain_still_false() -> None:
    # Canonicalizing both sides must not collapse distinct IDN domains.
    assert same_registrable_domain("straße.de", "xn--bcher-kva.de") is False
