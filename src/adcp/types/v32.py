"""Public AdCP 3.2 beta request/response models."""

from adcp.types.versioned import versioned_surface

__getattr__, __dir__, __all__ = versioned_surface("3.2-beta.0", __name__)
