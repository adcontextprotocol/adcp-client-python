"""Built-in inspection for immutable reporting file manifests."""

from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import hashlib
import io
import ipaddress
import json
import zlib
from collections.abc import Awaitable, Callable, Iterable, Mapping
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Protocol, cast
from urllib.parse import urljoin, urlsplit

import httpx
import idna
import jsonschema
import rfc8785
from pydantic import BaseModel, ValidationError

from adcp.reporting import ReportingInspectionContext, ReportingObservation
from adcp.signing._bounded_http import ResponseTooLargeError, async_read_limited_bytes
from adcp.signing._idna_canonicalize import canonicalize_host
from adcp.signing.ip_pinned_transport import build_async_ip_pinned_transport
from adcp.signing.jwks import SSRFValidationError
from adcp.types import (
    ReportingCanonicalContentDigest,
    ReportingCanonicalizationContract,
    ReportingControlTotal,
    ReportingFileManifest,
    ReportingReportDefinition,
)


class ReportingInspectionCode(str, Enum):
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    UNSAFE_RESOURCE = "UNSAFE_RESOURCE"
    RESOURCE_TOO_LARGE = "RESOURCE_TOO_LARGE"
    UNEXPECTED_CONTENT_TYPE = "UNEXPECTED_CONTENT_TYPE"
    MANIFEST_DIGEST_MISMATCH = "MANIFEST_DIGEST_MISMATCH"
    INVALID_MANIFEST = "INVALID_MANIFEST"
    MANIFEST_IDENTITY_MISMATCH = "MANIFEST_IDENTITY_MISMATCH"
    DUPLICATE_OBJECT = "DUPLICATE_OBJECT"
    OBJECT_SIZE_MISMATCH = "OBJECT_SIZE_MISMATCH"
    OBJECT_DIGEST_MISMATCH = "OBJECT_DIGEST_MISMATCH"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    UNSUPPORTED_COMPRESSION = "UNSUPPORTED_COMPRESSION"
    INVALID_ROWS = "INVALID_ROWS"
    ROW_SCHEMA_INVALID = "ROW_SCHEMA_INVALID"
    ROW_COUNT_MISMATCH = "ROW_COUNT_MISMATCH"
    CONTROL_TOTAL_MISMATCH = "CONTROL_TOTAL_MISMATCH"
    UNSUPPORTED_CONTROL_TOTAL = "UNSUPPORTED_CONTROL_TOTAL"
    CANONICAL_DIGEST_MISMATCH = "CANONICAL_DIGEST_MISMATCH"
    INVALID_CONTRACT = "INVALID_CONTRACT"


