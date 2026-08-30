"""Logic nodes: IF condition, Set/Transform, Code, Delay."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ..context import ExecutionContext
from .base import BaseNode, Handle, NodeExecutionError, NodeResult


class IfConditionNode(BaseNode):
    """Branches the flow into ``true`` / ``false`` output handles."""

    type = "if_condition"
    name = "IF Condition"
    description = "Routes the flow down the true or false branch based on a condition."
    category = "logic"
    icon = "git-branch"
    color = "#a3e635"
    outputs: ClassVar[list[Handle]] = [Handle("true", "True"), Handle("false", "False")]

    class ParamsModel(BaseModel):
        left_value: Any = Field(default="", description="Left operand — supports {{ expressions }}")
        operator: str = Field(
            default="equals",
            json_schema_extra={
                "widget": "select",
                "options": [
                    "equals", "not_equals", "contains", "not_contains",
                    "greater_than", "less_than", "is_empty", "is_true", "regex",
                ],
            },
        )
        right_value: Any = Field(default="", description="Right operand (ignored by is_empty/is_true)")

    def _compare(self, left: Any, op: str, right: Any) -> bool:
        if op == "is_empty":
            return left in (None, "", [], {})
        if op == "is_true":
            return bool(left)
        if op in ("greater_than", "less_than"):
            try:
                lnum, rnum = float(left), float(right)
            except (TypeError, ValueError):
                return False
            return lnum > rnum if op == "greater_than" else lnum < rnum
        lstr, rstr = str(left), str(right)
        if op == "equals":
            return lstr == rstr or left == right
        if op == "not_equals":
            return not (lstr == rstr or left == right)
        if op == "contains":
            return rstr in lstr
        if op == "not_contains":
            return rstr not in lstr
        if op == "regex":
            return bool(re.search(rstr, lstr))
        raise NodeExecutionError(f"Unknown operator {op!r}")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: IfConditionNode.ParamsModel
        verdict = self._compare(p.left_value, p.operator, p.right_value)
        # pass the incoming payload through on the active branch only
        payload = {"condition": verdict, "input": context.current_input}
        return NodeResult(
            outputs={"true": payload if verdict else None, "false": None if verdict else payload},
            raw_output={"condition": verdict},
        )


class SetVariableNode(BaseNode):
    """Builds an output object from static values / Jinja expressions."""

    type = "set_variable"
    name = "Set / Transform"
    description = "Creates a clean output object from mapped keys and {{ expressions }}."
    category = "logic"
    icon = "braces"
    color = "#22d3ee"

    class ParamsModel(BaseModel):
        assignments: dict[str, Any] = Field(
            default_factory=dict,
            description="Key → value mapping. Values support {{ expressions }}",
            json_schema_extra={"widget": "code", "rows": 8},
        )
        keep_input: bool = Field(default=True, description="Merge the incoming payload into the output")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: SetVariableNode.ParamsModel
        output: dict[str, Any] = {}
        if p.keep_input and isinstance(context.current_input, dict):
            output.update(context.current_input)
        for key, raw in (p.assignments or {}).items():
            output[key] = context.resolve(raw)
        return self._single(output)


SAFE_BUILTINS = {
    "len": len, "str": str, "int": int, "float": float, "bool": bool,
    "dict": dict, "list": list, "tuple": tuple, "set": set,
    "sum": sum, "min": min, "max": max, "abs": abs, "round": round,
    "sorted": sorted, "enumerate": enumerate, "zip": zip, "range": range,
    "map": map, "filter": filter, "any": any, "all": all, "print": lambda *a, **k: None,
}
SAFE_MODULES = {"json": json, "re": re, "math": __import__("math")}


class CodeNode(BaseNode):
    """Runs a sandboxed Python snippet against the execution context."""

    type = "code"
    name = "Python Code"
    description = "Executes a Python snippet. Set `result` to output data. Imports are sandboxed."
    category = "logic"
    icon = "terminal"
    color = "#c084fc"

    class ParamsModel(BaseModel):
        code: str = Field(
            default="result = {'hello': 'from py8n'}\n",
            json_schema_extra={"widget": "code", "rows": 12, "language": "python"},
        )
        timeout_seconds: float = Field(default=10, ge=1, le=60)

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: CodeNode.ParamsModel
        user_globals: dict[str, Any] = {"__builtins__": dict(SAFE_BUILTINS)}
        user_globals.update(SAFE_MODULES)
        user_globals["input_data"] = context.current_input
        user_globals["inputs"] = context.current_inputs
        user_globals["nodes"] = context.node_states
        user_globals["result"] = None

        import math as _math  # ensure present in SAFE_MODULES
        user_globals["math"] = _math

        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, self._exec_sync, p.code, user_globals),
                timeout=p.timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise NodeExecutionError(f"Code node timed out after {p.timeout_seconds}s")
        except NodeExecutionError:
            raise
        result = user_globals.get("result")
        return self._single({"result": result})

    @staticmethod
    def _exec_sync(code: str, user_globals: dict[str, Any]) -> None:
        try:
            exec(code, user_globals)  # noqa: S102 (sandboxed namespace, self-hosted tool)
        except Exception as exc:  # noqa: BLE001
            raise NodeExecutionError(f"Code error: {type(exc).__name__}: {exc}") from exc


class DelayNode(BaseNode):
    """Pauses the branch for N seconds — useful to demo the async queue."""

    type = "delay"
    name = "Delay"
    description = "Waits a number of seconds before continuing down the branch."
    category = "logic"
    icon = "hourglass"
    color = "#94a3b8"

    class ParamsModel(BaseModel):
        seconds: float = Field(default=2, ge=0.1, le=3600)

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: DelayNode.ParamsModel
        await asyncio.sleep(p.seconds)
        return self._single({"delayed_seconds": p.seconds, "resumed_at": context.resolve("{{ now }}")})


def _json_dumps(value: Any) -> str:  # small helper kept for future nodes
    return json.dumps(value, default=str, ensure_ascii=False)


class StopAndErrorNode(BaseNode):
    """Fails the run deliberately with a custom message (v22).

    n8n's "Stop and Error": turns data-level validation failures into real
    run failures so error workflows fire and the UI shows a red run. The
    message is a Jinja template, so upstream values can be embedded.
    """

    type = "stop_and_error"
    name = "Stop and Error"
    description = "Stops the workflow deliberately with a custom error message — use it for validation failures or to exercise error workflows."
    category = "logic"
    icon = "octagon-x"
    color = "#f43f5e"

    class ParamsModel(BaseModel):
        error_message: str = Field(
            default="Workflow stopped intentionally",
            description="Error message to raise — supports {{ expressions }} referencing upstream outputs",
            json_schema_extra={"widget": "textarea", "rows": 3},
        )
        error_type: str = Field(
            default="ValidationError",
            description="Label describing the failure kind (e.g. ValidationError, OutOfStock)",
        )

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: StopAndErrorNode.ParamsModel
        message = context.resolve(p.error_message)
        if not isinstance(message, str):
            message = json.dumps(message, ensure_ascii=False)
        raise NodeExecutionError(f"[{p.error_type}] {message}")
