"""Execute Workflow node — run another Py8n workflow inline (sub-workflow)."""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ..context import ExecutionContext
from .base import BaseNode, NodeExecutionError, NodeResult

MAX_DEPTH = 3  # root workflow may nest up to this many levels


class ExecuteWorkflowNode(BaseNode):
    """Loads a workflow from the library and runs it synchronously.

    The sub-run shares the parent's event stream silence: it does not emit
    node events (canvas node ids would collide) — instead the summarized
    result lands on this node's output. Each sub-run is NOT persisted as a
    top-level execution log; the parent execution carries everything.
    """

    type = "execute_workflow"
    name = "Execute Workflow"
    description = "Runs another workflow from your library and returns its last node output."
    category = "actions"
    icon = "workflow"
    color = "#60a5fa"

    class ParamsModel(BaseModel):
        workflow_id: str = Field(default="", description="Target workflow id", json_schema_extra={"widget": "workflow"})
        payload: dict[str, Any] = Field(
            default_factory=dict,
            description="Data injected into the sub-workflow trigger output",
            json_schema_extra={"widget": "code", "rows": 6, "language": "json"},
        )
        wait_for_completion: bool = Field(default=True, description="Run synchronously and return the result")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: ExecuteWorkflowNode.ParamsModel
        if not p.workflow_id:
            raise NodeExecutionError("No workflow selected — pick one in the node settings")
        if context.depth >= MAX_DEPTH:
            raise NodeExecutionError(f"Sub-workflow nesting too deep (limit {MAX_DEPTH})")

        payload = context.resolve(p.payload) if p.payload else {}
        if not isinstance(payload, dict):
            payload = {"value": payload}

        from sqlalchemy import select

        from ...db import AsyncSessionLocal
        from ...models import Workflow
        from ..runner import GraphRunner, validate_graph_document

        async with AsyncSessionLocal() as session:
            workflow = (
                await session.execute(select(Workflow).where(Workflow.id == p.workflow_id))
            ).scalar_one_or_none()
        if workflow is None:
            raise NodeExecutionError(f"Workflow {p.workflow_id!r} not found")
        if workflow.id == context.workflow_id:
            raise NodeExecutionError("A workflow cannot execute itself (infinite recursion)")

        graph = validate_graph_document(workflow.graph or {"nodes": [], "edges": []})

        runner = GraphRunner(
            graph,
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            trigger_type="manual",
            trigger_payload={"payload": payload},
            execution_id=uuid.uuid4().hex,
            depth=context.depth + 1,
            honor_pinned=context.honor_pinned,  # v17: manual chains honor pins, production never does
        )
        if not p.wait_for_completion:
            return self._single({"subworkflow": {"id": workflow.id, "name": workflow.name}, "queued": True})

        result = await runner.run()
        sub_status = result["status"]
        if sub_status == "waiting":
            raise NodeExecutionError(
                f"Sub-workflow {workflow.name!r} paused on a Wait for Resume node — "
                "wait nodes are not supported inside sub-workflows"
            )
        if sub_status != "success":
            raise NodeExecutionError(
                f"Sub-workflow {workflow.name!r} finished with status {sub_status}: {result.get('error')}"
            )

        last_run = next(
            (r for r in reversed(result["node_runs"]) if r["status"] == "success"),
            None,
        )
        return self._single(
            {
                "subworkflow": {
                    "id": workflow.id,
                    "name": workflow.name,
                    "execution_id": result["execution_id"],
                    "status": sub_status,
                    "duration_ms": result["duration_ms"],
                },
                "output": last_run["output"] if last_run else None,
            }
        )
