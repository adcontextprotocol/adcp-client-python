"""Canary test for the (a2a-sdk, protobuf) dependency-resolution matrix.

Background
----------
``a2a-sdk`` decorates ``on_message_send`` with
``@validate_proto_required_fields``, which walks the request message's
``DESCRIPTOR.fields`` and reads one attribute on each ``FieldDescriptor``
to decide whether the field is repeated. The attribute name has changed
across upstream releases:

- a2a-sdk **1.0.1** reads ``field.is_repeated``
- a2a-sdk **1.0.2** reads ``field.label`` (compared against ``LABEL_REPEATED``)

With the default protobuf C-extension backend (``google._upb._message``),
the upb ``FieldDescriptor`` exposes a *different subset* of these
attributes depending on the protobuf release:

================  ================  ============
protobuf release  ``is_repeated``   ``label``
================  ================  ============
5.x               **missing**       present
6.x               present           present
7.x               present           **missing**
================  ================  ============

So the "right" a2a-sdk version depends on the protobuf wheel pip
resolves, and a single bad cell in the cross product breaks every
``message/send`` JSON-RPC call before any handler runs.

This module is the canary. It fails fast — at the next ``pytest`` run
after a dep upgrade — when the resolved combo lands on a broken cell,
so the regression surfaces in CI rather than at runtime in an adopter's
deployment.

Both project-level pins (``a2a-sdk>=1.0.1,<1.0.2`` and ``protobuf>=6,<8``
in ``pyproject.toml``) keep us in the working column. Loosen either
without re-running this test at your own peril.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any

from a2a import types as pb


def _resolved_a2a_sdk_version() -> str:
    return importlib.metadata.version("a2a-sdk")


def _resolved_protobuf_version() -> str:
    return importlib.metadata.version("protobuf")


def _real_field_descriptor() -> Any:
    """Return a FieldDescriptor instance from a real a2a proto message.

    The class-level ``dir(FieldDescriptor)`` view does not match what's
    available on instance attribute access for the upb extension — the
    descriptor proxy advertises attribute names that raise
    ``AttributeError`` when actually read. So we must probe a real
    instance, not the class. Annotated as ``Any`` because protobuf's
    type stubs declare a union of the pure-Python and upb FieldDescriptor
    types and we don't care which we get — only what attributes are
    readable on it.
    """
    return pb.Message.DESCRIPTOR.fields[0]


def test_a2a_sdk_required_field_attribute_is_readable_on_resolved_protobuf() -> None:
    """The attribute ``a2a-sdk`` reads inside its required-field validator
    must be readable on the upb FieldDescriptor of the resolved protobuf
    wheel. If it isn't, every ``message/send`` will 500 with an
    ``AttributeError`` at request validation time.

    Pinned to ``a2a-sdk>=1.0.1,<1.0.2`` ⇒ the attribute is
    ``is_repeated``. If the pin moves, update this test in the same PR.
    """
    fd = _real_field_descriptor()
    sdk_version = _resolved_a2a_sdk_version()

    if sdk_version.startswith("1.0.1"):
        attr = "is_repeated"
    elif sdk_version.startswith("1.0.2"):
        attr = "label"
    else:
        # New a2a-sdk version arrived without a deliberate review here.
        # Force the contributor bumping the pin to also revisit this test
        # so we don't silently skip the canary.
        raise AssertionError(
            f"Unrecognised a2a-sdk version {sdk_version!r}. Update "
            f"tests/test_a2a_protobuf_compat.py to map this version to "
            f"the FieldDescriptor attribute the new release reads inside "
            f"@validate_proto_required_fields, then re-run."
        )

    try:
        getattr(fd, attr)
    except AttributeError as exc:
        raise AssertionError(
            f"a2a-sdk {sdk_version} reads FieldDescriptor.{attr} but "
            f"protobuf {_resolved_protobuf_version()} upb does not "
            f"expose it (descriptor type "
            f"{type(fd).__module__}.{type(fd).__name__}). Every "
            f"message/send JSON-RPC call will fail. Adjust the protobuf "
            f"or a2a-sdk pin in pyproject.toml — see the matrix at the "
            f"top of this file."
        ) from exc


def test_protobuf_floor_blocks_known_broken_5x_cell() -> None:
    """The floor pin in pyproject.toml (``protobuf>=6``) must keep us out
    of the 5.x cell where a2a-sdk 1.0.1 breaks. This is a sanity check
    that the resolved environment honours the floor."""
    version = _resolved_protobuf_version()
    major = int(version.split(".", 1)[0])
    assert major >= 6, (
        f"protobuf {version} is below the 6.x floor. a2a-sdk 1.0.1's "
        f"@validate_proto_required_fields reads is_repeated, which is "
        f"missing on protobuf 5.x upb FieldDescriptor — message/send "
        f"will fail. Honour the pyproject.toml pin."
    )
