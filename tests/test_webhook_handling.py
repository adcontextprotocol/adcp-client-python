"""Tests for webhook handling (MCP and A2A protocols)."""

from __future__ import annotations

import json
import re as _re
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from a2a.types import TaskState, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict as _MessageToDict
from pydantic import BaseModel

from adcp.client import ADCPClient
from adcp.exceptions import ADCPWebhookSignatureError
from adcp.types import GeneratedTaskStatus
from adcp.types.core import AgentConfig, Protocol, TaskStatus
from adcp.webhooks import (
    create_a2a_webhook_payload,
    create_mcp_webhook_payload,
    extract_webhook_result_data,
    get_adcp_signed_headers_for_webhook,
)
from tests.a2a_compat_shim import (
    Artifact,
    DataPart,
    Message,
    Part,
    Role,
    Task,
    TextPart,
)
from tests.a2a_compat_shim import (
    TaskStatus as A2ATaskStatus,
)


class TestMCPWebhooks:
    """Test MCP webhook handling (HTTP POST with dict payload)."""

    def setup_method(self):
        """Set up test fixtures."""
        self.config = AgentConfig(
            id="test_agent",
            agent_uri="https://test.example.com",
            protocol=Protocol.MCP,
        )
        self.client = ADCPClient(self.config)

    @pytest.mark.asyncio
    async def test_mcp_webhook_completed_success(self):
        """Test MCP webhook with completed status and valid response."""
        payload = {
            "idempotency_key": "whk_task_123xxxx",
            "task_id": "task_123",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": {"media_buy_id": "mb_123", "buyer_ref": "ref_123", "packages": []},
            "message": "Media buy created successfully",
        }

        result = await self.client.handle_webhook(
            payload, task_type="create_media_buy", operation_id="op_123"
        )

        assert result.success is True
        assert result.status == TaskStatus.COMPLETED
        assert result.data is not None
        assert result.metadata["task_id"] == "task_123"
        assert result.metadata["operation_id"] == "op_123"

    @pytest.mark.asyncio
    async def test_mcp_webhook_completed_with_errors(self):
        """Test MCP webhook with completed status but has errors in result."""
        payload = {
            "idempotency_key": "whk_task_456xxxx",
            "task_id": "task_456",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": {"errors": [{"code": "NOT_FOUND", "message": "No matching inventory"}]},
            "message": "No matching inventory found",
        }

        result = await self.client.handle_webhook(
            payload, task_type="create_media_buy", operation_id="op_456"
        )

        # Completed status
        assert result.status == TaskStatus.COMPLETED
        # Error is in structured data, not in error field
        assert result.data is not None

    @pytest.mark.asyncio
    async def test_mcp_webhook_failed_status(self):
        """Test MCP webhook with failed status."""
        payload = {
            "idempotency_key": "whk_task_789xxxx",
            "task_id": "task_789",
            "task_type": "create_media_buy",
            "status": "failed",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": {
                "errors": [
                    {
                        "code": "INTERNAL_ERROR",
                        "message": "Database connection failed",
                    }
                ]
            },
            "message": "Task failed due to internal error",
        }

        result = await self.client.handle_webhook(
            payload, task_type="create_media_buy", operation_id="op_789"
        )

        assert result.success is False
        assert result.status == TaskStatus.FAILED
        assert result.data is not None  # Errors in structured data
        assert result.metadata["message"] == "Task failed due to internal error"

    @pytest.mark.asyncio
    async def test_mcp_webhook_working_status(self):
        """Test MCP webhook with working status (async in progress)."""
        payload = {
            "idempotency_key": "whk_task_111xxxx",
            "task_id": "task_111",
            "task_type": "create_media_buy",
            "status": "working",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": None,  # Working status may have no result yet
            "message": "Processing request...",
        }

        result = await self.client.handle_webhook(
            payload, task_type="create_media_buy", operation_id="op_111"
        )

        assert result.status == TaskStatus.WORKING
        assert result.success is False  # Not completed yet

    @pytest.mark.asyncio
    async def test_mcp_webhook_input_required_status(self):
        """Test MCP webhook with input-required status."""
        payload = {
            "idempotency_key": "whk_task_222xxxx",
            "task_id": "task_222",
            "task_type": "create_media_buy",
            "status": "input-required",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": {
                "errors": [
                    {
                        "code": "APPROVAL_REQUIRED",
                        "field": "total_budget",
                        "message": "Budget exceeds auto-approval threshold",
                    }
                ],
            },
            "message": "Campaign budget $150K requires VP approval",
            "context_id": "ctx_abc",
        }

        result = await self.client.handle_webhook(
            payload, task_type="create_media_buy", operation_id="op_222"
        )

        assert result.status == TaskStatus.NEEDS_INPUT
        assert result.success is False
        assert result.data is not None  # Errors in structured data
        assert result.metadata["context_id"] == "ctx_abc"

    @pytest.mark.asyncio
    async def test_mcp_webhook_signature_verification_valid(self):
        """Test signature verification with valid HMAC."""
        payload = {
            "idempotency_key": "whk_task_333xxxx",
            "task_id": "task_333",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": {"media_buy_id": "mb_333", "buyer_ref": "ref_333", "packages": []},
        }

        # Generate valid signature using {timestamp}.{payload} format
        # (matching get_adcp_signed_headers_for_webhook)
        import hashlib
        import hmac

        header_timestamp = str(int(time.time()))
        payload_json = json.dumps(payload)
        signed_message = f"{header_timestamp}.{payload_json}"
        signature = hmac.new(
            b"test_secret", signed_message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        client = ADCPClient(self.config, webhook_secret="test_secret")
        result = await client.handle_webhook(
            payload,
            task_type="create_media_buy",
            operation_id="op_333",
            signature=signature,
            timestamp=header_timestamp,
            raw_body=payload_json.encode("utf-8"),
        )

        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_mcp_webhook_signature_verification_with_raw_body(self):
        """Test signature verification using raw body bytes (cross-language safe)."""
        payload = {
            "idempotency_key": "whk_task_333bxxx",
            "task_id": "task_333b",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": {"media_buy_id": "mb_333b", "buyer_ref": "ref_333b", "packages": []},
        }

        import hashlib
        import hmac

        header_timestamp = str(int(time.time()))
        # Simulate raw body from a different serializer (e.g., compact JSON from JS)
        raw_body = json.dumps(payload, separators=(",", ":"))
        signed_message = f"{header_timestamp}.{raw_body}"
        signature = hmac.new(
            b"test_secret", signed_message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        client = ADCPClient(self.config, webhook_secret="test_secret")
        result = await client.handle_webhook(
            payload,
            task_type="create_media_buy",
            operation_id="op_333b",
            signature=signature,
            timestamp=header_timestamp,
            raw_body=raw_body,
        )

        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_mcp_webhook_signature_verification_invalid(self):
        """Test signature verification with invalid HMAC."""
        payload = {
            "idempotency_key": "whk_task_444xxxx",
            "task_id": "task_444",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": {"media_buy_id": "mb_444", "buyer_ref": "ref_444", "packages": []},
        }

        client = ADCPClient(self.config, webhook_secret="test_secret")
        with pytest.raises(ADCPWebhookSignatureError):
            await client.handle_webhook(
                payload,
                task_type="create_media_buy",
                operation_id="op_444",
                signature="invalid_signature",
                timestamp=str(int(time.time())),
            )

    @pytest.mark.asyncio
    async def test_mcp_webhook_timestamp_valid(self):
        """Test that a webhook with a current timestamp passes verification."""
        import hashlib
        import hmac

        payload = {
            "idempotency_key": "whk_task_ts1xxxx",
            "task_id": "task_ts1",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": {"media_buy_id": "mb_ts1", "buyer_ref": "ref_ts1", "packages": []},
        }

        header_timestamp = str(int(time.time()))
        payload_json = json.dumps(payload)
        signed_message = f"{header_timestamp}.{payload_json}"
        signature = hmac.new(
            b"test_secret", signed_message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        client = ADCPClient(self.config, webhook_secret="test_secret")
        result = await client.handle_webhook(
            payload,
            task_type="create_media_buy",
            operation_id="op_ts1",
            signature=signature,
            timestamp=header_timestamp,
            raw_body=payload_json.encode("utf-8"),
        )

        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_mcp_webhook_stale_timestamp_rejected(self):
        """Test that a webhook with a timestamp older than 5 minutes is rejected."""
        import hashlib
        import hmac

        payload = {
            "idempotency_key": "whk_task_ts2xxxx",
            "task_id": "task_ts2",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": {"media_buy_id": "mb_ts2", "buyer_ref": "ref_ts2", "packages": []},
        }

        # Timestamp 10 minutes in the past
        header_timestamp = str(int(time.time()) - 600)
        payload_json = json.dumps(payload)
        signed_message = f"{header_timestamp}.{payload_json}"
        signature = hmac.new(
            b"test_secret", signed_message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        client = ADCPClient(self.config, webhook_secret="test_secret")
        with pytest.raises(ADCPWebhookSignatureError):
            await client.handle_webhook(
                payload,
                task_type="create_media_buy",
                operation_id="op_ts2",
                signature=signature,
                timestamp=header_timestamp,
            )

    @pytest.mark.asyncio
    async def test_mcp_webhook_future_timestamp_rejected(self):
        """Test that a webhook with a timestamp more than 5 minutes in the future is rejected."""
        import hashlib
        import hmac

        payload = {
            "idempotency_key": "whk_task_ts3xxxx",
            "task_id": "task_ts3",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": {"media_buy_id": "mb_ts3", "buyer_ref": "ref_ts3", "packages": []},
        }

        # Timestamp 10 minutes in the future
        header_timestamp = str(int(time.time()) + 600)
        payload_json = json.dumps(payload)
        signed_message = f"{header_timestamp}.{payload_json}"
        signature = hmac.new(
            b"test_secret", signed_message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        client = ADCPClient(self.config, webhook_secret="test_secret")
        with pytest.raises(ADCPWebhookSignatureError):
            await client.handle_webhook(
                payload,
                task_type="create_media_buy",
                operation_id="op_ts3",
                signature=signature,
                timestamp=header_timestamp,
            )

    @pytest.mark.asyncio
    async def test_mcp_webhook_missing_headers_with_secret_rejects(self):
        """Omitting signature/timestamp headers when secret is configured must fail."""
        client = ADCPClient(
            agent_config=self.config,
            webhook_secret="test-secret",
        )
        payload = {
            "idempotency_key": "whk_test-123xxxx",
            "task_id": "test-123",
            "timestamp": "2024-01-01T00:00:00Z",
            "status": "completed",
            "result": {"products": []},
        }
        with pytest.raises(ADCPWebhookSignatureError, match="required"):
            await client._handle_mcp_webhook(
                payload=payload,
                task_type="get_products",
                operation_id="op-123",
                signature=None,
                timestamp=None,
            )

    @pytest.mark.asyncio
    async def test_mcp_webhook_missing_required_fields(self):
        """Test MCP webhook with missing required fields."""
        payload = {
            # Missing task_id and timestamp
            "status": "completed",
            "result": {"products": []},
        }

        with pytest.raises(Exception):  # Pydantic ValidationError
            await self.client.handle_webhook(
                payload, task_type="create_media_buy", operation_id="op_555"
            )


class TestA2AWebhooks:
    """Test A2A webhook handling (Task objects from TaskStatusUpdateEvent)."""

    def setup_method(self):
        """Set up test fixtures."""
        self.config = AgentConfig(
            id="test_agent",
            agent_uri="https://test.example.com",
            protocol=Protocol.A2A,
        )
        self.client = ADCPClient(self.config)

    @pytest.mark.asyncio
    async def test_a2a_webhook_completed_success(self):
        """Test A2A Task with completed status and valid AdCP payload."""
        media_buy_data = {"media_buy_id": "mb_123", "buyer_ref": "ref_123", "packages": []}

        task = Task(
            id="task_123",
            context_id="ctx_456",
            status=A2ATaskStatus(
                state="completed", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[
                Artifact(
                    artifact_id="artifact_123",
                    parts=[
                        Part(root=DataPart(data=media_buy_data)),
                        Part(root=TextPart(text="Media buy created")),
                    ],
                )
            ],
        )

        result = await self.client.handle_webhook(
            task, task_type="create_media_buy", operation_id="op_123"
        )

        assert result.success is True
        assert result.status == TaskStatus.COMPLETED
        assert result.data is not None
        assert result.metadata["task_id"] == "task_123"
        assert result.metadata["operation_id"] == "op_123"

    @pytest.mark.asyncio
    async def test_a2a_webhook_completed_with_errors(self):
        """Test A2A Task with completed status but errors in AdCP result."""
        error_data = {
            "errors": [{"code": "NOT_FOUND", "message": "No matching inventory"}],
        }

        task = Task(
            id="task_456",
            context_id="ctx_789",
            status=A2ATaskStatus(
                state="completed", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[
                Artifact(artifact_id="test_artifact", parts=[Part(root=DataPart(data=error_data))])
            ],
        )

        result = await self.client.handle_webhook(
            task, task_type="create_media_buy", operation_id="op_456"
        )

        assert result.status == TaskStatus.COMPLETED
        assert result.data is not None  # Errors in structured data

    @pytest.mark.asyncio
    async def test_a2a_webhook_failed_status(self):
        """Test A2A Task with failed status."""
        error_data = {
            "errors": [
                {
                    "code": "INTERNAL_ERROR",
                    "message": "Database connection failed",
                }
            ]
        }

        task = Task(
            id="task_789",
            context_id="ctx_111",
            status=A2ATaskStatus(state="failed", timestamp=datetime.now(timezone.utc).isoformat()),
            artifacts=[
                Artifact(
                    artifact_id="test_artifact",
                    parts=[
                        Part(root=DataPart(data=error_data)),
                        Part(root=TextPart(text="Task failed due to internal error")),
                    ],
                )
            ],
        )

        result = await self.client.handle_webhook(
            task, task_type="create_media_buy", operation_id="op_789"
        )

        assert result.success is False
        assert result.status == TaskStatus.FAILED
        assert result.data is not None  # Errors in structured data

    @pytest.mark.asyncio
    async def test_a2a_webhook_working_status(self):
        """Test A2A Task with working status (async in progress)."""
        task = Task(
            id="task_111",
            context_id="ctx_222",
            status=A2ATaskStatus(state="working", timestamp=datetime.now(timezone.utc).isoformat()),
            artifacts=[
                Artifact(
                    artifact_id="test_artifact",
                    parts=[
                        Part(root=TextPart(text="Processing request...")),
                    ],
                )
            ],
        )

        result = await self.client.handle_webhook(
            task, task_type="create_media_buy", operation_id="op_111"
        )

        assert result.status == TaskStatus.WORKING
        assert result.success is False  # Not completed yet

    @pytest.mark.asyncio
    async def test_a2a_webhook_input_required_status(self):
        """Test A2A Task with input-required status."""
        input_data = {
            "reason": "APPROVAL_REQUIRED",
        }

        task = Task(
            id="task_222",
            context_id="ctx_333",
            status=A2ATaskStatus(
                state="input-required", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[
                Artifact(
                    artifact_id="test_artifact",
                    parts=[
                        Part(root=DataPart(data=input_data)),
                        Part(root=TextPart(text="Campaign budget $150K requires VP approval")),
                    ],
                )
            ],
        )

        result = await self.client.handle_webhook(
            task, task_type="create_media_buy", operation_id="op_222"
        )

        assert result.status == TaskStatus.NEEDS_INPUT
        assert result.success is False
        assert result.data is not None  # Errors in structured data

    @pytest.mark.asyncio
    async def test_a2a_webhook_missing_artifacts(self):
        """Test A2A Task with no artifacts array."""
        task = Task(
            id="task_333",
            context_id="ctx_444",
            status=A2ATaskStatus(
                state="completed", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[],  # Empty artifacts
        )

        result = await self.client.handle_webhook(
            task, task_type="create_media_buy", operation_id="op_333"
        )

        # Should still return result, but with None/empty data
        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_a2a_webhook_missing_data_part(self):
        """Test A2A Task with no DataPart in artifacts."""
        task = Task(
            id="task_444",
            context_id="ctx_555",
            status=A2ATaskStatus(
                state="completed", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[
                Artifact(
                    artifact_id="test_artifact",
                    parts=[Part(root=TextPart(text="Only text, no data"))],  # Only TextPart
                )
            ],
        )

        result = await self.client.handle_webhook(
            task, task_type="create_media_buy", operation_id="op_444"
        )

        # Should still return result, but with None/empty data
        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_a2a_webhook_malformed_adcp_data(self):
        """Test A2A Task with minimal data that passes basic validation."""
        # Minimal valid data structure
        minimal_data = {"errors": [{"code": "TEST", "message": "Test error"}]}

        task = Task(
            id="task_555",
            context_id="ctx_666",
            status=A2ATaskStatus(
                state="completed", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[
                Artifact(
                    artifact_id="test_artifact", parts=[Part(root=DataPart(data=minimal_data))]
                )
            ],
        )

        result = await self.client.handle_webhook(
            task, task_type="create_media_buy", operation_id="op_555"
        )

        # Should handle error response
        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_a2a_webhook_taskstatusupdateevent_working(self):
        """Test A2A TaskStatusUpdateEvent with working status (correct intermediate payload)."""
        progress_data = {
            "current_step": "fetching_inventory",
            "percentage": 50,
        }

        # Intermediate status uses TaskStatusUpdateEvent, not Task
        event = TaskStatusUpdateEvent(
            task_id="task_777",
            context_id="ctx_888",
            status=A2ATaskStatus(
                state=TaskState.working,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message=Message(
                    message_id="msg_777",
                    role=Role.agent,
                    parts=[
                        Part(root=DataPart(data=progress_data)),
                        Part(root=TextPart(text="Processing request...")),
                    ],
                ),
            ),
        )

        result = await self.client.handle_webhook(
            event, task_type="create_media_buy", operation_id="op_777"
        )

        assert result.status == TaskStatus.WORKING
        assert result.success is False
        assert result.data is not None

    @pytest.mark.asyncio
    async def test_a2a_webhook_taskstatusupdateevent_input_required(self):
        """Test A2A TaskStatusUpdateEvent with input-required status."""
        input_data = {
            "reason": "APPROVAL_REQUIRED",
        }

        event = TaskStatusUpdateEvent(
            task_id="task_888",
            context_id="ctx_999",
            status=A2ATaskStatus(
                state=TaskState.input_required,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message=Message(
                    message_id="msg_888",
                    role=Role.agent,
                    parts=[
                        Part(root=DataPart(data=input_data)),
                        Part(root=TextPart(text="Campaign budget $150K requires VP approval")),
                    ],
                ),
            ),
        )

        result = await self.client.handle_webhook(
            event, task_type="create_media_buy", operation_id="op_888"
        )

        assert result.status == TaskStatus.NEEDS_INPUT
        assert result.success is False
        assert result.data is not None  # Errors in structured data
        assert result.metadata["context_id"] == "ctx_999"

    @pytest.mark.asyncio
    async def test_a2a_webhook_taskstatusupdateevent_submitted(self):
        """Test A2A TaskStatusUpdateEvent with submitted status."""
        event = TaskStatusUpdateEvent(
            task_id="task_999",
            context_id="ctx_000",
            status=A2ATaskStatus(
                state=TaskState.submitted,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message=Message(
                    message_id="msg_999",
                    role=Role.agent,
                    parts=[
                        Part(root=TextPart(text="Task submitted and queued for processing")),
                    ],
                ),
            ),
        )

        result = await self.client.handle_webhook(
            event, task_type="create_media_buy", operation_id="op_999"
        )

        assert result.status == TaskStatus.SUBMITTED
        assert result.success is False
        assert result.metadata["task_id"] == "task_999"

    @pytest.mark.asyncio
    async def test_a2a_webhook_taskstatusupdateevent_no_message(self):
        """Test A2A TaskStatusUpdateEvent with no status.message (edge case)."""
        event = TaskStatusUpdateEvent(
            task_id="task_1010",
            context_id="ctx_1010",
            status=A2ATaskStatus(
                state=TaskState.working,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message=None,  # No message
            ),
        )

        result = await self.client.handle_webhook(
            event, task_type="create_media_buy", operation_id="op_1010"
        )

        assert result.status == TaskStatus.WORKING
        assert result.data is None  # No data extracted

    @pytest.mark.asyncio
    async def test_a2a_webhook_signature_not_required(self):
        """Verify signature parameter is ignored for A2A webhooks."""
        task = Task(
            id="task_666",
            context_id="ctx_777",
            status=A2ATaskStatus(
                state="completed", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[
                Artifact(
                    artifact_id="test_artifact",
                    parts=[
                        Part(
                            root=DataPart(
                                data={
                                    "media_buy_id": "mb_666",
                                    "buyer_ref": "ref_666",
                                    "packages": [],
                                }
                            )
                        )
                    ],
                )
            ],
        )

        # Signature should be ignored for A2A webhooks
        result = await self.client.handle_webhook(
            task,
            task_type="create_media_buy",
            operation_id="op_666",
            signature="ignored_signature",
        )

        assert result.status == TaskStatus.COMPLETED


class TestUnifiedInterface:
    """Test unified webhook interface across protocols."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mcp_config = AgentConfig(
            id="mcp_agent",
            agent_uri="https://mcp.example.com",
            protocol=Protocol.MCP,
        )
        self.a2a_config = AgentConfig(
            id="a2a_agent",
            agent_uri="https://a2a.example.com",
            protocol=Protocol.A2A,
        )
        self.mcp_client = ADCPClient(self.mcp_config)
        self.a2a_client = ADCPClient(self.a2a_config)

    @pytest.mark.asyncio
    async def test_type_detection_mcp_dict(self):
        """Verify dict payload routes to MCP handler."""
        payload = {
            "idempotency_key": "whk_task_mcpxxxx",
            "task_id": "task_mcp",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": {"media_buy_id": "mb_mcp", "buyer_ref": "ref_mcp", "packages": []},
        }

        result = await self.mcp_client.handle_webhook(
            payload, task_type="create_media_buy", operation_id="op_mcp"
        )

        assert result.status == TaskStatus.COMPLETED
        assert result.metadata["task_id"] == "task_mcp"

    @pytest.mark.asyncio
    async def test_type_detection_a2a_task(self):
        """Verify Task object routes to A2A handler."""
        task = Task(
            id="task_a2a",
            context_id="ctx_a2a",
            status=A2ATaskStatus(
                state="completed", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[
                Artifact(
                    artifact_id="test_artifact",
                    parts=[
                        Part(
                            root=DataPart(
                                data={
                                    "media_buy_id": "mb_a2a",
                                    "buyer_ref": "ref_a2a",
                                    "packages": [],
                                }
                            )
                        )
                    ],
                )
            ],
        )

        result = await self.a2a_client.handle_webhook(
            task, task_type="create_media_buy", operation_id="op_a2a"
        )

        assert result.status == TaskStatus.COMPLETED
        assert result.metadata["task_id"] == "task_a2a"

    @pytest.mark.asyncio
    async def test_type_detection_a2a_taskstatusupdateevent(self):
        """Verify TaskStatusUpdateEvent object routes to A2A handler."""
        event = TaskStatusUpdateEvent(
            task_id="task_event",
            context_id="ctx_event",
            status=A2ATaskStatus(
                state=TaskState.working,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message=Message(
                    message_id="msg_event",
                    role=Role.agent,
                    parts=[Part(root=TextPart(text="Processing"))],
                ),
            ),
        )

        result = await self.a2a_client.handle_webhook(
            event, task_type="create_media_buy", operation_id="op_event"
        )

        assert result.status == TaskStatus.WORKING
        assert result.metadata["task_id"] == "task_event"

    @pytest.mark.asyncio
    async def test_consistent_result_format(self):
        """Verify MCP and A2A return identical TaskResult structure."""
        media_buy_data = {"media_buy_id": "mb_test", "buyer_ref": "ref_test", "packages": []}

        # MCP webhook
        mcp_payload = {
            "idempotency_key": "whk_task_1xxxxxx",
            "task_id": "task_1",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": media_buy_data,
        }

        # A2A webhook with same data
        a2a_task = Task(
            id="task_2",
            context_id="ctx_2",
            status=A2ATaskStatus(
                state="completed", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[
                Artifact(
                    artifact_id="test_artifact", parts=[Part(root=DataPart(data=media_buy_data))]
                )
            ],
        )

        mcp_result = await self.mcp_client.handle_webhook(
            mcp_payload, task_type="create_media_buy", operation_id="op_1"
        )
        a2a_result = await self.a2a_client.handle_webhook(
            a2a_task, task_type="create_media_buy", operation_id="op_2"
        )

        # Both should return same structure
        assert mcp_result.success == a2a_result.success
        assert mcp_result.status == a2a_result.status
        assert mcp_result.data is not None
        assert a2a_result.data is not None


class TestExtractWebhookResultData:
    """Test extract_webhook_result_data utility function."""

    def test_extract_from_mcp_webhook(self):
        """Test extracting result from MCP webhook payload."""
        mcp_payload = {
            "idempotency_key": "whk_task_123xxxx",
            "task_id": "task_123",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": {"media_buy_id": "mb_123", "buyer_ref": "ref_123", "packages": []},
        }

        result = extract_webhook_result_data(mcp_payload)

        assert result is not None
        assert result["media_buy_id"] == "mb_123"
        assert result["buyer_ref"] == "ref_123"
        assert result["packages"] == []

    def test_extract_from_a2a_task_webhook(self):
        """Test extracting result from A2A Task webhook payload."""
        media_buy_data = {"media_buy_id": "mb_456", "buyer_ref": "ref_456", "packages": []}

        task = Task(
            id="task_456",
            context_id="ctx_456",
            status=A2ATaskStatus(
                state="completed", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[
                Artifact(
                    artifact_id="artifact_456",
                    parts=[
                        Part(root=DataPart(data=media_buy_data)),
                        Part(root=TextPart(text="Media buy created")),
                    ],
                )
            ],
        )

        # Convert to dict (simulating JSON deserialization)
        task_dict = _MessageToDict(task, preserving_proto_field_name=False)
        result = extract_webhook_result_data(task_dict)

        assert result is not None
        assert result["media_buy_id"] == "mb_456"
        assert result["buyer_ref"] == "ref_456"

    def test_extract_from_a2a_taskstatusupdateevent_webhook(self):
        """Test extracting result from A2A TaskStatusUpdateEvent webhook payload."""
        progress_data = {
            "current_step": "fetching_inventory",
            "percentage": 50,
        }

        event = TaskStatusUpdateEvent(
            task_id="task_777",
            context_id="ctx_777",
            status=A2ATaskStatus(
                state=TaskState.working,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message=Message(
                    message_id="msg_777",
                    role=Role.agent,
                    parts=[
                        Part(root=DataPart(data=progress_data)),
                        Part(root=TextPart(text="Processing...")),
                    ],
                ),
            ),
        )

        # Convert to dict (simulating JSON deserialization)
        event_dict = _MessageToDict(event, preserving_proto_field_name=False)
        result = extract_webhook_result_data(event_dict)

        assert result is not None
        assert result["current_step"] == "fetching_inventory"
        assert result["percentage"] == 50

    def test_extract_from_a2a_with_response_wrapper(self):
        """Test extracting result from A2A payload with {"response": {...}} wrapper."""
        wrapped_data = {
            "response": {"media_buy_id": "mb_789", "buyer_ref": "ref_789", "packages": []}
        }

        task = Task(
            id="task_789",
            context_id="ctx_789",
            status=A2ATaskStatus(
                state="completed", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[
                Artifact(artifact_id="artifact_789", parts=[Part(root=DataPart(data=wrapped_data))])
            ],
        )

        # Convert to dict
        task_dict = _MessageToDict(task, preserving_proto_field_name=False)
        result = extract_webhook_result_data(task_dict)

        # Should unwrap the response wrapper
        assert result is not None
        assert "response" not in result  # Unwrapped
        assert result["media_buy_id"] == "mb_789"

    def test_extract_from_mcp_with_null_result(self):
        """Test extracting from MCP webhook with None result."""
        mcp_payload = {
            "idempotency_key": "whk_task_111xxxx",
            "task_id": "task_111",
            "task_type": "create_media_buy",
            "status": "working",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": None,
        }

        result = extract_webhook_result_data(mcp_payload)

        assert result is None

    def test_extract_from_a2a_with_empty_artifacts(self):
        """Test extracting from A2A Task with empty artifacts array."""
        task = Task(
            id="task_222",
            context_id="ctx_222",
            status=A2ATaskStatus(
                state="completed", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[],
        )

        task_dict = _MessageToDict(task, preserving_proto_field_name=False)
        result = extract_webhook_result_data(task_dict)

        assert result is None

    def test_extract_from_a2a_with_no_data_part(self):
        """Test extracting from A2A Task with only TextPart (no DataPart)."""
        task = Task(
            id="task_333",
            context_id="ctx_333",
            status=A2ATaskStatus(
                state="completed", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[
                Artifact(
                    artifact_id="artifact_333",
                    parts=[Part(root=TextPart(text="Only text, no data"))],
                )
            ],
        )

        task_dict = _MessageToDict(task, preserving_proto_field_name=False)
        result = extract_webhook_result_data(task_dict)

        assert result is None

    def test_extract_from_a2a_with_multiple_artifacts(self):
        """Test extracting from A2A Task with multiple artifacts (should use last)."""
        old_data = {"media_buy_id": "mb_old"}
        new_data = {"media_buy_id": "mb_new"}

        task = Task(
            id="task_444",
            context_id="ctx_444",
            status=A2ATaskStatus(
                state="completed", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[
                Artifact(artifact_id="artifact_old", parts=[Part(root=DataPart(data=old_data))]),
                Artifact(artifact_id="artifact_new", parts=[Part(root=DataPart(data=new_data))]),
            ],
        )

        task_dict = _MessageToDict(task, preserving_proto_field_name=False)
        result = extract_webhook_result_data(task_dict)

        # Should use last artifact
        assert result is not None
        assert result["media_buy_id"] == "mb_new"

    def test_extract_from_a2a_taskstatusupdateevent_with_no_message(self):
        """Test extracting from A2A TaskStatusUpdateEvent with no status.message."""
        event = TaskStatusUpdateEvent(
            task_id="task_555",
            context_id="ctx_555",
            status=A2ATaskStatus(
                state=TaskState.working,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message=None,
            ),
        )

        event_dict = _MessageToDict(event, preserving_proto_field_name=False)
        result = extract_webhook_result_data(event_dict)

        assert result is None

    def test_extract_from_mcp_with_missing_result_field(self):
        """Test extracting from MCP webhook without result field."""
        mcp_payload = {
            "idempotency_key": "whk_task_666xxxx",
            "task_id": "task_666",
            "task_type": "create_media_buy",
            "status": "working",
            "timestamp": "2025-01-15T10:00:00Z",
            # No result field
        }

        result = extract_webhook_result_data(mcp_payload)

        assert result is None

    def test_extract_from_a2a_with_nested_response_wrapper(self):
        """Test that only single-key {"response": {...}} wrapper is unwrapped."""
        # Data with response wrapper but also other keys (should NOT unwrap)
        data_with_extra_keys = {"response": {"media_buy_id": "mb_777"}, "other_key": "value"}

        task = Task(
            id="task_777",
            context_id="ctx_777",
            status=A2ATaskStatus(
                state="completed", timestamp=datetime.now(timezone.utc).isoformat()
            ),
            artifacts=[
                Artifact(
                    artifact_id="artifact_777",
                    parts=[Part(root=DataPart(data=data_with_extra_keys))],
                )
            ],
        )

        task_dict = _MessageToDict(task, preserving_proto_field_name=False)
        result = extract_webhook_result_data(task_dict)

        # Should NOT unwrap (has multiple keys)
        assert result is not None
        assert "response" in result
        assert "other_key" in result

    def test_extract_from_mcp_with_error_response(self):
        """Test extracting from MCP webhook with error response."""
        mcp_payload = {
            "idempotency_key": "whk_task_888xxxx",
            "task_id": "task_888",
            "task_type": "create_media_buy",
            "status": "failed",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": {
                "errors": [
                    {
                        "code": "INTERNAL_ERROR",
                        "message": "Database connection failed",
                    }
                ]
            },
        }

        result = extract_webhook_result_data(mcp_payload)

        assert result is not None
        assert "errors" in result
        assert len(result["errors"]) == 1
        assert result["errors"][0]["code"] == "INTERNAL_ERROR"


class _DeliveryResponse(BaseModel):
    """Minimal Pydantic model for testing the BaseModel branch in payload builders."""

    media_buy_id: str
    buyer_ref: str
    packages: list[str] = []


class TestWebhookPayloadBuilderPydanticModel:
    """Pydantic BaseModel inputs to create_mcp_webhook_payload / create_a2a_webhook_payload.

    Regression guard for the PydanticBaseModel branch (model_dump path inside
    both builders). Prior to the fix these functions were typed to accept only
    AdcpAsyncResponseData, which is a narrow discriminated union — passing any
    other BaseModel subclass required a type: ignore comment even though the
    runtime hasattr(result, "model_dump") guard handled it correctly.
    """

    def test_create_mcp_payload_accepts_pydantic_model(self):
        from adcp.webhooks import to_wire_dict

        model = _DeliveryResponse(media_buy_id="mb_1", buyer_ref="ref_1")
        payload = create_mcp_webhook_payload(
            task_id="task_1",
            task_type="create_media_buy",
            status=GeneratedTaskStatus.completed,
            result=model,
        )
        assert to_wire_dict(payload)["result"] == {
            "media_buy_id": "mb_1",
            "buyer_ref": "ref_1",
            "packages": [],
        }

    def test_create_mcp_payload_pydantic_model_serialized_as_json(self):
        from adcp.webhooks import to_wire_dict

        model = _DeliveryResponse(media_buy_id="mb_2", buyer_ref="ref_2", packages=["pkg_a"])
        payload = create_mcp_webhook_payload(
            task_id="task_2",
            task_type="create_media_buy",
            status=GeneratedTaskStatus.completed,
            result=model,
        )
        result = to_wire_dict(payload)["result"]
        assert isinstance(result, dict)
        assert result["packages"] == ["pkg_a"]

    def test_create_a2a_payload_accepts_pydantic_model_completed(self):
        from a2a.types import Task as A2ATask

        model = _DeliveryResponse(media_buy_id="mb_3", buyer_ref="ref_3")
        task = create_a2a_webhook_payload(
            task_id="task_3",
            context_id="ctx_3",
            status=GeneratedTaskStatus.completed,
            result=model,
        )
        assert isinstance(task, A2ATask)
        task_dict = _MessageToDict(task, preserving_proto_field_name=False)
        extracted = extract_webhook_result_data(task_dict)
        assert extracted is not None
        assert extracted["media_buy_id"] == "mb_3"

    def test_create_a2a_payload_accepts_pydantic_model_working(self):
        from a2a.types import TaskStatusUpdateEvent as A2AEvent

        model = _DeliveryResponse(media_buy_id="mb_4", buyer_ref="ref_4")
        event = create_a2a_webhook_payload(
            task_id="task_4",
            context_id="ctx_4",
            status=GeneratedTaskStatus.working,
            result=model,
        )
        assert isinstance(event, A2AEvent)
        event_dict = _MessageToDict(event, preserving_proto_field_name=False)
        extracted = extract_webhook_result_data(event_dict)
        assert extracted is not None
        assert extracted["media_buy_id"] == "mb_4"


# Load official AdCP HMAC test vectors from fixtures.
# Source: adcontextprotocol/adcp PR #2478 (merged 2026-04-20), which pins the
# canonical on-wire JSON form (compact separators) and adds rejection vectors
# including the signer/serialization-mismatch case. Upstream file:
# https://github.com/adcontextprotocol/adcp/blob/main/static/test-vectors/webhook-hmac-sha256.json
_VECTORS_PATH = Path(__file__).parent / "fixtures" / "webhook-hmac-sha256.json"
_VECTORS_DATA = json.loads(_VECTORS_PATH.read_text())
HMAC_TEST_VECTORS_SECRET = _VECTORS_DATA["secret"]
HMAC_TEST_VECTORS = _VECTORS_DATA["vectors"]
HMAC_REJECTION_VECTORS = _VECTORS_DATA.get("rejection_vectors", [])


class TestHMACTestVectors:
    """Validate signing and verification against official AdCP HMAC test vectors."""

    @pytest.mark.parametrize(
        "vector",
        HMAC_TEST_VECTORS,
        ids=[v["description"] for v in HMAC_TEST_VECTORS],
    )
    def test_signing_matches_test_vector(self, vector):
        """Verify get_adcp_signed_headers_for_webhook produces correct signatures."""
        raw_body = vector["raw_body"]
        timestamp = vector["timestamp"]
        expected = vector["expected_signature"]

        # Sign using the raw_body as-is (simulating pre-serialized payload)
        import hashlib
        import hmac

        signed_message = f"{timestamp}.{raw_body}"
        signature_hex = hmac.new(
            HMAC_TEST_VECTORS_SECRET.encode("utf-8"),
            signed_message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        assert f"sha256={signature_hex}" == expected

    @pytest.mark.parametrize(
        "vector",
        HMAC_TEST_VECTORS,
        ids=[v["description"] for v in HMAC_TEST_VECTORS],
    )
    def test_get_adcp_signed_headers_produces_correct_signature(self, vector):
        """Verify get_adcp_signed_headers_for_webhook matches vectors for dict payloads."""
        raw_body = vector["raw_body"]
        timestamp = vector["timestamp"]
        expected = vector["expected_signature"]

        # Only test vectors with valid JSON that can be parsed as dicts
        if not raw_body or raw_body.strip() == "":
            pytest.skip("empty body cannot be passed as dict payload")
            return

        try:
            payload_dict = json.loads(raw_body)
        except json.JSONDecodeError:
            pytest.skip("non-JSON raw_body")
            return

        # The signer writes compact-separator bytes (matching httpx json=),
        # so the test vector only round-trips cleanly when its raw_body was
        # captured in the same form. Skip vectors that used spaced/pretty JSON.
        reserialized = json.dumps(payload_dict, separators=(",", ":"))
        if reserialized != raw_body:
            pytest.skip("raw_body uses different serialization than compact json.dumps")
            return

        headers = get_adcp_signed_headers_for_webhook(
            headers={},
            secret=HMAC_TEST_VECTORS_SECRET,
            timestamp=timestamp,
            payload=payload_dict,
        )

        assert headers["X-AdCP-Signature"] == expected
        assert headers["X-AdCP-Timestamp"] == str(timestamp)

    @pytest.mark.asyncio
    async def test_verify_fails_closed_when_raw_body_missing(self):
        """Per adcontextprotocol/adcp#2478, verifiers MUST fail closed when
        they cannot capture raw body bytes. Re-serializing a parsed payload
        to reconstruct the signed bytes silently fails against signers whose
        output differs in separator choice, key order, unicode escape policy,
        or number formatting — masking the signer bugs the verifier should
        surface.
        """
        config = AgentConfig(
            id="test_agent",
            agent_uri="https://test.example.com",
            protocol=Protocol.MCP,
        )
        client = ADCPClient(config, webhook_secret="test_secret")

        import hashlib
        import hmac

        valid_payload = {
            "idempotency_key": "whk_fail_closed_test",
            "task_id": "t1",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": "2025-01-15T10:00:00Z",
            "result": {"media_buy_id": "mb_1", "buyer_ref": "ref_1", "packages": []},
        }
        # Compute what would have been a valid signature under the old
        # re-serialize-from-payload fallback. Under the fail-closed rule,
        # this must no longer verify — even with a real HMAC over a real
        # serialization, if raw_body isn't captured, reject.
        timestamp = str(int(time.time()))
        body_bytes = json.dumps(valid_payload, separators=(",", ":")).encode("utf-8")
        signed_message = f"{timestamp}.{body_bytes.decode('utf-8')}"
        signature = hmac.new(
            b"test_secret", signed_message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        with pytest.raises(ADCPWebhookSignatureError):
            await client.handle_webhook(
                valid_payload,
                task_type="create_media_buy",
                operation_id="op_fail_closed",
                signature=f"sha256={signature}",
                timestamp=timestamp,
                # raw_body intentionally omitted — MUST reject
            )

        # Same call WITH raw_body must succeed, proving the rejection is
        # specifically about the missing raw_body, not the signature itself.
        result = await client.handle_webhook(
            valid_payload,
            task_type="create_media_buy",
            operation_id="op_fail_closed_ok",
            signature=f"sha256={signature}",
            timestamp=timestamp,
            raw_body=body_bytes,
        )
        assert result.status == TaskStatus.COMPLETED

    def test_signer_matches_httpx_json_wire_form(self):
        """Signer must produce the same bytes httpx writes for `json=payload`.

        Regression guard for the round-4 blocker: default json.dumps uses
        ", "/": " separators while httpx writes compact ","/":" — sellers
        using the documented ``client.post(url, json=payload, headers=signed)``
        pattern got silent 401s. Verifying directly against httpx's wire
        bytes means any future drift fails loudly in CI.
        """
        import hashlib
        import hmac

        import httpx

        payload = {
            "task_id": "task_123",
            "status": "completed",
            "result": {"products": [{"id": "p1", "price": 12.5}]},
            "nested": {"a": 1, "b": None, "c": [1, 2, 3]},
        }
        timestamp = "1700000000"
        secret = "test_secret"

        headers = get_adcp_signed_headers_for_webhook(
            headers={},
            secret=secret,
            timestamp=timestamp,
            payload=payload,
        )

        # The bytes httpx actually sends on the wire for json=
        httpx_wire = httpx.Request("POST", "http://localhost/", json=payload).content
        signed_message = f"{timestamp}.{httpx_wire.decode()}"
        expected_sig = hmac.new(
            secret.encode("utf-8"), signed_message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        assert headers["X-AdCP-Signature"] == f"sha256={expected_sig}"

    @pytest.mark.parametrize(
        "vector",
        HMAC_TEST_VECTORS,
        ids=[v["description"] for v in HMAC_TEST_VECTORS],
    )
    @pytest.mark.asyncio
    async def test_verify_webhook_signature_with_raw_body(self, vector):
        """Verify _verify_webhook_signature passes with raw_body for all vectors."""
        raw_body = vector["raw_body"]
        timestamp = str(vector["timestamp"])
        expected = vector["expected_signature"]

        config = AgentConfig(
            id="test_agent",
            agent_uri="https://test.example.com",
            protocol=Protocol.MCP,
        )
        # Use large tolerance so test vectors with historical timestamps pass
        client = ADCPClient(
            config,
            webhook_secret=HMAC_TEST_VECTORS_SECRET,
            webhook_timestamp_tolerance=10**10,
        )

        # Use raw_body path — should always verify correctly
        result = client._verify_webhook_signature(
            payload={},  # ignored when raw_body is provided
            signature=expected,
            timestamp=timestamp,
            raw_body=raw_body,
        )

        assert result is True

    @pytest.mark.parametrize(
        "vector",
        HMAC_TEST_VECTORS,
        ids=[v["description"] for v in HMAC_TEST_VECTORS],
    )
    @pytest.mark.asyncio
    async def test_verify_rejects_wrong_signature_with_raw_body(self, vector):
        """Verify _verify_webhook_signature rejects tampered signatures."""
        raw_body = vector["raw_body"]
        timestamp = str(vector["timestamp"])

        config = AgentConfig(
            id="test_agent",
            agent_uri="https://test.example.com",
            protocol=Protocol.MCP,
        )
        # Use large tolerance so test vectors with historical timestamps pass
        client = ADCPClient(
            config,
            webhook_secret=HMAC_TEST_VECTORS_SECRET,
            webhook_timestamp_tolerance=10**10,
        )

        result = client._verify_webhook_signature(
            payload={},
            signature="sha256=0000000000000000000000000000000000000000000000000000000000000000",
            timestamp=timestamp,
            raw_body=raw_body,
        )

        assert result is False

    @pytest.mark.parametrize(
        "vector",
        HMAC_REJECTION_VECTORS,
        ids=[v["description"] for v in HMAC_REJECTION_VECTORS],
    )
    def test_rejection_vectors_do_not_collapse_to_positive(self, vector):
        """Mirrors the upstream adcp#2478 CI: for each rejection vector whose
        claimed signature has a well-formed `sha256=<hex>` shape, verify that
        a correctly-computed HMAC over the claimed raw_body does NOT match
        the claimed signature — otherwise the rejection vector silently
        collapses into a positive case and stops catching what it claims to.
        """
        import hashlib
        import hmac as _hmac

        sig = vector.get("signature")
        raw_body = vector.get("raw_body")
        timestamp = vector.get("timestamp")

        # Skip structural-rejection cases where the signature is empty, None,
        # has prefix mismatches, or isn't a numeric HMAC (e.g. the
        # "sha256=valid_but_irrelevant" or "Double sha256= prefix" vectors).
        # Those are documented for verifier implementers but not computable.
        if not isinstance(sig, str) or not sig:
            pytest.skip("non-computable rejection shape")
        if not _re.fullmatch(r"sha(256|512)=[0-9a-f]+", sig):
            pytest.skip("malformed rejection signature — structural, not computational")
        if not isinstance(timestamp, int) or raw_body is None:
            pytest.skip("missing raw_body or timestamp")

        message = f"{timestamp}.{raw_body}"
        computed = (
            "sha256="
            + _hmac.new(
                HMAC_TEST_VECTORS_SECRET.encode("utf-8"),
                message.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )

        assert computed != sig, (
            f"Rejection vector {vector['description']!r} collapses into a "
            "positive case — the claimed signature is a correct HMAC over "
            "the claimed raw_body. Fix the vector upstream."
        )

    @pytest.mark.parametrize(
        "vector",
        HMAC_REJECTION_VECTORS,
        ids=[v["description"] for v in HMAC_REJECTION_VECTORS],
    )
    @pytest.mark.asyncio
    async def test_verifier_rejects_upstream_rejection_vectors(self, vector):
        """The verifier MUST reject every upstream rejection vector that
        provides enough context to run end-to-end (signature + raw_body +
        numeric timestamp). This is the behavioral mirror of the
        computational check above: not just "the claimed sig doesn't match"
        but "our verifier returns False."
        """
        sig = vector.get("signature")
        raw_body = vector.get("raw_body")
        timestamp = vector.get("timestamp")

        if not isinstance(sig, str) or not sig:
            pytest.skip("non-computable rejection shape")
        if raw_body is None or not isinstance(timestamp, int):
            pytest.skip("vector missing raw_body or timestamp")

        config = AgentConfig(
            id="test_agent",
            agent_uri="https://test.example.com",
            protocol=Protocol.MCP,
        )
        client = ADCPClient(
            config,
            webhook_secret=HMAC_TEST_VECTORS_SECRET,
            webhook_timestamp_tolerance=10**10,
        )
        result = client._verify_webhook_signature(
            payload={},
            signature=sig,
            timestamp=str(timestamp),
            raw_body=raw_body,
        )
        assert result is False, f"Verifier accepted rejection vector {vector['description']!r}"
