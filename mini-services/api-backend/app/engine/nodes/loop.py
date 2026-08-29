"""Loop Over Items node — per-batch downstream execution (n8n SplitInBatches parity).

Design
======
``loop_over_items`` is *runner-orchestrated*: the node itself only slices the
incoming items into batches. The GraphRunner detects ``is_loop_node``, treats
everything reachable from the ``loop`` output handle as the **loop body**, and
executes that body once per batch via a nested GraphRunner run.

Inside each batch iteration the body sees:

* ``input`` / ``input_data`` → ``{"items": <batch>, "batch": {"index", "total"}}``
  (emitted by the hidden ``_batch_trigger`` virtual node)
* ``{{ nodes.<loop_id>.output.items }}`` → the current batch (seeded context)
* ``{{ nodes.<upstream>.output.* }}`` → outputs of nodes that already ran
  *before* the Loop node (inherited execution context)

After all batches finish, the ``done`` handle carries::

    {"batches": N, "batch_size": B, "total_items": T, "items": <results>, "results": <results>}

where ``results[i]`` is the last successful body output of batch ``i``
(duplicated under ``items`` so Aggregate/Filter nodes can consume it directly).

Structural rules (validated at save/run — see ``runner.validate_loops``):
* every body node's inputs must come from inside the body or the Loop node
* a body node may not also hang off the ``done`` handle
* two sibling Loop nodes may not share body nodes (nested loops are fine)
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from ..context import ExecutionContext
from .base import BaseNode, Handle, NodeResult
from .data import _items, _pluck, _working_data


class LoopOverItemsNode(BaseNode):
    """Splits incoming items into batches and iterates the loop body over them."""

    type = "loop_over_items"
    name = "Loop Over Items"
    description = (
        "Iterates an array in batches: everything connected to the loop output runs "
        "once per batch; the aggregated results continue via done."
    )
    category = "logic"
    icon = "repeat"
    color = "#38bdf8"
    is_loop_node: ClassVar[bool] = True
    outputs: ClassVar[list[Handle]] = [Handle("loop", "Loop"), Handle("done", "Done")]

    class ParamsModel(BaseModel):
        items_path: str = Field(
            default="items",
            description="Dot-path to the array to iterate, relative to the incoming payload (empty = whole payload)",
        )
        batch_size: int = Field(default=1, ge=1, le=1000, description="How many items the body receives per iteration")

    def prepare(self, context: ExecutionContext) -> tuple[list[list], int]:
        """Validate parameters and slice the incoming data into batches."""
        p = self.validate_parameters(context)  # type: LoopOverItemsNode.ParamsModel
        working = _working_data(context.current_input)
        value = _pluck(working, p.items_path) if p.items_path else working
        if value is None:
            items: list = []
        elif isinstance(value, list):
            items = value
        else:
            items = [value]
        batch_size = max(1, int(p.batch_size))
        batches = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]
        return batches, batch_size

    async def execute(self, context: ExecutionContext) -> NodeResult:
        """Fallback used only when a Loop node is run *without* runner support."""
        batches, batch_size = self.prepare(context)
        return NodeResult(
            outputs={
                "loop": None,
                "done": {
                    "batches": len(batches),
                    "batch_size": batch_size,
                    "total_items": sum(len(b) for b in batches),
                    "items": [],
                    "results": [],
                    "note": "no body was executed (runner-orchestrated mode expected)",
                },
            },
            raw_output={"batches": len(batches)},
        )


class BatchTriggerNode(BaseNode):
    """Hidden virtual trigger that injects one batch into a loop-body sub-run.

    Registered so the runner can execute it, but hidden from the palette and
    node-definitions API — users never place this node themselves.
    """

    type = "_batch_trigger"
    name = "Batch Source"
    description = "Internal: injects the current loop batch into a loop-body sub-run."
    category = "logic"
    icon = "box"
    color = "#38bdf8"
    hidden: ClassVar[bool] = True
    inputs: ClassVar[list[Handle]] = []
    outputs: ClassVar[list[Handle]] = [Handle("main", "Out")]

    class ParamsModel(BaseModel):
        pass

    async def execute(self, context: ExecutionContext) -> NodeResult:
        tp = context.trigger_payload
        return self._single(
            {
                "items": tp.get("items", []),
                "batch": tp.get("batch", {"index": 0, "total": 1}),
            }
        )


# re-export convenience helpers for tests
__all__ = ["BatchTriggerNode", "LoopOverItemsNode", "_items", "_pluck", "_working_data"]
