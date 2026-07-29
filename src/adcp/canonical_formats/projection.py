"""TypeScript RC3-compatible legacy-to-canonical creative projection."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any, cast
from urllib.parse import urlsplit

from pydantic import ValidationError

from adcp.canonical_formats.fixtures import load_v1_reference_catalog
from adcp.canonical_formats.identity import canonicalize_agent_url
from adcp.types import Format, Product
from adcp.types.legacy import LegacyFormatId, LegacyProduct

AAO_CANONICAL_AGENT_URL = "https://creative.adcontextprotocol.org/"
_AAO_OWNER_ALIASES = {"https://adcontextprotocol.org/"}
_UNSAFE_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".test", ".invalid")
_HISTORICAL_DISPLAY_SIZE = re.compile(r"^display_([1-9][0-9]*)x([1-9][0-9]*)$")
try:
    SDK_ID = f"adcp-client-python@{version('adcp')}"
except PackageNotFoundError:  # pragma: no cover - editable/source-only fallback
    SDK_ID = "adcp-client-python@7.0.0rc1"


def migrated_format_option_id(format_id: LegacyFormatId | Mapping[str, Any]) -> str:
    """Return the normative stable option ID from the complete legacy tuple."""

    ref = (
        format_id
        if isinstance(format_id, LegacyFormatId)
        else LegacyFormatId.model_validate(format_id)
    )
    duration: int | float | None = ref.duration_ms
    if duration is not None and float(duration).is_integer():
        # JSON.stringify renders JavaScript's sole Number type without a
        # trailing .0. Pydantic stores duration_ms as float, so normalize the
        # representation before hashing to preserve byte-for-byte RC3 parity.
        duration = int(duration)
    identity = json.dumps(
        [
            str(ref.agent_url),
            ref.id,
            ref.width,
            ref.height,
            duration,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hmac.new(b"", identity.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"migrated_{digest}"


def _catalog_owner(raw: object) -> str | None:
    try:
        original = urlsplit(str(raw))
    except ValueError:
        return None
    if original.username or original.password or original.fragment:
        return None
    canonical = canonicalize_agent_url(raw)
    try:
        parts = urlsplit(canonical)
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname or parts.username or parts.password or parts.fragment:
        return None
    path = parts.path or "/"
    owner = parts._replace(path=path, fragment="").geturl()
    return AAO_CANONICAL_AGENT_URL if owner in _AAO_OWNER_ALIASES else owner


def _is_safe_public_https_owner(raw: object) -> bool:
    owner = _catalog_owner(raw)
    if owner is None:
        return False
    parts = urlsplit(owner)
    if parts.scheme != "https" or not parts.hostname:
        return False
    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(_UNSAFE_HOST_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


@dataclass(frozen=True)
class CatalogIndex:
    by_owner_and_id: dict[tuple[str, str], dict[str, Any] | None]
    by_unique_id: dict[str, dict[str, Any] | None]
    _allow_bare_id_fallback: bool = False


def build_catalog_index(entries: Iterable[Mapping[str, Any]]) -> CatalogIndex:
    """Build exact-owner and collision-aware bare-ID indexes."""

    by_owner_and_id: dict[tuple[str, str], dict[str, Any] | None] = {}
    by_unique_id: dict[str, dict[str, Any] | None] = {}
    for raw in entries:
        entry = dict(raw)
        ref = entry.get("format_id")
        if not isinstance(ref, Mapping):
            continue
        owner = _catalog_owner(ref.get("agent_url"))
        identifier = ref.get("id")
        if owner is None or not isinstance(identifier, str) or not identifier:
            continue
        owner_key = (owner, identifier)
        if owner_key in by_owner_and_id:
            by_owner_and_id[owner_key] = None
        else:
            by_owner_and_id[owner_key] = entry
        if identifier in by_unique_id:
            by_unique_id[identifier] = None
        else:
            by_unique_id[identifier] = entry
    return CatalogIndex(by_owner_and_id=by_owner_and_id, by_unique_id=by_unique_id)


@lru_cache(maxsize=1)
def load_rc3_catalog_index() -> CatalogIndex:
    """Load the vendored RC3 AAO catalog used by the compatibility fallback."""

    index = build_catalog_index(load_v1_reference_catalog())
    return CatalogIndex(
        by_owner_and_id=index.by_owner_and_id,
        by_unique_id=index.by_unique_id,
        _allow_bare_id_fallback=True,
    )


@dataclass(frozen=True)
class LegacyFormatConversionContext:
    format_id: LegacyFormatId
    product_id: str
    field: str


LegacyFormatConverter = Callable[[LegacyFormatConversionContext], Format | Mapping[str, Any] | None]


@dataclass(frozen=True)
class CanonicalFormatLegacyResolutionContext:
    declaration: Format
    product_id: str | None = None
    field: str = "format_options[]"


CanonicalFormatLegacyResolver = Callable[
    [CanonicalFormatLegacyResolutionContext], Sequence[LegacyFormatId] | None
]


class CanonicalFormatLegacyResolutionError(ValueError):
    """Raised when persisted canonical state has no explicit reverse route."""


class LegacyCreativeProjectionError(ValueError):
    """Raised when a legacy inbound request cannot reach a canonical handler."""


@dataclass(frozen=True)
class ProjectionDiagnostic:
    code: str
    field: str
    product_id: str
    resolution_failure: str | None = None
    format_kind: str = "custom"
    source: str = "sdk"
    reason: str | None = None

    def model_dump(self) -> dict[str, Any]:
        details = (
            {"product_id": self.product_id, "reason": self.reason}
            if self.code == "CANONICAL_PRODUCT_FORMATS_UNAVAILABLE"
            else {
                "format_kind": self.format_kind,
                "product_id": self.product_id,
                "resolution_failure": self.resolution_failure,
            }
        )
        return {
            "source": self.source,
            "sdk_id": SDK_ID,
            "field": self.field,
            "code": self.code,
            "error": {"details": details},
        }


@dataclass
class ProjectedFormat:
    declaration: Format | None = None
    diagnostic: ProjectionDiagnostic | None = None


class _CatalogConflictError(ValueError):
    pass


def _positive_integer(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
        and float(value).is_integer()
    )


def _validate_inline_parameters(ref: LegacyFormatId) -> bool:
    has_width = ref.width is not None
    has_height = ref.height is not None
    if has_width != has_height:
        return False
    if has_width and (not _positive_integer(ref.width) or not _positive_integer(ref.height)):
        return False
    return ref.duration_ms is None or _positive_integer(ref.duration_ms)


def _fixed_catalog_params(ref: LegacyFormatId, entry: Mapping[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    fixed_sizes: set[tuple[int, int]] = set()
    unsupported_size = False

    for render in entry.get("renders") or []:
        dimensions = render.get("dimensions") if isinstance(render, Mapping) else None
        if not isinstance(dimensions, Mapping):
            continue
        width, height = dimensions.get("width"), dimensions.get("height")
        if width is None and height is None:
            continue
        if _positive_integer(width) and _positive_integer(height):
            fixed_sizes.add((cast(int, width), cast(int, height)))
        else:
            unsupported_size = True

    fixed_durations: set[int] = set()
    ranged_duration = False
    for asset in entry.get("assets") or []:
        requirements = asset.get("requirements") if isinstance(asset, Mapping) else None
        if not isinstance(requirements, Mapping):
            continue
        width, height = requirements.get("width"), requirements.get("height")
        if width is not None or height is not None:
            if _positive_integer(width) and _positive_integer(height):
                fixed_sizes.add((cast(int, width), cast(int, height)))
            else:
                unsupported_size = True

        min_width, max_width = requirements.get("min_width"), requirements.get("max_width")
        min_height, max_height = requirements.get("min_height"), requirements.get("max_height")
        if any(v is not None for v in (min_width, max_width, min_height, max_height)):
            if (
                _positive_integer(min_width)
                and min_width == max_width
                and _positive_integer(min_height)
                and min_height == max_height
            ):
                fixed_sizes.add((cast(int, min_width), cast(int, min_height)))
            else:
                unsupported_size = True

        minimum, maximum = requirements.get("min_duration_ms"), requirements.get("max_duration_ms")
        if minimum is None and maximum is None:
            continue
        if (
            (minimum is not None and not _positive_integer(minimum))
            or (maximum is not None and not _positive_integer(maximum))
            or (isinstance(minimum, int) and isinstance(maximum, int) and minimum > maximum)
        ):
            raise _CatalogConflictError("invalid catalog duration")
        if minimum == maximum and _positive_integer(minimum):
            fixed_durations.add(cast(int, minimum))
        else:
            ranged_duration = True

    if len(fixed_sizes) > 1 or unsupported_size:
        raise _CatalogConflictError("ambiguous catalog dimensions")
    if fixed_sizes:
        width, height = next(iter(fixed_sizes))
        if (ref.width is not None and ref.width != width) or (
            ref.height is not None and ref.height != height
        ):
            raise _CatalogConflictError("contradictory inline dimensions")
        params.update(width=width, height=height)

    if len(fixed_durations) > 1 or ranged_duration:
        raise _CatalogConflictError("ambiguous catalog duration")
    if fixed_durations:
        duration = next(iter(fixed_durations))
        if ref.duration_ms is not None and ref.duration_ms != duration:
            raise _CatalogConflictError("contradictory inline duration")
        params["duration_ms_exact"] = duration

    if ref.width is not None:
        params["width"] = ref.width
        params["height"] = ref.height
    if ref.duration_ms is not None:
        params["duration_ms_exact"] = ref.duration_ms
    return params


def _converted_format(
    converter: LegacyFormatConverter,
    context: LegacyFormatConversionContext,
) -> ProjectedFormat | None:
    try:
        converted = converter(context)
        if converted is None:
            return None
        body = converted.model_dump() if isinstance(converted, Format) else dict(converted)
        if any(key in body for key in ("format_id", "format_ids", "v1_format_ref", "agent_url")):
            raise ValueError("converter returned legacy identity")
        body["v1_format_ref"] = [context.format_id]
        body.setdefault("format_option_id", migrated_format_option_id(context.format_id))
        declaration = Format.model_validate(body)
        if declaration.canonical_formats_only:
            raise ValueError("legacy conversion cannot be canonical-only")
        return ProjectedFormat(declaration=declaration)
    except (TypeError, ValueError, ValidationError):
        return ProjectedFormat(
            diagnostic=ProjectionDiagnostic(
                code="FORMAT_PROJECTION_FAILED",
                field=context.field,
                product_id=context.product_id,
                resolution_failure="custom_converter_failed",
            )
        )


def project_legacy_format_id(
    format_id: LegacyFormatId | Mapping[str, Any],
    *,
    product_id: str,
    field: str,
    legacy_format_converter: LegacyFormatConverter | None = None,
    catalog: CatalogIndex | None = None,
) -> ProjectedFormat:
    """Project one legacy tuple using RC3 precedence and safety semantics."""

    try:
        ref = (
            format_id
            if isinstance(format_id, LegacyFormatId)
            else LegacyFormatId.model_validate(format_id)
        )
    except ValidationError:
        return ProjectedFormat(
            diagnostic=ProjectionDiagnostic(
                code="FORMAT_PROJECTION_FAILED",
                field=field,
                product_id=product_id,
                resolution_failure="invalid_format_id_parameters",
            )
        )
    if not _validate_inline_parameters(ref):
        return ProjectedFormat(
            diagnostic=ProjectionDiagnostic(
                code="FORMAT_PROJECTION_FAILED",
                field=field,
                product_id=product_id,
                resolution_failure="invalid_format_id_parameters",
            )
        )

    index = catalog or load_rc3_catalog_index()
    owner = _catalog_owner(ref.agent_url)
    if owner is None or not _is_safe_public_https_owner(ref.agent_url):
        return ProjectedFormat(
            diagnostic=ProjectionDiagnostic(
                code="FORMAT_PROJECTION_FAILED",
                field=field,
                product_id=product_id,
                resolution_failure="no_match",
            )
        )
    exact_key = (owner, ref.id)
    exact = index.by_owner_and_id.get(exact_key)
    if exact_key in index.by_owner_and_id and exact is None:
        return ProjectedFormat(
            diagnostic=ProjectionDiagnostic(
                code="FORMAT_PROJECTION_FAILED",
                field=field,
                product_id=product_id,
                resolution_failure="catalog_collision",
            )
        )
    entry = exact

    if exact is None:
        unique = index.by_unique_id.get(ref.id) if index._allow_bare_id_fallback else None
        if legacy_format_converter is not None:
            converted = _converted_format(
                legacy_format_converter,
                LegacyFormatConversionContext(ref, product_id, field),
            )
            if converted is not None:
                return converted
        historical_size = _HISTORICAL_DISPLAY_SIZE.fullmatch(ref.id)
        if owner == AAO_CANONICAL_AGENT_URL and historical_size is not None:
            return ProjectedFormat(
                declaration=Format(
                    format_option_id=migrated_format_option_id(ref),
                    format_kind="image",
                    params={
                        "width": int(historical_size.group(1)),
                        "height": int(historical_size.group(2)),
                    },
                    v1_format_ref=[ref],
                )
            )
        if unique is None:
            return ProjectedFormat(
                diagnostic=ProjectionDiagnostic(
                    code="FORMAT_PROJECTION_FAILED",
                    field=field,
                    product_id=product_id,
                    resolution_failure="no_match",
                )
            )
        entry = unique

    canonical = entry.get("canonical") if isinstance(entry, Mapping) else None
    if not isinstance(canonical, Mapping) or not isinstance(canonical.get("kind"), str):
        if legacy_format_converter is not None:
            converted = _converted_format(
                legacy_format_converter,
                LegacyFormatConversionContext(ref, product_id, field),
            )
            if converted is not None:
                return converted
        return ProjectedFormat(
            diagnostic=ProjectionDiagnostic(
                code="FORMAT_PROJECTION_FAILED",
                field=field,
                product_id=product_id,
                resolution_failure="catalog_lacks_canonical_annotation",
            )
        )

    assert isinstance(entry, Mapping)
    try:
        params = _fixed_catalog_params(ref, entry)
    except _CatalogConflictError:
        return ProjectedFormat(
            diagnostic=ProjectionDiagnostic(
                code="FORMAT_PROJECTION_FAILED",
                field=field,
                product_id=product_id,
                format_kind=canonical["kind"],
                resolution_failure="catalog_requirement_conflict",
            )
        )
    if canonical.get("asset_source"):
        params["asset_source"] = canonical["asset_source"]
    if canonical.get("slots_override"):
        params["slots"] = canonical["slots_override"]
    try:
        declaration = Format(
            format_option_id=migrated_format_option_id(ref),
            format_kind=canonical["kind"],
            params=params,
            v1_format_ref=[ref],
        )
    except ValidationError:
        return ProjectedFormat(
            diagnostic=ProjectionDiagnostic(
                code="FORMAT_PROJECTION_FAILED",
                field=field,
                product_id=product_id,
                resolution_failure="catalog_requirement_conflict",
            )
        )
    return ProjectedFormat(declaration=declaration)


@dataclass
class CanonicalProductProjection:
    product: Product | None
    diagnostics: list[ProjectionDiagnostic] = field(default_factory=list)


def project_legacy_product(
    product: LegacyProduct | Mapping[str, Any],
    *,
    legacy_format_converter: LegacyFormatConverter | None = None,
    catalog: CatalogIndex | None = None,
) -> CanonicalProductProjection:
    """Project a raw product, omitting it when no format can map."""

    raw = product.model_dump(mode="json") if isinstance(product, LegacyProduct) else dict(product)
    product_id = str(raw.get("product_id", ""))
    declarations: list[Format] = []
    diagnostics: list[ProjectionDiagnostic] = []

    for option in raw.get("format_options") or []:
        try:
            declarations.append(Format.model_validate(option))
        except ValidationError:
            diagnostics.append(
                ProjectionDiagnostic(
                    code="FORMAT_PROJECTION_FAILED",
                    field=f"products[{product_id}].format_options",
                    product_id=product_id,
                    resolution_failure="invalid_canonical_declaration",
                )
            )

    for index, ref in enumerate(raw.get("format_ids") or []):
        result = project_legacy_format_id(
            ref,
            product_id=product_id,
            field=f"products[{product_id}].format_ids[{index}]",
            legacy_format_converter=legacy_format_converter,
            catalog=catalog,
        )
        if result.declaration is not None:
            declarations.append(result.declaration)
        if result.diagnostic is not None:
            diagnostics.append(result.diagnostic)

    projected_placements: list[dict[str, Any]] = []
    for placement_index, placement_value in enumerate(raw.get("placements") or []):
        placement = (
            placement_value.model_dump(mode="json")
            if hasattr(placement_value, "model_dump")
            else dict(placement_value)
        )
        placement_options: list[Format] = []
        for option in placement.pop("format_options", None) or []:
            try:
                placement_options.append(Format.model_validate(option))
            except ValidationError:
                diagnostics.append(
                    ProjectionDiagnostic(
                        code="FORMAT_PROJECTION_FAILED",
                        field=(
                            f"products[{product_id}].placements[{placement_index}]"
                            ".format_options"
                        ),
                        product_id=product_id,
                        resolution_failure="invalid_canonical_declaration",
                    )
                )
        placement_ids = placement.pop("format_ids", None)
        if placement_ids == [] and not placement_options:
            diagnostics.append(
                ProjectionDiagnostic(
                    code="CANONICAL_PRODUCT_FORMATS_UNAVAILABLE",
                    field=f"products[{product_id}].placements[{placement_index}].format_options",
                    product_id=product_id,
                    reason="nested_placement_format_list_empty",
                )
            )
        for ref_index, ref in enumerate(placement_ids or []):
            result = project_legacy_format_id(
                ref,
                product_id=product_id,
                field=(
                    f"products[{product_id}].placements[{placement_index}]"
                    f".format_ids[{ref_index}]"
                ),
                legacy_format_converter=legacy_format_converter,
                catalog=catalog,
            )
            if result.declaration is not None:
                placement_options.append(result.declaration)
                declarations.append(result.declaration)
            if result.diagnostic is not None:
                diagnostics.append(result.diagnostic)
        if placement_options:
            placement["format_options"] = placement_options
        projected_placements.append(placement)

    raw.pop("format_ids", None)
    if projected_placements:
        raw["placements"] = projected_placements
    raw["format_options"] = declarations
    if not declarations:
        had_legacy = "format_ids" in product if isinstance(product, Mapping) else True
        diagnostics.append(
            ProjectionDiagnostic(
                code="CANONICAL_PRODUCT_FORMATS_UNAVAILABLE",
                field=f"products[{product_id}].format_options",
                product_id=product_id,
                reason=(
                    "legacy_format_list_empty"
                    if had_legacy and not raw.get("format_ids")
                    else "missing_format_declaration"
                ),
            )
        )
        return CanonicalProductProjection(product=None, diagnostics=diagnostics)
    return CanonicalProductProjection(
        product=Product.model_validate(raw),
        diagnostics=diagnostics,
    )


def normalize_legacy_creative_request(
    value: Mapping[str, Any],
    *,
    legacy_format_converter: LegacyFormatConverter | None = None,
    projection_sources: list[Any] | None = None,
) -> dict[str, Any]:
    """Upgrade legacy selectors before a primary server handler runs.

    This projector performs no network I/O. Unmappable selectors reject the
    request rather than silently broadening its creative scope.
    """

    def retain_routes(declarations: Sequence[Format], product_id: object) -> None:
        """Keep exact tuples beside, never inside, canonical handler input."""

        if projection_sources is None or not declarations:
            return
        projection_sources.append(
            {
                "product_id": product_id if isinstance(product_id, str) else None,
                "format_options": list(declarations),
            }
        )

    def visit(item: Any, field_path: str) -> Any:
        if isinstance(item, list):
            return [visit(child, f"{field_path}[{index}]") for index, child in enumerate(item)]
        if not isinstance(item, Mapping):
            return item

        result = {key: visit(child, f"{field_path}.{key}") for key, child in item.items()}
        fields = result.get("fields")
        if isinstance(fields, list):
            result["fields"] = list(
                dict.fromkeys(
                    "format_options" if field in {"format_id", "format_ids"} else field
                    for field in fields
                )
            )
        format_ids = result.pop("format_ids", None)
        if format_ids is not None:
            declarations: list[Format] = []
            for index, legacy_id in enumerate(format_ids):
                projected = project_legacy_format_id(
                    legacy_id,
                    product_id=str(result.get("product_id") or ""),
                    field=f"{field_path}.format_ids[{index}]",
                    legacy_format_converter=legacy_format_converter,
                )
                if projected.declaration is None:
                    reason = (
                        projected.diagnostic.resolution_failure
                        if projected.diagnostic is not None
                        else "no_match"
                    )
                    raise LegacyCreativeProjectionError(
                        f"{field_path}.format_ids[{index}] cannot be projected ({reason})"
                    )
                declarations.append(projected.declaration)
            retain_routes(declarations, result.get("product_id"))
            if "product_id" in result:
                result["format_option_refs"] = [
                    {
                        "scope": "product",
                        "format_option_id": declaration.format_option_id,
                    }
                    for declaration in declarations
                ]
            else:
                result["format_options"] = declarations

        legacy_id = result.pop("format_id", None)
        if legacy_id is not None:
            projected = project_legacy_format_id(
                legacy_id,
                product_id=str(result.get("product_id") or ""),
                field=f"{field_path}.format_id",
                legacy_format_converter=legacy_format_converter,
            )
            if projected.declaration is None:
                reason = (
                    projected.diagnostic.resolution_failure
                    if projected.diagnostic is not None
                    else "no_match"
                )
                raise LegacyCreativeProjectionError(
                    f"{field_path}.format_id cannot be projected ({reason})"
                )
            declaration = projected.declaration
            retain_routes([declaration], result.get("product_id"))
            result["format_kind"] = declaration.format_kind.value
            if declaration.format_option_id:
                result["format_option_ref"] = {
                    "scope": "product",
                    "format_option_id": declaration.format_option_id,
                }
        return result

    return cast(dict[str, Any], visit(value, "request"))


def resolve_legacy_format_refs(
    declaration: Format,
    *,
    resolver: CanonicalFormatLegacyResolver | None = None,
    product_id: str | None = None,
    field: str = "format_options[]",
) -> list[LegacyFormatId]:
    """Resolve a canonical declaration without ever reverse-guessing.

    Same-process projections retain the original tuple in private model state.
    JSON/process boundaries deliberately erase that state; callers must then
    provide a durable resolver backed by adopter storage or catalog snapshots.
    """

    if declaration.legacy_format_refs:
        return list(declaration.legacy_format_refs)
    if resolver is None:
        raise CanonicalFormatLegacyResolutionError(
            "canonical creative crossed a process boundary without a durable "
            "canonical-to-legacy resolver; rediscover the product or configure a resolver"
        )
    resolved = resolver(
        CanonicalFormatLegacyResolutionContext(
            declaration=declaration,
            product_id=product_id,
            field=field,
        )
    )
    if not resolved:
        raise CanonicalFormatLegacyResolutionError(
            f"no durable legacy route for {field}; refusing to reverse-guess"
        )
    return [
        item if isinstance(item, LegacyFormatId) else LegacyFormatId.model_validate(item)
        for item in resolved
    ]


def project_canonical_response_to_legacy(
    value: Any,
    *,
    resolver: CanonicalFormatLegacyResolver | None = None,
    sources: Sequence[Any] = (),
) -> Any:
    """Project a canonical server result to a captured legacy caller dialect.

    Private same-process routes are honored. Once serialization erased them,
    only the explicit durable resolver may authorize legacy delivery.
    """

    declaration_routes: dict[tuple[str, str | None, str], Format | None] = {}

    def declaration_fingerprint(declaration: Format) -> tuple[str, tuple[str, ...]]:
        canonical = json.dumps(
            declaration.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        legacy = tuple(
            json.dumps(
                ref.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for ref in declaration.legacy_format_refs
        )
        return canonical, legacy

    def register_declaration(
        scope: str,
        owner: str | None,
        option_id: str,
        declaration: Format,
    ) -> None:
        key = (scope, owner, option_id)
        if key not in declaration_routes:
            declaration_routes[key] = declaration
            return
        existing = declaration_routes[key]
        if existing is None or declaration_fingerprint(existing) != declaration_fingerprint(
            declaration
        ):
            declaration_routes[key] = None

    def collect(item: Any, product_id: str | None = None) -> None:
        if isinstance(item, Format):
            if item.format_option_id:
                register_declaration("product", product_id, item.format_option_id, item)
                if item.publisher_domain:
                    register_declaration(
                        "publisher",
                        item.publisher_domain,
                        item.format_option_id,
                        item,
                    )
            return
        if hasattr(item.__class__, "model_fields"):
            current_product = getattr(item, "product_id", None) or product_id
            for name in (
                "format_options",
                "products",
                "placements",
                "packages",
                "affected_packages",
                "creatives",
                "media_buys",
                "variants",
                "manifest",
            ):
                if name in item.__class__.model_fields:
                    collect(getattr(item, name, None), current_product)
            return
        if isinstance(item, Mapping):
            current_product = item.get("product_id") or product_id
            for source in getattr(item, "_canonical_sources", ()):
                collect(source, current_product)
            for child in item.values():
                collect(child, current_product)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                collect(child, product_id)

    collect(value)
    for source in sources:
        collect(source)

    def declaration_for_ref(
        ref: Mapping[str, Any],
        raw: Mapping[str, Any],
        field_path: str,
        product_id: str | None,
    ) -> Format:
        option_id = ref.get("format_option_id")
        if not isinstance(option_id, str):
            raise CanonicalFormatLegacyResolutionError(
                f"{field_path} has an invalid format_option_id"
            )
        scope = ref.get("scope")
        scope = getattr(scope, "value", scope)
        if scope == "product":
            key = ("product", product_id, option_id)
        elif scope == "publisher":
            publisher_domain = ref.get("publisher_domain")
            if not isinstance(publisher_domain, str) or not publisher_domain:
                raise CanonicalFormatLegacyResolutionError(
                    f"{field_path} publisher reference has no publisher_domain"
                )
            key = ("publisher", publisher_domain, option_id)
        else:
            raise CanonicalFormatLegacyResolutionError(
                f"{field_path} has unsupported format option scope {scope!r}"
            )
        if key in declaration_routes:
            declaration = declaration_routes[key]
            if declaration is None:
                raise CanonicalFormatLegacyResolutionError(
                    f"{field_path} has conflicting declarations for {scope} route {option_id!r}"
                )
            return declaration
        kind = raw.get("format_kind")
        if not isinstance(kind, str):
            raise CanonicalFormatLegacyResolutionError(
                f"{field_path} has no discovered declaration; configure a durable resolver"
            )
        params = raw.get("params")
        return Format(
            format_option_id=option_id,
            publisher_domain=(key[1] if scope == "publisher" else None),
            format_kind=kind,
            params=params if isinstance(params, dict) else {},
        )

    def visit(item: Any, field_path: str, product_id: str | None = None) -> Any:
        if isinstance(item, Format):
            return item
        if hasattr(item, "model_dump"):
            raw = item.model_dump(mode="json", exclude_none=True)
            # Keep the actual nested objects long enough to read private
            # same-process legacy routes; model_dump intentionally erases them.
            model_options = getattr(item, "format_options", None)
            if model_options is not None:
                raw["format_options"] = model_options
            for collection in (
                "products",
                "packages",
                "affected_packages",
                "creatives",
                "media_buys",
            ):
                if collection in item.__class__.model_fields:
                    model_items = getattr(item, collection, None)
                    if model_items is not None:
                        raw[collection] = model_items
        elif isinstance(item, Mapping):
            raw = dict(item)
        elif isinstance(item, (list, tuple)):
            return [
                visit(child, f"{field_path}[{index}]", product_id)
                for index, child in enumerate(item)
            ]
        else:
            return item

        raw_product_id = raw.get("product_id")
        current_product_id = raw_product_id if isinstance(raw_product_id, str) else product_id

        options = raw.get("format_options")
        if isinstance(options, list):
            legacy_ids: list[dict[str, Any]] = []
            for index, option in enumerate(options):
                parsed_declaration = (
                    option if isinstance(option, Format) else Format.model_validate(option)
                )
                option_id = parsed_declaration.format_option_id
                declaration = parsed_declaration
                if option_id is not None:
                    key = ("product", current_product_id, option_id)
                    if key in declaration_routes:
                        registered = declaration_routes[key]
                        if registered is None:
                            raise CanonicalFormatLegacyResolutionError(
                                f"{field_path}.format_options[{index}] has conflicting "
                                f"product declarations for {option_id!r}"
                            )
                        declaration = registered
                legacy_ids.extend(
                    ref.model_dump(mode="json")
                    for ref in resolve_legacy_format_refs(
                        declaration,
                        resolver=resolver,
                        product_id=current_product_id,
                        field=f"{field_path}.format_options[{index}]",
                    )
                )
            raw.pop("format_options", None)
            raw["format_ids"] = legacy_ids

        option_refs = raw.pop("format_option_refs", None)
        if isinstance(option_refs, list):
            legacy_ids = []
            for index, option_ref in enumerate(option_refs):
                if not isinstance(option_ref, Mapping):
                    raise CanonicalFormatLegacyResolutionError(
                        f"{field_path}.format_option_refs[{index}] is invalid"
                    )
                declaration = declaration_for_ref(option_ref, raw, field_path, current_product_id)
                legacy_ids.extend(
                    ref.model_dump(mode="json")
                    for ref in resolve_legacy_format_refs(
                        declaration,
                        resolver=resolver,
                        product_id=current_product_id,
                        field=f"{field_path}.format_option_refs[{index}]",
                    )
                )
            raw["format_ids"] = legacy_ids

        option_ref = raw.pop("format_option_ref", None)
        if isinstance(option_ref, Mapping):
            declaration = declaration_for_ref(option_ref, raw, field_path, current_product_id)
            creative_legacy_refs = resolve_legacy_format_refs(
                declaration,
                resolver=resolver,
                product_id=current_product_id,
                field=f"{field_path}.format_option_ref",
            )
            if len(creative_legacy_refs) != 1:
                raise CanonicalFormatLegacyResolutionError(
                    f"{field_path}.format_option_ref resolves to "
                    f"{len(creative_legacy_refs)} legacy "
                    "formats; a creative requires exactly one"
                )
            raw["format_id"] = creative_legacy_refs[0].model_dump(mode="json")
            raw.pop("format_kind", None)
            raw.pop("params", None)
        elif isinstance(raw.get("format_kind"), str) and (
            "creative_id" in raw or "package_id" in raw
        ):
            declaration = Format(
                format_kind=raw["format_kind"],
                params=raw.get("params") if isinstance(raw.get("params"), dict) else {},
            )
            inferred_refs = resolve_legacy_format_refs(
                declaration,
                resolver=resolver,
                product_id=current_product_id,
                field=f"{field_path}.format_kind",
            )
            raw.pop("format_kind", None)
            raw.pop("params", None)
            if "creative_id" in raw:
                if len(inferred_refs) != 1:
                    raise CanonicalFormatLegacyResolutionError(
                        f"{field_path}.format_kind resolves to {len(inferred_refs)} legacy "
                        "formats; a creative requires exactly one"
                    )
                raw["format_id"] = inferred_refs[0].model_dump(mode="json")
            else:
                raw["format_ids"] = [ref.model_dump(mode="json") for ref in inferred_refs]

        for key, child in list(raw.items()):
            raw[key] = visit(child, f"{field_path}.{key}", current_product_id)
        return raw

    return visit(value, "response")


def _snapshot_formats(snapshot: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    formats = snapshot.get("formats")
    if not isinstance(formats, list):
        return ()
    return (item for item in formats if isinstance(item, Mapping))


def _snapshot_priority(snapshot: Mapping[str, Any]) -> int:
    source = snapshot.get("source")
    if source == "publisher":
        return 0
    if source in {"community", "approved_community_mirror"}:
        return 1
    return 2


def canonical_format_legacy_resolver_from_catalog_snapshots(
    snapshots: Iterable[Mapping[str, Any]],
) -> CanonicalFormatLegacyResolver:
    """Compile owner-scoped, one-to-one durable reverse routes."""

    routes: dict[
        tuple[str | None, str, str],
        tuple[int, tuple[dict[str, Any], list[LegacyFormatId]] | None],
    ] = {}
    for snapshot in snapshots:
        priority = _snapshot_priority(snapshot)
        for raw in _snapshot_formats(snapshot):
            if raw.get("canonical_formats_only") is True:
                continue
            option_id = raw.get("format_option_id")
            kind = raw.get("format_kind")
            refs = raw.get("v1_format_ref")
            if (
                not isinstance(option_id, str)
                or not isinstance(kind, str)
                or not isinstance(refs, list)
            ):
                continue
            try:
                parsed = [LegacyFormatId.model_validate(ref) for ref in refs]
            except ValidationError:
                continue
            if not parsed:
                continue
            publisher = raw.get("publisher_domain", snapshot.get("publisher_domain"))
            key = (publisher if isinstance(publisher, str) else None, option_id, kind)
            candidate = (deepcopy(raw.get("params") or {}), parsed)
            existing = routes.get(key)
            if existing is None or priority < existing[0]:
                routes[key] = (priority, candidate)
            elif priority == existing[0]:
                routes[key] = (priority, None)

    def resolve(context: CanonicalFormatLegacyResolutionContext) -> Sequence[LegacyFormatId] | None:
        declaration = context.declaration
        if not declaration.format_option_id:
            return None
        ranked = routes.get(
            (
                declaration.publisher_domain,
                declaration.format_option_id,
                declaration.format_kind.value,
            )
        )
        route = ranked[1] if ranked else None
        if not route:
            return None
        expected_params, refs = route
        if declaration.params != expected_params:
            return None
        return tuple(refs)

    return resolve


@dataclass(frozen=True)
class ProjectionCatalogAdapters:
    legacy_format_converter: LegacyFormatConverter
    canonical_format_legacy_resolver: CanonicalFormatLegacyResolver


def legacy_format_converter_from_catalog_snapshots(
    snapshots: Iterable[Mapping[str, Any]],
) -> LegacyFormatConverter:
    """Compile exact owner+ID forward routes from catalog snapshots."""

    routes: dict[
        tuple[str, str, int | None, int | None, float | None],
        tuple[int, dict[str, Any] | None],
    ] = {}
    for snapshot in snapshots:
        priority = _snapshot_priority(snapshot)
        for raw in _snapshot_formats(snapshot):
            if raw.get("canonical_formats_only") is True:
                continue
            refs = raw.get("v1_format_ref")
            if not isinstance(refs, list):
                continue
            canonical = deepcopy(
                {key: value for key, value in raw.items() if key != "v1_format_ref"}
            )
            canonical.setdefault("publisher_domain", snapshot.get("publisher_domain"))
            for item in refs:
                try:
                    ref = LegacyFormatId.model_validate(item)
                except ValidationError:
                    continue
                owner = _catalog_owner(ref.agent_url)
                if owner is None or not _is_safe_public_https_owner(ref.agent_url):
                    continue
                key = (owner, ref.id, ref.width, ref.height, ref.duration_ms)
                existing = routes.get(key)
                if existing is None or priority < existing[0]:
                    routes[key] = (priority, canonical)
                elif priority == existing[0]:
                    routes[key] = (priority, None)

    def convert(context: LegacyFormatConversionContext) -> Mapping[str, Any] | None:
        ref = context.format_id
        owner = _catalog_owner(ref.agent_url)
        if owner is None:
            return None
        ranked = routes.get((owner, ref.id, ref.width, ref.height, ref.duration_ms))
        route = ranked[1] if ranked else None
        return deepcopy(route) if route else None

    return convert


def projection_adapters_from_catalog_snapshots(
    snapshots: Iterable[Mapping[str, Any]],
) -> ProjectionCatalogAdapters:
    """Build symmetric forward and durable reverse routes from one corpus."""

    materialized = list(snapshots)
    return ProjectionCatalogAdapters(
        legacy_format_converter=legacy_format_converter_from_catalog_snapshots(materialized),
        canonical_format_legacy_resolver=canonical_format_legacy_resolver_from_catalog_snapshots(
            materialized
        ),
    )


__all__ = [
    "AAO_CANONICAL_AGENT_URL",
    "CanonicalProductProjection",
    "CanonicalFormatLegacyResolutionContext",
    "CanonicalFormatLegacyResolutionError",
    "CanonicalFormatLegacyResolver",
    "CatalogIndex",
    "LegacyFormatConversionContext",
    "LegacyFormatConverter",
    "LegacyCreativeProjectionError",
    "ProjectionCatalogAdapters",
    "ProjectedFormat",
    "ProjectionDiagnostic",
    "build_catalog_index",
    "canonical_format_legacy_resolver_from_catalog_snapshots",
    "legacy_format_converter_from_catalog_snapshots",
    "load_rc3_catalog_index",
    "migrated_format_option_id",
    "normalize_legacy_creative_request",
    "project_legacy_format_id",
    "project_legacy_product",
    "project_canonical_response_to_legacy",
    "projection_adapters_from_catalog_snapshots",
    "resolve_legacy_format_refs",
]
