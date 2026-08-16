"""Pre-SSRF URL rewrite hooks for :class:`adcp.webhook_sender.WebhookSender`.

The ``TransportHook`` Protocol lets adopters rewrite the destination URL
before SSRF validation runs. The canonical use case is a sender running
inside a Docker container that needs to deliver to host-side
``localhost`` — the OS-level hostname differs (``host.docker.internal``
on Docker Desktop, the bridge gateway on Linux).

Security boundary
-----------------

Hooks run BEFORE SSRF, but SSRF remains authoritative on the rewritten
URL. A hook returning a private-IP literal cannot bypass the range
check unless the sender is *separately* configured with
``allow_private_destinations=True`` — that flag is the operator's
explicit opt-in for private-destination delivery (test harnesses,
container-network deliveries to known internal services).

:class:`DockerLocalhostRewrite` enforces this contract by raising at
sender construction time if the sender does not have
``allow_private_destinations=True``. There is no scenario where the
rewrite is useful without that flag — rewriting ``localhost`` to
``host.docker.internal`` and then having SSRF reject the resolved
private IP would be a confusing failure mode. Surface it at config time.

Hooks should be hostname-only rewrites. The framework parses the URL,
exposes the hostname to the hook, and reassembles the URL preserving
scheme/path/port/query/fragment — a hook that returns a different
scheme or different port is rejected. This narrows the hook's authority
to the part the use case actually needs.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

#: Characters that, interpolated into a netloc, move the boundary between
#: authority / userinfo / port / path / query / fragment -- i.e. rewrite the
#: signed URL into a different URL. `%` covers RFC 6874 IPv6 zone IDs.
#: `:` is deliberately absent: IPv6 literals are made of them, so a stray port
#: is caught after the IP-literal parse instead.
_STRUCTURAL_CHARS = frozenset('/@?#\\[]%"<>^`{|}')


class TransportHook(Protocol):
    """Rewrite the destination URL before SSRF runs.

    Implementations return either ``None`` (no rewrite — pass through)
    or a new URL string. The framework validates that the new URL has
    the same scheme and port as the input, and reassembles
    path/query/fragment from the original; only the hostname is
    permitted to change.

    Hooks may be called many times per sender (once per delivery), so
    they should be cheap and side-effect-free.
    """

    def rewrite_url(self, url: str) -> str | None: ...


_LOCALHOST_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


@dataclass(frozen=True)
class DockerLocalhostRewrite:
    """Rewrite ``localhost`` / ``127.0.0.1`` / ``::1`` to a Docker-host alias.

    Activated by adopters running e2e tests against host-side services
    from inside a Docker container. The default ``host.docker.internal``
    works on Docker Desktop (Mac/Windows). On Linux, pass
    ``rewrite_to="172.17.0.1"`` (default bridge gateway) or
    ``rewrite_to="host.docker.internal"`` after adding
    ``--add-host=host.docker.internal:host-gateway`` to the container
    run.

    Construction-time validation: this hook is only meaningful when the
    sender has ``allow_private_destinations=True``. The construct
    method on the sender side checks the flag — a hook attached to a
    sender without it raises :class:`ValueError` so the misconfiguration
    surfaces at wiring time rather than at the first delivery.

    The check happens via :meth:`validate_for_sender`, called by
    :meth:`WebhookSender._from_strategy` (and ``__init__``) when
    ``transport_hooks`` is set.

    ``rewrite_to`` is validated and canonicalized at construction: it
    must be a hostname or IP literal, is lower-cased, and bare IPv6
    literals are bracketed automatically (``"::1"`` is stored as
    ``"[::1]"``) so the assembled authority is unambiguous with a port.
    IPv4-mapped IPv6 is re-formatted to its canonical compressed form
    (``"::ffff:127.0.0.1"`` becomes ``"[::ffff:7f00:1]"`` — the same
    address, spelled canonically). Non-ASCII hostnames are IDNA-encoded
    to A-labels. IPv6 zone IDs (``"fe80::1%eth0"``) are rejected: RFC
    6874 requires the ``%`` be percent-encoded inside a URI, and
    silently emitting an invalid authority is worse than failing at
    wiring time.

    The validation is deliberately **structural**, not a hostname-syntax
    check. ASCII names are accepted as-is once they cannot alter the
    URL's shape, because Docker Compose service names legally contain
    underscores (``my_service``, ``host_gateway``) which RFC 952/1123
    and IDNA both reject — and Docker's embedded DNS resolves them.
    Enforcing hostname syntax here would refuse the exact configuration
    this class exists to serve. Whether the name resolves is the
    resolver's business; whether it rewrites the signed URL is ours.
    """

    rewrite_to: str = "host.docker.internal"

    def __post_init__(self) -> None:
        # Deferred import: ``adcp.signing``'s package __init__ is an
        # order of magnitude heavier than this leaf module, and both
        # real consumers (webhook_sender, webhooks) already import it.
        # This runs once per hook construction, never per delivery.
        from adcp.signing._idna_canonicalize import canonicalize_host

        value = self.rewrite_to
        if not value:
            raise ValueError(
                "DockerLocalhostRewrite(rewrite_to=...) must be a hostname or IP "
                "literal; got an empty string"
            )

        # Accept an already-bracketed IPv6 literal by unwrapping it first;
        # the brackets are re-applied below from the canonical form.
        inner = value[1:-1] if value.startswith("[") and value.endswith("]") else value
        if not inner:
            # `"[]"` survives the non-empty check above and empties here. An
            # empty host is the one outcome this guard exists to prevent: it
            # assembles to `https://:9000/hook`, an authority with a port and
            # no host -- exactly the shape @target-uri canonicalization rejects.
            raise ValueError(
                f"DockerLocalhostRewrite(rewrite_to=...) must be a hostname or IP "
                f"literal; got {value!r}, which has no host"
            )

        # Structural rejection comes FIRST and is the actual point of this
        # guard: `rewrite_to` is interpolated straight into the netloc, so any
        # character that can move the boundary between authority, path, query,
        # fragment or userinfo rewrites the signed URL into a different URL.
        # `%` is here for RFC 6874 IPv6 zone IDs, which are not representable
        # in a URI authority without percent-encoding.
        bad = {ch for ch in inner if ch in _STRUCTURAL_CHARS or ord(ch) < 0x21 or ord(ch) == 0x7F}
        if bad:
            raise ValueError(
                f"DockerLocalhostRewrite(rewrite_to=...) must be a hostname or IP "
                f"literal; got {value!r}, which contains {sorted(bad)!r} and would "
                f"change the structure of the signed URL"
            )

        try:
            ip = ipaddress.ip_address(inner)
        except ValueError:
            pass
        else:
            # Bracket v6 so the assembled authority is unambiguous with a port
            # -- the defect this guard exists to close. `str(ip)` also folds the
            # literal to its canonical compressed form.
            canonical = f"[{ip}]" if ip.version == 6 else str(ip)
            object.__setattr__(self, "rewrite_to", canonical)
            return

        if ":" in inner:
            # Not an IP literal, so a colon is a port -- which `rewrite_url`
            # re-appends itself, and which `apply_hooks`' port guard would then
            # see as a port change.
            raise ValueError(
                f"DockerLocalhostRewrite(rewrite_to=...) must be a hostname or IP "
                f"literal without a port; got {value!r}"
            )

        if inner.isascii():
            # Deliberately NOT routed through `canonicalize_host`: this is a
            # Docker helper, and Docker Compose service names legally contain
            # underscores (`my_service`, `host_gateway`), which IDNA rejects
            # under RFC 952/1123. Docker's embedded DNS resolves them, so
            # refusing them here would break the case this class exists for.
            # Structural safety is already established above; anything further
            # is the resolver's business, not ours.
            # One trailing root dot, matching `canonicalize_host` -- `rstrip`
            # would eat every dot, so `"."` and `".."` normalized to the empty
            # host rather than being rejected.
            ascii_host = inner.lower()
            if ascii_host.endswith("."):
                ascii_host = ascii_host[:-1]
            if not ascii_host or any(label == "" for label in ascii_host.split(".")):
                # Catches `"."`, `".."` and `"a..b"`. An empty label is not a
                # host, and `".."` in particular survives a single-dot strip as
                # `"."` -- non-empty, but still no host.
                raise ValueError(
                    f"DockerLocalhostRewrite(rewrite_to=...) must be a hostname or IP "
                    f"literal; got {value!r}, which normalizes to an empty label"
                )
            object.__setattr__(self, "rewrite_to", ascii_host)
            return

        # Non-ASCII: convert to A-labels so the netloc is wire-legal. Failure
        # here is a genuinely unusable host, not a naming-convention quibble.
        try:
            canonical = canonicalize_host(inner)
        except (UnicodeError, ValueError) as exc:
            # ``idna.IDNAError`` subclasses ``UnicodeError``.
            raise ValueError(
                f"DockerLocalhostRewrite(rewrite_to=...) must be a hostname or IP "
                f"literal; got {value!r} ({exc})"
            ) from exc
        object.__setattr__(self, "rewrite_to", canonical)

    def rewrite_url(self, url: str) -> str | None:
        parsed = urlsplit(url)
        # ``hostname`` lower-cases and strips brackets from IPv6 — match
        # against both bare and bracketed forms above.
        host = (parsed.hostname or "").lower()
        if host not in _LOCALHOST_HOSTS:
            return None
        # Reassemble with the rewritten host, preserving port, path,
        # query, fragment. Userinfo (``user:pass@``) is intentionally
        # dropped — webhook URLs in AdCP do not carry credentials in the
        # URL, and ``_extract_config_fields`` rejects userinfo upstream.
        # If a future caller needs it, propagate ``parsed.username`` /
        # ``parsed.password`` here.
        netloc = self.rewrite_to
        if parsed.port is not None:
            netloc = f"{self.rewrite_to}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

    def validate_for_sender(self, *, allow_private_destinations: bool) -> None:
        """Reject misconfiguration at sender-construction time.

        Without ``allow_private_destinations=True``, SSRF would reject
        the post-rewrite URL — silently making this hook a no-op at
        best, confusing failure at worst. Raise.
        """
        if not allow_private_destinations:
            raise ValueError(
                "DockerLocalhostRewrite requires the sender to be constructed "
                "with allow_private_destinations=True. The hook rewrites "
                "localhost to a private-IP destination; SSRF would reject the "
                "rewritten URL otherwise. Pass allow_private_destinations=True "
                "to opt in explicitly, or remove the hook for production senders."
            )


def apply_hooks(url: str, hooks: tuple[TransportHook, ...]) -> str:
    """Run ``hooks`` against ``url`` in order, returning the (possibly rewritten) URL.

    Each hook receives the output of the previous one. Returning
    ``None`` means "no change" — the URL passes through unchanged.

    The framework validates that no hook changes scheme or port: the
    use case is hostname rewrite for container-network delivery, not
    arbitrary URL rewriting. A hook that needs scheme/port changes is
    out of scope and should fail loudly so we don't silently widen the
    hook's authority.
    """
    if not hooks:
        return url
    current = url
    for hook in hooks:
        rewritten = hook.rewrite_url(current)
        if rewritten is None:
            continue
        original = urlsplit(current)
        new = urlsplit(rewritten)
        if new.scheme != original.scheme:
            raise ValueError(
                f"transport hook {type(hook).__name__} attempted to change URL "
                f"scheme from {original.scheme!r} to {new.scheme!r}; hooks may "
                f"only rewrite hostname"
            )
        if new.port != original.port:
            raise ValueError(
                f"transport hook {type(hook).__name__} attempted to change URL "
                f"port from {original.port!r} to {new.port!r}; hooks may only "
                f"rewrite hostname"
            )
        current = rewritten
    return current


__all__ = [
    "DockerLocalhostRewrite",
    "TransportHook",
    "apply_hooks",
]
