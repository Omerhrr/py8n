"""Data-flow nodes: Filter, Switch, Merge, Split Out, Aggregate, Compare,
Summarize, CSV - plus the v45 deep data-engineering set: Join, Pivot,
Unpivot, Cast Columns, Handle Nulls, Data Quality and Analyze.

Convention: nodes that work over lists look for an ``items`` array in the
incoming payload; if absent, the payload itself is treated as a single item.
This mirrors n8n's item model while staying JSON-friendly.

The v45 nodes use pandas internally but speak the SAME item model at their
edges: dicts in, dicts out (NaN → null via services.datasets.jsonable_rows),
so they compose with every existing node without a wire-format break.
"""

from __future__ import annotations

import csv
import io
import json
import re as _re
from typing import Any, ClassVar

import pandas as pd
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
            description="Exact string match per rule output - JSON-encodes non-strings before comparing",
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


def _sort_key(value: Any) -> tuple:
    """Total-order key tolerant of mixed types: numbers < strings, None last."""
    if value is None:
        return (2, 0, "")
    if isinstance(value, bool):
        return (1, 0, str(value).lower())
    if isinstance(value, (int, float)):
        return (0, value, "")
    return (1, 0, str(value))


class SortNode(BaseNode):
    """Sorts the items array by a field (dot-path), ascending or descending."""

    type = "sort"
    name = "Sort"
    description = "Sorts the items array by a field (dot-path, empty = whole item) in ascending or descending order."
    category = "logic"
    icon = "arrow-down-up"
    color = "#38bdf8"

    class ParamsModel(BaseModel):
        field: str = Field(default="", description="Dot-path to sort by, e.g. price or user.name (empty = whole item)")
        direction: str = Field(
            default="asc",
            description="Sort direction",
            json_schema_extra={"widget": "select", "options": ["asc", "desc"]},
        )

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: SortNode.ParamsModel
        items = _items(context.current_input)
        missing = [it for it in items if _pluck(it, p.field) is None]
        ordered = sorted(items, key=lambda it: _sort_key(_pluck(it, p.field)), reverse=p.direction == "desc")
        return self._single({"items": ordered, "count": len(ordered), "missing_field": len(missing)})


class LimitNode(BaseNode):
    """Keeps only the first or last N items of the array."""

    type = "limit"
    name = "Limit"
    description = "Keeps only the first or last N items of the items array."
    category = "logic"
    icon = "list-end"
    color = "#60a5fa"

    class ParamsModel(BaseModel):
        max_items: int = Field(default=10, ge=0, description="How many items to keep (0 = keep none)")
        keep: str = Field(
            default="first",
            description="Which end of the array to keep",
            json_schema_extra={"widget": "select", "options": ["first", "last"]},
        )

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: LimitNode.ParamsModel
        items = _items(context.current_input)
        kept = items[: p.max_items] if p.keep == "first" else items[len(items) - p.max_items:] if p.max_items else []
        return self._single({"items": kept, "kept": len(kept), "total": len(items)})


