"""Standardized graph JSON schema.

A workflow graph is a plain JSON document:

    {
      "nodes": [
        {"id": "trigger_1", "type": "manual_trigger", "name": "Manual",
         "position": {"x": 0, "y": 0}, "parameters": {}}
      ],
      "edges": [
        {"id": "e1", "source": "trigger_1", "target": "set_1",
         "sourceHandle": "main", "targetHandle": "main"}
      ]
    }

This module validates that document with Pydantic before the runner touches it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class Position(BaseModel):
    x: float = 0
    y: float = 0


class NodeSettings(BaseModel):
    """Per-node resilience knobs (n8n-parity error handling)."""

    retry_on_fail: bool = Field(default=False, description="Retry the node when it raises")
    max_retries: int = Field(default=2, ge=1, le=5, description="Extra attempts when retry_on_fail is on")
    retry_wait_ms: int = Field(default=500, ge=0, le=10_000, description="Pause between attempts (ms)")
    continue_on_fail: bool = Field(
        default=False,
        description="On final failure, keep the flow alive: emit {'error': ...} on the main handle",
    )


class NodeSpec(BaseModel):
    """One node on the canvas."""

    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_\-]+$")
    type: str = Field(min_length=1, max_length=80)
    name: str = Field(default="", max_length=200)
    position: Position = Field(default_factory=Position)
    parameters: dict[str, Any] = Field(default_factory=dict)
    settings: NodeSettings = Field(default_factory=NodeSettings)
    # n8n-style node disable: the node is skipped but its active input is
    # passed through untouched so downstream nodes keep working.
    disabled: bool = Field(default=False, description="Skip execution, pass input through")
    # v17 n8n-style pinned output: when honored (manual runs + test step) the
    # node returns this data WITHOUT executing — mock data for building.
    # Webhook/schedule/error runs always execute for real. None = not pinned.
    pinned_data: Any = Field(
        default=None,
        description="Pinned output — returned instead of executing on manual runs and test steps",
    )

    @property
    def display_name(self) -> str:
        return self.name or self.type


class EdgeSpec(BaseModel):
    """A directed connection between two nodes (Vue Flow compatible)."""

    id: str = Field(min_length=1, max_length=120)
    source: str = Field(min_length=1, max_length=80)
    target: str = Field(min_length=1, max_length=80)
    # "main" for normal nodes; "true"/"false" for the IF node branches.
    sourceHandle: str = "main"
    targetHandle: str = "main"


class GraphSpec(BaseModel):
    """The full workflow graph document."""

    nodes: list[NodeSpec] = Field(default_factory=list)
    edges: list[EdgeSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_integrity(self) -> "GraphSpec":
        ids = [n.id for n in self.nodes]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"Duplicate node ids in graph: {sorted(dupes)}")
        known = set(ids)
        for edge in self.edges:
            if edge.source not in known:
                raise ValueError(f"Edge {edge.id!r} references unknown source node {edge.source!r}")
            if edge.target not in known:
                raise ValueError(f"Edge {edge.id!r} references unknown target node {edge.target!r}")
        return self

    # ------------------------------------------------------------------
    # Convenience accessors used by the runner
    # ------------------------------------------------------------------
    def node_map(self) -> dict[str, NodeSpec]:
        return {n.id: n for n in self.nodes}

    def incoming(self, node_id: str) -> list[EdgeSpec]:
        return [e for e in self.edges if e.target == node_id]

    def trigger_nodes(self) -> list[NodeSpec]:
        return [n for n in self.nodes if n.type.endswith("_trigger")]
