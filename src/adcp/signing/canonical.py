"""AdCP URL canonicalization for the RFC 9421 request-signing profile.

This module is the AdCP-specific part of the profile: the ``@target-uri``
and ``@authority`` values the spec defines. URI canonicalization follows
RFC 3986 §6.2 scheme-based normalization plus AdCP-specific rules: query
preserved byte-for-byte, percent-encoding hex uppercased, unreserved chars
decoded, default ports stripped, dot-segments collapsed, hosts converted to
A-labels, and the malformed-authority rejections.

RFC 9421 itself (the ``Signature-Input`` grammar and the signature base)
comes from ``http-message-signatures`` and ``http-sf``;
:func:`parse_signature_input_header` and :func:`build_signature_base` are
the public entry points onto them, and
:class:`adcp.signing._rfc9421.AdcpComponentResolver` plugs the
canonicalization below into the library.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

import idna

from adcp.signing._idna_canonicalize import canonicalize_host
from adcp.signing._rfc9421 import (
    component_identifier,
    is_inner_list,
    parse_dictionary,
    ser_item_or_inner_list,
)

#: Spec code for an authority the AdCP profile requires be rejected rather than
#: canonicalized. Declared here rather than imported from ``errors`` so this
#: module stays a leaf: ``errors`` may depend on canonicalization, never the
#: reverse. ``verifier`` maps this onto the wire error at its boundary.
REQUEST_TARGET_URI_MALFORMED = "request_target_uri_malformed"


class TargetUriMalformedError(ValueError):
    """A URL whose authority the profile requires be rejected.

    Subclasses ``ValueError`` because every existing caller of the
    canonicalizers already treats a bad URL as a ``ValueError``; carrying
    ``.code`` lets the verifier boundary emit the spec code instead of
    re-deriving one. The ``reason`` names which rule fired, so a rejection
    message says what was wrong rather than restating "malformed".
    """

    code = REQUEST_TARGET_URI_MALFORMED

    def __init__(self, subject: str, reason: str) -> None:
        super().__init__(f"{reason}: {subject!r}")
        self.reason = reason


def host_has_raw_non_ascii(host: str) -> bool:
    """Whether *host* carries raw non-ASCII bytes (an un-normalized U-label).

    One definition, deliberately two call sites with two different codes: here
    it selects the UTS-46 branch on the signing path, while the verifier's
    header precheck uses it to reject a U-label arriving on the wire as
    ``request_signature_header_malformed``. Share the predicate, never the code.
    """
    return not host.isascii()


@dataclass(frozen=True)
class SignatureInputLabel:
    """A single label in a Signature-Input header (e.g. the `sig1=...` entry).

    ``raw_value`` is the member's RFC 8941 serialization, which is the
    ``@signature-params`` value the signature base carries.
    """

    label: str
    components: tuple[str, ...]
    params: dict[str, str | int]
    raw_value: str


def parse_signature_input_header(header_value: str) -> dict[str, SignatureInputLabel]:
    """Parse a Signature-Input header value into a dict keyed by label.

    The field is an RFC 8941 Dictionary, parsed by ``http-sf`` with duplicate
    keys and parameters refused. The AdCP profile then narrows its shape:
    every member is an inner list of component names (sf-strings without
    component parameters), and every signature parameter is an sf-string or
    an integer. A Signature-Input header may contain several labels; the
    profile verifies exactly one (conventionally ``sig1``) and ignores others.
    """
    labels: dict[str, SignatureInputLabel] = {}
    for label, member in parse_dictionary(header_value).items():
        if not is_inner_list(member):
            raise ValueError(f"malformed Signature-Input entry: {label!r} is not an inner list")
        items, member_params = member
        components: list[str] = []
        for name, component_params in items:
            if type(name) is not str:
                raise ValueError(f"component name not quoted: {name!r}")
            if component_params:
                raise ValueError(f"component {name!r} carries parameters the profile does not use")
            components.append(name)
        params: dict[str, str | int] = {}
        for key, value in member_params.items():
            if type(value) is not str and type(value) is not int:
                raise ValueError(
                    f"signature param {key!r} must be a quoted string or integer, got {value!r}"
                )
            params[key] = value
        labels[label] = SignatureInputLabel(
            label=label,
            components=tuple(components),
            params=params,
            raw_value=ser_item_or_inner_list(member),
        )
    return labels


def build_signature_base(
    method: str,
    url: str,
    headers: Mapping[str, str],
    parsed: SignatureInputLabel,
) -> str:
    """Build the RFC 9421 signature base string for the AdCP profile.

    ``http-message-signatures`` builds the base: lines joined with a single
    LF, no trailing newline, components in ``Signature-Input`` order, then
    ``@signature-params``. :class:`AdcpComponentResolver` supplies the AdCP
    ``@target-uri`` and ``@authority`` canonicalization. The base comes from
    ``parsed.components`` and ``parsed.params`` (in order); ``raw_value`` is
    not consulted, so ``@signature-params`` is the RFC 8941 serialization.
    """
    from adcp.signing._rfc9421 import AdcpComponentResolver, Request, build_base

    base = build_base(
        Request(method=method, url=url, headers=dict(headers)),
        covered_component_ids=[component_identifier((name, {})) for name in parsed.components],
        params=parsed.params,
        resolver=AdcpComponentResolver,
    )
    return base.decode("utf-8")


def canonicalize_target_uri(url: str) -> str:
    """Produce the `@target-uri` derived-component value per AdCP profile."""
    parts = _split_or_reject(url)
    scheme = parts.scheme.lower()
    netloc = _canon_authority(parts.netloc, scheme)
    path = _normalize_path(parts.path)
    if not path:
        # Step 5: an empty path with an authority present canonicalizes to "/"
        # (RFC 3986 §6.2.3). The condition is the path and the authority, never
        # the query: `https://host` takes the slash exactly as `https://host?x=1`
        # does. `_canon_authority` above has already rejected an authority with
        # no host, so reaching this line guarantees one is present.
        path = "/"
    # RFC 9421 §2.2.2 + RFC 7230 §5.5: effective request URI excludes the
    # fragment (client-local, never sent on wire).
    target = urlunsplit((scheme, netloc, path, parts.query, ""))
    if not parts.query and "?" in url.split("#", 1)[0]:
        # `urlsplit` maps both `/p` and `/p?` to `query == ""`, and `urlunsplit`
        # emits no `?` for an empty string -- so the two collapse to one
        # signature base. A signer that sent `/p?` and a verifier that
        # reconstructs `/p` then sign different bytes for different URLs and
        # agree, which is exactly the confusion `@target-uri` exists to prevent.
        # The distinction has to be recovered from the raw URL because it is
        # already lost by the time `parts` exists.
        target += "?"
    return target


def canonicalize_authority(url: str) -> str:
    """Produce the `@authority` derived-component value per AdCP profile."""
    parts = _split_or_reject(url)
    return _canon_authority(parts.netloc, parts.scheme.lower())


def canonical_origin(url: str) -> str:
    """Return scheme and authority after AdCP URL canonicalization steps 1–4.

    Paths, queries and fragments do not participate in an origin identity.
    Only absolute HTTP(S) URLs have an origin in this SDK.
    """
    parts = _split_or_reject(url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("an origin requires an absolute HTTP(S) URL")
    return f"{scheme}://{_canon_authority(parts.netloc, scheme)}"


def _split_or_reject(url: str) -> SplitResult:
    """`urlsplit`, with its bare refusals normalized onto the typed error.

    `urlsplit` rejects some malformed authorities itself -- `https://[::1/p`
    among them -- with a plain `ValueError` carrying no code. That refusal is
    correct but anonymous, so it is re-raised here as the same typed error the
    authority gate raises. Without this, one of the six malformed-authority
    vectors would "pass" on someone else's exception.
    """
    try:
        return urlsplit(url)
    except ValueError as exc:
        raise TargetUriMalformedError(url, f"the URL could not be parsed ({exc})") from exc


_DEFAULT_PORTS = {"http": 80, "https": 443}

# RFC 3986 §2.3 unreserved = ALPHA / DIGIT / "-" / "." / "_" / "~"
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _lookup(headers: Mapping[str, str], name_lower: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name_lower:
            return v
    return None


def malformed_authority_reason(authority: str) -> str | None:
    """Why *authority* is malformed per the profile's steps 2-3, or `None`.

    A reason rather than a bool, so every caller's rejection names the rule that
    fired. *authority* is the raw netloc as received -- from a URL's `netloc`
    here, from the as-received `Host` header at the verifier boundary; the rule
    is identical on both and must not be written twice.

    Note what is deliberately absent: a raw-non-ASCII-host rejection. This
    module is shared by the signer and the verifier, and the signer is required
    to CONVERT a U-label to its A-label form, not refuse it (see the
    `idn-to-punycode` vector). Rejecting a U-label received on the wire is the
    verifier's header precheck, which raises a different code at an earlier
    step; it shares `host_has_raw_non_ascii` with this module rather than
    re-deriving the test.
    """
    host = authority.rsplit("@", 1)[-1]  # step 3: strip userinfo before judging the host
    if not host:
        return "the authority carries no host (empty, or userinfo/port with nothing before it)"
    if host.startswith("["):
        return _bracketed_host_reason(host)
    if host.count(":") > 1:
        return "an IPv6 address outside brackets is ambiguous with a port and is malformed"
    if not host.split(":", 1)[0]:
        return "the authority carries a port but no host"
    return None


def _bracketed_host_reason(host: str) -> str | None:
    """Step 2's two IPv6-literal rejections."""
    end = host.find("]")
    if end < 0:
        return "a bracketed IPv6 host missing its closing bracket is malformed"
    if "%" in host[1:end]:
        return (
            "an IPv6 zone identifier (RFC 6874) is node-local "
            "and MUST be rejected in signed URLs"
        )
    return None


