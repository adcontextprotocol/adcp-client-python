"""Python-version-compatible StrEnum export for generated schema enums."""

from __future__ import annotations

import sys
from enum import Enum

if sys.version_info >= (3, 11):
    from enum import StrEnum as StrEnum
else:

    class StrEnum(str, Enum):
        """Backport the stdlib StrEnum behavior needed by generated enums."""

        def __str__(self) -> str:
            return str.__str__(self)

        def __format__(self, format_spec: str) -> str:
            return str.__format__(self, format_spec)