class ReportingInspectionError(RuntimeError):
    def __init__(
        self, code: ReportingInspectionCode, message: str, *, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ReportingResourceReader(Protocol):
    """Read a bounded locator; adapters own credentials and provider routing."""

    async def read(self, locator: str, *, base: str | None = None, max_bytes: int) -> bytes:
        raise NotImplementedError


ReportingCredentialProvider = Callable[[str], Awaitable[Mapping[str, str]]]
ReportingTrustedOriginPolicy = Callable[[str], bool]
ControlTotalCalculator = Callable[
    [list[dict[str, Any]], ReportingReportDefinition], list[ReportingControlTotal]
]


class HttpsReportingResourceReader:
    """Redirect-free, DNS-pinned reader for explicitly trusted HTTPS origins.

    ``trusted_origins`` must contain the seller, provider, and/or registry
    origins authorized by the selected reporting offering.  This deliberately
    has no permissive default: ledger-controlled contract URLs are not an
    authorization decision.  A policy is useful when the trusted origin set is
    resolved from an authenticated principal at inspection time.
    """

    def __init__(
        self,
        credential_provider: ReportingCredentialProvider | None = None,
        *,
        timeout_seconds: float = 10.0,
        trusted_origins: Iterable[str] | None = None,
        trusted_origin_policy: ReportingTrustedOriginPolicy | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if trusted_origins is not None and trusted_origin_policy is not None:
            raise ValueError("pass trusted_origins or trusted_origin_policy, not both")
        self._credential_provider = credential_provider
        self._timeout = timeout_seconds
        self._trusted_origins = (
            frozenset(_origin(value) for value in trusted_origins)
            if trusted_origins is not None
            else None
        )
        self._trusted_origin_policy = trusted_origin_policy

    async def read(
        self,
        locator: str,
        *,
        base: str | None = None,
        max_bytes: int,
        expected_content_types: frozenset[str] | None = None,
    ) -> bytes:
        url = urljoin(base, locator) if base else locator
        origin = _origin(url)
        if base:
            if origin != _origin(base):
                raise ReportingInspectionError(
                    ReportingInspectionCode.UNSAFE_RESOURCE,
                    "reporting object reference crossed the manifest origin",
                )
        origin_text = _origin_text(origin)
        if self._trusted_origins is not None:
            trusted = origin in self._trusted_origins
        elif self._trusted_origin_policy is not None:
            trusted = self._trusted_origin_policy(origin_text)
        else:
            trusted = False
        if not trusted:
            raise ReportingInspectionError(
                ReportingInspectionCode.UNSAFE_RESOURCE,
                f"reporting resource origin {origin_text!r} is not trusted",
            )
        try:
            # Host resolution in the pin factory uses socket.getaddrinfo.  Keep
            # it off the event loop so the reconciler's inspection deadline can
            # still cancel the awaiting task during a slow DNS lookup.
            transport = await asyncio.to_thread(
                build_async_ip_pinned_transport, url, allowed_ports=frozenset({443})
            )
            headers = (
                dict(await self._credential_provider(url)) if self._credential_provider else {}
            )
            async with httpx.AsyncClient(
                transport=transport,
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    if 300 <= response.status_code < 400:
                        raise ReportingInspectionError(
                            ReportingInspectionCode.UNSAFE_RESOURCE,
                            "redirects are not allowed for reporting resources",
                        )
                    if response.status_code != 200:
                        raise ReportingInspectionError(
                            ReportingInspectionCode.RESOURCE_UNAVAILABLE,
                            f"reporting resource returned HTTP {response.status_code}",
                            retryable=response.status_code >= 500
                            or response.status_code in {408, 429},
                        )
                    if expected_content_types is not None:
                        content_type = response.headers.get("content-type", "").split(";", 1)[0]
                        if content_type.strip().lower() not in expected_content_types:
                            raise ReportingInspectionError(
                                ReportingInspectionCode.UNEXPECTED_CONTENT_TYPE,
                                "reporting resource returned an unexpected content type",
                            )
                    return await async_read_limited_bytes(response, limit=max_bytes)
        except ReportingInspectionError:
            raise
        except ResponseTooLargeError as error:
            raise ReportingInspectionError(
                ReportingInspectionCode.RESOURCE_TOO_LARGE, str(error)
            ) from error
        except SSRFValidationError as error:
            raise ReportingInspectionError(
                ReportingInspectionCode.UNSAFE_RESOURCE,
                "reporting resource failed public-network validation",
            ) from error
        except (httpx.HTTPError, OSError, ValueError) as error:
            raise ReportingInspectionError(
                ReportingInspectionCode.RESOURCE_UNAVAILABLE,
                "reporting resource fetch failed",
                retryable=True,
            ) from error


def _origin(url: str) -> tuple[str, str, int]:
    """Return a normalized HTTPS origin while rejecting ambiguous locators."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ReportingInspectionError(
            ReportingInspectionCode.UNSAFE_RESOURCE,
            "reporting resources must be credential-free absolute HTTPS URLs",
        )
    try:
        # IP literals are never valid reporting resource origins, even when
        # publicly routable: safe-fetch policy authorizes DNS names only.
        ipaddress.ip_address(parsed.hostname)
        raise ReportingInspectionError(
            ReportingInspectionCode.UNSAFE_RESOURCE,
            "reporting resource URLs must not use IP-literal hosts",
        )
    except ValueError:
        pass
    try:
        hostname = canonicalize_host(parsed.hostname)
        port = parsed.port or 443
    except (ValueError, UnicodeError, idna.IDNAError) as error:
        raise ReportingInspectionError(
            ReportingInspectionCode.UNSAFE_RESOURCE,
            "reporting resource URL has an invalid host or port",
        ) from error
    if port != 443:
        raise ReportingInspectionError(
            ReportingInspectionCode.UNSAFE_RESOURCE,
            "reporting resources must use port 443",
        )
    return ("https", hostname, port)


def _origin_text(origin: tuple[str, str, int]) -> str:
    scheme, host, port = origin
    return f"{scheme}://{host}" if port == 443 else f"{scheme}://{host}:{port}"


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _dump(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def _same(left: object, right: object) -> bool:
    return _dump(left) == _dump(right)


def _totals_same(left: list[ReportingControlTotal], right: list[ReportingControlTotal]) -> bool:
    def normalized(values: list[ReportingControlTotal]) -> list[object]:
        return sorted(
            (_dump(item) for item in values),
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )

    return normalized(left) == normalized(right)


_MAX_SCHEMA_DEPTH = 64
_MAX_SCHEMA_NODES = 10_000


def _validate_row_schema(schema: dict[str, Any], expected_dialect: str) -> None:
    """Apply the reporting profile's non-executable schema safety contract."""
    if schema.get("$schema") != expected_dialect:
        raise ReportingInspectionError(
            ReportingInspectionCode.INVALID_CONTRACT,
            "reporting row schema does not declare the revision schema dialect",
        )
    stack: list[tuple[object, int]] = [(schema, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_SCHEMA_NODES or depth > _MAX_SCHEMA_DEPTH:
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                "reporting row schema exceeds static safety limits",
            )
        if isinstance(value, dict):
            if "$dynamicRef" in value or "$recursiveRef" in value or "$vocabulary" in value:
                raise ReportingInspectionError(
                    ReportingInspectionCode.INVALID_CONTRACT,
                    "reporting row schema uses an unsupported reference or vocabulary",
                )
            ref = value.get("$ref")
            if ref is not None and (not isinstance(ref, str) or not ref.startswith("#")):
                raise ReportingInspectionError(
                    ReportingInspectionCode.INVALID_CONTRACT,
                    "reporting row schema contains a non-local reference",
                )
            for key, child in value.items():
                # stdlib/jsonschema evaluates Python backtracking regexes with
                # no per-pattern deadline. Until the SDK uses a linear-time
                # engine, reject regex-bearing contracts before compilation;
                # even a short nested-quantifier pattern can be catastrophic.
                if key == "pattern" and isinstance(child, str):
                    raise ReportingInspectionError(
                        ReportingInspectionCode.INVALID_CONTRACT,
                        "reporting row schema contains an unsupported regular expression",
                    )
                if key == "patternProperties" and isinstance(child, dict):
                    raise ReportingInspectionError(
                        ReportingInspectionCode.INVALID_CONTRACT,
                        "reporting row schema contains unsupported pattern properties",
                    )
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    _reject_local_ref_cycles(schema)


def _local_refs(value: object) -> set[str]:
    if isinstance(value, dict):
        result = {value["$ref"]} if isinstance(value.get("$ref"), str) else set()
        for child in value.values():
            result.update(_local_refs(child))
        return result
    if isinstance(value, list):
        return set().union(*(_local_refs(child) for child in value)) if value else set()
    return set()


def _resolve_json_pointer(document: dict[str, Any], ref: str) -> object:
    value: object = document
    if ref == "#":
        return value
    for token in ref.removeprefix("#/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                "reporting row schema contains an unresolved local reference",
            )
    return value


def _reject_local_ref_cycles(schema: dict[str, Any]) -> None:
    """Reject local reference cycles before jsonschema can recursively follow them."""
    targets: dict[str, set[str]] = {}

    def edges(ref: str) -> set[str]:
        if ref not in targets:
            targets[ref] = _local_refs(_resolve_json_pointer(schema, ref))
        return targets[ref]

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(ref: str) -> None:
        if ref in visiting:
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                "reporting row schema contains a local reference cycle",
            )
        if ref in visited:
            return
        visiting.add(ref)
        for child in edges(ref):
            visit(child)
        visiting.remove(ref)
        visited.add(ref)

    for ref in _local_refs(schema):
        visit(ref)


class _InvalidJsonError(ValueError):
    pass


def _json_loads_no_duplicates(body: bytes) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _InvalidJsonError(f"duplicate JSON object key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise _InvalidJsonError(f"non-finite JSON value {value!r}")

    try:
        return json.loads(body, object_pairs_hook=object_pairs, parse_constant=reject_constant)
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError) as error:
        raise _InvalidJsonError("invalid JSON") from error


def _decode_rows(
    body: bytes,
    format_name: str,
    compression: str,
    *,
    max_decoded_bytes: int,
    max_rows: int,
) -> tuple[list[dict[str, Any]], int]:
    if compression == "gzip":
        try:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decoded = decoder.decompress(body, max_decoded_bytes + 1)
            if (
                len(decoded) > max_decoded_bytes
                or decoder.unconsumed_tail
                or not decoder.eof
                or decoder.unused_data
            ):
                raise ReportingInspectionError(
                    ReportingInspectionCode.RESOURCE_TOO_LARGE,
                    "decoded reporting object exceeds the configured byte limit",
                )
            body = decoded
        except ReportingInspectionError:
            raise
        except zlib.error as error:
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_ROWS, "invalid gzip reporting object"
            ) from error
    elif compression != "none":
        raise ReportingInspectionError(
            ReportingInspectionCode.UNSUPPORTED_COMPRESSION,
            f"built-in reader does not support {compression} compression",
        )
    decoded_size = len(body)
    try:
        text = body.decode("utf-8")
        if format_name == "jsonl":
            values = []
            for line in text.splitlines():
                if line.strip():
                    values.append(_json_loads_no_duplicates(line.encode("utf-8")))
                    if len(values) > max_rows:
                        raise ReportingInspectionError(
                            ReportingInspectionCode.RESOURCE_TOO_LARGE,
                            "reporting object exceeds the configured row limit",
                        )
        elif format_name == "csv":
            parser = csv.DictReader(io.StringIO(text, newline=""))
            if parser.fieldnames and len(parser.fieldnames) != len(set(parser.fieldnames)):
                raise ReportingInspectionError(
                    ReportingInspectionCode.INVALID_ROWS,
                    "CSV reporting object has duplicate column names",
                )
            values = []
            for value in parser:
                values.append(value)
                if len(values) > max_rows:
                    raise ReportingInspectionError(
                        ReportingInspectionCode.RESOURCE_TOO_LARGE,
                        "reporting object exceeds the configured row limit",
                    )
        else:
            raise ReportingInspectionError(
                ReportingInspectionCode.UNSUPPORTED_FORMAT,
                f"built-in reader does not support {format_name}; install a provider adapter",
            )
    except ReportingInspectionError:
        raise
    except (_InvalidJsonError, UnicodeDecodeError, json.JSONDecodeError, csv.Error) as error:
        raise ReportingInspectionError(
            ReportingInspectionCode.INVALID_ROWS, "reporting object is not valid row data"
        ) from error
    if any(not isinstance(value, dict) for value in values):
        raise ReportingInspectionError(
            ReportingInspectionCode.INVALID_ROWS, "every reporting row must be an object"
        )
    return cast(list[dict[str, Any]], values), decoded_size


def _decimal_string(value: Decimal, value_type: str) -> str:
    if value_type == "integer":
        if value != value.to_integral_value():
            raise ValueError("non-integral value")
        return str(int(value))
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _default_control_totals(
    rows: list[dict[str, Any]],
    definition: ReportingReportDefinition,
    expected: list[ReportingControlTotal],
) -> list[ReportingControlTotal]:
    metrics = {metric.name: metric for metric in definition.metrics}
    totals: list[ReportingControlTotal] = []
    for target in expected:
        metric = metrics.get(target.name)
        if metric is None:
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                f"control total {target.name!r} is absent from the report definition",
            )
        aggregation = str(metric.aggregation)
        expression = metric.source_expression
        if aggregation == "count" and expression == "*":
            value = Decimal(len(rows))
        else:
            if not expression.isidentifier():
                raise ReportingInspectionError(
                    ReportingInspectionCode.UNSUPPORTED_CONTROL_TOTAL,
                    f"control total {target.name!r} requires a provider expression adapter",
                )
            try:
                values = [
                    Decimal(str(row[expression])) for row in rows if row.get(expression) is not None
                ]
            except (InvalidOperation, KeyError, ValueError) as error:
                raise ReportingInspectionError(
                    ReportingInspectionCode.INVALID_ROWS,
                    f"control total source {expression!r} is not numeric",
                ) from error
            if aggregation == "count":
                value = Decimal(len(values))
            elif aggregation == "sum":
                value = sum(values, Decimal(0))
            elif aggregation == "min" and values:
                value = min(values)
            elif aggregation == "max" and values:
                value = max(values)
            elif aggregation == "average" and values:
                value = sum(values, Decimal(0)) / Decimal(len(values))
            else:
                raise ReportingInspectionError(
                    ReportingInspectionCode.UNSUPPORTED_CONTROL_TOTAL,
                    f"control total {target.name!r} requires a custom calculator",
                )
        try:
            payload = target.model_dump(mode="json", exclude_none=True)
            payload["value"] = _decimal_string(value, str(target.value_type))
            totals.append(ReportingControlTotal.model_validate(payload))
        except (ValueError, ValidationError) as error:
            raise ReportingInspectionError(
                ReportingInspectionCode.CONTROL_TOTAL_MISMATCH,
                f"control total {target.name!r} cannot be represented canonically",
            ) from error
    return totals


class ManifestReportingInspector:
    """Retrieve and verify a manifest materialization into an observation."""

    def __init__(
        self,
        reader: ReportingResourceReader,
        *,
        max_manifest_bytes: int = 1024 * 1024,
        max_object_bytes: int = 128 * 1024 * 1024,
        max_contract_bytes: int = 4 * 1024 * 1024,
        max_total_object_bytes: int = 256 * 1024 * 1024,
        max_total_decoded_bytes: int = 256 * 1024 * 1024,
        max_total_rows: int = 500_000,
        max_files: int = 4_096,
        control_total_calculator: ControlTotalCalculator | None = None,
    ) -> None:
        if (
            min(
                max_manifest_bytes,
                max_object_bytes,
                max_contract_bytes,
                max_total_object_bytes,
                max_total_decoded_bytes,
                max_total_rows,
                max_files,
            )
            <= 0
        ):
            raise ValueError("inspection byte limits must be positive")
        self._reader = reader
        self._max_manifest_bytes = max_manifest_bytes
        self._max_object_bytes = max_object_bytes
        self._max_contract_bytes = max_contract_bytes
        self._max_total_object_bytes = max_total_object_bytes
        self._max_total_decoded_bytes = max_total_decoded_bytes
        self._max_total_rows = max_total_rows
        self._max_files = max_files
        self._control_total_calculator = control_total_calculator

    async def __call__(self, context: ReportingInspectionContext) -> ReportingObservation:
        materialization = context.materialization
        resource = materialization.resource
        revision = context.revision
        obligation = context.obligation
        if resource is None or str(resource.kind) != "manifest" or not resource.manifest_sha256:
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_MANIFEST,
                "materialization does not expose a digest-pinned manifest",
            )

        manifest_bytes = await self._read(
            resource.location,
            max_bytes=self._max_manifest_bytes,
            expected_content_types=frozenset(
                {"application/json", "application/vnd.adcp.reporting-file-manifest+json"}
            ),
        )
        manifest_digest = _digest(manifest_bytes)
        if manifest_digest.lower() != resource.manifest_sha256.lower():
            raise ReportingInspectionError(
                ReportingInspectionCode.MANIFEST_DIGEST_MISMATCH,
                "manifest digest does not match the resource descriptor",
            )
        try:
            manifest_value = _json_loads_no_duplicates(manifest_bytes)
            manifest = ReportingFileManifest.model_validate(manifest_value)
        except (ValidationError, _InvalidJsonError) as error:
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_MANIFEST,
                "manifest is not valid reporting-file-manifest 1.0",
            ) from error
        if not (
            manifest.reporting_revision_id == revision.reporting_revision_id
            and manifest.reporting_obligation_id == obligation.reporting_obligation_id
            and manifest.reporting_materialization_id
            == materialization.reporting_materialization_id
            and _same(manifest.period, revision.period)
            and manifest.row_count == revision.row_count
            and _totals_same(manifest.control_totals, revision.control_totals)
        ):
            raise ReportingInspectionError(
                ReportingInspectionCode.MANIFEST_IDENTITY_MISMATCH,
                "manifest does not match the reporting ledger records",
            )
        refs = [entry.object_ref.root for entry in manifest.files]
        if len(refs) != len(set(refs)):
            raise ReportingInspectionError(
                ReportingInspectionCode.DUPLICATE_OBJECT,
                "manifest contains duplicate object references",
            )
        if len(manifest.files) > self._max_files:
            raise ReportingInspectionError(
                ReportingInspectionCode.RESOURCE_TOO_LARGE,
                "manifest exceeds the configured file limit",
            )
        verification = materialization.verification
        physical = {
            item.object_ref.root: item.value.lower()
            for item in (verification.physical_checksums if verification else None) or []
            if str(item.algorithm) == "sha256"
        }
        if any(
            physical.get(entry.object_ref.root) != entry.sha256.lower() for entry in manifest.files
        ):
            raise ReportingInspectionError(
                ReportingInspectionCode.MANIFEST_IDENTITY_MISMATCH,
                "manifest checksums do not match producer verification evidence",
            )
        if manifest.total_size_bytes != sum(entry.size_bytes for entry in manifest.files):
            raise ReportingInspectionError(
                ReportingInspectionCode.OBJECT_SIZE_MISMATCH,
                "manifest total_size_bytes does not equal its file entries",
            )
        if manifest.row_count != sum(entry.row_count for entry in manifest.files):
            raise ReportingInspectionError(
                ReportingInspectionCode.ROW_COUNT_MISMATCH,
                "manifest row_count does not equal its file entries",
            )

        rows: list[dict[str, Any]] = []
        total_object_bytes = 0
        total_decoded_bytes = 0
        total_rows = 0
        for entry in manifest.files:
            object_ref = entry.object_ref.root
            if entry.size_bytes > self._max_object_bytes:
                raise ReportingInspectionError(
                    ReportingInspectionCode.RESOURCE_TOO_LARGE,
                    "manifest object exceeds the configured byte limit",
                )
            total_object_bytes += entry.size_bytes
            total_rows += entry.row_count
            if (
                total_object_bytes > self._max_total_object_bytes
                or total_rows > self._max_total_rows
            ):
                raise ReportingInspectionError(
                    ReportingInspectionCode.RESOURCE_TOO_LARGE,
                    "manifest exceeds the configured aggregate inspection limits",
                )
            body = await self._read(
                object_ref,
                base=resource.location,
                max_bytes=min(self._max_object_bytes, entry.size_bytes + 1),
                expected_content_types=_object_content_types(str(manifest.format)),
            )
            if len(body) != entry.size_bytes:
                raise ReportingInspectionError(
                    ReportingInspectionCode.OBJECT_SIZE_MISMATCH,
                    f"reporting object {object_ref!r} has the wrong size",
                )
            if _digest(body).lower() != entry.sha256.lower():
                raise ReportingInspectionError(
                    ReportingInspectionCode.OBJECT_DIGEST_MISMATCH,
                    f"reporting object {object_ref!r} failed checksum validation",
                )
            file_rows, decoded_bytes = _decode_rows(
                body,
                str(manifest.format),
                str(manifest.compression),
                max_decoded_bytes=self._max_object_bytes,
                max_rows=self._max_total_rows - len(rows),
            )
            total_decoded_bytes += decoded_bytes
            if total_decoded_bytes > self._max_total_decoded_bytes:
                raise ReportingInspectionError(
                    ReportingInspectionCode.RESOURCE_TOO_LARGE,
                    "manifest exceeds the configured aggregate decoded-byte limit",
                )
            if len(file_rows) != entry.row_count:
                raise ReportingInspectionError(
                    ReportingInspectionCode.ROW_COUNT_MISMATCH,
                    f"reporting object {object_ref!r} has the wrong row count",
                )
            rows.extend(file_rows)

        schema = await self._read_json_contract(
            str(revision.schema_uri),
            revision.schema_sha256,
            "row schema",
            frozenset({"application/schema+json", "application/json"}),
        )
        _validate_row_schema(schema, str(revision.schema_dialect))
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
            validator = jsonschema.Draft202012Validator(schema)
            first_error = next(
                (error for row in rows for error in validator.iter_errors(row)), None
            )
        except (jsonschema.SchemaError, RecursionError, TypeError, ValueError) as error:
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT, "reporting row schema is invalid"
            ) from error
        if first_error is not None:
            raise ReportingInspectionError(
                ReportingInspectionCode.ROW_SCHEMA_INVALID,
                "a reporting row failed its pinned schema",
            )

        definition_json = await self._read_json_contract(
            str(revision.report_definition_uri),
            revision.report_definition_sha256,
            "report definition",
            frozenset({"application/vnd.adcp.reporting-definition+json"}),
        )
        try:
            definition = ReportingReportDefinition.model_validate(definition_json)
        except ValidationError as error:
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                "report definition is invalid",
            ) from error
        if (
            definition.report_definition_id != revision.report_definition_id
            or definition.reporting_profile != revision.reporting_profile
        ):
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                "report definition identity does not match the revision",
            )
        policy_keys = [
            (policy.finality_policy_id, str(policy.basis))
            for policy in definition.finality_policies
        ]
        if len({policy.finality_policy_id for policy in definition.finality_policies}) != len(
            definition.finality_policies
        ):
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                "report definition has duplicate finality policy identifiers",
            )
        if (
            str(revision.finality) == "official"
            and (
                revision.finality_policy_id,
                str(revision.finality_basis),
            )
            not in policy_keys
        ):
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                "official revision finality does not match the report definition",
            )

        observed_totals = (
            self._control_total_calculator(rows, definition)
            if self._control_total_calculator
            else _default_control_totals(rows, definition, revision.control_totals)
        )
        if not _totals_same(observed_totals, revision.control_totals):
            raise ReportingInspectionError(
                ReportingInspectionCode.CONTROL_TOTAL_MISMATCH,
                "recomputed control totals do not match the revision",
            )

        canonical_digest = None
        if revision.canonical_content_digest:
            canonical_digest = await self._canonical_digest(
                rows, revision.canonical_content_digest, revision.schema_sha256
            )
        return ReportingObservation(
            row_count=len(rows),
            control_totals=list(observed_totals),
            canonical_content_digest=canonical_digest,
            manifest_sha256=manifest_digest,
        )

    async def _read_json_contract(
        self,
        uri: str,
        expected_digest: str,
        description: str,
        expected_content_types: frozenset[str],
    ) -> dict[str, Any]:
        body = await self._read(
            uri,
            max_bytes=self._max_contract_bytes,
            expected_content_types=expected_content_types,
        )
        if _digest(body).lower() != expected_digest.lower():
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                f"{description} digest mismatch",
            )
        try:
            value = _json_loads_no_duplicates(body)
        except _InvalidJsonError as error:
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                f"{description} is not valid UTF-8 JSON",
            ) from error
        if not isinstance(value, dict):
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                f"{description} root must be an object",
            )
        return value

    async def _read(
        self,
        locator: str,
        *,
        max_bytes: int,
        base: str | None = None,
        expected_content_types: frozenset[str] | None = None,
    ) -> bytes:
        """Use typed content checks for the built-in HTTPS reader.

        The small resource-reader protocol intentionally remains byte-only so
        existing destination adapters stay compatible; those adapters own
        equivalent media-type validation for their native transports.
        """
        if isinstance(self._reader, HttpsReportingResourceReader):
            return await self._reader.read(
                locator,
                base=base,
                max_bytes=max_bytes,
                expected_content_types=expected_content_types,
            )
        return await self._reader.read(locator, base=base, max_bytes=max_bytes)

    async def _canonical_digest(
        self,
        rows: list[dict[str, Any]],
        expected: ReportingCanonicalContentDigest,
        schema_sha256: str,
    ) -> ReportingCanonicalContentDigest:
        contract_json = await self._read_json_contract(
            str(expected.canonicalization_uri),
            expected.canonicalization_sha256,
            "canonicalization contract",
            frozenset({"application/vnd.adcp.reporting-canonicalization+json"}),
        )
        try:
            contract = ReportingCanonicalizationContract.model_validate(contract_json)
        except ValidationError as error:
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                "canonicalization contract is invalid",
            ) from error
        if contract.schema_sha256.lower() != schema_sha256.lower():
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                "canonicalization contract targets a different row schema",
            )
        keys = [item.root for item in contract.primary_keys]
        try:
            _validate_golden_vectors(contract, keys)
            value = hashlib.sha256(_canonical_rows_bytes(rows, keys)).hexdigest()
        except ReportingInspectionError:
            raise
        except (KeyError, TypeError, ValueError, rfc8785.CanonicalizationError) as error:
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_ROWS,
                "reporting rows cannot be canonicalized with the declared primary keys",
            ) from error
        if value.lower() != expected.value.lower():
            raise ReportingInspectionError(
                ReportingInspectionCode.CANONICAL_DIGEST_MISMATCH,
                "canonical logical-content digest does not match the revision",
            )
        return expected


