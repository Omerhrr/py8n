"""Node registry - maps node type strings to executable classes."""

from __future__ import annotations

from .nodes.base import BaseNode, NodeDefinition
from .nodes.agent import AgentNode
from .nodes.data import (
    AggregateNode,
    CSVNode,
    CompareDatasetsNode,
    FilterNode,
    LimitNode,
    MergeNode,
    RemoveDuplicatesNode,
    SortNode,
    SplitOutNode,
    SummarizeNode,
    SwitchNode,
)
from .nodes.datasets import DatasetReadNode, DatasetWriteNode, SqlQueryNode  # v27
from .nodes.datascience import ChartNode, ModelTrainNode, PythonTransformNode  # v28
from .nodes.documents import DocumentExtractNode  # v32
from .nodes.integrations import EmailSendNode, SlackMessageNode
from .nodes.logic import CodeNode, DelayNode, IfConditionNode, SetVariableNode, StopAndErrorNode
from .nodes.llm import LlmChatNode
from .nodes.http import HttpRequestNode
from .nodes.loop import BatchTriggerNode, LoopOverItemsNode
from .nodes.sticky import StickyNoteNode
from .nodes.subflow import ExecuteWorkflowNode
from .nodes.triggers import ChatTriggerNode, ErrorTriggerNode, ManualTriggerNode, ScheduleTriggerNode, WebhookTriggerNode
from .nodes.wait import WaitForResumeNode
from .nodes.webhook_respond import RespondToWebhookNode

_REGISTRY: dict[str, type[BaseNode]] = {}


def register(cls: type[BaseNode]) -> type[BaseNode]:
    _REGISTRY[cls.type] = cls
    return cls


for _cls in (
    ManualTriggerNode,
    WebhookTriggerNode,
    ErrorTriggerNode,
    ScheduleTriggerNode,
    ChatTriggerNode,    # v25: conversational workflows - one run per chat message
    HttpRequestNode,
    ExecuteWorkflowNode,
    IfConditionNode,
    SwitchNode,
    FilterNode,
    SortNode,
    LimitNode,
    RemoveDuplicatesNode,
    MergeNode,
    SplitOutNode,
    AggregateNode,
    CompareDatasetsNode,  # v24: two-input reconciliation (matched / a_only / b_only)
    SummarizeNode,        # v24: group-by aggregation
    CSVNode,              # v24: CSV parse/serialize
    DatasetReadNode,      # v27: pull rows from a stored dataset
    DatasetWriteNode,     # v27: push items into a dataset (append/replace)
    SqlQueryNode,         # v27: DuckDB SQL across all datasets
    PythonTransformNode,  # v28: pandas/numpy code over the input items
    ChartNode,            # v28: matplotlib chart -> PNG artifact
    ModelTrainNode,       # v28: sklearn training -> metrics + model artifact
    DocumentExtractNode,  # v32: PDF/OCR/Word/Excel/CSV/JSON -> text + items
    LoopOverItemsNode,
    SetVariableNode,
    CodeNode,
    StopAndErrorNode,
    DelayNode,
    LlmChatNode,
    AgentNode,
    EmailSendNode,
    RespondToWebhookNode,
    WaitForResumeNode,
    SlackMessageNode,
    StickyNoteNode,    # v19: canvas annotation - hidden from definitions
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
