"""Which callers a report definition's ``contract_version`` may be served to.

AdCP 3.3 adds report-definition ``contract_version`` ``1.2``. Callers pinned
below 3.3 accept only ``1.0`` and ``1.1``, so a seller MUST serve them
offerings whose definitions are ``1.0`` or ``1.1``; a ``1.2`` offering serves
3.3 callers only (``core/reporting-report-definition.json``,
``x-adcp-validation.package_dimension``; adcontextprotocol/adcp#8983).

An offering pins its definition by ``report_definition_uri`` and
``report_definition_sha256`` only, so the version comes from the definition:
read from its bytes where the SDK holds them (the production verifier
registry), or declared by the seller in ``report_definition_versions``, keyed
by the same sha256. A definition the SDK can neither read nor find declared is
*undeclared*; ``undeclared_report_definitions`` decides how it is served.
"""

from __future__ import annotations

import json
import logging
import re
import warnings
from collections.abc import Iterable, Mapping
from typing import Any, Literal, get_args

from adcp._version import is_adcp_version_at_least, resolve_adcp_version_alias
from adcp.reporting._errors import ReliableReportingConfigurationError

logger = logging.getLogger(__name__)

ReportDefinitionContractVersion = Literal["1.0", "1.1", "1.2"]
"""A report definition's ``contract_version``."""

UndeclaredReportDefinitions = Literal["assume_pre_1_2", "withhold_from_pre_3_3"]
"""How an offering whose definition version is unknown is served.

``"assume_pre_1_2"`` (the default) treats it as ``1.0``/``1.1`` and serves it
to every caller, as SDKs before 10.0 did; every definition published before
AdCP 3.3 is one. ``"withhold_from_pre_3_3"`` withholds it from callers below
3.3, which is spec-safe even for a ``1.2`` definition nobody declared.
"""

_CONTRACT_VERSIONS: frozenset[str] = frozenset(get_args(ReportDefinitionContractVersion))
_UNDECLARED_POLICIES: frozenset[str] = frozenset(get_args(UndeclaredReportDefinitions))
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
#: The first release whose callers accept a 1.2 definition: any 3.3
#: prerelease sorts at or after this.
_FIRST_1_2_RELEASE = "3.3.0-0"


def caller_accepts_1_2(adcp_version: str | None) -> bool:
    """Whether a caller served at ``adcp_version`` accepts a 1.2 definition.

    True for 3.3, any 3.3 prerelease, and later. An absent version is the
    SDK's own release, which is 3.3. An unparseable one is treated as older.
    """
    if adcp_version is None:
        return True
    try:
        return is_adcp_version_at_least(
            resolve_adcp_version_alias(adcp_version.strip()), _FIRST_1_2_RELEASE
        )
    except ValueError:
        return False


def definition_contract_version(definition: bytes | str | Mapping[str, Any]) -> str | None:
    """The ``contract_version`` a report definition document states, if valid."""
    document: Any = definition
    if isinstance(definition, (bytes, str)):
        try:
            document = json.loads(definition)
        except (TypeError, ValueError):
            return None
    if not isinstance(document, Mapping):
        return None
    version = document.get("contract_version")
    return version if version in _CONTRACT_VERSIONS else None


