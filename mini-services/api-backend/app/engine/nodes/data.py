"""Data-flow nodes: Filter, Switch, Merge, Split Out, Aggregate, Compare, Summarize, CSV.

Convention: nodes that work over lists look for an ``items`` array in the
incoming payload; if absent, the payload itself is treated as a single item.
This mirrors n8n's item model while staying JSON-friendly.
"""

from __future__ import annotations

import csv
import io
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
    description = "Removes repeated items from the array — compares by a field (dot-path, empty = whole item). Keeps the first occurrence."
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
    sharing its key; results are routed to three output handles —
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
            # or both edges landed on one handle — arrival (edge) order then
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
        # downstream nodes are skipped — action branches fire only when
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
    """v24: group-by aggregation — one output item per group.

    ``group_by`` lists dot-path fields; every distinct combination forms a
    group. ``aggregates`` compute count/sum/avg/min/max over a field per
    group. With no group_by, all items form one global group.
    """

    type = "summarize"
    name = "Summarize"
    description = "Groups the items array by field(s) and computes count/sum/avg/min/max per group (one output item per group)."
    category = "logic"
    icon = "table-properties"
    color = "#4ade80"

    class ParamsModel(BaseModel):
        group_by: list[str] = Field(
            default_factory=list,
            description="Dot-path fields to group by (JSON array, empty = one global group)",
            json_schema_extra={"widget": "code", "rows": 3, "language": "json", "hint": '["region"]'},
        )
        aggregates: list[dict] = Field(
            default_factory=list,
            description='Aggregations per group, e.g. [{"field": "amount", "op": "sum"}] — op: count|sum|avg|min|max (field optional for count)',
            json_schema_extra={"widget": "code", "rows": 5, "language": "json", "hint": '[{"field": "amount", "op": "sum"}]'},
        )

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
        values = [_pluck(it, field) for it in group_items]
        values = [v for v in values if v is not None]
        if op in ("min", "max") and values:
            nums = self._numeric(values)
            if nums:
                return min(nums) if op == "min" else max(nums)
            # string domain (e.g. ISO dates): total-order min/max
            ordered = sorted(values, key=_sort_key)
            return ordered[0] if op == "min" else ordered[-1]
        nums = self._numeric(values)
        if op == "sum":
            return sum(nums) if nums else None
        if op == "avg":
            return round(sum(nums) / len(nums), 4) if nums else None
        if op == "min":
            return min(nums) if nums else None
        if op == "max":
            return max(nums) if nums else None
        raise NodeExecutionError(f"Summarize: unknown aggregate op {op!r} (use count|sum|avg|min|max)")

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
            description="CSV text to parse (parse mode) — supports {{ expressions }}",
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
