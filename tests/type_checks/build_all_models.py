"""Public freeze-time setup is a typed helper without a test-only runtime dep."""

from typing_extensions import assert_type

from adcp.testing import build_all_models

assert_type(build_all_models(), None)
