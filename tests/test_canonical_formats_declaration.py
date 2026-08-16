"""Wire-faithful behaviour of the hand-rolled ``ProductFormatDeclaration``.

Covers the contracts the upstream schema enforces that codegen drops:

* ``params`` required (``required: ["format_kind", "params"]``).
* ``canonical_formats_only`` mutually exclusive with ``v1_format_ref[]``
  (``allOf.not`` clause in the schema).
* Credential-shaped keys in ``params`` or model extras are rejected at
  construction (parallels the dispatcher's ``ctx_metadata`` gate).
* ``params_as`` validates the open dict against a typed canonical class.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from adcp.types import (
    CanonicalFormatImage,
    CanonicalFormatKind,
    CanonicalFormatVastVideo,
    ProductFormatDeclaration,
)
from adcp.types.legacy import LegacyFormatId as FormatId


def _ref(id_: str = "display_300x250_image") -> FormatId:
    return FormatId(agent_url="https://creative.adcontextprotocol.org", id=id_)


# ---------------------------------------------------------------------------
# params required
# ---------------------------------------------------------------------------


def test_params_is_required() -> None:
    """Schema declares ``required: ["format_kind", "params"]``."""
    with pytest.raises(ValidationError) as exc:
        ProductFormatDeclaration(format_kind=CanonicalFormatKind.image)  # type: ignore[call-arg]

    msgs = str(exc.value)
    assert "params" in msgs


# ---------------------------------------------------------------------------
# canonical_formats_only ⊥ v1_format_ref (allOf.not)
# ---------------------------------------------------------------------------


def test_canonical_formats_only_excludes_v1_format_ref() -> None:
    """The schema's ``allOf.not`` clause forbids the combination at the wire level."""
    with pytest.raises(ValidationError) as exc:
        ProductFormatDeclaration(
            format_kind=CanonicalFormatKind.image,
            params={},
            canonical_formats_only=True,
            v1_format_ref=[_ref()],
        )

    msg = str(exc.value)
    assert "mutually exclusive" in msg


def test_canonical_formats_only_alone_is_accepted() -> None:
    """The exclusion fires only on the combination — either alone is fine."""
    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={},
        canonical_formats_only=True,
    )
    assert decl.canonical_formats_only is True
    assert decl.legacy_format_refs == ()


def test_v1_format_ref_alone_is_accepted() -> None:
    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={},
        v1_format_ref=[_ref()],
    )
    assert decl.canonical_formats_only is None
    assert decl.legacy_format_refs == (_ref(),)


# ---------------------------------------------------------------------------
# Credential-shaped key guard (parallels ctx_metadata gate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "credential_key",
    [
        "api_token",
        "bearer_token",
        "upstream.api_key",
        "x_apikey",
        "user_credential",
        "OAuthBearer",
    ],
)
def test_credential_shaped_keys_in_params_are_rejected(credential_key: str) -> None:
    """``params`` is open; credential-shaped keys would round-trip to buyers."""
    with pytest.raises(ValidationError) as exc:
        ProductFormatDeclaration(
            format_kind=CanonicalFormatKind.image,
            params={credential_key: "sekret"},
        )
    assert "credential-shaped" in str(exc.value)


def test_credential_shaped_keys_in_nested_params_are_rejected() -> None:
    """Walks the params dict recursively — nested credentials are caught."""
    with pytest.raises(ValidationError) as exc:
        ProductFormatDeclaration(
            format_kind=CanonicalFormatKind.image,
            params={"vendor": {"upstream": {"api_token": "sekret"}}},
        )
    assert "credential-shaped" in str(exc.value)


def test_credential_shaped_keys_in_extras_are_rejected() -> None:
    """``extra='allow'`` opens a second credential-stuffing surface; gated too."""
    with pytest.raises(ValidationError) as exc:
        ProductFormatDeclaration(
            format_kind=CanonicalFormatKind.image,
            params={},
            api_key="sekret",  # type: ignore[call-arg]
        )
    assert "credential-shaped" in str(exc.value)


def test_non_credential_extras_pass_through() -> None:
    """Forward-compat: unknown non-credential extras are preserved."""
    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={},
        correlation_id="trace_xyz",  # type: ignore[call-arg]
        future_field="value",  # type: ignore[call-arg]
    )
    dumped = decl.model_dump()
    assert dumped["correlation_id"] == "trace_xyz"
    assert dumped["future_field"] == "value"


# ---------------------------------------------------------------------------
# params_as
# ---------------------------------------------------------------------------


def test_params_as_validates_image_canonical() -> None:
    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={
            "sizes": [{"width": 300, "height": 250}],
            "asset_source": "buyer_uploaded",
        },
    )

    typed = decl.params_as(CanonicalFormatImage)

    assert isinstance(typed, CanonicalFormatImage)
    assert typed.sizes[0].width == 300
    assert typed.sizes[0].height == 250


def test_params_as_raises_on_invalid_params_shape() -> None:
    """params_as is a validate, not a cast — type-incorrect input raises."""
    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        # ``sizes`` MUST be a list of {width, height} objects per the schema;
        # passing a scalar is a wire-shape violation.
        params={"sizes": 12345},
    )

    with pytest.raises(ValidationError):
        decl.params_as(CanonicalFormatImage)


def test_params_as_returns_typed_vast_video() -> None:
    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.video_vast,
        params={"vast_version": "4.2"},
    )

    typed = decl.params_as(CanonicalFormatVastVideo)

    assert isinstance(typed, CanonicalFormatVastVideo)
    assert typed.vast_version.value == "4.2"
