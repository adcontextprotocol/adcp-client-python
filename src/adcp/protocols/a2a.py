"""A2A protocol adapter using HTTP client.

The official a2a-sdk is primarily for building A2A servers. For client functionality,
we implement the A2A protocol using HTTP requests as per the A2A specification.
"""

from typing import Any
from uuid import uuid4

import httpx

from adcp.protocols.base import ProtocolAdapter
from adcp.types.core import TaskResult, TaskStatus


class A2AAdapter(ProtocolAdapter):
    """Adapter for A2A protocol following the Agent2Agent specification."""

    async def call_tool(self, tool_name: str, params: dict[str, Any]) -> TaskResult[Any]:
        """
        Call a tool using A2A protocol.

        A2A uses a tasks/send endpoint to initiate tasks. The agent responds with
        task status and may require multiple roundtrips for completion.
        """
        async with httpx.AsyncClient() as client:
            headers = {"Content-Type": "application/json"}

            if self.agent_config.auth_token:
                headers["Authorization"] = f"Bearer {self.agent_config.auth_token}"

            # Construct A2A message
            message = {
                "role": "user",
                "parts": [
                    {
                        "type": "text",
                        "text": self._format_tool_request(tool_name, params),
                    }
                ],
            }

            # A2A uses tasks/send endpoint
            url = f"{self.agent_config.agent_uri}/tasks/send"

            request_data = {
                "message": message,
                "context_id": str(uuid4()),
            }

            try:
                response = await client.post(
                    url,
                    json=request_data,
                    headers=headers,
                    timeout=30.0,
                )
                response.raise_for_status()

                data = response.json()

                # Parse A2A response format
                # A2A tasks have lifecycle: submitted, working, completed, failed, input-required
                task_status = data.get("task", {}).get("status")

                if task_status in ("completed", "working"):
                    # Extract the result from the response message
                    result_data = self._extract_result(data)

                    return TaskResult[Any](
                        status=TaskStatus.COMPLETED,
                        data=result_data,
                        success=True,
                        metadata={"task_id": data.get("task", {}).get("id")},
                    )
                elif task_status == "failed":
                    return TaskResult[Any](
                        status=TaskStatus.FAILED,
                        error=data.get("message", {})
                        .get("parts", [{}])[0]
                        .get("text", "Task failed"),
                        success=False,
                    )
                else:
                    # Handle other states (submitted, input-required)
                    return TaskResult[Any](
                        status=TaskStatus.SUBMITTED,
                        data=data,
                        success=True,
                        metadata={"task_id": data.get("task", {}).get("id")},
                    )

            except httpx.HTTPError as e:
                return TaskResult[Any](
                    status=TaskStatus.FAILED,
                    error=str(e),
                    success=False,
                )

    def _format_tool_request(self, tool_name: str, params: dict[str, Any]) -> str:
        """Format tool request as natural language for A2A."""
        # For AdCP tools, we format as a structured request
        import json

        return f"Execute tool: {tool_name}\nParameters: {json.dumps(params, indent=2)}"

    def _extract_result(self, response_data: dict[str, Any]) -> Any:
        """Extract result data from A2A response."""
        # Try to extract structured data from response
        message = response_data.get("message", {})
        parts = message.get("parts", [])

        if not parts:
            return response_data

        # Return the first part's content
        first_part = parts[0]
        if first_part.get("type") == "text":
            # Try to parse as JSON if it looks like structured data
            text = first_part.get("text", "")
            try:
                import json

                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return text

        return first_part

    async def list_tools(self) -> list[str]:
        """
        List available tools from A2A agent.

        Note: A2A doesn't have a standard tools/list endpoint. Agents expose
        their capabilities through the agent card. For AdCP, we rely on the
        standard AdCP tool set.
        """
        async with httpx.AsyncClient() as client:
            headers = {"Content-Type": "application/json"}

            if self.agent_config.auth_token:
                headers["Authorization"] = f"Bearer {self.agent_config.auth_token}"

            # Try to fetch agent card (OpenAPI spec)
            url = f"{self.agent_config.agent_uri}/agent-card"

            try:
                response = await client.get(url, headers=headers, timeout=10.0)
                response.raise_for_status()

                data = response.json()

                # Extract skills from agent card
                skills = data.get("skills", [])
                return [skill.get("name", "") for skill in skills if skill.get("name")]

            except httpx.HTTPError:
                # If agent card is not available, return empty list
                return []
