"""Node registry — maps node type strings to executable classes."""

from __future__ import annotations

from .nodes.base import BaseNode, NodeDefinition
from .nodes.data import AggregateNode, FilterNode, MergeNode, SplitOutNode, SwitchNode
from .nodes.integrations import EmailSendNode, SlackMessageNode
from .nodes.logic import CodeNode, DelayNode, IfConditionNode, SetVariableNode
from .nodes.llm import LlmChatNode
from .nodes.http import HttpRequestNode
from .nodes.loop import BatchTriggerNode, LoopOverItemsNode
from .nodes.subflow import ExecuteWorkflowNode
from .nodes.triggers import ManualTriggerNode, ScheduleTriggerNode, WebhookTriggerNode
from .nodes.wait import WaitForResumeNode

_REGISTRY: dict[str, type[BaseNode]] = {}


def register(cls: type[BaseNode]) -> type[BaseNode]:
    _REGISTRY[cls.type] = cls
    return cls


for _cls in (
    ManualTriggerNode,
    WebhookTriggerNode,
    ScheduleTriggerNode,
    HttpRequestNode,
    ExecuteWorkflowNode,
    IfConditionNode,
    SwitchNode,
    FilterNode,
    MergeNode,
    SplitOutNode,
    AggregateNode,
    LoopOverItemsNode,
    SetVariableNode,
    CodeNode,
    DelayNode,
    LlmChatNode,
    EmailSendNode,
    WaitForResumeNode,
    SlackMessageNode,
    BatchTriggerNode,  # internal: injected into loop-body sub-runs
):
    register(_cls)


def get_node_class(node_type: str) -> type[BaseNode] | None:
    return _REGISTRY.get(node_type)


def all_definitions() -> list[dict]:
    """Full definitions payload for GET /api/v1/node-definitions."""
    defs: list[NodeDefinition] = [
        cls.get_definition() for cls in _REGISTRY.values() if not getattr(cls, "hidden", False)
    ]
    category_order = {"triggers": 0, "actions": 1, "logic": 2, "ai": 3}
    return [
        {
            "type": d.type,
            "name": d.name,
            "description": d.description,
            "category": d.category,
            "icon": d.icon,
            "color": d.color,
            "inputs": [{"key": h.key, "label": h.label} for h in d.inputs],
            "outputs": [{"key": h.key, "label": h.label} for h in d.outputs],
            "parameters_schema": d.parameters_schema,
            "defaults": d.defaults,
        }
        for d in sorted(defs, key=lambda d: (category_order.get(d.category, 9), d.name))
    ]
