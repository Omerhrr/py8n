"""AI Agent node - LLM with an iterative tool-calling loop (v19, deepened v34).

The flagship agentic node: the model receives a tool catalogue and may call
tools over multiple rounds before producing its final answer. Tool calls use
a strict JSON wire protocol (works with ANY OpenAI-compatible chat model -
no native function-calling support required):

    {"tool": "<tool_name>", "arguments": {...}}   -> run tool, feed result back
    {"answer": "<final text>"}                     -> loop ends

Built-in tool kinds
-------------------
* workflow  - run another Py8n workflow (args become the trigger payload);
              reuses the same nested GraphRunner machinery as the
              Execute Workflow node (depth-limited).
* http      - perform an HTTP request; method/url/headers/body come from the
              model, guarded by an optional domain allow-list.
* knowledge - return a static knowledge snippet stored on the node.
* dataset   - (v34) run READ-ONLY SQL (SELECT/WITH, single statement) over
              the stored datasets via DuckDB - every dataset is a view named
              after it. The agent can actually interrogate your data.
* code      - (v34) run a sandboxed Python snippet (same restricted runtime
              as the Code node) and hand the model back `result` + stdout -
              lets the agent compute, format and do arithmetic reliably.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, Field

from ..context import ExecutionContext
from .. import sandbox
from .base import BaseNode, Handle, NodeExecutionError, NodeResult

MAX_TOOL_RESULT_CHARS = 4000

# v36: caps for the live SSE trace frames (keep frames small; the full data
# still lands in the execution log via the node's final output)
MAX_EVENT_REPLY_CHARS = 400
MAX_EVENT_ARGS_CHARS = 300
MAX_EVENT_PREVIEW_CHARS = 240


class ToolSpec(BaseModel):
    kind: Literal["workflow", "http", "knowledge", "dataset", "code"] = "knowledge"
    name: str = Field(default="", description="Tool name the model will call (snake_case)")
    description: str = Field(default="", description="What the tool does - helps the model choose")
    # workflow tool
    workflow_id: str | None = Field(default=None, json_schema_extra={"widget": "workflow"})
    # http tool
    allowed_domains: list[str] = Field(default_factory=list, description="Empty = any domain")
    # knowledge tool
    content: str | None = Field(default=None, json_schema_extra={"widget": "textarea", "rows": 4})
    # v34 dataset tool - cap on rows handed back to the model
    max_rows: int = Field(default=25, ge=1, le=200, description="dataset tool: max rows returned to the model")
    # v34 code tool - executor timeout
    timeout_seconds: float = Field(default=10, ge=1, le=60, description="code tool: sandbox timeout in seconds")


class AgentNode(BaseNode):
    type = "ai_agent"
    name = "AI Agent"
    description = "LLM agent that can call tools (sub-workflows, HTTP, knowledge) in a loop until it answers."
    category = "ai"
    icon = "bot"
    color = "#a78bfa"
    inputs: ClassVar[list[Handle]] = [Handle("main", "In")]
    outputs: ClassVar[list[Handle]] = [Handle("main", "Out")]

    class ParamsModel(BaseModel):
        provider: str = Field(
            default="sandbox_bridge",
            json_schema_extra={"widget": "select", "options": ["sandbox_bridge", "openai_compatible"]},
        )
        model: str = Field(default="", description="Model name (optional; bridge picks a default)")
        system_prompt: str = Field(
            default="You are a precise automation agent. Use the available tools when they help, then answer.",
            json_schema_extra={"widget": "textarea", "rows": 3},
        )
        user_message: str = Field(
            default="Task: {{ input | tojson }}",
            description="User message - supports {{ expressions }}",
            json_schema_extra={"widget": "textarea", "rows": 5},
        )
        max_iterations: int = Field(default=5, ge=1, le=10)
        temperature: float = Field(default=0.4, ge=0, le=2)
        credential_id: str | None = Field(default=None)
        # v23: session memory - persisted per session_key, injected as prior turns
        memory: str = Field(
            default="none",
            description="none = stateless (fresh each run); buffer = remembers prior turns for the same session key",
            json_schema_extra={"widget": "select", "options": ["none", "buffer"]},
        )
        session_key: str = Field(
            default="default",
            description="Conversation key - same key = same memory. Supports {{ expressions }}, e.g. 'support-{{ input.customer_id }}'",
        )
        max_history_turns: int = Field(
            default=5, ge=1, le=50, description="How many recent user/assistant turn pairs the agent remembers"
        )
        tools: list[ToolSpec] = Field(
            default_factory=list,
            description="Tools the agent may call",
            json_schema_extra={"widget": "tools"},
        )

    # ------------------------------------------------------------------
    # LLM transport (monkeypatchable in tests)
    # ------------------------------------------------------------------
    async def _chat(self, messages: list[dict], temperature: float) -> str:
        """One chat completion -> assistant content string."""
        p = self.params  # type: AgentNode.ParamsModel
        if p.provider == "sandbox_bridge":
            from ...config import settings

            url = f"{settings.llm_bridge_url.rstrip('/')}/v1/chat/completions"
            headers: dict = {}
            payload: dict = {"messages": messages, "temperature": temperature, "max_tokens": 2048}
            if p.model:
                payload["model"] = p.model
        else:
            if not p.credential_id:
                raise NodeExecutionError("openai_compatible provider requires a credential")
            from ...services.crypto import decrypt_credential

            cred = await decrypt_credential(
                self._context_for_creds, p.credential_id,
                owner_id=getattr(self._context_for_creds, "owner_id", None),
            )
            if cred.get("type") != "openai_compatible":
                raise NodeExecutionError("Selected credential is not of type openai_compatible")
            base = (cred.get("base_url") or "").rstrip("/")
            if not base:
                raise NodeExecutionError("Credential is missing base_url")
            url = f"{base}/chat/completions"
            headers = {"Authorization": f"Bearer {cred.get('api_key', '')}"}
            payload = {"model": p.model or "gpt-4o-mini", "messages": messages, "temperature": temperature, "max_tokens": 2048}

        try:
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise NodeExecutionError(f"LLM request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise NodeExecutionError(f"LLM API returned HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError):
            return data.get("content") or ""

    # ------------------------------------------------------------------
    # Wire-protocol parsing
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_reply(content: str) -> dict:
        """Extract the JSON directive from a reply; {} when it is plain prose."""
        text = (content or "").strip()
        # strip markdown fences if the model wrapped them
        fenced = re.fullmatch(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        # first balanced {...} block in the text
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, ValueError):
                pass
        return {}

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------
    @staticmethod
    def _truncate(value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        return text[:MAX_TOOL_RESULT_CHARS]

    MAX_PREVIEW_ROWS = 3
    MAX_PREVIEW_COLS = 8
    MAX_PREVIEW_CELL_CHARS = 60

    @classmethod
    def _data_preview(cls, value: Any) -> dict | None:
        """v40: compact row preview attached to agent_tool_result frames so the
        live trace can render actual dataset rows instead of a JSON blob.
        Only dataset-shaped payloads ({columns: [...], rows: [...]}) qualify."""
        if not isinstance(value, dict):
            return None
        columns = value.get("columns")
        rows = value.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list) or not columns:
            return None
        cols = [str(c) for c in columns][: cls.MAX_PREVIEW_COLS]

        def _cell(row: Any, col: str) -> str:
            v = row.get(col) if isinstance(row, dict) else row
            return str(v)[: cls.MAX_PREVIEW_CELL_CHARS]

        shown = rows[: cls.MAX_PREVIEW_ROWS]
        total = value.get("row_count", len(rows))
        try:
            total = int(total)
        except (TypeError, ValueError):
            total = len(rows)
        return {
            "columns": cols,
            "rows": [[_cell(r, c) for c in cols] for r in shown],
            "total_rows": total,
            "rows_shown": len(shown),
            "columns_shown": len(cols),
            "columns_total": len(columns),
        }

    # ------------------------------------------------------------------
    # v34 tool kinds - dataset (read-only SQL) and code (sandboxed Python)
    # ------------------------------------------------------------------
    @staticmethod
    def _guard_readonly_sql(sql: str) -> str:
        """Whitelist a single read-only statement; returns the cleaned SQL."""
        text = (sql or "").strip().rstrip(";").strip()
        first = text.split(None, 1)[0].lower() if text else ""
        if first not in ("select", "with"):
            raise NodeExecutionError(
                "dataset tool is read-only: only a single SELECT (or WITH ... SELECT) statement is allowed"
            )
        if ";" in text:
            raise NodeExecutionError("dataset tool: multiple SQL statements are not allowed")
        for banned in ("attach", "install", "load", "copy", "call", "pragma", "export"):
            if re.search(rf"\b{banned}\b", text.lower()):
                raise NodeExecutionError(f"dataset tool: {banned.upper()} is not allowed")
        return text

    async def _run_tool_dataset(self, tool: ToolSpec, args: dict, context: ExecutionContext) -> dict:
        sql = self._guard_readonly_sql(str(args.get("sql", args.get("query", ""))))
        if not sql:
            raise NodeExecutionError(f"dataset tool {tool.name!r}: pass {{\"sql\": \"SELECT ...\"}}")
        from ...db import AsyncSessionLocal
        from ...services import datasets as ds_svc

        async with AsyncSessionLocal() as session:
            # run_sql registers only owner-visible datasets as DuckDB views
            try:
                result = await ds_svc.run_sql(session, sql, owner_id=context.owner_id)
            except ValueError as exc:
                # binder/parse errors must reach the MODEL as tool feedback
                # (it can fix the query), not kill the workflow node
                raise NodeExecutionError(str(exc)) from exc
        rows = result["rows"][: tool.max_rows]
        return {
            "columns": result["columns"],
            "rows": rows,
            "row_count": result["row_count"],
            "returned_rows": len(rows),
            "duration_ms": result["duration_ms"],
        }

    async def _run_tool_code(self, tool: ToolSpec, args: dict) -> dict:
        from .logic import SAFE_BUILTINS, SAFE_MODULES

        code = str(args.get("code", ""))
        if not code.strip():
            raise NodeExecutionError(f"code tool {tool.name!r}: pass {{\"code\": \"...\"}}")

        import io
        import contextlib

        def _print(*a, **k):  # SAFE_BUILTINS no-ops print - shadow it with a capture
            buf.write(str(k.get("sep", " ")).join(str(x) for x in a) + str(k.get("end", "\n")))

        buf = io.StringIO()
        user_globals: dict[str, Any] = {"__builtins__": dict(SAFE_BUILTINS), "result": None, "print": _print}
        user_globals.update(SAFE_MODULES)

        def _exec() -> tuple[str, Any]:
            with contextlib.redirect_stdout(buf):
                exec(code_obj, user_globals)  # noqa: S102 - sandboxed, see sandbox.py
            return buf.getvalue(), user_globals.get("result")

        # Audit hardening: AST guard + module proxies + bounded pool
        # (see app/engine/sandbox.py). Violations surface as tool feedback so
        # the model can self-correct instead of killing the workflow.
        try:
            code_obj = sandbox.guard(code, SAFE_MODULES)
        except sandbox.SandboxViolation as exc:
            raise NodeExecutionError(f"code rejected by sandbox: {exc}") from exc
        sandbox.make_proxies(user_globals, SAFE_MODULES)
        sandbox.deepcopy_state(user_globals, skip={"result", "print"})
        try:
            stdout, result = await sandbox.run_bounded(
                _exec, timeout_seconds=tool.timeout_seconds, label="code tool"
            )
        except sandbox.SandboxTimeout as exc:
            raise NodeExecutionError(str(exc)) from None
        except NodeExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface sandbox errors to the model
            raise NodeExecutionError(f"code error: {type(exc).__name__}: {exc}") from exc
        return {"result": result, "stdout": stdout.strip()}

    async def _run_tool(self, tool: ToolSpec, args: dict, context: ExecutionContext) -> Any:
        """Run a tool and return the STRUCTURED value (v40); the caller owns
        stringification via _truncate so the trace can also preview rows."""
        if tool.kind == "knowledge":
            return tool.content or ""
        if tool.kind == "dataset":
            return await self._run_tool_dataset(tool, args, context)
        if tool.kind == "code":
            return await self._run_tool_code(tool, args)
        if tool.kind == "http":
            method = str(args.get("method", "GET")).upper()
            url = str(args.get("url", ""))
            if not url.startswith(("http://", "https://")):
                raise NodeExecutionError(f"HTTP tool {tool.name!r}: url must be absolute")
            if tool.allowed_domains:
                host = httpx.URL(url).host or ""
                if not any(host == d or host.endswith("." + d) for d in tool.allowed_domains):
                    raise NodeExecutionError(f"HTTP tool {tool.name!r}: domain {host!r} not allowed")
            headers = args.get("headers") or {}
            body = args.get("body")
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.request(
                        method, url, headers=headers,
                        json=body if isinstance(body, (dict, list)) else None,
                        content=None if isinstance(body, (dict, list)) else body,
                    )
            except httpx.HTTPError as exc:
                return {"error": str(exc)}
            try:
                parsed: Any = resp.json()
            except ValueError:
                parsed = resp.text
            return {"status": resp.status_code, "body": parsed}
        if tool.kind == "workflow":
            if not tool.workflow_id:
                raise NodeExecutionError(f"Workflow tool {tool.name!r}: no workflow selected")
            return await self._run_tool_workflow(tool, args, context)
        raise NodeExecutionError(f"Unknown tool kind {tool.kind!r}")

    async def _run_tool_workflow(self, tool: ToolSpec, args: dict, context: ExecutionContext) -> Any:
        import uuid

        from sqlalchemy import select

        from ...db import AsyncSessionLocal
        from ...models import Workflow
        from ..runner import GraphRunner, validate_graph_document
        from .subflow import MAX_DEPTH

        async with AsyncSessionLocal() as session:
            workflow = (
                await session.execute(select(Workflow).where(Workflow.id == tool.workflow_id))
            ).scalar_one_or_none()
        if workflow is None:
            raise NodeExecutionError(f"Workflow tool {tool.name!r}: workflow not found")

        graph = validate_graph_document(workflow.graph or {"nodes": [], "edges": []})
        runner = GraphRunner(
            graph,
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            trigger_type="manual",
            trigger_payload={"payload": {"arguments": args, "question": str(self.params.user_message)}},
            execution_id=uuid.uuid4().hex,
            depth=context.depth + 1,
            honor_pinned=context.honor_pinned,
        )
        result = await runner.run()
        if result["status"] != "success":
            return {"tool_status": result["status"], "error": result.get("error")}
        last_run = next((r for r in reversed(result["node_runs"]) if r["status"] == "success"), None)
        return {"tool_status": "success", "output": last_run["output"] if last_run else None}

    # ------------------------------------------------------------------
    # v36 live trace - fine-grained events onto the execution bus
    # ------------------------------------------------------------------
    async def _emit_agent(self, context: ExecutionContext, event: dict) -> None:
        """Publish one agent_* event; a dead/absent bus must NEVER fail the run."""
        emit = getattr(context, "emit", None)
        if emit is None:
            return
        try:
            await emit(event)
        except Exception:  # noqa: BLE001 - live trace is best-effort by design
            pass

    @staticmethod
    def _preview(value: Any, limit: int) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        return text[:limit]

    # ------------------------------------------------------------------
    # Main agentic loop
    # ------------------------------------------------------------------
    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: AgentNode.ParamsModel
        self._context_for_creds = context
        tools = {t.name: t for t in (p.tools or []) if t.name}
        if not p.user_message:
            raise NodeExecutionError("Agent needs a user message")

        catalogue = {
            "type": "object",
            "properties": {
                t.name: {"type": "object", "description": t.description or t.name}
                for t in tools.values()
            },
        }
        protocol = (
            "You operate in a tool loop. Reply with EXACTLY one JSON object and nothing else.\n"
            'To call a tool: {"tool": "<name>", "arguments": {...}} - allowed names + argument schemas:\n'
            f"{json.dumps(catalogue, ensure_ascii=False)}\n"
            'After each tool call you receive "TOOL RESULT <name>: <json>".\n'
            'When you can answer without more tools, reply {"answer": "<final answer>"}.'
        )
        messages: list[dict] = [
            {"role": "system", "content": f"{p.system_prompt}\n\n{protocol}"},
            {"role": "user", "content": str(p.user_message)},
        ]

        # v23: session memory - prior turns for this key sit between the
        # system message and the current user message.
        memory_used = 0
        memory_key = (p.session_key or "default").strip()
        if p.memory == "buffer":
            from ...services.agent_memory import load_history

            history = await load_history(memory_key, owner_id=context.owner_id)
            messages[1:1] = history
            memory_used = len(history) // 2

        tool_calls: list[dict] = []
        answer: str | None = None
        iterations = 0
        while iterations < p.max_iterations:
            iterations += 1
            await self._emit_agent(context, {
                "event": "agent_iteration", "iteration": iterations, "max_iterations": p.max_iterations,
            })
            content = await self._chat(messages, p.temperature)
            await self._emit_agent(context, {
                "event": "agent_reply", "iteration": iterations,
                "reply": self._preview(content, MAX_EVENT_REPLY_CHARS),
            })
            directive = self._parse_reply(content)
            # normalize the directive: some models nest the tool call as
            # {"tool": {"name": ..., "arguments": {...}}} - accept both shapes
            tool_name = directive.get("tool")
            tool_args = directive.get("arguments")
            if isinstance(tool_name, dict):
                tool_args = tool_name.get("arguments", tool_args)
                tool_name = tool_name.get("name")
            if isinstance(tool_name, str) and tool_name in tools:
                tool = tools[tool_name]
                args = tool_args or {}
                if not isinstance(args, dict):
                    args = {"value": args}
                try:
                    await self._emit_agent(context, {
                        "event": "agent_tool_call", "iteration": iterations,
                        "tool": tool.name, "arguments": self._preview(args, MAX_EVENT_ARGS_CHARS),
                    })
                    value = await self._run_tool(tool, args, context)  # v40: structured
                    result = self._truncate(value)
                    status = "ok"
                except NodeExecutionError as exc:
                    value = None
                    result = f"tool error: {exc}"
                    status = "error"
                frame = {
                    "event": "agent_tool_result", "iteration": iterations,
                    "tool": tool.name, "status": status,
                    "preview": self._preview(result, MAX_EVENT_PREVIEW_CHARS),
                }
                data_preview = self._data_preview(value) if status == "ok" else None
                if data_preview is not None:
                    frame["data"] = data_preview  # v40: row preview for the live trace
                await self._emit_agent(context, frame)
                tool_calls.append({"tool": tool.name, "arguments": args, "status": status, "result": result})
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": f"TOOL RESULT {tool.name}: {result}"})
                continue
            # plain prose or an explicit {"answer": ...}
            answer = directive.get("answer") if isinstance(directive.get("answer"), str) else (content or "").strip()
            break

        if answer is None:
            raise NodeExecutionError(
                f"Agent hit the iteration cap ({p.max_iterations}) without a final answer"
            )

        # v23: persist the finished turn so the next run with the same key remembers it
        if p.memory == "buffer":
            from ...services.agent_memory import append_history

            await append_history(
                memory_key, str(p.user_message), answer, p.max_history_turns,
                owner_id=context.owner_id,
            )

        await self._emit_agent(context, {"event": "agent_answer", "answer": answer})

        return self._single(
            {
                "answer": answer,
                "iterations": iterations,
                "tool_calls": tool_calls,
                "tools_available": list(tools.keys()),
                "memory_key": memory_key if p.memory == "buffer" else None,
                "memory_turns_loaded": memory_used,
            }
        )