def _object_content_types(format_name: str) -> frozenset[str]:
    if format_name == "jsonl":
        return frozenset({"application/x-ndjson", "application/ndjson", "application/jsonl"})
    if format_name == "csv":
        return frozenset({"text/csv"})
    return frozenset()


def _canonical_rows_bytes(rows: list[dict[str, Any]], keys: list[str]) -> bytes:
    """Canonicalize rows using the byte ordering required by adcp_jcs_rows_v1."""
    encoded: list[tuple[bytes, bytes]] = []
    for row in rows:
        identity = [row[key] for key in keys]
        if any(isinstance(value, (dict, list)) for value in identity):
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_ROWS,
                "reporting primary keys must be scalar values",
            )
        encoded.append((rfc8785.dumps(identity), rfc8785.dumps(row)))
    encoded.sort(key=lambda item: item[0])
    if any(left[0] == right[0] for left, right in zip(encoded, encoded[1:])):
        raise ReportingInspectionError(
            ReportingInspectionCode.INVALID_ROWS,
            "reporting rows contain duplicate primary keys",
        )
    return b"[" + b",".join(row for _identity, row in encoded) + b"]"


def _validate_golden_vectors(contract: ReportingCanonicalizationContract, keys: list[str]) -> None:
    vectors: list[Any] = [
        contract.golden_vectors.empty_report,
        contract.golden_vectors.ordering_encoding,
        *(contract.golden_vectors.additional or []),
    ]
    if len({vector.name for vector in vectors}) != len(vectors):
        raise ReportingInspectionError(
            ReportingInspectionCode.INVALID_CONTRACT,
            "canonicalization contract has duplicate golden-vector names",
        )
    for vector in vectors:
        try:
            declared = base64.b64decode(vector.canonical_utf8_base64, validate=True)
            reproduced = _canonical_rows_bytes(vector.input_rows, keys)
        except (
            binascii.Error,
            ValueError,
            TypeError,
            KeyError,
            rfc8785.CanonicalizationError,
        ) as error:
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                "canonicalization contract has an invalid golden vector",
            ) from error
        if (
            declared != reproduced
            or hashlib.sha256(declared).hexdigest().lower() != vector.sha256.lower()
        ):
            raise ReportingInspectionError(
                ReportingInspectionCode.INVALID_CONTRACT,
                "canonicalization contract golden vector does not match adcp_jcs_rows_v1",
            )


__all__ = [
    "ControlTotalCalculator",
    "HttpsReportingResourceReader",
    "ManifestReportingInspector",
    "ReportingCredentialProvider",
    "ReportingInspectionCode",
    "ReportingInspectionError",
    "ReportingResourceReader",
    "ReportingTrustedOriginPolicy",
]
