"""Trigger nodes: manual, webhook, schedule."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import ClassVar

from pydantic import BaseModel, Field

from .base import BaseNode, Handle, NodeResult


class ManualTriggerNode(BaseNode):
    """Fires when a user presses ▶ Run in the canvas."""

    type = "manual_trigger"
    name = "Manual Trigger"
    description = "Starts the workflow when you click Run. Injects an optional test payload."
    category = "triggers"
    icon = "play"
    color = "#8b5cf6"
    inputs: ClassVar[list[Handle]] = []
    outputs: ClassVar[list[Handle]] = [Handle("main", "Out")]

    class ParamsModel(BaseModel):
        payload: dict = Field(
            default_factory=dict,
            description="Static test payload injected into the execution context",
            json_schema_extra={"widget": "code", "rows": 6},
        )

    async def execute(self, context) -> NodeResult:
        p = self.params  # type: ManualTriggerNode.ParamsModel
        # Run-request payload merges over the configured static payload,
        # so pressing Run (no payload) still uses the canvas-configured data.
        override = context.trigger_payload.get("payload") or {}
        payload = {**(p.payload or {}), **override}
        return self._single(
            {
                "payload": payload,
                "trigger_type": "manual",
                "triggered_at": datetime.now(timezone.utc).isoformat(),
            }
        )


class WebhookTriggerNode(BaseNode):
    """Fired by an inbound HTTP call to /api/v1/webhooks/{workflow_id}."""

    type = "webhook_trigger"
    name = "Webhook Trigger"
    description = "Runs the workflow when an external HTTP request hits the workflow webhook URL."
    category = "triggers"
    icon = "webhook"
    color = "#f97316"
    inputs: ClassVar[list[Handle]] = []
    outputs: ClassVar[list[Handle]] = [Handle("main", "Out")]

    class ParamsModel(BaseModel):
        response_mode: str = Field(
            default="immediately",
            description=(
                "immediately = reply 202 at once; last_node = wait and return the final "
                "node output; respond_node = wait for a Respond to Webhook node to send a "
                "custom response mid-flow"
            ),
            json_schema_extra={"widget": "select", "options": ["immediately", "last_node", "respond_node"]},
        )
        allowed_methods: str = Field(
            default="POST",
            description="Comma-separated HTTP methods accepted by this webhook",
        )
        # v23: webhook authentication — checked BEFORE the flow runs (401 on failure)
        auth_mode: str = Field(
            default="none",
            description="none = public; header = a required header must carry the expected value; basic = HTTP Basic auth",
            json_schema_extra={"widget": "select", "options": ["none", "header", "basic"]},
        )
        auth_header_name: str = Field(
            default="X-Webhook-Token",
            description="Header mode: required header name",
        )
        auth_header_value: str = Field(
            default="",
            description="Header mode: required header value",
        )
        auth_user: str = Field(
            default="",
            description="Basic mode: expected username",
        )
        auth_pass: str = Field(
            default="",
            description="Basic mode: expected password",
        )

    async def execute(self, context) -> NodeResult:
        tp = context.trigger_payload
        return self._single(
            {
                "method": tp.get("method", "POST"),
                "headers": tp.get("headers", {}),
                "query": tp.get("query", {}),
                "body": tp.get("body"),
                "trigger_type": "webhook",
                "triggered_at": datetime.now(timezone.utc).isoformat(),
            }
        )


class ScheduleTriggerNode(BaseNode):
    """Fired by APScheduler on interval or CRON expressions."""

    type = "schedule_trigger"
    name = "Schedule Trigger"
    description = "Runs the workflow on a fixed interval or a CRON schedule."
    category = "triggers"
    icon = "clock"
    color = "#eab308"
    inputs: ClassVar[list[Handle]] = []
    outputs: ClassVar[list[Handle]] = [Handle("main", "Out")]

    class ParamsModel(BaseModel):
        mode: str = Field(
            default="interval",
            description="Scheduling mode",
            json_schema_extra={"widget": "select", "options": ["interval", "cron"]},
        )
        interval_seconds: int = Field(
            default=300,
            ge=5,
            description="Seconds between runs (interval mode)",
        )
        cron: str = Field(
            default="*/5 * * * *",
            description="CRON expression (cron mode), e.g. '0 9 * * 1-5'",
        )

    async def execute(self, context) -> NodeResult:
        tp = context.trigger_payload
        return self._single(
            {
                "scheduled_time": tp.get("fired_at", datetime.now(timezone.utc).isoformat()),
                "node_id": self.id,
                "trigger_type": "schedule",
            }
        )


class ErrorTriggerNode(BaseNode):
    """Dedicated entry point for error-handler workflows (v22).

    When a workflow fails and has an error-workflow binding, the runner
    dispatches the handler with ``trigger_type="error"``; this trigger is
    selected for the run (see GraphRunner._pick_trigger) and its output
    exposes the structured error payload to downstream nodes.
    """

    type = "error_trigger"
    name = "Error Trigger"
    description = (
        "Entry point for error-handler workflows: starts when another workflow "
        "fails and exposes {execution_id, workflow_id, workflow_name, error, failed_nodes}."
    )
    category = "triggers"
    icon = "siren"
    color = "#ef4444"
    inputs: ClassVar[list[Handle]] = []
    outputs: ClassVar[list[Handle]] = [Handle("main", "Out")]

    class ParamsModel(BaseModel):
        include_failed_nodes: bool = Field(
            default=True,
            description="Include the failed_nodes list (id/name/error per failed node) in the output",
        )

    async def execute(self, context) -> NodeResult:
        tp = context.trigger_payload
        payload: dict[str, Any] = {
            "execution_id": tp.get("execution_id"),
            "workflow_id": tp.get("workflow_id"),
            "workflow_name": tp.get("workflow_name"),
            "error": tp.get("error"),
            "trigger_type": "error",
            "triggered_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.params.include_failed_nodes:
            payload["failed_nodes"] = tp.get("failed_nodes") or []
        return self._single(payload)
