"""BaseNode - every Py8n node derives from this class.

The contract
============
* Each node declares a **Pydantic ``ParamsModel``**; raw canvas parameters are
  validated against it before execution. The JSON schema of that model is
  exported by ``GET /api/v1/node-definitions`` and the frontend renders
  configuration forms directly from it.
* ``execute()`` receives the shared :class:`~app.engine.context.ExecutionContext`
  and returns a :class:`NodeResult` mapping *output handles* to payloads.
  Normal nodes emit handle ``"main"``; branching nodes (IF) emit
  ``"true"`` / ``"false"``. A handle that is absent/None deactivates the
  corresponding outgoing edges, which marks downstream nodes as *skipped*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from ..context import ExecutionContext
from ..schema import NodeSpec


class NodeExecutionError(RuntimeError):
    """Raised when a node fails during execution."""


class NodeParameterError(RuntimeError):
    """Raised when canvas parameters fail schema validation."""


@dataclass
class Handle:
    """An input or output port on a node."""

    key: str
    label: str = ""


@dataclass
class NodeDefinition:
    """Self-describing metadata exported to the UI."""

    type: str
    name: str
    description: str
    category: str  # triggers | actions | logic | ai
    icon: str      # lucide icon name in the frontend
    color: str     # hex accent used on the canvas card
    inputs: list[Handle]
    outputs: list[Handle]
    parameters_schema: dict  # JSON schema of ParamsModel
    defaults: dict  # default parameter values


@dataclass
class NodeResult:
    """Outcome of one node execution."""

    outputs: dict[str, Any] = field(default_factory=dict)
    raw_output: Any = None  # convenience: whole payload (used for response_mode=last_node)


class BaseNode:
    """Abstract base for all executable nodes."""

    type: ClassVar[str] = "base"
    name: ClassVar[str] = "Base Node"
    description: ClassVar[str] = ""
    category: ClassVar[str] = "actions"
    icon: ClassVar[str] = "box"
    color: ClassVar[str] = "#8b8b9e"
    inputs: ClassVar[list[Handle]] = [Handle("main", "In")]
    outputs: ClassVar[list[Handle]] = [Handle("main", "Out")]
    ParamsModel: ClassVar[type[BaseModel] | None] = None

    def __init__(self, spec: NodeSpec):
        self.spec = spec
        self.id = spec.id
        self.name = spec.display_name
        self.params: BaseModel | None = None

    # ------------------------------------------------------------------
    # Schema export (used by /api/v1/node-definitions)
    # ------------------------------------------------------------------
    @classmethod
    def get_definition(cls) -> NodeDefinition:
        schema: dict = {}
        defaults: dict = {}
        if cls.ParamsModel is not None:
            schema = cls.ParamsModel.model_json_schema()
            schema.setdefault("properties", {})
            for prop_name, prop in schema["properties"].items():
                # promote pydantic field defaults into the schema so the UI can
                # prefill newly added nodes
                default = prop.pop("default", None) if isinstance(prop, dict) else None
                if default is not None:
                    defaults[prop_name] = default
            # inline nested-model $defs (e.g. the AI Agent's ToolSpec list) so
            # the frontend receives fully self-contained property schemas
            defs = schema.pop("$defs", None)
            if defs:
                schema = cls._inline_refs(schema, defs)
        return NodeDefinition(
            type=cls.type,
            name=cls.name,
            description=cls.description,
            category=cls.category,
            icon=cls.icon,
            color=cls.color,
            inputs=list(cls.inputs),
            outputs=list(cls.outputs),
            parameters_schema=schema,
            defaults=defaults,
        )

    # ------------------------------------------------------------------
    # Validation + execution
    # ------------------------------------------------------------------
    def validate_parameters(self, context: ExecutionContext) -> BaseModel:
        """Resolve Jinja expressions then validate against ParamsModel."""
        if self.ParamsModel is None:
            return BaseModel()
        resolved = {k: context.resolve(v) for k, v in self.spec.parameters.items()}
        try:
            self.params = self.ParamsModel(**resolved)
        except ValidationError as exc:
            raise NodeParameterError(f"Invalid parameters for {self.name}: {exc.errors(include_url=False)[:3]}") from exc
        return self.params

    async def run(self, context: ExecutionContext) -> NodeResult:
        """Public entry: validate params then execute. Never raises."""
        try:
            self.validate_parameters(context)
            return await self.execute(context)
        except (NodeParameterError, NodeExecutionError, Exception) as exc:  # noqa: BLE001
            raise NodeExecutionError(f"{type(exc).__name__}: {exc}") from exc

    async def execute(self, context: ExecutionContext) -> NodeResult:  # pragma: no cover
        raise NotImplementedError

    # ------------------------------------------------------------------
    @staticmethod
    def _inline_refs(node: Any, defs: dict) -> Any:
        """Recursively replace {"$ref": "#/$defs/X"} with the definition of X."""
        if isinstance(node, dict):
            if "$ref" in node and isinstance(node["$ref"], str):
                name = node["$ref"].split("/")[-1]
                if name in defs:
                    return BaseNode._inline_refs(defs[name], defs)
            return {k: BaseNode._inline_refs(v, defs) for k, v in node.items()}
        if isinstance(node, list):
            return [BaseNode._inline_refs(x, defs) for x in node]
        return node

    # ------------------------------------------------------------------
    @staticmethod
    def _single(payload: Any) -> NodeResult:
        return NodeResult(outputs={"main": payload}, raw_output=payload)