class RemoveDuplicatesNode(BaseNode):
    """Drops repeated items, comparing by a field (dot-path) or the whole item."""

    type = "remove_duplicates"
    name = "Remove Duplicates"
    description = "Removes repeated items from the array - compares by a field (dot-path, empty = whole item). Keeps the first occurrence."
    category = "logic"
    icon = "eraser"
    color = "#a78bfa"

    class ParamsModel(BaseModel):
        field: str = Field(default="", description="Dot-path used for comparison, e.g. email (empty = compare whole items)")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: RemoveDuplicatesNode.ParamsModel
        items = _items(context.current_input)
        seen: set[str] = set()
        unique: list[Any] = []
        for item in items:
            key = json.dumps(_pluck(item, p.field), sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return self._single({"items": unique, "unique": len(unique), "duplicates_removed": len(items) - len(unique)})


class CompareDatasetsNode(BaseNode):
    """v24: reconciles two item lists (Input A vs Input B) by a key field.

    Each input arrives on its own targetHandle ("main" = Input A,
    "secondary" = Input B). Every A item is paired with the FIRST B item
    sharing its key; results are routed to three output handles -
    ``matched`` ({a, b} pairs), ``a_only`` and ``b_only``. B items whose key
    was already paired once (duplicates) are counted, never silently lost.
    """

    type = "compare_datasets"
    name = "Compare Datasets"
    description = (
        "Compares two item lists by a key field: Input A vs Input B. Routes matched pairs "
        "(as {a, b}) to Matched, and orphans to A-only / B-only."
    )
    category = "logic"
    icon = "git-compare"
    color = "#e879f9"
    inputs: ClassVar[list[Handle]] = [Handle("main", "Input A"), Handle("secondary", "Input B")]
    outputs: ClassVar[list[Handle]] = [
        Handle("matched", "Matched"),
        Handle("a_only", "A only"),
        Handle("b_only", "B only"),
    ]

    class ParamsModel(BaseModel):
        field_a: str = Field(default="id", description="Dot-path of the match key on Input A items, e.g. id or user.email")
        field_b: str = Field(default="id", description="Dot-path of the match key on Input B items (may differ from field_a)")

    @staticmethod
    def _key(item: Any, path: str) -> str:
        return json.dumps(_pluck(item, path), sort_keys=True, default=str)

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: CompareDatasetsNode.ParamsModel
        handles = context.current_input_handles or {}
        a_payload = handles.get("main")
        b_payload = handles.get("secondary")
        if "secondary" not in handles:
            # No edge on the secondary handle: either only Input A is wired,
            # or both edges landed on one handle - arrival (edge) order then
            # decides: first active payload = A, second = B.
            vals = list(context.current_inputs.values())
            a_payload = vals[0] if vals else None
            b_payload = vals[1] if len(vals) > 1 else None
        if a_payload is None and b_payload is None:
            raise NodeExecutionError("Compare Datasets needs at least one connected input")

        a_items = _items(a_payload)
        b_items = _items(b_payload)

        # First B occurrence wins per key; duplicates are counted, not lost.
        b_index: dict[str, Any] = {}
        b_duplicate_keys = 0
        for item in b_items:
            key = self._key(item, p.field_b)
            if key in b_index:
                b_duplicate_keys += 1
                continue
            b_index[key] = item

        matched: list[dict[str, Any]] = []
        a_only: list[Any] = []
        a_keys: set[str] = set()
        for item in a_items:
            key = self._key(item, p.field_a)
            a_keys.add(key)
            if key in b_index:
                matched.append({"a": item, "b": b_index[key]})
            else:
                a_only.append(item)
        b_only = [item for item in b_items if self._key(item, p.field_b) not in a_keys]

        # Empty buckets emit None so their outgoing edges deactivate and
        # downstream nodes are skipped - action branches fire only when
        # there is something to act on (matches IF-branch semantics).
        outputs = {
            "matched": matched or None,
            "a_only": a_only or None,
            "b_only": b_only or None,
        }
        raw = {
            "matched": len(matched),
            "a_only": len(a_only),
            "b_only": len(b_only),
            "b_duplicates_skipped": b_duplicate_keys,
        }
        return NodeResult(outputs=outputs, raw_output=raw)


class SummarizeNode(BaseNode):
    """v24 group-by aggregation, v45-deepened: one output item per group.

    ``group_by`` lists dot-path fields; every distinct combination forms a
    group. ``aggregates`` compute an op over a field per group - v45 ops:
    count, count_distinct, sum, avg, median, std, min, max, first, last,
    concat. With no group_by, all items form one global group. Optional
    ``having`` post-filters groups (e.g. drop groups with total < 100),
    ``sort_by``/``sort_dir`` orders the output and ``limit`` caps it.
    """

    type = "summarize"
    name = "Summarize"
    description = (
        "Groups the items array by field(s) and computes per-group aggregations "
        "(count, count_distinct, sum, avg, median, std, min, max, first, last, concat) "
        "with optional having / sort / limit."
    )
    category = "logic"
    icon = "table-properties"
    color = "#4ade80"

    AGG_OPS = ("count", "count_distinct", "sum", "avg", "median", "std", "min", "max", "first", "last", "concat")

    class ParamsModel(BaseModel):
        group_by: list[str] = Field(
            default_factory=list,
            description="Dot-path fields to group by (JSON array, empty = one global group)",
            json_schema_extra={"widget": "code", "rows": 3, "language": "json", "hint": '["region"]'},
        )
        aggregates: list[dict] = Field(
            default_factory=list,
            description='Aggregations per group, e.g. [{"field": "amount", "op": "sum"}] - '
            "op: count|count_distinct|sum|avg|median|std|min|max|first|last|concat (field optional for count)",
            json_schema_extra={"widget": "code", "rows": 5, "language": "json", "hint": '[{"field": "amount", "op": "sum"}]'},
        )
        having: list[dict] = Field(
            default_factory=list,
            description='Post-filter on aggregate labels, e.g. [{"label": "amount_sum", "op": ">=", "value": 100}] - '
            "op: >|>=|<|<=|==|!=",
            json_schema_extra={"widget": "code", "rows": 3, "language": "json",
                               "hint": '[{"label": "amount_sum", "op": ">=", "value": 100}]'},
        )
        sort_by: str = Field(default="", description="Output label to sort by (e.g. amount_sum or a group field); empty = bucket order")
        sort_dir: str = Field(
            default="asc",
            json_schema_extra={"widget": "select", "options": ["asc", "desc"]},
        )
        limit: int = Field(default=0, ge=0, description="Keep only the first N groups after sorting (0 = all)")

    @staticmethod
    def _numeric(values: list[Any]) -> list[float]:
        nums = []
        for v in values:
            try:
                if isinstance(v, bool):
                    continue
                nums.append(float(v))
            except (TypeError, ValueError):
                continue
        return nums

    def _aggregate(self, op: str, field: str, group_items: list[Any]) -> Any:
        if op == "count":
            return len(group_items)
        if op == "count_distinct":
            return len({json.dumps(_pluck(it, field), sort_keys=True, default=str) for it in group_items})
        values = [_pluck(it, field) for it in group_items]
        values = [v for v in values if v is not None]
        if op in ("min", "max") and values:
            nums = self._numeric(values)
            if nums:
                return min(nums) if op == "min" else max(nums)
            # string domain (e.g. ISO dates): total-order min/max
            ordered = sorted(values, key=_sort_key)
            return ordered[0] if op == "min" else ordered[-1]
        if op == "first":
            return values[0] if values else None
        if op == "last":
            return values[-1] if values else None
        if op == "concat":
            return ", ".join(str(v) for v in values[:100])
        nums = self._numeric(values)
        if op == "sum":
            return sum(nums) if nums else None
        if op == "avg":
            return round(sum(nums) / len(nums), 4) if nums else None
        if op == "median":
            return round(float(pd.Series(nums).median()), 4) if nums else None
        if op == "std":
            return round(float(pd.Series(nums).std()), 4) if len(nums) > 1 else None
        if op == "min":
            return min(nums) if nums else None
        if op == "max":
            return max(nums) if nums else None
        raise NodeExecutionError(
            f"Summarize: unknown aggregate op {op!r} (use {'|'.join(self.AGG_OPS)})"
        )

    @staticmethod
    def _having_pass(value: Any, op: str, right: Any) -> bool:
        try:
            if op == ">=":
                return value >= right
            if op == "<=":
                return value <= right
            if op == ">":
                return value > right
            if op == "<":
                return value < right
            if op == "==":
                return value == right
            if op == "!=":
                return value != right
        except TypeError:
            return False
        return False

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: SummarizeNode.ParamsModel
        items = _items(context.current_input)
        group_by = [str(g) for g in (p.group_by or [])]

        buckets: dict[tuple, list[Any]] = {}
        for item in items:
            key = tuple(json.dumps(_pluck(item, g), sort_keys=True, default=str) for g in group_by)
            buckets.setdefault(key, []).append(item)

        out_items: list[dict[str, Any]] = []
        for key, group_items in buckets.items():
            out: dict[str, Any] = {}
            if group_by:
                # group_items[0] is the item that created the bucket, so its
                # raw values are exactly what the JSON key was derived from.
                for g in group_by:
                    out[g] = _pluck(group_items[0], g)
            for agg in p.aggregates or []:
                op = str(agg.get("op", "count"))
                field = str(agg.get("field", "") or "")
                label = f"{field}_{op}" if field else op
                out[label] = self._aggregate(op, field, group_items)
            out["_count"] = len(group_items)
            out_items.append(out)

        # v45: having post-filter (group fails when ANY clause fails)
        for clause in p.having or []:
            label = str(clause.get("label", ""))
            if not label:
                raise NodeExecutionError("Summarize having clauses need a 'label' (e.g. amount_sum)")
            op = str(clause.get("op", ">="))
            right = clause.get("value")
            out_items = [row for row in out_items if self._having_pass(row.get(label), op, right)]

        if p.sort_by:
            if p.sort_by not in (out_items[0] if out_items else {}):
                raise NodeExecutionError(
                    f"Summarize sort_by {p.sort_by!r} is not an output label - available: "
                    f"{list(out_items[0].keys()) if out_items else []}"
                )
            out_items.sort(key=lambda row: _sort_key(row.get(p.sort_by)), reverse=p.sort_dir == "desc")
        if p.limit and p.limit > 0:
            out_items = out_items[: p.limit]

        return self._single({"items": out_items, "groups": len(out_items), "total_items": len(items)})


class CSVNode(BaseNode):
    """v24: CSV ⇄ items conversion (parse or serialize).

    Parse turns CSV text (from an expression, e.g. an HTTP response body)
    into an items array; serialize flattens the incoming items array into
    RFC-4180 CSV text. Deliberately dependency-free (stdlib csv).
    """

    type = "csv"
    name = "CSV"
    description = "Parses CSV text into items, or serializes the incoming items array into CSV text (spreadsheet interop)."
    category = "logic"
    icon = "file-spreadsheet"
    color = "#fb923c"

    class ParamsModel(BaseModel):
        mode: str = Field(
            default="parse",
            description="parse = CSV text → items · serialize = items → CSV text",
            json_schema_extra={"widget": "select", "options": ["parse", "serialize"]},
        )
        content: str = Field(
            default="",
            description="CSV text to parse (parse mode) - supports {{ expressions }}",
            json_schema_extra={"widget": "textarea", "rows": 5, "hint": "name,amount\nAlice,120\nBob,90"},
        )
        delimiter: str = Field(default=",", description="Single delimiter character (e.g. , ; \\t)")
        has_header: bool = Field(default=True, description="First row is the header (parse mode)")
        auto_convert: bool = Field(default=False, description="Convert numeric/boolean cells to real numbers/booleans when parsing")

    @staticmethod
    def _convert(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        low = text.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if text:
            try:
                return int(text)
            except ValueError:
                pass
            try:
                return float(text)
            except ValueError:
                pass
        return value

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: CSVNode.ParamsModel
        delim = (p.delimiter or ",")[:1] or ","
        try:
            if p.mode == "parse":
                rows = [r for r in csv.reader(io.StringIO(p.content or ""), delimiter=delim) if r]
                if not rows:
                    return self._single({"items": [], "count": 0, "columns": []})
                if p.has_header:
                    header = [h.strip() for h in rows[0]]
                    items = []
                    for row in rows[1:]:
                        item = {header[i]: (row[i] if i < len(row) else "") for i in range(len(header))}
                        items.append({k: self._convert(v) for k, v in item.items()} if p.auto_convert else item)
                    return self._single({"items": items, "count": len(items), "columns": header})
                items = [{str(i): self._convert(v) if p.auto_convert else v for i, v in enumerate(row)} for row in rows]
                return self._single({"items": items, "count": len(items), "columns": None})

            # serialize
            items = _items(context.current_input)
            rows: list[dict[str, Any]] = []
            for it in items:
                rows.append(it if isinstance(it, dict) else {"value": it})
            columns: list[str] = []
            for row in rows:
                for k in row.keys():
                    if k not in columns:
                        columns.append(k)

            def cell(v: Any) -> Any:
                if v is None:
                    return ""
                if isinstance(v, (dict, list)):
                    return json.dumps(v, ensure_ascii=False, default=str)
                if isinstance(v, bool):
                    return "true" if v else "false"
                return v

            buf = io.StringIO()
            writer = csv.writer(buf, delimiter=delim)
            writer.writerow(columns)
            for row in rows:
                writer.writerow([cell(row.get(c)) for c in columns])
            return self._single({"csv": buf.getvalue(), "rows": len(rows), "columns": columns})
        except csv.Error as exc:
            raise NodeExecutionError(f"CSV failed: {exc}") from exc


# ================================================================ v45 helpers
def _frame_from_items(items: list[Any], label: str) -> pd.DataFrame:
    """Items → DataFrame for the v45 transform nodes (dicts only kept)."""
    rows = [r for r in items if isinstance(r, dict)]
    if len(rows) < len(items):
        raise NodeExecutionError(
            f"{label} needs object items - {len(items) - len(rows)} non-object item(s) "
            "(shape upstream, e.g. with Set Variable or Code)"
        )
    return pd.DataFrame(rows)


def _rows_out(df: pd.DataFrame) -> list[dict]:
    """DataFrame → JSON-native rows (NaN → null, datetimes → ISO)."""
    from ...services.datasets import jsonable_rows

    return jsonable_rows(df)


def _numeric_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


class JoinNode(BaseNode):
    """v45: no-code SQL-style join of two item lists.

    Input A (handle ``main``) joins Input B (``secondary``) on
    ``left_field`` / ``right_field`` (dot-paths). how: inner | left | right |
    outer | anti (A rows with no B match). Colliding column names keep the A
    value; B columns get ``suffix_right``. Backed by pandas.merge - full
    many-to-many fidelity, unlike Compare Datasets' first-match pairing.
    """

    type = "join"
    name = "Join"
    description = (
        "Joins two item lists (Input A + Input B) on key fields - inner / left / right / "
        "outer / anti, many-to-many safe. Colliding B columns get a suffix."
    )
    category = "logic"
    icon = "git-merge"
    color = "#22d3ee"
    inputs: ClassVar[list[Handle]] = [Handle("main", "Input A"), Handle("secondary", "Input B")]
    outputs: ClassVar[list[Handle]] = [Handle("main", "Joined items")]

    class ParamsModel(BaseModel):
        left_field: str = Field(default="id", description="Key dot-path on Input A items")
        right_field: str = Field(default="id", description="Key dot-path on Input B items")
        how: str = Field(
            default="inner",
            description="Join type",
            json_schema_extra={"widget": "select", "options": ["inner", "left", "right", "outer", "anti"]},
        )
        suffix_right: str = Field(default="_right", description="Suffix for colliding B columns")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: JoinNode.ParamsModel
        handles = context.current_input_handles or {}
        a_payload = handles.get("main")
        b_payload = handles.get("secondary")
        if "secondary" not in handles:
            vals = list(context.current_inputs.values())
            a_payload = vals[0] if vals else None
            b_payload = vals[1] if len(vals) > 1 else None
        if a_payload is None or b_payload is None:
            raise NodeExecutionError("Join needs both inputs connected (Input A and Input B)")

        if not p.left_field or not p.right_field:
            raise NodeExecutionError("Join needs left_field and right_field key paths")
        a_items, b_items = _items(a_payload), _items(b_payload)
        df_a = _frame_from_items(a_items, "Join Input A")  # also validates dict-ness
        df_b = _frame_from_items(b_items, "Join Input B")
        how = p.how

        # Empty-side short-circuits (pandas merges on empty frames lack the
        # key column and would crash - these semantics are the correct ones).
        if df_a.empty or df_b.empty:
            if how == "anti":
                return self._single({"items": _rows_out(df_a) if len(df_a) else [], "rows_out": len(df_a), "how": "anti", "matched": 0, "left_only": len(df_a), "right_only": 0})
            if how == "inner":
                return self._single({"items": [], "rows_out": 0, "how": "inner", "matched": 0, "left_only": 0, "right_only": 0})
            if how == "left":
                return self._single({"items": _rows_out(df_a) if len(df_a) else [], "rows_out": len(df_a), "how": "left", "matched": 0, "left_only": len(df_a), "right_only": 0})
            if how == "right":
                return self._single({"items": _rows_out(df_b) if len(df_b) else [], "rows_out": len(df_b), "how": "right", "matched": 0, "left_only": 0, "right_only": len(df_b)})
            combined = pd.concat([df_a, df_b], ignore_index=True)
            rows = _rows_out(combined) if len(combined) else []
            return self._single({"items": rows, "rows_out": len(rows), "how": "outer", "matched": 0, "left_only": len(df_a), "right_only": len(df_b)})

        # materialize dot-path keys as concrete columns for pandas
        key_a, key_b = f"__key_a_{uuid4hex()}", f"__key_b_{uuid4hex()}"
        df_a = pd.DataFrame([{**{k: v for k, v in it.items()}, key_a: json.dumps(_pluck(it, p.left_field), sort_keys=True, default=str)} for it in a_items])
        df_b = pd.DataFrame([{**{k: v for k, v in it.items()}, key_b: json.dumps(_pluck(it, p.right_field), sort_keys=True, default=str)} for it in b_items])
        df_a = df_a.loc[:, ~df_a.columns.duplicated()]
        df_b = df_b.loc[:, ~df_b.columns.duplicated()]

        n_left_only = n_right_only = 0
        if how == "anti":
            b_keys = set(df_b[key_b])
            merged = df_a[~df_a[key_a].isin(b_keys)].drop(columns=[key_a])
            n_left_only = int(len(merged))
            items = _rows_out(merged) if len(merged) else []
            return self._single({"items": items, "rows_out": len(items), "how": "anti", "matched": 0, "left_only": n_left_only, "right_only": 0})

        merged = df_a.merge(
            df_b, how=how, left_on=key_a, right_on=key_b,
            suffixes=("", p.suffix_right or "_right"), indicator=True,
        )
        matched = int((merged["_merge"] == "both").sum()) if len(merged) else 0
        left_only = int((merged["_merge"] == "left_only").sum()) if len(merged) else 0
        right_only = int((merged["_merge"] == "right_only").sum()) if len(merged) else 0
        drop_cols = [c for c in (key_a, key_b, "_merge") if c in merged.columns]
        merged = merged.drop(columns=drop_cols)
        items = _rows_out(merged) if len(merged) else []
        return self._single({
            "items": items,
            "rows_out": len(items),
            "how": p.how,
            "matched": matched,
            "left_only": left_only,
            "right_only": right_only,
        })


def uuid4hex() -> str:
    import uuid as _uuid

    return _uuid.uuid4().hex[:8]


class PivotNode(BaseNode):
    """v45: rows → matrix (spreadsheet pivot) over the incoming items."""

    type = "pivot"
    name = "Pivot"
    description = "Pivots the items array: rows grouped by index field(s), one column per value of pivot_on, aggregated (sum/mean/count/min/max/median/first/last)."
    category = "logic"
    icon = "grid-3x3"
    color = "#34d399"

    class ParamsModel(BaseModel):
        index: list[str] = Field(
            default_factory=list,
            description="Row-grouping columns (JSON array, e.g. [\"region\"])",
            json_schema_extra={"widget": "code", "rows": 3, "language": "json", "hint": '["region"]'},
        )
        pivot_on: str = Field(default="", description="Column whose distinct values become the new columns")
        value: str = Field(default="", description="Column to aggregate (optional for count)")
        agg: str = Field(
            default="sum",
            description="Aggregation applied to value",
            json_schema_extra={"widget": "select", "options": ["sum", "mean", "count", "min", "max", "median", "first", "last"]},
        )

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: PivotNode.ParamsModel
        df = _frame_from_items(_items(context.current_input), "Pivot")
        if df.empty:
            return self._single({"items": [], "rows_out": 0, "columns": []})
        index = [str(i) for i in (p.index or [])]
        missing = [c for c in [*index, p.pivot_on] if c and c not in df.columns]
        if missing:
            raise NodeExecutionError(f"Pivot: column(s) {missing} not found - available: {[str(c) for c in df.columns]}")
        if not p.pivot_on:
            raise NodeExecutionError("Pivot needs pivot_on (the column that spreads into new columns)")
        if p.agg != "count":
            if not p.value:
                raise NodeExecutionError(f"Pivot agg={p.agg} requires a value column")
            if p.value not in df.columns:
                raise NodeExecutionError(f"Pivot: value column {p.value!r} not found")
        agg_map = {"mean": "mean", "sum": "sum", "count": "count", "min": "min", "max": "max", "median": "median", "first": "first", "last": "last"}
        table = pd.pivot_table(
            df,
            index=index or None,
            columns=p.pivot_on,
            values=p.value or None,
            aggfunc=agg_map[p.agg],
            fill_value=0 if p.agg == "count" else None,
        ).reset_index()
        table.columns = [str(c) for c in table.columns]
        items = _rows_out(table)
        return self._single({"items": items, "rows_out": len(items), "columns": [str(c) for c in table.columns]})


class UnpivotNode(BaseNode):
    """v45: matrix → tidy rows (reverse pivot / melt)."""

    type = "unpivot"
    name = "Unpivot"
    description = "Melts the items array from wide to tidy: chosen columns become (variable, value) rows keyed by the index column(s)."
    category = "logic"
    icon = "ungroup"
    color = "#2dd4bf"

    class ParamsModel(BaseModel):
        index: list[str] = Field(
            default_factory=list,
            description="Columns to keep as identifiers (JSON array; empty = none)",
            json_schema_extra={"widget": "code", "rows": 3, "language": "json", "hint": '["region"]'},
        )
        columns: list[str] = Field(
            default_factory=list,
            description="Columns to melt (JSON array; empty = everything not in index)",
            json_schema_extra={"widget": "code", "rows": 3, "language": "json", "hint": '["q1", "q2"]'},
        )
        var_name: str = Field(default="variable", description="Name of the new column holding melted column names")
        value_name: str = Field(default="value", description="Name of the new column holding values")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: UnpivotNode.ParamsModel
        df = _frame_from_items(_items(context.current_input), "Unpivot")
        if df.empty:
            return self._single({"items": [], "rows_out": 0})
        index = [str(i) for i in (p.index or [])]
        missing = [c for c in index if c not in df.columns]
        if missing:
            raise NodeExecutionError(f"Unpivot: index column(s) {missing} not found - available: {[str(c) for c in df.columns]}")
        cols = [str(c) for c in (p.columns or [])]
        if cols:
            absent = [c for c in cols if c not in df.columns]
            if absent:
                raise NodeExecutionError(f"Unpivot: column(s) {absent} not found - available: {[str(c) for c in df.columns]}")
        melted = pd.melt(
            df,
            id_vars=index or None,
            value_vars=cols or None,
            var_name=p.var_name or "variable",
            value_name=p.value_name or "value",
        )
        items = _rows_out(melted)
        return self._single({"items": items, "rows_out": len(items)})


class CastColumnsNode(BaseNode):
    """v45: per-column type casting (integer / number / boolean / text / datetime)."""

    type = "cast_columns"
    name = "Cast Columns"
    description = "Casts columns to declared types (integer|number|boolean|text|datetime) - coerce or fail on bad values, optional datetime format."
    category = "logic"
    icon = "replace"
    color = "#818cf8"

    class ParamsModel(BaseModel):
        casts: list[dict] = Field(
            default_factory=list,
            description='Cast specs, e.g. [{"column": "age", "dtype": "integer"}, {"column": "ts", "dtype": "datetime", "format": "%Y-%m-%d"}]',
            json_schema_extra={"widget": "code", "rows": 5, "language": "json",
                               "hint": '[{"column": "age", "dtype": "integer"}]'},
        )
        on_error: str = Field(
            default="coerce",
            description="coerce = bad values become null · raise = fail the node",
            json_schema_extra={"widget": "select", "options": ["coerce", "raise"]},
        )

    def _cast(self, s: pd.Series, dtype: str, fmt: str | None, col: str) -> pd.Series:
        errors = "raise" if self._on_error == "raise" else "coerce"
        if dtype == "integer":
            nums = pd.to_numeric(s, errors=errors)
            return nums.round().astype("Int64") if errors == "coerce" else nums.astype("int64")
        if dtype == "number":
            return pd.to_numeric(s, errors=errors).astype("float64")
        if dtype == "boolean":
            low = s.astype(str).str.strip().str.lower()
            mapped = low.map({"true": True, "false": False, "1": True, "0": False, "yes": True, "no": False})
            if errors == "raise" and mapped.isna().any() and s.notna().any():
                raise NodeExecutionError(f"Cast: column {col!r} has values that are not boolean-like")
            return mapped.astype("boolean")
        if dtype == "text":
            return s.map(lambda v: None if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)).astype("object")
        if dtype == "datetime":
            return pd.to_datetime(s, format=fmt or None, errors=errors)
        raise NodeExecutionError(f"Cast: unknown dtype {dtype!r} (use integer|number|boolean|text|datetime)")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        self._on_error = self.params.on_error  # type: CastColumnsNode.ParamsModel
        p = self.params
        if not p.casts:
            raise NodeExecutionError("Cast Columns needs at least one cast spec")
        df = _frame_from_items(_items(context.current_input), "Cast Columns")
        applied: list[str] = []
        for spec in p.casts or []:
            col = str(spec.get("column", ""))
            dtype = str(spec.get("dtype", "text"))
            fmt = spec.get("format")
            if not col:
                raise NodeExecutionError("Cast specs need a 'column'")
            if col not in df.columns:
                if self._on_error == "raise":
                    raise NodeExecutionError(f"Cast: column {col!r} not found - available: {[str(c) for c in df.columns]}")
                continue
            try:
                df[col] = self._cast(df[col], dtype, fmt if isinstance(fmt, str) else None, col)
            except NodeExecutionError:
                raise
            except (ValueError, TypeError) as exc:
                raise NodeExecutionError(f"Cast failed for column {col!r} → {dtype}: {exc}") from exc
            applied.append(col)
        return self._single({"items": _rows_out(df), "rows_out": len(df), "cast": applied})


class HandleNullsNode(BaseNode):
    """v45: drop rows with nulls, or fill them (zero / empty / mean / median / mode / value)."""

    type = "handle_nulls"
    name = "Handle Nulls"
    description = "Drops rows containing nulls (optionally only in chosen columns) or fills them with zero / empty string / mean / median / mode / a constant."
    category = "logic"
    icon = "eraser"
    color = "#60a5fa"

    class ParamsModel(BaseModel):
        mode: str = Field(
            default="drop",
            description="drop = remove rows with nulls · fill = replace them",
            json_schema_extra={"widget": "select", "options": ["drop", "fill"]},
        )
        columns: list[str] = Field(
            default_factory=list,
            description="Columns to consider (JSON array; empty = all)",
            json_schema_extra={"widget": "code", "rows": 3, "language": "json", "hint": '["age"]'},
        )
        fill: str = Field(
            default="value",
            description="Fill strategy (fill mode)",
            json_schema_extra={"widget": "select", "options": ["zero", "empty", "mean", "median", "mode", "value"]},
        )
        fill_value: str = Field(default="", description="Constant used by the 'value' strategy (Jinja supported)")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: HandleNullsNode.ParamsModel
        df = _frame_from_items(_items(context.current_input), "Handle Nulls")
        cols = [str(c) for c in (p.columns or []) if str(c) in df.columns] or None
        before = len(df)
        if df.empty:
            return self._single({"items": [], "rows_out": 0, "rows_in": 0, "changed": 0})
        if p.mode == "drop":
            out = df.dropna(subset=cols)
            changed = before - len(out)
            return self._single({"items": _rows_out(out), "rows_in": before, "rows_out": len(out), "dropped": changed})
        col_list = cols if cols else list(df.columns)
        filled = 0
        for col in col_list:
            s = df[col]
            nulls = int(s.isna().sum())
            if not nulls:
                continue
            filled += nulls
            if p.fill == "zero":
                df[col] = _numeric_series(s).fillna(0)
            elif p.fill == "empty":
                df[col] = s.map(lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) else v)
            elif p.fill in ("mean", "median"):
                nums = _numeric_series(s)
                stat = nums.mean() if p.fill == "mean" else nums.median()
                df[col] = nums.fillna(0 if pd.isna(stat) else stat)
            elif p.fill == "mode":
                modes = s.dropna().mode()
                df[col] = s.fillna(modes.iloc[0] if len(modes) else "")
            else:
                df[col] = s.fillna(p.fill_value)
        return self._single({"items": _rows_out(df), "rows_in": before, "rows_out": len(df), "filled": filled})


