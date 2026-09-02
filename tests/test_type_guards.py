"""Tests for ADCP type guards."""

from __future__ import annotations

import pytest

from adcp.types.guards import (
    is_adcp_error,
    is_adcp_success,
    is_build_creative_error,
    is_build_creative_submitted,
    is_build_creative_success,
    is_create_media_buy_error,
    is_create_media_buy_submitted,
    is_create_media_buy_success,
    is_sync_catalogs_error,
    is_sync_catalogs_submitted,
    is_sync_catalogs_success,
    is_sync_creatives_error,
    is_sync_creatives_submitted,
    is_sync_creatives_success,
    is_update_media_buy_error,
    is_update_media_buy_submitted,
    is_update_media_buy_success,
)


class TestGenericGuards:
    """Tests for is_adcp_error and is_adcp_success."""

    def test_error_with_errors_list(self) -> None:
        """Response with non-empty errors list is an error."""

        class FakeError:
            errors = [{"code": "INVALID_BUDGET", "message": "Budget too low"}]

        assert is_adcp_error(FakeError()) is True
        assert is_adcp_success(FakeError()) is False

    @pytest.mark.parametrize(
        ("response", "expected_error"),
        [
            ({"errors": [{"code": "INVALID_BUDGET", "message": "Budget too low"}]}, True),
            ({"errors": []}, False),
            ({"errors": None}, False),
        ],
    )
    def test_top_level_mapping_errors_are_classified(
        self, response: dict[str, object], expected_error: bool
    ) -> None:
        assert is_adcp_error(response) is expected_error
        assert is_adcp_success(response) is not expected_error

    def test_success_without_errors(self) -> None:
        """Response without errors attribute is a success."""

        class FakeSuccess:
            media_buy_id = "mb_123"

        assert is_adcp_error(FakeSuccess()) is False
        assert is_adcp_success(FakeSuccess()) is True

    def test_success_with_none_errors(self) -> None:
        """Response with errors=None is a success."""

        class FakeSuccess:
            errors = None

        assert is_adcp_error(FakeSuccess()) is False
        assert is_adcp_success(FakeSuccess()) is True

    def test_success_with_empty_errors(self) -> None:
        """Response with empty errors list is a success."""

        class FakeSuccess:
            errors: list = []

        assert is_adcp_error(FakeSuccess()) is False
        assert is_adcp_success(FakeSuccess()) is True

    def test_error_with_real_pydantic_model(self) -> None:
        """Test with actual Pydantic models from the type system."""
        from adcp.types.aliases import (
            CreateMediaBuyErrorResponse,
            CreateMediaBuySuccessResponse,
        )

        # Create an error response
        error_resp = CreateMediaBuyErrorResponse.model_validate(
            {"errors": [{"message": "Budget too low", "code": "INVALID_BUDGET"}]}
        )
        assert is_adcp_error(error_resp) is True
        assert is_adcp_success(error_resp) is False

        # Create a success response
        success_resp = CreateMediaBuySuccessResponse.model_validate(
            {
                "media_buy_id": "mb_123",
                "packages": [],
                "confirmed_at": "2026-05-27T12:00:00Z",
                "revision": 1,
            }
        )
        assert is_adcp_error(success_resp) is False
        assert is_adcp_success(success_resp) is True

    def test_nested_failed_results_are_errors_for_real_models(self) -> None:
        from adcp.types import (
            GetPrincipalResponse,
            SyncPrincipalResponse,
            SyncReportingReceiptsResponse,
        )

        responses = [
            GetPrincipalResponse.model_validate(
                {
                    "result": {
                        "kind": "failed",
                        "errors": [{"code": "UNAUTHORIZED", "message": "Unauthorized"}],
                    }
                }
            ),
            SyncPrincipalResponse.model_validate(
                {
                    "result": {
                        "kind": "failed",
                        "errors": [{"code": "CONFLICT", "message": "Version conflict"}],
                    }
                }
            ),
            SyncReportingReceiptsResponse.model_validate(
                {
                    "results": [
                        {
                            "result": "failed",
                            "reporting_receipt_id": "receipt-failed-001",
                            "errors": [{"code": "INVALID_REQUEST", "message": "Invalid receipt"}],
                        }
                    ]
                }
            ),
        ]

        for response in responses:
            assert is_adcp_error(response) is True
            assert is_adcp_success(response) is False

    def test_nested_successful_results_are_not_errors_for_real_models(self) -> None:
        from adcp.types import (
            GetPrincipalResponse,
            SyncPrincipalResponse,
            SyncReportingReceiptsResponse,
        )

        receipt = {
            "reporting_receipt_id": "receipt-success-001",
            "reporting_obligation_id": "obligation-1",
            "reporting_revision_id": "revision-1",
            "reporting_materialization_id": "materialization-1",
            "status": "accepted",
            "verification_profile": "native_commit",
            "observed_row_count": 0,
            "observed_control_totals": [],
            "observed_native_version_ref": "v1",
            "observed_at": "2026-01-01T00:00:00Z",
        }
        responses = [
            GetPrincipalResponse.model_validate({"result": {"kind": "unconfigured"}}),
            SyncPrincipalResponse.model_validate(
                {"result": {"kind": "validated", "action": "would_update", "dry_run": True}}
            ),
            SyncReportingReceiptsResponse.model_validate(
                {"results": [{"result": "recorded", "receipt": receipt}]}
            ),
        ]

        for response in responses:
            assert is_adcp_error(response) is False
            assert is_adcp_success(response) is True