def _canon_authority(netloc: str, scheme: str) -> str:
    reason = malformed_authority_reason(netloc)
    if reason is not None:
        raise TargetUriMalformedError(netloc, reason)
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    host: str
    port: int | None = None
    if netloc.startswith("["):
        end = netloc.find("]")
        if end < 0:  # pragma: no cover - malformed_authority_reason rejects this first
            raise TargetUriMalformedError(
                netloc, "a bracketed IPv6 host missing its closing bracket"
            )
        host = netloc[: end + 1]
        tail = netloc[end + 1 :]
        if tail.startswith(":"):
            port = _port_or_reject(tail[1:], netloc)
    elif ":" in netloc:
        host, portstr = netloc.rsplit(":", 1)
        port = _port_or_reject(portstr, netloc)
    else:
        host = netloc
    host = _canon_host(host, netloc)
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        return f"{host}:{port}"
    return host


_ASCII_DIGITS = frozenset("0123456789")


def _port_or_reject(portstr: str, netloc: str) -> int | None:
    """Parse a port per RFC 3986 §3.2.3 (`port = *DIGIT`), or reject.

    The port used to go straight into `int()`, which is far more permissive
    than the grammar and produced three distinct problems:

    * `int("-80")` gave the authority `host:-80`, which is not an authority.
    * `int("8_0")` is 80 -- Python accepts underscore digit separators.
    * `int("٨٠")` is also 80 -- `int()` accepts non-ASCII digits, so
      `host:٨٠` and `host:80` collapsed to the SAME canonical authority. A
      peer that does not fold Arabic-Indic digits derives a different
      `@authority` from identical bytes, and the signature fails for a reason
      neither side can see in its own logs.

    `str.isdigit()` does not close that last one -- `"٨٠".isdigit()` is True --
    so the test is ASCII digits specifically.

    An EMPTY port is legal and means "default": the grammar is `*DIGIT`, and
    §3.2.3 says a normalizer should drop the port and its colon when empty. So
    `https://host:/p` normalizes to `host` rather than being rejected.
    """
    if not portstr:
        return None
    if not all(ch in _ASCII_DIGITS for ch in portstr):
        raise TargetUriMalformedError(
            netloc, f"the port {portstr!r} is not a sequence of ASCII digits"
        )
    return int(portstr)


