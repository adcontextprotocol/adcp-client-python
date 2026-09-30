"""Resolve an inbound request's signing ``keyid`` to a buyer agent and its key.

The RFC 9421 verifier checks a signature against a JWK, but a ``keyid`` alone
does not say *which buyer agent* signed. A seller that turns a verified
signature into a caller identity needs that mapping explicitly — it is what
fills :attr:`adcp.signing.VerifiedSigner.agent_url`, which in turn is the key
:class:`adcp.decisioning.BuyerAgentRegistry` dispatches on.

A :class:`SignerKeyResolver` is that mapping. Two implementations ship:

* :class:`StaticSignerKeys` — agent URLs mapped to inline JWKS documents.
  Suits pilots, tests, and sellers who onboard buyer keys out of band.
* :class:`JwksUriSignerKeys` — agent URLs mapped to published ``jwks_uri``
  endpoints, fetched and cached asynchronously.

Neither discovers an *unknown* signer; each maps only agents the seller
configured. Resolving a stranger's agent URL from its key is an open spec
question (adcontextprotocol/adcp#7814).

A resolver runs before the signature is checked, on attacker-chosen
``keyid`` values, so implementations must be cheap on a miss — cache, and
never fetch per unknown ``keyid`` without a cooldown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from adcp.signing.errors import SignatureVerificationError
from adcp.signing.jwks import (
    DEFAULT_JWKS_COOLDOWN_SECONDS,
    AsyncCachingJwksResolver,
    AsyncJwksFetcher,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedSignerKey:
    """The buyer agent that owns a ``keyid``, and the public JWK to verify with."""

    agent_url: str
    jwk: Mapping[str, Any]


@runtime_checkable
class SignerKeyResolver(Protocol):
    """Map a request-signing ``keyid`` to its owning agent and public key.

    Return ``None`` when the ``keyid`` is unknown; the verifier then rejects
    the request with ``request_signature_key_unknown``. Raise
    :class:`SignatureVerificationError` (e.g. ``request_signature_jwks_unavailable``)
    for a known agent whose keys cannot be fetched.
    """

    async def __call__(self, keyid: str) -> ResolvedSignerKey | None: ...


def _jwks_keys(agent_url: str, jwks: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        raise ValueError(f"JWKS for {agent_url!r} has no 'keys' array")
    return [key for key in keys if isinstance(key, Mapping)]


class StaticSignerKeys:
    """Resolve ``keyid`` values against inline JWKS documents, keyed by agent URL.

    ::

        signer_keys = StaticSignerKeys({
            "https://buyer.example.com": {"keys": [buyer_public_jwk]},
        })

    A ``kid`` published by two agents is rejected at construction — the
    verifier could not tell which agent signed, so the ambiguity is a
    configuration error rather than something to resolve at request time.
    """

    def __init__(self, jwks_by_agent: Mapping[str, Mapping[str, Any]]) -> None:
        index: dict[str, ResolvedSignerKey] = {}
        for agent_url, jwks in jwks_by_agent.items():
            for key in _jwks_keys(agent_url, jwks):
                kid = key.get("kid")
                if not isinstance(kid, str) or not kid:
                    continue
                existing = index.get(kid)
                if existing is not None and existing.agent_url != agent_url:
                    raise ValueError(
                        f"kid {kid!r} is published by both {existing.agent_url!r} "
                        f"and {agent_url!r}; a keyid must identify one signing agent"
                    )
                index[kid] = ResolvedSignerKey(agent_url=agent_url, jwk=dict(key))
        self._index = index

    async def __call__(self, keyid: str) -> ResolvedSignerKey | None:
        return self._index.get(keyid)


class JwksUriSignerKeys:
    """Resolve ``keyid`` values against published JWKS endpoints, keyed by agent URL.

    ::

        signer_keys = JwksUriSignerKeys({
            "https://buyer.example.com": "https://buyer.example.com/.well-known/jwks.json",
        })

    Each endpoint gets its own :class:`AsyncCachingJwksResolver` and all are
    consulted concurrently, so one slow buyer endpoint does not serialize
    lookups for the others. An endpoint whose fetch fails is skipped for
    ``failure_cooldown_seconds`` rather than refetched on every request.

    A ``kid`` found under more than one agent resolves to ``None`` (unknown)
    and is logged — a configured buyer publishing another buyer's ``kid`` can
    therefore make that ``kid`` unusable; prefer :class:`StaticSignerKeys`
    where that matters. When no agent resolves the ``kid`` and some endpoint
    is failing or cooling down, the fetch error is raised instead of
    reporting the key as unknown.
    """

    def __init__(
        self,
        jwks_uri_by_agent: Mapping[str, str],
        *,
        fetcher: AsyncJwksFetcher | None = None,
        allow_private: bool = False,
        failure_cooldown_seconds: float = DEFAULT_JWKS_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolvers = {
            agent_url: AsyncCachingJwksResolver(
                jwks_uri, fetcher=fetcher, allow_private=allow_private
            )
            for agent_url, jwks_uri in jwks_uri_by_agent.items()
        }
        self._failure_cooldown = failure_cooldown_seconds
        self._clock = clock
        self._failures: dict[str, tuple[float, SignatureVerificationError]] = {}

    async def _lookup(
        self, agent_url: str, keyid: str
    ) -> ResolvedSignerKey | SignatureVerificationError | None:
        failure = self._failures.get(agent_url)
        if failure is not None and self._clock() - failure[0] < self._failure_cooldown:
            return failure[1]
        try:
            jwk = await self._resolvers[agent_url](keyid)
        except SignatureVerificationError as exc:
            self._failures[agent_url] = (self._clock(), exc)
            return exc
        self._failures.pop(agent_url, None)
        return ResolvedSignerKey(agent_url=agent_url, jwk=jwk) if jwk is not None else None

    async def __call__(self, keyid: str) -> ResolvedSignerKey | None:
        results = await asyncio.gather(*(self._lookup(agent, keyid) for agent in self._resolvers))
        matches = [result for result in results if isinstance(result, ResolvedSignerKey)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "request-signing keyid %r is published by %d agents; treating as unknown",
                keyid,
                len(matches),
            )
            return None
        failure = next(
            (result for result in results if isinstance(result, SignatureVerificationError)),
            None,
        )
        if failure is not None:
            raise failure
        return None


__all__ = [
    "JwksUriSignerKeys",
    "ResolvedSignerKey",
    "SignerKeyResolver",
    "StaticSignerKeys",
]
