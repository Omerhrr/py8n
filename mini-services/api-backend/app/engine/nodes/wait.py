"""Wait for Resume node - human-in-the-loop pause point.

When the runner reaches this node the whole execution *suspends*: the run
state (node outputs + active edges) is persisted with the execution log and
the execution transitions to ``waiting``. Nothing downstream runs until an
external actor calls the resume endpoint with the token embedded in the
wait node's output (an approval, a callback, a form submission...).

The runner owns the suspend/resume mechanics (see ``GraphRunner._suspend``
and the ``resume_state`` constructor argument); this class only carries the
configuration and the ``pauses_execution`` marker.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ..context import ExecutionContext
from .base import BaseNode, Handle, NodeExecutionError, NodeResult


class WaitForResumeNode(BaseNode):
    """Pauses the execution until resumed with a token + optional payload."""

    type = "wait_for_resume"
    name = "Wait for Resume"
    description = (
        "Pauses the workflow and waits for an external call (approval, callback, form). "
        "Execution resumes through the generated resume URL; its payload becomes this node's output."
    )
    category = "logic"
    icon = "pause-circle"
    color = "#c084fc"

    pauses_execution: ClassVar[bool] = True

    inputs: ClassVar[list[Handle]] = [Handle("main", "In")]
    outputs: ClassVar[list[Handle]] = [Handle("main", "Out")]

    class ParamsModel(BaseModel):
        resume_hint: str = Field(
            default="Approve or POST data to this URL to continue the workflow",
            description="Human-readable instruction shown with the resume URL",
            json_schema_extra={"widget": "textarea", "rows": 2},
        )
        pass_through: bool = Field(
            default=False,
            description="Include the pre-wait upstream payload in the output as 'input'",
        )

    async def execute(self, context: ExecutionContext) -> NodeResult:  # pragma: no cover
        raise NodeExecutionError(
            "Wait for Resume must be suspended by the runner - this node cannot execute inline"
        )

    @staticmethod
    def waiting_output(execution_id: str, token: str, resume_hint: str, upstream: Any = None) -> dict:
        """Payload persisted as the node's output while the run is paused."""
        out: dict[str, Any] = {
            "paused": True,
            "resume_hint": resume_hint,
            "method": "POST",
            "resume_url": f"/api/v1/executions/{execution_id}/resume",
            "token": token,
        }
        if upstream is not None:
            out["input"] = upstream
        return out


__all__ = ["WaitForResumeNode"]
