"""Data-flow nodes: Filter, Switch, Merge, Split Out, Aggregate.

Convention: nodes that work over lists look for an ``items`` array in the
incoming payload; if absent, the payload itself is treated as a single item.
This mirrors n8n's item model while staying JSON-friendly.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ..context import ExecutionContext
from .base import BaseNode, Handle, NodeExecutionError, NodeResult
from .logic import IfConditionNode  # reuse the operator set


def _working_data(input_data: Any) -> Any:
    """Canonical working data: unwrap the manual trigger's ``payload`` envelope
    so field paths / item lists are written against the user's own data."""
    if isinstance(input_data, dict) and "payload" in input_data and len(input_data) <= 3:
        return input_data["payload"]
    return input_data


def _items(input_data: Any) -> list[Any]:
    """Canonical item list extraction."""
    data = _working_data(input_data)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return items
        return [data]
    if data is None:
        return []
    return [data]


def _pluck(data: Any, path: str) -> Any:
    """Dot-path lookup: 'user.name' → data['user']['name']; '' → data itself."""
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            cur = cur[int(part)]
        else:
            return None
    return cur


class FilterNode(BaseNode):
    """Keeps only the items matching a condition (same operators as IF)."""

    type = "filter"
    name = "Filter"
    description = "Keeps only the items that match a condition (operates on the items array)."
    category = "logic"
    icon = "filter"
    color = "#f59e0b"

    class ParamsModel(BaseModel):
        field: str = Field(default="", description="Dot-path to test on each item, e.g. score or user.name (empty = whole item)")
        operator: str = Field(
            default="not_empty",
            json_schema_extra={
                "widget": "select",
                "options": [
                    "equals", "not_equals", "contains", "not_contains",
                    "greater_than", "less_than", "is_empty", "is_true", "regex", "not_empty",
                ],
            },
        )
        right_value: Any = Field(default="", description="Right operand for the comparison")
        as_json: bool = Field(default=False, description="Compare after JSON-encoding the left value (useful for dicts/lists)")

    def _left(self, item: Any, p: "FilterNode.ParamsModel") -> Any:
        left = _pluck(item, p.field)
        if p.as_json and not isinstance(left, (str, int, float, bool)):
            left = json.dumps(left, default=str, ensure_ascii=False)
        return left

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: FilterNode.ParamsModel
        probe = IfConditionNode.__new__(IfConditionNode)  # operator-only reuse
        working = _working_data(context.current_input)
        kept, dropped = [], 0
        for item in _items(working):
            left = self._left(item, p)
            try:
                ok = probe._compare(left, p.operator, p.right_value)
            except NodeExecutionError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise NodeExecutionError(f"Filter comparison failed: {exc}") from exc
            if ok:
                kept.append(item)
            else:
                dropped += 1
        return self._single({"items": kept, "matched": len(kept), "dropped": dropped})


class SwitchNode(BaseNode):
    """Routes each payload to the first matching rule output (or fallback)."""

    type = "switch"
    name = "Switch"
    description = "Multi-branch router: sends the flow down the first rule whose value matches."
    category = "logic"
    icon = "split"
    color = "#fb7185"
    outputs: ClassVar[list[Handle]] = [
        Handle("0", "Rule 1"),
        Handle("1", "Rule 2"),
        Handle("2", "Rule 3"),
        Handle("fallback", "Fallback"),
    ]

    class ParamsModel(BaseModel):
        field: str = Field(default="", description="Dot-path to switch on (empty = whole payload)")
        rules: list[str] = Field(
            default_factory=lambda: ["", "", ""],
            description="Exact string match per rule output — JSON-encodes non-strings before comparing",
            json_schema_extra={"widget": "code", "rows": 5, "language": "json", "hint": '["urgent", "normal", "low"]'},
        )
        use_fallback: bool = Field(default=True, description="Route non-matching payloads down the fallback handle")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: SwitchNode.ParamsModel
        value = _pluck(_working_data(context.current_input), p.field)
        value_cmp = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
        rules = list(p.rules or [])[:3] + [""] * max(0, 3 - len(p.rules or []))
        outputs: dict[str, Any] = {"0": None, "1": None, "2": None, "fallback": None}
        for idx, expected in enumerate(rules):
            if expected and value_cmp == expected:
                outputs[str(idx)] = context.current_input
                return NodeResult(outputs=outputs, raw_output={"matched_rule": idx})
        if p.use_fallback:
            outputs["fallback"] = context.current_input
            return NodeResult(outputs=outputs, raw_output={"matched_rule": "fallback"})
        return NodeResult(outputs=outputs, raw_output={"matched_rule": None})


