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
            description="Reply to the caller right away, or wait for the last node result",
            json_schema_extra={"widget": "select", "options": ["immediately", "last_node"]},
        )
        allowed_methods: str = Field(
            default="POST",
            description="Comma-separated HTTP methods accepted by this webhook",
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