class TestTypedGuards:
    """Tests for typed TypeGuard functions."""

    def test_create_media_buy_success_guard(self) -> None:
        from adcp.types.aliases import CreateMediaBuySuccessResponse

        resp = CreateMediaBuySuccessResponse.model_validate(
            {
                "media_buy_id": "mb_123",
                "packages": [],
                "confirmed_at": "2026-05-27T12:00:00Z",
                "revision": 1,
            }
        )
        assert is_create_media_buy_success(resp) is True
        assert is_create_media_buy_error(resp) is False

    def test_create_media_buy_error_guard(self) -> None:
        from adcp.types.aliases import CreateMediaBuyErrorResponse

        resp = CreateMediaBuyErrorResponse.model_validate(
            {"errors": [{"message": "fail", "code": "INVALID_REQUEST"}]}
        )
        assert is_create_media_buy_success(resp) is False
        assert is_create_media_buy_error(resp) is True
        assert is_create_media_buy_submitted(resp) is False

    def test_create_media_buy_submitted_guard(self) -> None:
        """Submitted (async) envelope is neither success nor error."""
        from adcp.types.aliases import (
            CreateMediaBuyErrorResponse,
            CreateMediaBuySubmittedResponse,
            CreateMediaBuySuccessResponse,
        )

        submitted = CreateMediaBuySubmittedResponse.model_validate(
            {"status": "submitted", "task_id": "task_abc"}
        )
        assert is_create_media_buy_submitted(submitted) is True
        assert is_create_media_buy_success(submitted) is False
        assert is_create_media_buy_error(submitted) is False

        # Negative cases: success and error payloads must not be classified
        # as submitted.
        success = CreateMediaBuySuccessResponse.model_validate(
            {
                "media_buy_id": "mb_123",
                "packages": [],
                "confirmed_at": "2026-05-27T12:00:00Z",
                "revision": 1,
            }
        )
        assert is_create_media_buy_submitted(success) is False

        error = CreateMediaBuyErrorResponse.model_validate(
            {"errors": [{"message": "fail", "code": "INVALID_REQUEST"}]}
        )
        assert is_create_media_buy_submitted(error) is False

    def test_update_media_buy_guards(self) -> None:
        from adcp.types.aliases import (
            UpdateMediaBuyErrorResponse,
            UpdateMediaBuySuccessResponse,
        )

        success = UpdateMediaBuySuccessResponse.model_validate(
            {"media_buy_id": "mb_123", "packages": [], "revision": 2}
        )
        assert is_update_media_buy_success(success) is True
        assert is_update_media_buy_error(success) is False

        error = UpdateMediaBuyErrorResponse.model_validate(
            {"errors": [{"message": "not found", "code": "NOT_FOUND"}]}
        )
        assert is_update_media_buy_success(error) is False
        assert is_update_media_buy_error(error) is True
        assert is_update_media_buy_submitted(error) is False

    def test_update_media_buy_submitted_guard(self) -> None:
        """Submitted (async) envelope is neither success nor error."""
        from adcp.types.aliases import (
            UpdateMediaBuyErrorResponse,
            UpdateMediaBuySubmittedResponse,
            UpdateMediaBuySuccessResponse,
        )

        submitted = UpdateMediaBuySubmittedResponse.model_validate(
            {"status": "submitted", "task_id": "task_abc"}
        )
        assert is_update_media_buy_submitted(submitted) is True
        assert is_update_media_buy_success(submitted) is False
        assert is_update_media_buy_error(submitted) is False

        success = UpdateMediaBuySuccessResponse.model_validate(
            {"media_buy_id": "mb_123", "packages": [], "revision": 2}
        )
        assert is_update_media_buy_submitted(success) is False

        error = UpdateMediaBuyErrorResponse.model_validate(
            {"errors": [{"message": "not found", "code": "NOT_FOUND"}]}
        )
        assert is_update_media_buy_submitted(error) is False

    def test_build_creative_submitted_guard(self) -> None:
        """Submitted build_creative envelope is neither sync success nor error."""
        from adcp.types.legacy import (
            LegacyBuildCreativeErrorResponse,
            LegacyBuildCreativeSubmittedResponse,
            LegacyBuildCreativeSuccessResponse,
        )

        submitted = LegacyBuildCreativeSubmittedResponse.model_validate(
            {"status": "submitted", "task_id": "task_build"}
        )
        assert is_build_creative_submitted(submitted) is True
        assert is_build_creative_success(submitted) is False
        assert is_build_creative_error(submitted) is False

        success = LegacyBuildCreativeSuccessResponse.model_construct()
        assert is_build_creative_success(success) is True
        assert is_build_creative_submitted(success) is False

        error = LegacyBuildCreativeErrorResponse.model_construct(errors=[{"message": "fail"}])
        assert is_build_creative_error(error) is True
        assert is_build_creative_submitted(error) is False

    def test_sync_creatives_submitted_guard(self) -> None:
        """Submitted sync_creatives envelope is neither sync success nor error."""
        from adcp.types.aliases import (
            SyncCreativesErrorResponse,
            SyncCreativesSubmittedResponse,
            SyncCreativesSuccessResponse,
        )

        submitted = SyncCreativesSubmittedResponse.model_validate(
            {"status": "submitted", "task_id": "task_creatives"}
        )
        assert is_sync_creatives_submitted(submitted) is True
        assert is_sync_creatives_success(submitted) is False
        assert is_sync_creatives_error(submitted) is False

        success = SyncCreativesSuccessResponse.model_construct(creatives=[])
        assert is_sync_creatives_success(success) is True
        assert is_sync_creatives_submitted(success) is False

        error = SyncCreativesErrorResponse.model_construct(errors=[{"message": "fail"}])
        assert is_sync_creatives_error(error) is True
        assert is_sync_creatives_submitted(error) is False

    def test_sync_catalogs_submitted_guard(self) -> None:
        """Submitted sync_catalogs envelope is neither sync success nor error."""
        from adcp.types.aliases import (
            SyncCatalogsErrorResponse,
            SyncCatalogsSubmittedResponse,
            SyncCatalogsSuccessResponse,
        )

        submitted = SyncCatalogsSubmittedResponse.model_validate(
            {"status": "submitted", "task_id": "task_catalogs"}
        )
        assert is_sync_catalogs_submitted(submitted) is True
        assert is_sync_catalogs_success(submitted) is False
        assert is_sync_catalogs_error(submitted) is False

        success = SyncCatalogsSuccessResponse.model_construct(catalogs=[])
        assert is_sync_catalogs_success(success) is True
        assert is_sync_catalogs_submitted(success) is False

        error = SyncCatalogsErrorResponse.model_construct(errors=[{"message": "fail"}])
        assert is_sync_catalogs_error(error) is True
        assert is_sync_catalogs_submitted(error) is False


class TestImportFromAdcp:
    """Test that guards are importable from top-level package."""

    def test_import_generic_guards(self) -> None:
        from adcp import is_adcp_error, is_adcp_success

        assert callable(is_adcp_error)
        assert callable(is_adcp_success)

    def test_import_typed_guards_from_types(self) -> None:
        from adcp.types import is_create_media_buy_success, is_sync_creatives_submitted

        assert callable(is_create_media_buy_success)
        assert callable(is_sync_creatives_submitted)
