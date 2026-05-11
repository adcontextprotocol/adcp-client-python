"""Shared URL helpers for the v2.5 adapter modules.

Several v2.5 → v3 translations need to convert v2.5 URL-string fields
(``brand_manifest``) into v3 bare-domain references (``brand.domain``).
The helper lives here so ``get_products`` and ``create_media_buy`` can
import the same canonical implementation.
"""

from __future__ import annotations


def strip_url_scheme(url: str) -> str:
    """``https://acme.example.com/`` → ``acme.example.com``.

    Tolerates missing scheme (returns the input domain-shaped string
    after trailing-slash strip), ``http://`` schemes (legacy clients
    don't all enforce https), and trailing slashes from sloppy
    concatenation.
    """
    s = url.strip()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    return s.rstrip("/")