class ReportDefinitionVersions:
    """The contract version of each report definition a service serves.

    Built from the seller's declarations (``report_definition_versions``,
    keyed by ``report_definition_sha256``) and the versions read from
    definition bytes the SDK holds. A declaration that contradicts the bytes
    is a configuration error.
    """

    def __init__(
        self,
        declared: Mapping[str, str] | None = None,
        *,
        undeclared: UndeclaredReportDefinitions = "assume_pre_1_2",
    ) -> None:
        if undeclared not in _UNDECLARED_POLICIES:
            raise ReliableReportingConfigurationError(
                "undeclared_report_definitions must be 'assume_pre_1_2' or "
                "'withhold_from_pre_3_3'"
            )
        self.undeclared: UndeclaredReportDefinitions = undeclared
        self._versions: dict[str, str] = {}
        for sha256, version in (declared or {}).items():
            key = _sha256_key(sha256)
            if version not in _CONTRACT_VERSIONS:
                raise ReliableReportingConfigurationError(
                    f"report_definition_versions[{sha256!r}] must be one of "
                    f"{sorted(_CONTRACT_VERSIONS)}, got {version!r}"
                )
            self._versions[key] = version

    def record(self, sha256: str, version: str | None, *, source: str) -> None:
        """Add a version read from definition bytes; refuse a contradiction."""
        if version is None:
            return
        key = _sha256_key(sha256)
        declared = self._versions.get(key)
        if declared is not None and declared != version:
            raise ReliableReportingConfigurationError(
                f"report definition {key} is contract_version {version} in {source}, "
                f"but report_definition_versions declares {declared}"
            )
        self._versions[key] = version

    def record_verified(self, registry: Any, keys: Iterable[Any]) -> None:
        """Record the version each verifier's proven definition bytes state."""
        for key in keys:
            try:
                verifier = registry.require(key)
            # A missing verifier fails offering readiness on its own.
            except Exception:  # nosec B112
                continue
            self.record(
                key.definition.report_definition_sha256,
                definition_contract_version(verifier.definition_bytes),
                source="its verifier's definition bytes",
            )

    def known(self) -> dict[str, str]:
        """Every definition digest with a known version."""
        return dict(self._versions)

    def refusal_message(self, offering: Mapping[str, Any]) -> str:
        """Why ``offering`` is not served below AdCP 3.3, worded as the TS SDK words it."""
        version = self.version(offering.get("report_definition_sha256"))
        reason = (
            f"its report definition is at contract_version {version}"
            if version is not None
            else "its report definition's contract_version is undeclared"
        )
        return (
            f'Reporting offering "{offering.get("offering_id")}" serves AdCP 3.3 callers '
            f"only: {reason}. Choose an offering from get_adcp_capabilities."
        )

    def version(self, sha256: Any) -> str | None:
        if not isinstance(sha256, str):
            return None
        return self._versions.get(sha256.strip().lower())

    def serves(self, offering: Mapping[str, Any], adcp_version: str | None) -> bool:
        """Whether ``offering`` may be served to a caller at ``adcp_version``."""
        if caller_accepts_1_2(adcp_version):
            return True
        version = self.version(offering.get("report_definition_sha256"))
        if version is None:
            return self.undeclared == "assume_pre_1_2"
        return version != "1.2"

    def warn_undeclared(self, offerings: Iterable[Mapping[str, Any]]) -> list[str]:
        """Warn once about every advertised offering whose version is unknown.

        Returns the undeclared definition digests (empty when all are known).
        """
        undeclared: dict[str, tuple[str, str]] = {}
        for offering in offerings:
            sha256 = offering.get("report_definition_sha256")
            if not isinstance(sha256, str) or self.version(sha256) is not None:
                continue
            undeclared.setdefault(
                sha256.strip().lower(),
                (str(offering.get("offering_id")), str(offering.get("report_definition_uri"))),
            )
        if not undeclared:
            return []
        lines = "\n".join(
            f"  offering {offering_id!r}: report_definition_versions[{sha256!r}] = "
            f"'<1.0, 1.1 or 1.2>'  # {uri}"
            for sha256, (offering_id, uri) in sorted(undeclared.items())
        )
        consequence = (
            "They are served to every caller, so a 1.2 definition among them reaches "
            "buyers pinned below AdCP 3.3, which accept only 1.0 and 1.1."
            if self.undeclared == "assume_pre_1_2"
            else "They are withheld from buyers pinned below AdCP 3.3."
        )
        message = (
            f"Reliable Reporting cannot tell the report definition contract_version of "
            f"{len(undeclared)} advertised offering(s). {consequence} Declare each one in "
            f"ReliableReportingService(report_definition_versions={{...}}):\n{lines}"
        )
        logger.warning("%s", message)
        warnings.warn(message, UserWarning, stacklevel=3)
        return sorted(undeclared)


def served_reporting_delivery(
    block: Mapping[str, Any], versions: ReportDefinitionVersions, adcp_version: str | None
) -> dict[str, Any]:
    """A ``reporting_delivery`` block without the offerings ``adcp_version`` cannot accept.

    ``offerings`` requires at least one entry, so a block left with none is
    returned empty: the caller advertises no reporting capability.
    ``reliability_statistics`` entries for withheld offerings go with them.
    """
    offerings = block.get("offerings")
    if not block or not isinstance(offerings, list):
        return dict(block)
    served = [
        offering
        for offering in offerings
        if not isinstance(offering, Mapping) or versions.serves(offering, adcp_version)
    ]
    if len(served) == len(offerings):
        return dict(block)
    if not served:
        return {}
    kept = {offering.get("offering_id") for offering in served if isinstance(offering, Mapping)}
    result = {**block, "offerings": served}
    statistics = block.get("reliability_statistics")
    if isinstance(statistics, list):
        remaining = [
            item
            for item in statistics
            if not isinstance(item, Mapping) or item.get("offering_id") in kept
        ]
        if remaining:
            result["reliability_statistics"] = remaining
        else:
            del result["reliability_statistics"]
    return result


def _sha256_key(sha256: Any) -> str:
    key = sha256.strip().lower() if isinstance(sha256, str) else ""
    if not _SHA256_RE.fullmatch(key):
        raise ReliableReportingConfigurationError(
            f"report_definition_versions keys must be report_definition_sha256 hex "
            f"digests, got {sha256!r}"
        )
    return key


__all__ = [
    "ReportDefinitionContractVersion",
    "ReportDefinitionVersions",
    "UndeclaredReportDefinitions",
    "caller_accepts_1_2",
    "definition_contract_version",
    "served_reporting_delivery",
]
