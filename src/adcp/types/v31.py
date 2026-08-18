"""Public AdCP 3.1 request/response models."""

from adcp.types.versioned import versioned_surface

__getattr__, __dir__, __all__ = versioned_surface("3.1", __name__)
