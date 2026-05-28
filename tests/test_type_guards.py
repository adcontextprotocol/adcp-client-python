"""Tests for ADCP type guards."""

from __future__ import annotations

from adcp.types.guards import (
    is_adcp_error,
    is_adcp_success,
    is_create_media_buy_error,
    is_create_media_buy_submitted,
    is_create_media_buy_success,
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


class TestImportFromAdcp:
    """Test that guards are importable from top-level package."""

    def test_import_generic_guards(self) -> None:
        from adcp import is_adcp_error, is_adcp_success

        assert callable(is_adcp_error)
        assert callable(is_adcp_success)

    def test_import_typed_guards_from_types(self) -> None:
        from adcp.types import is_create_media_buy_success

        assert callable(is_create_media_buy_success)
