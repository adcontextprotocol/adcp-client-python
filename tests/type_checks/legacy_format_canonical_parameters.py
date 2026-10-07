"""Adopters can inspect the restored nested declaration with static typing."""

from adcp.types.generated_poc.core.product_format_declaration import ProductFormatDeclaration1
from adcp.types.generated_poc.formats.canonical.image import CanonicalFormatImage
from adcp.types.legacy import LegacyFormat, LegacyListCreativeFormatsResponse


def image_width(format: LegacyFormat) -> int | None:
    declaration = format.canonical_parameters
    if declaration is None:
        return None
    if isinstance(declaration.root, ProductFormatDeclaration1):
        parameters: CanonicalFormatImage = declaration.root.params
        return parameters.width
    return None


def catalog_widths(response: LegacyListCreativeFormatsResponse) -> list[int | None]:
    return [image_width(format) for format in response.formats]
