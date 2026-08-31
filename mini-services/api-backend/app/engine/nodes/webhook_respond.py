"""Respond to Webhook - releases the waiting HTTP caller mid-flow.

n8n-parity node (v21). In a webhook-triggered workflow whose Webhook Trigger
uses ``response_mode="respond_node"``, this node answers the inbound HTTP
request with a custom status code + body while the flow continues running
downstream. Anywhere else (manual runs, schedules, other webhook modes) there
is no caller to answer and the node fails with an explicit error.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar

from pydantic import BaseModel, Field

from ..nodes.base import BaseNode, Handle, NodeExecutionError, NodeResult


class RespondToWebhookNode(BaseNode):
    """Sends a custom HTTP response for the current webhook call."""

    type = "respond_to_webhook"
    name = "Respond to Webhook"
    description = (
        "Answers the webhook caller with a custom status code and body, then lets "
        "the flow continue downstream. Requires the Webhook Trigger's response mode "
        "set to 'respond_node'."
    )
    category = "actions"
    icon = "reply"
    color = "#38bdf8"
    inputs: ClassVar[list[Handle]] = [Handle("main", "In")]
    outputs: ClassVar[list[Handle]] = [Handle("main", "Out")]

    class ParamsModel(BaseModel):
        status_code: int = Field(
            default=200,
            ge=100,
            le=599,
            description="HTTP status code returned to the webhook caller",
        )
        body: str = Field(
            default='{"ok": true}',
            description=(
                'Response body. Jinja is resolved first - a JSON template like '
                '{"echo": "{{ input.body.msg }}"} - and parsed when content type is JSON.'
            ),
            json_schema_extra={"widget": "textarea", "rows": 6, "hint": 'e.g. {"ticket": "{{ nodes.webhook1.output.body.id }}" }'},
        )
        content_type: str = Field(
            default="application/json",
            description="Content-Type of the response body",
            json_schema_extra={"widget": "select", "options": ["application/json", "text/plain"]},
        )

    async def execute(self, context) -> NodeResult:  # type: ignore[override]
        p: RespondToWebhookNode.ParamsModel = self.params  # type: ignore[assignment]
        if context.respond_channel is None:
            raise NodeExecutionError(
                "Respond to Webhook has no caller to answer: this run is not a webhook "
                "execution with response_mode='respond_node' (the node only produces an "
                "HTTP response on webhook-triggered runs)"
            )

        body_text = p.body if isinstance(p.body, str) else json.dumps(p.body, default=str)
        if p.content_type == "application/json":
            try:
                payload: object = json.loads(body_text)
            except (ValueError, TypeError) as exc:
                raise NodeExecutionError(
                    f'Respond body is not valid JSON after template resolution: {body_text[:120]!r} ({exc})'
                ) from exc
        else:
            payload = body_text

        # params have already been Jinja-resolved by validate_parameters(), so a
        # template like "{{ 200 }}" arriving as a string is still accepted here.
        status = int(re.sub(r"[^0-9]", "", str(p.status_code)) or 200)

        await context.respond_channel(status, payload, p.content_type)
        # Pass the incoming payload through so downstream nodes keep running -
        # answering early does not end the flow (n8n semantics).
        return self._single(context.current_input)
