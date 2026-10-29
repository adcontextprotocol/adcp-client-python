"""Per-server selection of installed AdCP wire contracts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from adcp._version import normalize_to_release_precision, resolve_adcp_version_alias
from adcp.exceptions import ConfigurationError
from adcp.validation.envelope import SUPPORTED_WIRE_VERSIONS
from adcp.validation.schema_loader import list_validator_keys


def default_adcp_version(
    adcp_version: str | None, supported_versions: Sequence[str] | None
) -> str | None:
    """The pin to resolve when a server restricts ``supported_versions``.

    An explicit ``adcp_version`` is returned as given. Otherwise, when the
    adopter lists the wire versions the server serves, the newest listed
    release is the pin: a server restricted to ``["3.2"]`` serves 3.2 even
    while the SDK's own default pin is a newer line. ``None`` keeps the SDK
    default when nothing is restricted.
    """
    if adcp_version is not None or not supported_versions:
        return adcp_version
    from adcp._version import is_adcp_version_at_least, resolve_adcp_version

    newest: str | None = None
    for version in supported_versions:
        try:
            # Only a release this SDK can run on is a candidate: a
            # legacy-only selection (``["2.5"]``) keeps the SDK default.
            release = resolve_adcp_version(version)
        except ConfigurationError:
            continue
        if newest is None or is_adcp_version_at_least(release, newest):
            newest = release
    return newest


def handler_adcp_version_pin(handler: Any, explicit_version: str | None = None) -> str | None:
    """The trusted server pin: an explicit argument, else the handler's own.

    A handler declares its pin through ``get_adcp_version()`` or a
    ``_adcp_version`` attribute. ``None`` means the server has no pin of its
    own and inherits the SDK default.
    """
    if explicit_version is not None:
        return explicit_version
    if handler is None:
        return None
    getter = getattr(handler, "get_adcp_version", None)
    if callable(getter):
        try:
            candidate = getter()
        except Exception:
            candidate = None
        if isinstance(candidate, str):
            return candidate
    candidate = getattr(handler, "_adcp_version", None)
    return candidate if isinstance(candidate, str) else None


def resolve_application_adcp_version(
    handler: Any = None,
    *,
    adcp_version: str | None = None,
    supported_versions: Sequence[str] | None = None,
) -> str:
    """The release a server's handlers run on.

    Every dispatch path converts requests *to* this version before the
    handler runs and responses *from* it before they reach the buyer. It is
    the server's ``adcp_version`` pin when it has one, else the newest
    release in its ``supported_versions``, else the release packaged with
    the SDK. A handler composed by Decisioning carries both settings, so
    transports built on it agree without repeating them.

    Raises :class:`adcp.exceptions.ConfigurationError` when the pin is not
    a release this SDK can speak.
    """
    from adcp._version import resolve_adcp_version

    pin = handler_adcp_version_pin(handler, adcp_version)
    if supported_versions is None:
        supported_versions = getattr(handler, "_supported_adcp_versions", None)
    pin = default_adcp_version(pin, supported_versions)
    try:
        return resolve_adcp_version(pin)
    except ConfigurationError:
        # A frozen historical prerelease mount (``3.2.0-rc.1``) runs on that
        # exact bundle; it is served natively and never converted.
        if pin is not None:
            try:
                release = resolve_adcp_version_alias(normalize_to_release_precision(pin))
            except ValueError:
                raise ConfigurationError(f"adcp_version={pin!r} is not a valid version") from None
            if "-" in release and list_validator_keys(version=release):
                return release
        raise


def resolve_supported_versions(
    supported_versions: Sequence[str] | None,
    *,
    handler: Any = None,
    adcp_version: str | None = None,
) -> tuple[str, ...] | None:
    """Freeze an explicit selection, checking routing, bundles and trusted pin.

    A handler created by Decisioning composition carries its selection to
    subsequent transport factories. Ordinary handlers retain the SDK defaults
    when no selection is supplied. The handler is never mutated here.
    """
    if supported_versions is None:
        supported_versions = getattr(handler, "_supported_adcp_versions", None)
    if supported_versions is None:
        return None
    if isinstance(supported_versions, (str, bytes)) or not isinstance(supported_versions, Sequence):
        raise ConfigurationError("supported_versions must be a non-empty sequence of wire versions")
    versions = tuple(supported_versions)
    if not versions or any(not isinstance(version, str) for version in versions):
        raise ConfigurationError("supported_versions must be a non-empty sequence of wire versions")
    versions = tuple(dict.fromkeys(versions))
    unsupported = [version for version in versions if version not in SUPPORTED_WIRE_VERSIONS]
    if unsupported:
        raise ConfigurationError(
            f"supported_versions contains unsupported wire versions {unsupported!r}; "
            f"choose from {list(SUPPORTED_WIRE_VERSIONS)!r}"
        )
    unavailable = []
    for version in versions:
        keys = list_validator_keys(version=version)
        if not any(key.endswith("::request") for key in keys) or not any(
            key.endswith("::sync") for key in keys
        ):
            unavailable.append(version)
    if unavailable:
        raise ConfigurationError(
            f"supported_versions requires installed schema bundles for {unavailable!r}"
        )
    if adcp_version is not None:
        try:
            pin = resolve_adcp_version_alias(normalize_to_release_precision(adcp_version))
        except ValueError as exc:
            raise ConfigurationError(
                f"adcp_version={adcp_version!r} is not a valid version"
            ) from exc
        if pin not in {resolve_adcp_version_alias(version) for version in versions}:
            raise ConfigurationError(
                f"adcp_version={adcp_version!r} is outside supported_versions={list(versions)!r}"
            )
    return versions


def enforce_selected_version(
    operation: str,
    payload: dict[str, Any],
    supported_versions: tuple[str, ...] | None,
    *,
    default: str | None,
) -> str | None:
    """Gate SDK tools which dispatch outside the general task caller."""
    if supported_versions is None:
        return None
    from adcp._version import resolve_adcp_version
    from adcp.exceptions import ADCPTaskError
    from adcp.types import Error
    from adcp.validation.envelope import UnsupportedVersionError, detect_wire_version

    try:
        version = detect_wire_version(payload, supported=supported_versions)
        version = version or default or resolve_adcp_version(None)
        version = resolve_adcp_version_alias(normalize_to_release_precision(version))
        if version not in {resolve_adcp_version_alias(value) for value in supported_versions}:
            raise UnsupportedVersionError(version, supported_versions)
    except UnsupportedVersionError as exc:
        raise ADCPTaskError(
            operation=operation,
            errors=[
                Error(
                    code="VERSION_UNSUPPORTED",
                    message=str(exc),
                    details={
                        "claimed_version": exc.wire_value,
                        "supported_versions": list(exc.supported),
                    },
                )
            ],
        ) from exc
    return version


def project_supported_versions(
    response: dict[str, Any], supported_versions: tuple[str, ...] | None
) -> dict[str, Any]:
    """Copy capability metadata to match this server's accepted wire versions."""
    if supported_versions is None:
        return response
    projected = dict(response)
    adcp = dict(response.get("adcp") or {})
    adcp["supported_versions"] = list(supported_versions)
    adcp["major_versions"] = sorted(
        {int(version.split(".", 1)[0]) for version in supported_versions}
    )
    projected["adcp"] = adcp
    return projected
