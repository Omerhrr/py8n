"""Sticky Note - canvas annotation node (v19).

Rendered as a colored sticky on the canvas and persisted inside the graph
(like n8n notes), but it is NOT part of the execution vocabulary: it is
hidden from the node palette / definitions API and, if wired into a flow
anyway, simply passes its input through untouched.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from ..context import ExecutionContext
from .base import BaseNode, NodeResult


class StickyNoteNode(BaseNode):
    type = "sticky_note"
    name = "Sticky Note"
    description = "Canvas annotation - never executes; documents your workflow."
    category = "actions"
    icon = "sticky-note"
    color = "#fbbf24"
    hidden: ClassVar[bool] = True  # excluded from /node-definitions and the palette

    class ParamsModel(BaseModel):
        text: str = Field(
            default="Note something down…",
            json_schema_extra={"widget": "textarea", "rows": 5},
        )
        color: str = Field(
            default="amber",
            description="amber | emerald | sky | rose | violet",
            json_schema_extra={"widget": "select", "options": ["amber", "emerald", "sky", "rose", "violet"]},
        )

    async def execute(self, context: ExecutionContext) -> NodeResult:
        # Annotation only - pass the input through untouched.
        return self._single(context.current_input)