def _canon_host(host: str, netloc: str) -> str:
    """Lower-case an ASCII host, or convert a U-label to its A-label form.

    The trailing FQDN-root dot is stripped BEFORE the branch, not inside it.
    `canonicalize_host` strips it as part of UTS-46 preparation while a bare
    `.lower()` keeps it, so branching first would make `example.com.` and
    `bücher.example.` normalize differently -- a signer and a verifier reading
    the same host in two spellings would derive two authorities and every
    signature between them would fail. Stripping once, up front, is the only
    place the two branches can be made to agree without re-implementing the
    helper here and becoming another host normalizer in a tree that already has
    six.

    The ASCII fast path is load-bearing, not an optimization: `canonicalize_host`
    strips IPv6 brackets and raises on underscore hosts and over-long labels,
    all of which sign fine today.
    """
    if host.endswith("."):
        host = host[:-1]
    if not host or any(label == "" for label in host.split(".")):
        # Re-checked AFTER the strip. `malformed_authority_reason` runs on the
        # raw netloc, where `.` and `..` are non-empty and look like hosts; it
        # is only stripping the root dot that empties them. Without this,
        # `https://./p` canonicalized to `https:///p` -- the empty authority
        # this very module rejects two functions up, reached by a path that
        # skipped the check.
        raise TargetUriMalformedError(netloc, "the authority carries no host once normalized")
    if not host_has_raw_non_ascii(host):
        return host.lower()
    try:
        return canonicalize_host(host)
    except (idna.IDNAError, UnicodeError) as exc:
        # Fail closed. A signature base must never be computed over a host we
        # could not canonicalize: the alternative is signing bytes the peer will
        # derive differently. The permissive fallback used by some sibling
        # callers is right for comparison paths, where raising would turn a
        # mismatch into an outage; it is wrong here.
        raise TargetUriMalformedError(netloc, f"the host is not a valid IDNA name ({exc})") from exc