class DataQualityNode(BaseNode):
    """v45: expectation checks over the items - nulls, uniqueness, ranges,
    allowed values, regex shape, row counts and schema drift.

    Emits a structured report; with ``on_fail=error`` the node fails the run
    (bad data can stop pipelines), with ``warn`` it just flags. Pair the
    boolean-ish report with an IF node to route quarantined rows.
    """

    type = "data_quality"
    name = "Data Quality"
    description = (
        "Runs expectation checks over the items: not_null, unique, range, non_negative, "
        "allowed_values, regex, min_rows, max_rows, schema. Fails the run on error mode."
    )
    category = "logic"
    icon = "shield-check"
    color = "#f87171"

    CHECKS = ("not_null", "unique", "range", "non_negative", "allowed_values", "regex", "min_rows", "max_rows", "schema")

    class ParamsModel(BaseModel):
        checks: list[dict] = Field(
            default_factory=list,
            description=(
                'Check specs, e.g. [{"check": "not_null", "column": "email"}, '
                '{"check": "range", "column": "age", "min": 0, "max": 120}, '
                '{"check": "allowed_values", "column": "plan", "values": ["free", "pro"]}, '
                '{"check": "regex", "column": "email", "pattern": "^.+@.+$"}, '
                '{"check": "min_rows", "min": 1}, {"check": "schema", "expected": [{"name": "email", "dtype": "text"}]}]'
            ),
            json_schema_extra={"widget": "code", "rows": 8, "language": "json",
                               "hint": '[{"check": "not_null", "column": "email"}]'},
        )
        on_fail: str = Field(
            default="warn",
            description="warn = report only · error = fail the node (and the run)",
            json_schema_extra={"widget": "select", "options": ["warn", "error"]},
        )
        sample_bad: int = Field(default=5, ge=0, le=50, description="Example failing rows kept per check")

    def _run_check(self, spec: dict, df: pd.DataFrame) -> dict:
        check = str(spec.get("check", ""))
        if check not in self.CHECKS:
            raise NodeExecutionError(f"Data Quality: unknown check {check!r} (use {'|'.join(self.CHECKS)})")
        col = str(spec.get("column", ""))
        result: dict[str, Any] = {"check": check, "column": col or None, "passed": True, "failed": 0}
        if check in ("min_rows", "max_rows", "schema"):
            if check == "min_rows":
                need = int(spec.get("min", 1))
                result["passed"] = len(df) >= need
                result["failed"] = max(0, need - len(df))
                result["expected"] = need
                result["actual"] = len(df)
            elif check == "max_rows":
                cap = int(spec.get("max", 1_000_000))
                result["passed"] = len(df) <= cap
                result["failed"] = max(0, len(df) - cap)
                result["expected"] = cap
                result["actual"] = len(df)
            else:
                expected = spec.get("expected") or []
                actual = {c["name"]: c["dtype"] for c in names_list(df)}
                drifted = [
                    {"name": e.get("name"), "expected_dtype": e.get("dtype"), "actual_dtype": actual.get(e.get("name"))}
                    for e in expected
                    if actual.get(e.get("name")) != e.get("dtype")
                ]
                missing = [e.get("name") for e in expected if e.get("name") not in actual]
                result["passed"] = not drifted and not missing
                result["drifted"] = drifted
                result["missing"] = missing
                result["failed"] = len(drifted) + len(missing)
            return result

        if not col:
            raise NodeExecutionError(f"Data Quality: check {check!r} needs a 'column'")
        if col not in df.columns:
            result["passed"] = False
            result["failed"] = len(df)
            result["error"] = f"column {col!r} not found"
            return result
        s = df[col]
        bad_mask: pd.Series
        if check == "not_null":
            bad_mask = s.isna()
        elif check == "unique":
            bad_mask = s.notna() & s.duplicated(keep=False)
        elif check == "non_negative":
            bad_mask = _numeric_series(s) < 0
        elif check == "range":
            nums = _numeric_series(s)
            lo = spec.get("min")
            hi = spec.get("max")
            bad_mask = pd.Series(False, index=df.index)
            if lo is not None:
                bad_mask |= nums < float(lo)
            if hi is not None:
                bad_mask |= nums > float(hi)
            bad_mask &= s.notna()
            result["min"] = lo
            result["max"] = hi
        elif check == "allowed_values":
            allowed = [str(v) for v in (spec.get("values") or [])]
            if not allowed:
                raise NodeExecutionError("Data Quality: allowed_values check needs 'values'")
            bad_mask = s.notna() & ~s.astype(str).str.strip().isin(allowed)
            result["values"] = allowed
        elif check == "regex":
            pattern = str(spec.get("pattern", ""))
            if not pattern:
                raise NodeExecutionError("Data Quality: regex check needs a 'pattern'")
            try:
                compiled = _re.compile(pattern)
            except _re.error as exc:
                raise NodeExecutionError(f"Data Quality: bad regex {pattern!r}: {exc}") from exc
            bad_mask = s.notna() & ~s.astype(str).str.match(compiled)
            result["pattern"] = pattern
        else:  # pragma: no cover - guarded by CHECKS
            raise NodeExecutionError(f"Data Quality: unhandled check {check!r}")

        failed = int(bad_mask.sum())
        result["passed"] = failed == 0
        result["failed"] = failed
        if failed and self.params.sample_bad:
            result["sample"] = _rows_out(df.loc[bad_mask].head(self.params.sample_bad))
        return result

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: DataQualityNode.ParamsModel
        if not p.checks:
            raise NodeExecutionError("Data Quality needs at least one check")
        df = _frame_from_items(_items(context.current_input), "Data Quality")
        results = [self._run_check(spec, df) for spec in (p.checks or [])]
        failed_checks = [r for r in results if not r["passed"]]
        passed = not failed_checks
        payload = {
            "items": _rows_out(df),
            "rows_in": len(df),
            "passed": passed,
            "total_checks": len(results),
            "failed_checks": len(failed_checks),
            "checks": results,
        }
        if not passed and p.on_fail == "error":
            summary = "; ".join(
                f"{r['check']}{':' + r['column'] if r.get('column') else ''} ({r['failed']} bad)"
                for r in failed_checks
            )
            raise NodeExecutionError(f"Data quality failed: {summary}")
        return self._single(payload)


