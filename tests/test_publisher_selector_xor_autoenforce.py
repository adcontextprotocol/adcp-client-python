"""Auto-enforcement of the PublisherPropertySelector XOR constraint at Pydantic parse time.

Closes adcp-client-python#759. The patch in ``adcp.types.aliases`` uses
``pydantic._internal._decorators`` (private API but stable across
Pydantic 2.x point releases) to attach a ``model_validator(mode='after')``
to the generated selector classes. The first three tests verify the
behavior; the last test is a **drift sentinel** that fails loudly if
Pydantic's internal decorator registration ever changes shape so the
issue surfaces in CI rather than as silent validation regressions.

Upstream 3.0.10+ dropped the ``publisher_domains[]`` compact form from
``core/publisher-property-selector.json`` (see issue #771). The generated
Pydantic models no longer carry the field, so the four ``compact_form``
/ ``bare_construct`` cases here can't exercise the XOR path — the
field-required check on ``publisher_domain`` dominates. The SDK-side
dict-layer enforcement in ``adcp.validation.legacy.validate_publisher_properties_item``
still implements the contract for adopters consuming raw adagents.json
bytes (see ``tests/test_adagents.py::TestPublisherDomainsCompactForm``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

_COMPACT_FORM_DROPPED = pytest.mark.skip(
    reason=(
        "Upstream 3.0.10+ dropped publisher_domains[] from the generated "
        "selector schema (issue #771). The Pydantic-layer XOR auto-enforce "
        "patch has nothing to fire on; SDK-layer enforcement lives in "
        "adcp.validation.legacy.validate_publisher_properties_item."
    )
)


class TestSelectorXorAutoEnforce:
    """Direct construction of an XOR-violating selector must fail."""

    @_COMPACT_FORM_DROPPED
    def test_selector1_rejects_bare_construct(self):
        # selection_type='all' with neither publisher_domain nor publisher_domains
        from adcp.types.generated_poc.core.publisher_property_selector import (
            PublisherPropertySelector1,
        )

        with pytest.raises(ValidationError, match="exactly one"):
            PublisherPropertySelector1(selection_type="all")

    def test_selector1_rejects_both_publisher_fields(self):
        from adcp.types.generated_poc.core.publisher_property_selector import (
            PublisherPropertySelector1,
        )

        with pytest.raises(ValidationError, match="mutually exclusive"):
            PublisherPropertySelector1(
                selection_type="all",
                publisher_domain="cnn.com",
                publisher_domains=["espn.com"],
            )

    def test_selector1_accepts_singular_form(self):
        from adcp.types.generated_poc.core.publisher_property_selector import (
            PublisherPropertySelector1,
        )

        s = PublisherPropertySelector1(selection_type="all", publisher_domain="cnn.com")
        assert s.publisher_domain == "cnn.com"

    @_COMPACT_FORM_DROPPED
    def test_selector1_accepts_compact_form(self):
        from adcp.types.generated_poc.core.publisher_property_selector import (
            PublisherPropertySelector1,
        )

        s = PublisherPropertySelector1(
            selection_type="all", publisher_domains=["a.example", "b.example"]
        )
        assert s.publisher_domains is not None
        assert [str(d.root) for d in s.publisher_domains] == ["a.example", "b.example"]

    @_COMPACT_FORM_DROPPED
    def test_selector3_rejects_bare_construct(self):
        from adcp.types.generated_poc.core.publisher_property_selector import (
            PublisherPropertySelector3,
        )

        with pytest.raises(ValidationError, match="exactly one"):
            PublisherPropertySelector3(selection_type="by_tag", property_tags=["ctv"])

    @_COMPACT_FORM_DROPPED
    def test_selector3_accepts_compact_form_with_required_tags(self):
        from adcp.types.generated_poc.core.publisher_property_selector import (
            PublisherPropertySelector3,
        )

        s = PublisherPropertySelector3(
            selection_type="by_tag",
            property_tags=["ctv"],
            publisher_domains=["a.example", "b.example"],
        )
        assert s.publisher_domains is not None
        assert [str(d.root) for d in s.publisher_domains] == ["a.example", "b.example"]

    def test_selector2_unpatched_passes_valid_input(self):
        # by_id selector has no XOR — only publisher_domain is allowed,
        # publisher_domains is rejected at the JSON-schema level. The
        # auto-enforce patch correctly leaves this class alone.
        from adcp.types.generated_poc.core.publisher_property_selector import (
            PublisherPropertySelector2,
        )

        s = PublisherPropertySelector2(
            selection_type="by_id",
            property_ids=["p1"],
            publisher_domain="cnn.com",
        )
        assert s.publisher_domain == "cnn.com"


class TestPydanticInternalApiDriftSentinel:
    """If Pydantic ever changes the shape of ``_internal._decorators``
    the patch in ``adcp.types.aliases`` silently no-ops and selector
    validation regresses. This test imports the same private surface
    the patch uses and verifies the API still exists. CI failure here
    is the canary for "rework the patch or pin Pydantic".
    """

    def test_decorator_class_present(self):
        from pydantic._internal._decorators import Decorator  # noqa: F401

    def test_model_validator_decorator_info_present(self):
        from pydantic._internal._decorators import (  # noqa: F401
            ModelValidatorDecoratorInfo,
        )

    def test_selector1_has_registered_validator(self):
        # The patch lands at module-import time. If the registration
        # shape changes and the patch silently no-ops, this catches it.
        from adcp.types.generated_poc.core.publisher_property_selector import (
            PublisherPropertySelector1,
        )

        validators = PublisherPropertySelector1.__pydantic_decorators__.model_validators
        assert "_selector_xor_validate" in validators, (
            "XOR auto-enforce validator missing from PublisherPropertySelector1 — "
            "patch in adcp.types.aliases may have silently failed."
        )