def _normalize_path(path: str) -> str:
    return _normalize_pct(_remove_dot_segments(path))


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 §5.2.4 remove_dot_segments."""
    input_buf = path
    output = ""
    while input_buf:
        if input_buf.startswith("../"):
            input_buf = input_buf[3:]
        elif input_buf.startswith("./"):
            input_buf = input_buf[2:]
        elif input_buf.startswith("/./"):
            input_buf = "/" + input_buf[3:]
        elif input_buf == "/.":
            input_buf = "/"
        elif input_buf.startswith("/../"):
            input_buf = "/" + input_buf[4:]
            slash = output.rfind("/")
            output = output[:slash] if slash >= 0 else ""
        elif input_buf == "/..":
            input_buf = "/"
            slash = output.rfind("/")
            output = output[:slash] if slash >= 0 else ""
        elif input_buf in (".", ".."):
            input_buf = ""
        else:
            if input_buf.startswith("/"):
                next_slash = input_buf.find("/", 1)
            else:
                next_slash = input_buf.find("/")
            if next_slash < 0:
                output += input_buf
                input_buf = ""
            else:
                output += input_buf[:next_slash]
                input_buf = input_buf[next_slash:]
    return output


def _normalize_pct(s: str) -> str:
    """Uppercase %XX hex and decode percent-encoded unreserved chars."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "%" and i + 2 < n:
            hex2 = s[i + 1 : i + 3]
            try:
                b = int(hex2, 16)
            except ValueError:
                out.append(c)
                i += 1
                continue
            ch = chr(b)
            if ch in _UNRESERVED:
                out.append(ch)
            else:
                out.append("%" + hex2.upper())
            i += 3
        else:
            out.append(c)
            i += 1
    return "".join(out)
