"""Runtime contracts retained by post-generation compatibility fixes."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError


def test_product_signal_targeting_option_keeps_discriminated_signal_ref() -> None:
    from adcp import ProductSignalTargetingOption
    from adcp.types.generated_poc.core.signal_ref import SignalRef

    assert ProductSignalTargetingOption.model_fields["signal_ref"].annotation is SignalRef
    assert ProductSignalTargetingOption.model_json_schema()["properties"]["signal_ref"][
        "description"
    ].startswith("Canonical signal reference.")

    option = ProductSignalTargetingOption.model_validate(
        {"signal_ref": {"scope": "product", "signal_id": "signal_1"}}
    )
    assert isinstance(option.signal_ref, SignalRef)
    assert option.signal_ref.scope == "product"

    for invalid_signal_ref in (
        {"scope": "unknown", "signal_id": "signal_1"},
        "signal_1",
    ):
        with pytest.raises(ValidationError):
            ProductSignalTargetingOption.model_validate({"signal_ref": invalid_signal_ref})


def test_creative_representation_keeps_canonical_format_contract() -> None:
    from adcp import LegacyBuildCreativeRequest
    from adcp.types import CanonicalFormatKind
    from adcp.types.generated_poc.core.creative_representation import CreativeRepresentation

    assert CreativeRepresentation.model_fields["format_kind"].annotation is CanonicalFormatKind
    schema = CreativeRepresentation.model_json_schema()
    assert schema["properties"]["format_kind"]["description"].startswith("Canonical 3.2 path.")
    assert schema["not"] == {
        "anyOf": [
            {"required": ["format_id"]},
            {"required": ["format_option_ref"]},
            {"required": ["representation_selection"]},
        ]
    }

    representation = {
        "representation_id": "representation_1",
        "source": {"system": "test", "source_representation": "source_1"},
        "format_kind": "image",
        "assets": {},
    }
    parsed_representation = CreativeRepresentation.model_validate(representation)
    assert parsed_representation.format_kind is CanonicalFormatKind.image

    with pytest.raises(ValidationError):
        CreativeRepresentation.model_validate({**representation, "format_kind": "unknown"})

    for seller_bound_field in ("format_id", "format_option_ref", "representation_selection"):
        with pytest.raises(ValidationError):
            # JSON Schema's ``required`` considers a key present even when null.
            CreativeRepresentation.model_validate({**representation, seller_bound_field: None})

    representation_set = {
        "creative_id": "creative_1",
        "revision_id": "revision_1",
        "revision_content_digest": "sha256:" + "0" * 64,
        "name": "Test creative",
        "representations": [{**representation, "format_kind": "unknown"}],
    }
    with pytest.raises(ValidationError):
        LegacyBuildCreativeRequest.model_validate(
            {
                "idempotency_key": "idem-123456789012",
                "creative_representation_set": representation_set,
            }
        )


def test_transformer_requires_a_canonical_or_legacy_output_declaration() -> None:
    from adcp.types import ListTransformersResponse
    from adcp.types.generated_poc.core.transformer import Transformer

    base_transformer = {"transformer_id": "transformer_1", "name": "Test transformer"}
    with pytest.raises(ValidationError):
        ListTransformersResponse.model_validate({"transformers": [base_transformer]})

    assert (
        Transformer.model_validate(
            {**base_transformer, "output_capability_ids": ["capability_1"]}
        ).output_capability_ids
        is not None
    )
    assert (
        Transformer.model_validate(
            {
                **base_transformer,
                "output_format_ids": [{"agent_url": "https://creative.example", "id": "format_1"}],
            }
        ).model_dump()["output_format_ids"]
        is not None
    )


def test_public_response_bases_remain_constructible_and_arms_remain_specific() -> None:
    from adcp.types import (
        ComplyTestControllerResponse,
        CreateContentStandardsResponse,
        ListContentStandardsResponse,
        SyncGovernanceResponse,
        UpdateContentStandardsResponse,
    )
    from adcp.types.aliases import (
        ComplyListScenariosResponse,
        CreateContentStandardsSuccessResponse,
        ListContentStandardsSuccessResponse,
        UpdateContentStandardsSuccessResponse,
    )
    from adcp.types.generated_poc.account.sync_governance_response import SyncGovernanceResponse1
    from adcp.types.generated_poc.compliance.comply_test_controller_response import (
        ComplyTestControllerResponse1,
    )
    from adcp.types.generated_poc.content_standards.create_content_standards_response import (
        CreateContentStandardsResponse1,
    )
    from adcp.types.generated_poc.content_standards.list_content_standards_response import (
        ListContentStandardsResponse1,
    )
    from adcp.types.generated_poc.content_standards.update_content_standards_response import (
        UpdateContentStandardsResponse1,
    )

    responses = (
        (
            ComplyTestControllerResponse,
            ComplyListScenariosResponse,
            ComplyTestControllerResponse1,
            {"success": True, "scenarios": []},
            {"success": True},
        ),
        (
            CreateContentStandardsResponse,
            CreateContentStandardsSuccessResponse,
            CreateContentStandardsResponse1,
            {"standards_id": "standards_1"},
            {},
        ),
        (
            ListContentStandardsResponse,
            ListContentStandardsSuccessResponse,
            ListContentStandardsResponse1,
            {"standards": []},
            {},
        ),
        (
            SyncGovernanceResponse,
            SyncGovernanceResponse1,
            SyncGovernanceResponse1,
            {"accounts": []},
            {},
        ),
        (
            UpdateContentStandardsResponse,
            UpdateContentStandardsSuccessResponse,
            UpdateContentStandardsResponse1,
            {"success": True, "standards_id": "standards_1"},
            {"success": False, "standards_id": "standards_1"},
        ),
    )

    for response_base, response_alias, response_arm, valid, invalid in responses:
        assert issubclass(response_base, BaseModel)
        assert isinstance(response_base(), response_base)
        assert response_alias is response_arm
        assert issubclass(response_arm, response_base)
        assert isinstance(response_alias.model_validate(valid), response_base)
        with pytest.raises(ValidationError):
            response_alias.model_validate(invalid)
