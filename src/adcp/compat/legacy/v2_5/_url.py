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
_STANDARD_BRAND_MANIFEST_PATHS = {"", "/", "/.well-known/brand.json"}

# Per-URL dedup so high-RPS v2.5 buyers don't saturate the log pipeline
# when many requests carry the same non-canonical manifest URL.
_brand_manifest_path_warned: set[str] = set()


def strip_url_scheme(url: str) -> str:
    """``https://acme.example.com/`` → ``acme.example.com``.

    Tolerates missing scheme (returns the input domain-shaped string
    after trailing-slash strip), ``http://`` schemes (legacy clients
    don't all enforce https), and trailing slashes from sloppy
    concatenation.

    Does NOT strip URL paths or ports.  Use ``extract_brand_domain``
    when the input may be a full URL (scheme + path) and you need only
    the hostname component.
    """
    s = url.strip()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    return s.rstrip("/")


def extract_brand_domain(url: str) -> str:
    """Extract a ``BrandReference.domain``-safe hostname from a brand_manifest URL.

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
      ``urlparse`` returns ``hostname=None``; falls back to
      ``strip_url_scheme`` which returns the input unchanged after
      stripping any trailing slashes.
    * IPv6 literal (``https://[::1]/path``):
      ``urlparse`` returns ``hostname="::1"`` (brackets stripped).
      This value does not satisfy ``BrandReference.domain``'s regex;
      callers must validate the result if IPv6 addresses are possible.

    The caller is responsible for ensuring ``url`` is non-empty before
    calling (both adapter call sites already gate on
    ``isinstance(manifest, str) and manifest``).
    """
    s = url.strip()
    # urlparse correctly handles full URLs: extracts hostname, drops path and port.
    # For bare domains (no scheme), urlparse treats the input as a path-only URI
    # and returns hostname=None, so we fall back to strip_url_scheme.
    hostname = urlparse(s).hostname
    return hostname if hostname is not None else strip_url_scheme(s)


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

    Dedup is per-URL across the process lifetime; the cache is
    intentionally unbounded since per-deployment cardinality of distinct
    ``brand_manifest`` URLs is small.
    """
    parsed = urlparse(manifest_url.strip())
    if parsed.hostname is None:
        return
    if parsed.path in _STANDARD_BRAND_MANIFEST_PATHS:
        return
    if manifest_url in _brand_manifest_path_warned:
        return
    _brand_manifest_path_warned.add(manifest_url)
    _logger.warning(
        "brand_manifest at %s uses a non-standard path; "
        "v3 sellers derive %s/.well-known/brand.json from BrandReference.domain. "
        "Manifest fetch may 404.",
        manifest_url,
        domain,
    )
