"""Execution context passed through nodes during a run."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .templating import resolve_value


@dataclass
class ExecutionContext:
    """The global dictionary handed sequentially to every node.

    Structure exposed to nodes and to Jinja2 expressions::

        {
          "workflow":  {"id": ..., "name": ...},
          "execution": {"id": ..., "trigger_type": ..., "trigger_payload": ...},
          "nodes": {
             "<node_id>": {"status": "success", "output": {...}},
          },
          "input": <payload of first active incoming edge>,
          "inputs": {"<source_node_id>": <payload>, ...}
        }
    """

    workflow_id: str
    workflow_name: str
    execution_id: str
    trigger_type: str
    trigger_payload: dict[str, Any]
    node_states: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Filled by the runner right before a node executes
    current_inputs: dict[str, Any] = field(default_factory=dict)
    current_input: Any = None

    # v24 multi-input nodes: payload keyed by *targetHandle* ("main" /
    # "secondary" for Compare Datasets). Populated alongside current_inputs;
    # nodes with a single input never need to look at it.
    current_input_handles: dict[str, Any] = field(default_factory=dict)

    # Nesting depth for Execute Workflow nodes (sub-workflow recursion guard)
    depth: int = 0

    # Global environment variables (v15) - exposed to templates as ``env.KEY``.
    # Deliberately NOT included in snapshot(): execution logs must never
    # contain a dump of (possibly secret) values.
    env_vars: dict[str, str] = field(default_factory=dict)

    # v17 pinned data: whether THIS run honors node.pinned_data. Mirrored from
    # the owning GraphRunner so nodes that spawn sub-runs (Execute Workflow)
    # inherit the root decision - sub-workflows of a manual run honor pins,
    # production triggers (webhook/schedule/error) never do.
    honor_pinned: bool = False

    # v21 Respond to Webhook: async callable ``send(status_code, body,
    # content_type)`` installed ONLY by webhook runs with response_mode=
    # "respond_node". The respond_to_webhook node calls it to release the
    # waiting HTTP request; every other run leaves it None and the node
    # fails with a clear explanation instead of silently no-op'ing.
    respond_channel: Any = None

    # v36 live agent trace: async callable ``emit(event: dict)`` wired by the
    # runner to the execution event bus. The AI Agent node publishes fine-
    # grained ``agent_*`` events (iteration / reply / tool_call / tool_result /
    # answer) so SSE clients can watch the loop think in real time. Like
    # respond_channel this is deliberately NOT part of snapshot().
    emit: Any = None

    # ------------------------------------------------------------------
    def as_jinja_context(self) -> dict[str, Any]:
        return {
            "workflow": {"id": self.workflow_id, "name": self.workflow_name},
            "execution": {
                "id": self.execution_id,
                "trigger_type": self.trigger_type,
                "trigger_payload": self.trigger_payload,
            },
            "nodes": self.node_states,
            "input": self.current_input,
            "inputs": self.current_inputs,
            "env": dict(self.env_vars or {}),
            "now": datetime.now(timezone.utc).isoformat(),
        }

    def resolve(self, value: Any) -> Any:
        """Resolve a parameter value against this context (Jinja2)."""
        return resolve_value(value, self.as_jinja_context())

    def register(self, node_id: str, status: str, output: Any = None) -> None:
        self.node_states[node_id] = {"status": status, "output": output}

    def snapshot(self) -> dict[str, Any]:
        return {
            "workflow": {"id": self.workflow_id, "name": self.workflow_name},
            "execution": {
                "id": self.execution_id,
                "trigger_type": self.trigger_type,
                "trigger_payload": self.trigger_payload,
            },
            "nodes": self.node_states,
        }
