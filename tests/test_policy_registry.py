from __future__ import annotations

"""Tests for policy registry client methods."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from adcp.exceptions import RegistryError
from adcp.registry import RegistryClient
from adcp.types.core import Policy, PolicyRevision, PolicySummary

POLICY_SUMMARY_DATA = {
    "policy_id": "uk_hfss",
    "version": 1,
    "name": "UK HFSS Advertising Restrictions",
    "description": "Restricts paid online advertising of less healthy food and drink products.",
    "category": "regulation",
    "enforcement": "must",
    "jurisdictions": ["GB"],
    "region_aliases": [],
    "verticals": ["food_beverage"],
    "channels": [],
    "governance_domains": ["campaign"],
    "effective_date": "2025-10-01",
    "sunset_date": None,
    "source_url": "https://www.legislation.gov.uk/uksi/2023/890",
    "source_name": "UK Statutory Instrument 2023/890",
    "source_type": "registry",
    "review_status": "approved",
    "created_at": "2025-01-15T10:00:00Z",
    "updated_at": "2025-06-01T12:00:00Z",
}

POLICY_DATA = {
    **POLICY_SUMMARY_DATA,
    "policy": "Paid online advertising of less healthy food and drink products is prohibited.",
    "guidance": "Applies to products classified as HFSS under the Nutrient Profiling Model.",
    "exemplars": [
        {
            "scenario": "A candy bar ad shown on a children's website",
            "expected": "fail",
            "explanation": "HFSS product advertised online, violates restriction.",
        },
        {
            "scenario": "An organic fruit juice ad on a news site",
            "expected": "pass",
            "explanation": "Non-HFSS product, not restricted.",
        },
    ],
}

POLICY_REVISION_DATA = {
    "policy_id": "uk_hfss",
    "total": 2,
    "revisions": [
        {
            "revision_number": 2,
            "editor_name": "admin",
            "edit_summary": "Updated effective date",
            "is_rollback": False,
            "rolled_back_to": None,
            "created_at": "2025-06-01T12:00:00Z",
        },
        {
            "revision_number": 1,
            "editor_name": "admin",
            "edit_summary": "Initial entry",
            "is_rollback": False,
            "rolled_back_to": None,
            "created_at": "2025-01-15T10:00:00Z",
        },
    ],
}


def _mock_response(status_code: int = 200, json_data: object = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


class TestListPolicies:
    """Test policy registry listing."""

    @pytest.mark.asyncio
    async def test_lists_policies(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"policies": [POLICY_SUMMARY_DATA]})
        )

        rc = RegistryClient(client=mock_client)
        policies = await rc.list_policies()

        assert len(policies) == 1
        assert isinstance(policies[0], PolicySummary)
        assert policies[0].policy_id == "uk_hfss"
        assert policies[0].category == "regulation"
        assert policies[0].enforcement == "must"

    @pytest.mark.asyncio
    async def test_filters_by_category(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"policies": []})
        )

        rc = RegistryClient(
            base_url="https://test.example.com",
            client=mock_client,
            user_agent="test-agent",
        )
        await rc.list_policies(category="regulation")

        mock_client.get.assert_called_once_with(
            "https://test.example.com/api/policies/registry",
            params={"category": "regulation"},
            headers={"User-Agent": "test-agent"},
            timeout=10.0,
        )

    @pytest.mark.asyncio
    async def test_filters_by_jurisdiction(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"policies": []})
        )

        rc = RegistryClient(client=mock_client)
        await rc.list_policies(jurisdiction="GB")

        call_kwargs = mock_client.get.call_args
        assert call_kwargs[1]["params"]["jurisdiction"] == "GB"

    @pytest.mark.asyncio
    async def test_filters_by_vertical(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"policies": []})
        )

        rc = RegistryClient(client=mock_client)
        await rc.list_policies(vertical="food_beverage")

        call_kwargs = mock_client.get.call_args
        assert call_kwargs[1]["params"]["vertical"] == "food_beverage"

    @pytest.mark.asyncio
    async def test_filters_by_domain(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"policies": []})
        )

        rc = RegistryClient(client=mock_client)
        await rc.list_policies(domain="campaign")

        call_kwargs = mock_client.get.call_args
        assert call_kwargs[1]["params"]["domain"] == "campaign"

    @pytest.mark.asyncio
    async def test_multiple_filters(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"policies": []})
        )

        rc = RegistryClient(client=mock_client)
        await rc.list_policies(category="regulation", jurisdiction="US", domain="creative")

        call_kwargs = mock_client.get.call_args
        params = call_kwargs[1]["params"]
        assert params == {
            "category": "regulation",
            "jurisdiction": "US",
            "domain": "creative",
        }

    @pytest.mark.asyncio
    async def test_no_filters_sends_empty_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"policies": []})
        )

        rc = RegistryClient(client=mock_client)
        await rc.list_policies()

        call_kwargs = mock_client.get.call_args
        assert call_kwargs[1]["params"] == {}

    @pytest.mark.asyncio
    async def test_empty_list(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, {"policies": []})
        )

        rc = RegistryClient(client=mock_client)
        policies = await rc.list_policies()
        assert policies == []

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.list_policies()
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="timed out"):
            await rc.list_policies()

    @pytest.mark.asyncio
    async def test_missing_policies_key_returns_empty_list(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, {}))

        rc = RegistryClient(client=mock_client)
        policies = await rc.list_policies()
        assert policies == []


class TestResolvePolicy:
    """Test single policy resolution."""

    @pytest.mark.asyncio
    async def test_resolves_known_policy(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, POLICY_DATA))

        rc = RegistryClient(client=mock_client)
        result = await rc.resolve_policy("uk_hfss")

        assert result is not None
        assert isinstance(result, Policy)
        assert result.policy_id == "uk_hfss"
        assert result.enforcement == "must"
        assert result.policy == POLICY_DATA["policy"]
        assert len(result.exemplars) == 2

    @pytest.mark.asyncio
    async def test_returns_none_for_404(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))

        rc = RegistryClient(client=mock_client)
        result = await rc.resolve_policy("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_sends_correct_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))

        rc = RegistryClient(
            base_url="https://test.example.com",
            client=mock_client,
            user_agent="test-agent",
        )
        await rc.resolve_policy("uk_hfss")

        mock_client.get.assert_called_once_with(
            "https://test.example.com/api/policies/resolve",
            params={"policy_id": "uk_hfss"},
            headers={"User-Agent": "test-agent"},
            timeout=10.0,
        )

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.resolve_policy("uk_hfss")
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="timed out"):
            await rc.resolve_policy("uk_hfss")

    @pytest.mark.asyncio
    async def test_returns_none_for_null_body(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, None))

        rc = RegistryClient(client=mock_client)
        result = await rc.resolve_policy("uk_hfss")
        assert result is None

    @pytest.mark.asyncio
    async def test_extra_fields_preserved(self):
        data = {**POLICY_DATA, "extra_field": "extra_value"}
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, data))

        rc = RegistryClient(client=mock_client)
        result = await rc.resolve_policy("uk_hfss")
        assert result is not None
        assert result.extra_field == "extra_value"  # type: ignore[attr-defined]


class TestResolvePolicies:
    """Test bulk policy resolution."""

    @pytest.mark.asyncio
    async def test_resolves_multiple_policies(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "results": {
                        "uk_hfss": POLICY_DATA,
                        "nonexistent": None,
                    }
                },
            )
        )

        rc = RegistryClient(client=mock_client)
        results = await rc.resolve_policies(["uk_hfss", "nonexistent"])

        assert len(results) == 2
        assert isinstance(results["uk_hfss"], Policy)
        assert results["nonexistent"] is None

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty_dict(self):
        rc = RegistryClient(client=MagicMock())
        results = await rc.resolve_policies([])
        assert results == {}

    @pytest.mark.asyncio
    async def test_sends_correct_body(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(200, {"results": {}})
        )

        rc = RegistryClient(
            base_url="https://test.example.com",
            client=mock_client,
            user_agent="test-agent",
        )
        await rc.resolve_policies(["uk_hfss", "us_coppa"])

        mock_client.post.assert_called_once_with(
            "https://test.example.com/api/policies/resolve/bulk",
            json={"policy_ids": ["uk_hfss", "us_coppa"]},
            headers={"User-Agent": "test-agent"},
            timeout=10.0,
        )

    @pytest.mark.asyncio
    async def test_auto_chunks_over_limit(self):
        policy_ids = [f"policy_{i}" for i in range(150)]

        call_count = 0

        async def mock_post(url, json, headers, timeout):
            nonlocal call_count
            call_count += 1
            chunk_ids = json["policy_ids"]
            results = {pid: None for pid in chunk_ids}
            return _mock_response(200, {"results": results})

        mock_client = MagicMock()
        mock_client.post = mock_post

        rc = RegistryClient(client=mock_client)
        results = await rc.resolve_policies(policy_ids)

        assert call_count == 2  # 100 + 50
        assert len(results) == 150

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.resolve_policies(["uk_hfss"])
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_policy_id_absent_from_response_defaults_to_none(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=_mock_response(200, {"results": {"uk_hfss": POLICY_DATA}})
        )

        rc = RegistryClient(client=mock_client)
        results = await rc.resolve_policies(["uk_hfss", "other"])

        assert isinstance(results["uk_hfss"], Policy)
        assert results["other"] is None


class TestGetPolicyHistory:
    """Test policy revision history."""

    @pytest.mark.asyncio
    async def test_gets_history(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(
            return_value=_mock_response(200, POLICY_REVISION_DATA)
        )

        rc = RegistryClient(client=mock_client)
        result = await rc.get_policy_history("uk_hfss")

        assert result is not None
        assert isinstance(result, PolicyRevision)
        assert result.policy_id == "uk_hfss"
        assert result.total == 2
        assert len(result.revisions) == 2

    @pytest.mark.asyncio
    async def test_returns_none_for_404(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))

        rc = RegistryClient(client=mock_client)
        result = await rc.get_policy_history("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_sends_correct_params(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))

        rc = RegistryClient(
            base_url="https://test.example.com",
            client=mock_client,
            user_agent="test-agent",
        )
        await rc.get_policy_history("uk_hfss")

        mock_client.get.assert_called_once_with(
            "https://test.example.com/api/policies/history",
            params={"policy_id": "uk_hfss"},
            headers={"User-Agent": "test-agent"},
            timeout=10.0,
        )

    @pytest.mark.asyncio
    async def test_raises_on_server_error(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=_mock_response(500))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError) as exc_info:
            await rc.get_policy_history("uk_hfss")
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_raises_on_timeout(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

        rc = RegistryClient(client=mock_client)
        with pytest.raises(RegistryError, match="timed out"):
            await rc.get_policy_history("uk_hfss")


class TestPolicyTypes:
    """Test Policy, PolicySummary, and PolicyRevision Pydantic models."""

    def test_policy_summary_validates(self):
        summary = PolicySummary.model_validate(POLICY_SUMMARY_DATA)
        assert summary.policy_id == "uk_hfss"
        assert summary.category == "regulation"
        assert summary.enforcement == "must"
        assert summary.jurisdictions == ["GB"]

    def test_policy_summary_optional_fields(self):
        minimal = {
            "policy_id": "test",
            "version": 1,
            "name": "Test Policy",
            "category": "standard",
            "enforcement": "should",
        }
        summary = PolicySummary.model_validate(minimal)
        assert summary.description is None
        assert summary.jurisdictions == []
        assert summary.verticals == []
        assert summary.governance_domains == []
        assert summary.effective_date is None
        assert summary.sunset_date is None

    def test_policy_validates(self):
        policy = Policy.model_validate(POLICY_DATA)
        assert policy.policy_id == "uk_hfss"
        assert policy.policy is not None
        assert len(policy.exemplars) == 2
        assert policy.exemplars[0]["expected"] == "fail"

    def test_policy_minimal(self):
        minimal = {
            "policy_id": "test",
            "version": 1,
            "name": "Test Policy",
            "category": "standard",
            "enforcement": "should",
        }
        policy = Policy.model_validate(minimal)
        assert policy.policy is None
        assert policy.guidance is None
        assert policy.exemplars == []

    def test_policy_revision_validates(self):
        rev = PolicyRevision.model_validate(POLICY_REVISION_DATA)
        assert rev.policy_id == "uk_hfss"
        assert rev.total == 2
        assert len(rev.revisions) == 2
        assert rev.revisions[0]["revision_number"] == 2

    def test_policy_extra_fields_preserved(self):
        data = {**POLICY_DATA, "custom_field": "custom_value"}
        policy = Policy.model_validate(data)
        assert policy.custom_field == "custom_value"  # type: ignore[attr-defined]


class TestPolicyPublicApiExports:
    """Test that policy types are exported from the adcp package."""

    def test_policy_exported_from_types(self):
        import adcp.types

        assert adcp.types.Policy is Policy

    def test_policy_summary_exported_from_types(self):
        import adcp.types

        assert adcp.types.PolicySummary is PolicySummary

    def test_policy_revision_exported_from_types(self):
        import adcp.types

        assert adcp.types.PolicyRevision is PolicyRevision

    def test_policy_exported_from_root(self):
        import adcp

        assert adcp.Policy is Policy

    def test_policy_summary_exported_from_root(self):
        import adcp

        assert adcp.PolicySummary is PolicySummary

    def test_policy_revision_exported_from_root(self):
        import adcp

        assert adcp.PolicyRevision is PolicyRevision