class MergeNode(BaseNode):
    """Combines payloads from converging branches (dict-merge or list-append)."""

    type = "merge"
    name = "Merge"
    description = "Combines payloads from two or more branches into one."
    category = "logic"
    icon = "git-merge"
    color = "#2dd4bf"

    class ParamsModel(BaseModel):
        mode: str = Field(
            default="combine",
            json_schema_extra={
                "widget": "select",
                "options": ["combine", "append", "keep_first"],
                "hint": "combine=merge dicts · append=concat item lists · keep_first=first active payload only",
            },
        )

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: MergeNode.ParamsModel
        payloads = list(context.current_inputs.values())

        if p.mode == "keep_first" or len(payloads) == 1:
            return self._single(payloads[0] if payloads else None)

        if p.mode == "append":
            merged: list[Any] = []
            for pl in payloads:
                merged.extend(_items(pl))
            return self._single({"items": merged, "count": len(merged)})

        # combine: shallow dict merge in edge arrival order
        combined: dict[str, Any] = {}
        for pl in payloads:
            if isinstance(pl, dict):
                combined.update(pl)
            elif pl is not None:
                combined.setdefault("_non_dict", []).append(pl)
        return self._single(combined)


class SplitOutNode(BaseNode):
    """Splits an array field into a canonical items list."""

    type = "split_out"
    name = "Split Out"
    description = "Splits an array field of the payload into individual items for list processing."
    category = "logic"
    icon = "ungroup"
    color = "#fbbf24"

    class ParamsModel(BaseModel):
        field: str = Field(default="items", description="Dot-path to the array to split (e.g. data.results)")
        include_meta: bool = Field(default=True, description="Include count + original context in the output")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: SplitOutNode.ParamsModel
        working = _working_data(context.current_input)
        value = _pluck(working, p.field)
        if value is None:
            return self._single({"items": [], "count": 0})
        if not isinstance(value, list):
            value = [value]
        out: dict[str, Any] = {"items": value, "count": len(value)}
        if p.include_meta and isinstance(working, dict):
            meta = {k: v for k, v in working.items() if k != p.field.split(".")[0]}
            if meta:
                out["context"] = meta
        return self._single(out)


class AggregateNode(BaseNode):
    """Reduces an items list into a single value (sum, count, average, ...)."""

    type = "aggregate"
    name = "Aggregate"
    description = "Reduces a list of items to a single value: count, sum, average, min, max, join."
    category = "logic"
    icon = "sigma"
    color = "#34d399"

    class ParamsModel(BaseModel):
        mode: str = Field(
            default="count",
            json_schema_extra={
                "widget": "select",
                "options": ["count", "sum", "average", "min", "max", "join"],
            },
        )
        field: str = Field(default="", description="Dot-path per item (required for sum/average/min/max/join)")
        separator: str = Field(default=", ", description="Separator for join mode")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: AggregateNode.ParamsModel
        items = _items(context.current_input)

        if p.mode == "count":
            return self._single({"value": len(items), "mode": "count"})

        values: list[float | str] = []
        for item in items:
            v = _pluck(item, p.field) if p.field else item
            if v is not None:
                values.append(v)

        if p.mode == "join":
            text = p.separator.join(str(v) for v in values)
            return self._single({"value": text, "count": len(values)})

        nums = []
        for v in values:
            try:
                nums.append(float(v))
            except (TypeError, ValueError):
                continue
        if not nums:
            raise NodeExecutionError(f"Aggregate {p.mode!r}: no numeric values at field {p.field!r}")

        result = {
            "sum": sum(nums),
            "average": sum(nums) / len(nums),
            "min": min(nums),
            "max": max(nums),
        }[p.mode]
        result = int(result) if float(result).is_integer() else round(result, 4)
        return self._single({"value": result, "count": len(nums), "mode": p.mode})
