"""An input overlay's container fields are containers, matching the output overlay.

``core/targeting-input.json`` declares 27 of its 37 fields as "the type
``core/targeting.json`` gives this property, or null". The two classes describe
one wire document, so a field that reads as ``list[GeoCountry]`` on
``TargetingOverlay`` reads the same way on ``TargetingOverlayInput``: indexable,
sized, and iterable over the elements, and passes to a function that takes the
output overlay's field type.
"""

from typing_extensions import assert_type

from adcp.types import (
    DaypartTarget,
    DevicePlatform,
    TargetingOverlay,
    TargetingOverlayInput,
)

overlay = TargetingOverlayInput.model_validate(
    {
        "geo_countries": ["US", "GB"],
        "audience_exclude": ["crm-1"],
        "axe_include_segment": "axe-1",
    }
)

countries = overlay.geo_countries
if countries is not None:
    assert_type(len(countries), int)
    # A scalar schema root is the ``str`` its schema declares (#1277), so the
    # element is usable as one with no ``.root`` read.
    first_code: str = countries[0]
    assert_type(first_code, str)
    for country in countries:
        code: str = country
        assert_type(code.upper(), str)

audiences = overlay.audience_exclude
if audiences is not None:
    assert_type(len(audiences), int)
    assert_type(audiences[0], str)
    for audience in audiences:
        assert_type(audience, str)

segment = overlay.axe_include_segment
if segment is not None:
    assert_type(segment, str)

# The output overlay reads identically: the element type, not a wrapper around it.
output = TargetingOverlay.model_validate(
    {"geo_countries": ["US"], "audience_exclude": ["crm-1"], "axe_include_segment": "axe-1"}
)

output_countries = output.geo_countries
if output_countries is not None:
    output_code: str = output_countries[0]
    assert_type(output_code, str)

output_audiences = output.audience_exclude
if output_audiences is not None:
    assert_type(output_audiences[0], str)

output_segment = output.axe_include_segment
if output_segment is not None:
    assert_type(output_segment, str)


# An adopter builds the input overlay and hands its fields to code written against
# the output overlay. Those call sites are the whole reason the two classes have to
# agree on a field type.
#
# This covers the fields whose element type is a shared class. A field whose schema
# nests an anonymous object or constrained scalar — geo_countries nests
# {"type": "string", "pattern": "^[A-Z]{2}$"} — still gets an element class per
# module, so geo_countries does not pass here. The containers themselves agree; the
# element classes are a separate codegen limitation.
def audiences_to_gam(segments: list[str] | None) -> list[str]:
    return list(segments) if segments else []


def segment_to_gam(segment: str | None) -> str:
    return segment or ""


def platforms_to_gam(platforms: list[DevicePlatform] | None) -> list[str]:
    return [platform.value for platform in platforms] if platforms else []


def dayparts_to_gam(dayparts: list[DaypartTarget] | None) -> int:
    return len(dayparts) if dayparts else 0


assert_type(audiences_to_gam(overlay.audience_exclude), list[str])
assert_type(audiences_to_gam(output.audience_exclude), list[str])
assert_type(segment_to_gam(overlay.axe_include_segment), str)
assert_type(segment_to_gam(output.axe_include_segment), str)
assert_type(platforms_to_gam(overlay.device_platform), list[str])
assert_type(platforms_to_gam(output.device_platform), list[str])
assert_type(dayparts_to_gam(overlay.daypart_targets), int)
assert_type(dayparts_to_gam(output.daypart_targets), int)
