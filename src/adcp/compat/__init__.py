"""AdCP wire-shape compatibility for buyers on older spec versions.

The framework natively validates against the SDK's pinned major (3.x).
Buyers on pre-3 wire shapes are handled by the per-tool adapter registry
in :mod:`adcp.compat.legacy` — see that module's docstring for the
``AdapterPair`` pattern and the JS-SDK parity notes.
"""
