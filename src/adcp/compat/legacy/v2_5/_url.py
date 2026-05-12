"""Shared URL helpers for the v2.5 adapter modules.

Several v2.5 → v3 translations need to convert v2.5 URL-string fields
(``brand_manifest``) into v3 bare-domain references (``brand.domain``).
The helper lives here so ``get_products`` and ``create_media_buy`` can
import the same canonical implementation.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)

# Paths that v3 sellers reconstruct correctly from ``brand.domain`` — no
# information is lost when the adapter flattens these to the hostname.
# Compared after a single trailing slash is stripped from the input path,
# so ``/.well-known/brand.json/`` is treated the same as the canonical form.
_STANDARD_BRAND_MANIFEST_PATHS = {"", "/.well-known/brand.json"}

# Per-URL dedup so high-RPS v2.5 buyers don't saturate the log pipeline
# when many requests carry the same non-canonical manifest URL. The key is
# normalized via ``urlparse`` (scheme + netloc + path-stripped-of-trailing-slash)
# so cache-buster query strings (``?v=1``, ``?v=2`` …) collapse to one entry.
_brand_manifest_path_warned: set[tuple[str, str, str]] = set()

# Cap on the dedup-set size. Distinct ``brand_manifest`` URLs per deployment
# are typically a handful (one per buyer), so 1024 is well above realistic
# cardinality. The cap is defense-in-depth against pathological inputs that
# defeat key normalization. On overflow the set is cleared and warnings
# resume from scratch — the same offending URL may warn again, but memory
# stays bounded.
_BRAND_MANIFEST_PATH_WARNED_CAP = 1024


def _bare_domain(s: str) -> str:
    """``https://acme.example.com/`` → ``acme.example.com``.

    Strips an ``http://`` or ``https://`` prefix and trailing slashes from
    a string that ``urlparse`` could not interpret as a full URL. Used as
    the fallback inside :func:`extract_brand_domain` for inputs that have
    no scheme (and therefore no parseable hostname).
    """
    s = s.strip()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    return s.rstrip("/")


def extract_brand_domain(url: str) -> str:
    """Extract a ``BrandReference.domain``-candidate hostname from a brand_manifest URL.

    v2.5 ``brand_manifest`` is documented as a URL to a JSON file
    (e.g. ``"https://acme.com/.well-known/brand.json"``).  The v3
    ``BrandReference.domain`` field requires a bare hostname matching
    ``^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$``.

    Behaviour by input shape:

    * Full URL with scheme (``https://acme.com/path``):
      ``urlparse`` extracts the hostname (``"acme.com"``); path and
      port are discarded.  Uppercase schemes are normalised by
      ``urlparse``; uppercase hostnames are lowercased by ``urlparse``
      (e.g. ``HTTPS://ACME.COM/path`` → ``"acme.com"``).
    * URL with port (``https://acme.com:8443/path``):
      hostname is extracted without the port (``"acme.com"``).
    * Bare domain without scheme (``acme.com``):
      ``urlparse`` returns ``hostname=None``; the fallback strips any
      scheme/trailing-slash and returns the input unchanged.
    * IPv6 literal (``https://[::1]/path``):
      ``urlparse`` returns ``hostname="::1"`` (brackets stripped).
      The returned value does not satisfy ``BrandReference.domain``'s
      regex — v3 Pydantic validation will reject it downstream with a
      clear pattern-mismatch error. This helper does not pre-validate;
      callers who want to filter out non-DNS hostnames before assigning
      ``brand.domain`` should regex-check the result.

    The caller is responsible for ensuring ``url`` is non-empty before
    calling (both adapter call sites already gate on
    ``isinstance(manifest, str) and manifest``).
    """
    s = url.strip()
    # urlparse correctly handles full URLs: extracts hostname, drops path and port.
    # For bare domains (no scheme), urlparse treats the input as a path-only URI
    # and returns hostname=None, so we fall back to ``_bare_domain``.
    hostname = urlparse(s).hostname
    return hostname if hostname is not None else _bare_domain(s)


def warn_brand_manifest_path_lossy(manifest_url: str, domain: str) -> None:
    """Emit a one-time WARNING if ``manifest_url`` has a path v3 sellers
    cannot reconstruct from ``BrandReference.domain``.

    v3 sellers derive the canonical manifest URL as
    ``https://{domain}/.well-known/brand.json``. When the v2.5 buyer's
    original ``brand_manifest`` URL pointed at a different path (e.g. a
    CDN-hosted manifest), translating to ``brand.domain`` silently drops
    that path and the v3 seller's fetch will likely 404.

    The adapter cannot avoid this — v3 ``BrandReference`` is hostname-only
    by schema (``additionalProperties: false``). The warning surfaces the
    lossy mapping to operators so debugging downstream 404s doesn't start
    from "no signal in the SDK".

    Inputs with no derivable hostname (bare-domain strings, blank URLs)
    are ignored — those don't represent a path mapping at all.

    Dedup is per ``(scheme, netloc, path)`` tuple — query strings and
    fragments are excluded so cache-buster variants of the same URL share
    a single dedup slot. The set is capped at
    :data:`_BRAND_MANIFEST_PATH_WARNED_CAP`; on overflow it clears entirely
    and warnings resume from scratch (memory stays bounded; the same
    offending URL may warn again later).
    """
    parsed = urlparse(manifest_url.strip())
    if parsed.hostname is None:
        return
    normalized_path = parsed.path.rstrip("/")
    if normalized_path in _STANDARD_BRAND_MANIFEST_PATHS:
        return
    key = (parsed.scheme, parsed.netloc, normalized_path)
    if key in _brand_manifest_path_warned:
        return
    if len(_brand_manifest_path_warned) >= _BRAND_MANIFEST_PATH_WARNED_CAP:
        _brand_manifest_path_warned.clear()
    _brand_manifest_path_warned.add(key)
    _logger.warning(
        "brand_manifest at %s uses a non-standard path; "
        "v3 sellers derive %s/.well-known/brand.json from BrandReference.domain. "
        "Manifest fetch may 404.",
        manifest_url,
        domain,
    )
