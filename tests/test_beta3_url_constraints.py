"""Regression tests for security-sensitive URL constraints in AdCP 3.2 beta.3."""

import pytest
from pydantic import ValidationError

from adcp import PackageRequest, PlacementPresentationReference, PublisherDesignatedPreviewProvider
from adcp.types import ReferenceRendererProvenance
from adcp.types.generated_poc.core.placement_presentation import ImageRef

_DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "agent_url",
    [
        "http://127.0.0.1/preview",
        "http://169.254.169.254/latest/meta-data",
    ],
)
def test_preview_provider_requires_https(agent_url: str) -> None:
    with pytest.raises(ValidationError, match="agent_url must use https"):
        PublisherDesignatedPreviewProvider(
            agent_url=agent_url,
            routes=[{"format_option_id": "display", "capability_id": "preview"}],
        )


def test_presentation_reference_requires_https() -> None:
    with pytest.raises(ValidationError, match="uri must use https"):
        PlacementPresentationReference(
            uri="http://169.254.169.254/presentation.json",
            digest=_DIGEST,
        )


def test_presentation_image_requires_https() -> None:
    with pytest.raises(ValidationError, match="uri must use https"):
        ImageRef(uri="http://127.0.0.1/frame.png", digest=_DIGEST)


@pytest.mark.parametrize(
    "source_repository",
    [
        "http://evil.example/repo",
        "https://github.com.evil.example/repo",
        "https://user@github.com/repo",
        "https://github.com:444/repo",
    ],
)
def test_reference_renderer_provenance_requires_github_origin(
    source_repository: str,
) -> None:
    with pytest.raises(ValidationError, match="source_repository must"):
        ReferenceRendererProvenance(
            source_repository=source_repository,
            workflow_path=".github/workflows/release.yml",
        )


def test_beta3_secure_urls_accept_valid_https_origins() -> None:
    provider = PublisherDesignatedPreviewProvider(
        agent_url="https://creative.example/preview",
        routes=[{"format_option_id": "display", "capability_id": "preview"}],
    )
    presentation = PlacementPresentationReference(
        uri="https://publisher.example/presentation.json",
        digest=_DIGEST,
    )
    image = ImageRef(uri="https://cdn.example/frame.png", digest=_DIGEST)
    provenance = ReferenceRendererProvenance(
        source_repository="https://github.com/adcontextprotocol/adcp",
        workflow_path=".github/workflows/release.yml",
    )

    assert provider.agent_url.scheme == "https"
    assert presentation.uri.scheme == "https"
    assert image.uri.scheme == "https"
    assert provenance.source_repository.host == "github.com"


@pytest.mark.parametrize("params", [{"width": 300}, {"height": 250}])
def test_image_package_dimensions_must_cooccur(params: dict[str, int]) -> None:
    with pytest.raises(ValidationError, match="width and height must co-occur"):
        PackageRequest(
            product_id="product-1",
            pricing_option_id="price-1",
            format_kind="image",
            params=params,
        )


def test_package_params_require_format_kind() -> None:
    with pytest.raises(ValidationError, match="params requires format_kind"):
        PackageRequest(
            product_id="product-1",
            pricing_option_id="price-1",
            params={"width": 300, "height": 250},
        )


def test_image_package_accepts_complete_dimensions() -> None:
    package = PackageRequest(
        product_id="product-1",
        pricing_option_id="price-1",
        format_kind="image",
        params={"width": 300, "height": 250},
    )

    assert package.params == {"width": 300, "height": 250}
