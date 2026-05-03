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

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit


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
    """

    rewrite_to: str = "host.docker.internal"

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