def names_list(df: pd.DataFrame) -> list[dict]:
    """Schema list for a frame (delegates to the datasets service)."""
    from ...services.datasets import schema_of

    return schema_of(df)


class AnalyzeNode(BaseNode):
    """v45: no-code statistics over the items - descriptive stats, correlations,
    outlier detection, distributions, value counts and time-bucketed trends."""

    type = "analyze"
    name = "Analyze"
    description = (
        "Statistical analysis of the items: descriptive (mean/std/quartiles), correlation "
        "matrix, outliers (IQR / z-score), distribution histogram, value counts, or a "
        "time-bucketed trend with period-over-period growth."
    )
    category = "actions"
    icon = "line-chart"
    color = "#a78bfa"

    class ParamsModel(BaseModel):
        analysis: str = Field(
            default="descriptive",
            description="Which analysis to run",
            json_schema_extra={"widget": "select", "options": ["descriptive", "correlation", "outliers", "distribution", "value_counts", "trend"]},
        )
        columns: list[str] = Field(
            default_factory=list,
            description="Columns to analyze (JSON array; empty = all numeric where applicable)",
            json_schema_extra={"widget": "code", "rows": 3, "language": "json", "hint": '["price"]'},
        )
        # outliers
        method: str = Field(
            default="iqr",
            description="Outlier method (iqr threshold=1.5 · zscore threshold=3)",
            json_schema_extra={"widget": "select", "options": ["iqr", "zscore"]},
        )
        threshold: float = Field(default=1.5, description="IQR multiplier (1.5/3 typical) or z-score cutoff")
        # distribution
        bins: int = Field(default=10, ge=2, le=100, description="Histogram bins for distribution analysis")
        # value_counts
        max_values: int = Field(default=50, ge=1, le=500, description="Distinct values kept for value_counts")
        # trend
        timestamp: str = Field(default="", description="Timestamp column (trend analysis)")
        freq: str = Field(
            default="day",
            description="Time bucket size (trend analysis)",
            json_schema_extra={"widget": "select", "options": ["hour", "day", "week", "month"]},
        )
        metric: str = Field(
            default="count",
            description="Bucket aggregation (trend analysis)",
            json_schema_extra={"widget": "select", "options": ["count", "sum", "avg", "median"]},
        )
        value_column: str = Field(default="", description="Column aggregated by metric (trend; not needed for count)")

    def _cols(self, df: pd.DataFrame, numeric_only: bool = False) -> list[str]:
        wanted = [str(c) for c in (self.params.columns or [])]  # type: AnalyzeNode.ParamsModel
        if wanted:
            missing = [c for c in wanted if c not in df.columns]
            if missing:
                raise NodeExecutionError(f"Analyze: column(s) {missing} not found - available: {[str(c) for c in df.columns]}")
            return wanted
        if numeric_only:
            return [c for c in df.select_dtypes(include=["number"]).columns]
        return [str(c) for c in df.columns]

    def _run(self, p: "AnalyzeNode.ParamsModel", df: pd.DataFrame) -> dict[str, Any]:
        if p.analysis == "descriptive":
            cols = self._cols(df, numeric_only=True)
            out = []
            for col in cols:
                nums = _numeric_series(df[col]).dropna()
                if not len(nums):
                    continue
                q1, med, q3 = nums.quantile([0.25, 0.5, 0.75])
                out.append({
                    "column": col,
                    "count": int(len(nums)),
                    "mean": round(float(nums.mean()), 4),
                    "std": round(float(nums.std()), 4),
                    "min": round(float(nums.min()), 4),
                    "q25": round(float(q1), 4),
                    "median": round(float(med), 4),
                    "q75": round(float(q3), 4),
                    "max": round(float(nums.max()), 4),
                })
            return {"analysis": "descriptive", "columns": out}

        if p.analysis == "correlation":
            cols = self._cols(df, numeric_only=True)[:15]
            if len(cols) < 2:
                raise NodeExecutionError("Analyze correlation needs at least 2 numeric columns")
            corr = df[cols].apply(_numeric_series).corr(method="pearson")
            matrix = []
            for name in cols:
                row = {"column": name, "correlations": {}}
                for other in cols:
                    v = corr.loc[name, other]
                    row["correlations"][other] = None if pd.isna(v) else round(float(v), 3)
                matrix.append(row)
            return {"analysis": "correlation", "method": "pearson", "matrix": matrix}

        if p.analysis == "outliers":
            cols = self._cols(df, numeric_only=True)
            if not cols:
                raise NodeExecutionError("Analyze outliers needs at least one numeric column (pass columns)")
            found = []
            for col in cols:
                nums = _numeric_series(df[col]).dropna()
                if len(nums) < 4:
                    continue
                if p.method == "zscore":
                    std = float(nums.std())
                    mean = float(nums.mean())
                    if std == 0:
                        continue
                    mask = ((nums - mean).abs() / std) > p.threshold
                    lower, upper = mean - p.threshold * std, mean + p.threshold * std
                else:
                    q1, q3 = nums.quantile([0.25, 0.75])
                    iqr = float(q3 - q1)
                    lower, upper = float(q1 - p.threshold * iqr), float(q3 + p.threshold * iqr)
                    mask = (nums < lower) | (nums > upper)
                idx = nums[mask].index
                found.append({
                    "column": col,
                    "method": p.method,
                    "lower": round(lower, 4),
                    "upper": round(upper, 4),
                    "outlier_count": int(len(idx)),
                    "outlier_pct": round(len(idx) / max(len(nums), 1) * 100, 2),
                    "outlier_rows": _rows_out(df.loc[idx[:50]]) if len(idx) else [],
                })
            return {"analysis": "outliers", "columns": found, "total_outliers": sum(f["outlier_count"] for f in found)}

        if p.analysis == "distribution":
            cols = self._cols(df, numeric_only=True)
            if not cols:
                raise NodeExecutionError("Analyze distribution needs a numeric column (pass columns)")
            col = cols[0]
            nums = _numeric_series(df[col]).dropna()
            if not len(nums):
                return {"analysis": "distribution", "column": col, "bins": []}
            counts, edges = pd.cut(nums, bins=p.bins, retbins=True, duplicates="drop")
            grouped = counts.value_counts().sort_index()
            buckets = []
            for interval, count in grouped.items():
                buckets.append({
                    "from": round(float(interval.left), 4),
                    "to": round(float(interval.right), 4),
                    "count": int(count),
                })
            return {
                "analysis": "distribution",
                "column": col,
                "bins": buckets,
                "mean": round(float(nums.mean()), 4),
                "std": round(float(nums.std()), 4),
                "missing": int(df[col].isna().sum()),
            }

        if p.analysis == "value_counts":
            cols = self._cols(df)
            if not cols:
                raise NodeExecutionError("Analyze value_counts needs a column (pass columns)")
            col = cols[0]
            vc = df[col].value_counts(dropna=True).head(p.max_values)
            total = max(int(df[col].notna().sum()), 1)
            return {
                "analysis": "value_counts",
                "column": col,
                "values": [
                    {"value": str(v), "count": int(c), "pct": round(c / total * 100, 2)}
                    for v, c in vc.items()
                ],
            }

        # trend
        if not p.timestamp:
            raise NodeExecutionError("Analyze trend needs a timestamp column")
        if p.timestamp not in df.columns:
            raise NodeExecutionError(f"Analyze: timestamp column {p.timestamp!r} not found")
        ts = pd.to_datetime(df[p.timestamp], errors="coerce")
        valid = ts.notna()
        tdf = df.loc[valid].copy()
        tdf["__ts"] = ts[valid]
        freq_map = {"hour": "h", "day": "D", "week": "W", "month": "ME"}
        rule = freq_map.get(p.freq, "D")
        if p.metric == "count":
            series = tdf.groupby(pd.Grouper(key="__ts", freq=rule)).size()
        else:
            if not p.value_column or p.value_column not in df.columns:
                raise NodeExecutionError(f"Analyze trend metric={p.metric} needs a value_column")
            nums = _numeric_series(tdf[p.value_column])
            agg = {"sum": "sum", "avg": "mean", "median": "median"}[p.metric]
            series = pd.Series(nums.values, index=tdf["__ts"].values).resample(rule).agg(agg)
        points = []
        for period, value in series.items():
            points.append({
                "period": period.isoformat() if hasattr(period, "isoformat") else str(period),
                "value": None if pd.isna(value) else round(float(value), 4),
            })
        filled = [pt for pt in points if pt["value"] is not None]
        growth = None
        if len(filled) >= 2 and filled[0]["value"] not in (None, 0):
            growth = {
                "first": filled[0]["value"],
                "last": filled[-1]["value"],
                "pct_change": round((filled[-1]["value"] - filled[0]["value"]) / abs(filled[0]["value"]) * 100, 2),
            }
        return {"analysis": "trend", "timestamp": p.timestamp, "freq": p.freq, "metric": p.metric, "points": points, "growth": growth}

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: AnalyzeNode.ParamsModel
        df = _frame_from_items(_items(context.current_input), "Analyze")
        if df.empty:
            raise NodeExecutionError("Analyze needs input items - connect a source (dataset_read, sql_query, …)")
        result = self._run(p, df)
        result["rows_analyzed"] = int(len(df))
        return self._single(result)
