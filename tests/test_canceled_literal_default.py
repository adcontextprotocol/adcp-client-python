"""Tests for issue #641 — canceled: Literal[True] = True destructive default.

UpdateMediaBuyRequest and PackageUpdate previously had ``canceled: Literal[True] = True``,
meaning any call that omitted the field silently triggered irreversible cancellation.
The fix widens the type to ``Literal[True] | None = None`` so omission is non-destructive.
"""

from __future__ import annotations

import pytest


class TestUpdateMediaBuyRequestCanceledDefault:
    """UpdateMediaBuyRequest.canceled must default to None (non-destructive)."""

    _ACCOUNT_KWARGS = {
        "account": {"account_id": "acc_test_001"},
        "media_buy_id": "mbuy-123",
        "idempotency_key": "a" * 16,
    }

    def test_canceled_defaults_to_none_when_omitted(self) -> None:
        from adcp.types.generated_poc.media_buy.update_media_buy_request import (
            UpdateMediaBuyRequest,
        )

        req = UpdateMediaBuyRequest(**self._ACCOUNT_KWARGS)
        assert req.canceled is None

    def test_canceled_true_still_accepted(self) -> None:
        from adcp.types.generated_poc.media_buy.update_media_buy_request import (
            UpdateMediaBuyRequest,
        )

        req = UpdateMediaBuyRequest(**self._ACCOUNT_KWARGS, canceled=True)
        assert req.canceled is True

    def test_canceled_none_excluded_from_wire_payload(self) -> None:
        """When canceled is None, model_dump(exclude_none=True) omits it — no cancellation on wire."""
        from adcp.types.generated_poc.media_buy.update_media_buy_request import (
            UpdateMediaBuyRequest,
        )

        req = UpdateMediaBuyRequest(**self._ACCOUNT_KWARGS)
        payload = req.model_dump(mode="json", exclude_none=True)
        assert "canceled" not in payload

    def test_canceled_true_present_in_wire_payload(self) -> None:
        """Explicit canceled=True must appear in the wire payload to trigger cancellation."""
        from adcp.types.generated_poc.media_buy.update_media_buy_request import (
            UpdateMediaBuyRequest,
        )

        req = UpdateMediaBuyRequest(**self._ACCOUNT_KWARGS, canceled=True)
        payload = req.model_dump(mode="json", exclude_none=True)
        assert payload["canceled"] is True

    def test_canceled_false_rejected(self) -> None:
        """Literal[True] still rejects False — the field is a one-way commit signal."""
        from pydantic import ValidationError

        from adcp.types.generated_poc.media_buy.update_media_buy_request import (
            UpdateMediaBuyRequest,
        )

        with pytest.raises(ValidationError):
            UpdateMediaBuyRequest(**self._ACCOUNT_KWARGS, canceled=False)  # type: ignore[arg-type]


class TestPackageUpdateCanceledDefault:
    """PackageUpdate.canceled must default to None (non-destructive)."""

    def test_canceled_defaults_to_none_when_omitted(self) -> None:
        from adcp.types.generated_poc.media_buy.package_update import PackageUpdate

        pkg = PackageUpdate(package_id="pkg-1")
        assert pkg.canceled is None

    def test_canceled_true_still_accepted(self) -> None:
        from adcp.types.generated_poc.media_buy.package_update import PackageUpdate

        pkg = PackageUpdate(package_id="pkg-1", canceled=True)
        assert pkg.canceled is True

    def test_canceled_none_excluded_from_wire_payload(self) -> None:
        from adcp.types.generated_poc.media_buy.package_update import PackageUpdate

        pkg = PackageUpdate(package_id="pkg-1")
        payload = pkg.model_dump(mode="json", exclude_none=True)
        assert "canceled" not in payload

    def test_canceled_true_present_in_wire_payload(self) -> None:
        from adcp.types.generated_poc.media_buy.package_update import PackageUpdate

        pkg = PackageUpdate(package_id="pkg-1", canceled=True)
        payload = pkg.model_dump(mode="json", exclude_none=True)
        assert payload["canceled"] is True


class TestFixIdempotency:
    """The post-gen fix must be idempotent — running it twice produces the same output."""

    def test_fix_is_idempotent_on_synthesized_source(self) -> None:
        from scripts.post_generate_fixes import _CANCELED_FIELD_RE

        already_fixed = (
            "    canceled: Annotated[\n"
            "        Literal[True] | None,\n"
            "        Field(\n"
            "            description='Cancel this specific package. Cancellation is irreversible"
            " — canceled packages stop delivery and cannot be reactivated."
            " Sellers MAY reject with NOT_CANCELLABLE.'\n"
            "        ),\n"
            "    ] = None\n"
        )

        result, count = _CANCELED_FIELD_RE.subn(
            r"\1Literal[True] | None\2 = None",
            already_fixed,
        )
        assert count == 0, "fix must not re-apply to already-widened source"
        assert result == already_fixed

    def test_fix_matches_destructive_source(self) -> None:
        from scripts.post_generate_fixes import _CANCELED_FIELD_RE

        destructive = (
            "    canceled: Annotated[\n"
            "        Literal[True],\n"
            "        Field(\n"
            "            description='Cancel this specific package. Cancellation is irreversible"
            " — canceled packages stop delivery and cannot be reactivated."
            " Sellers MAY reject with NOT_CANCELLABLE.'\n"
            "        ),\n"
            "    ] = True\n"
        )

        result, count = _CANCELED_FIELD_RE.subn(
            r"\1Literal[True] | None\2 = None",
            destructive,
        )
        assert count == 1
        assert "Literal[True] | None," in result
        assert "] = None" in result
        assert "Literal[True]," not in result
        assert "] = True" not in result
