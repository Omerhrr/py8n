"""Agent module runtime (v108) - first-class agents that run WITHOUT a graph.

Until v108 an agent only existed as an ``ai_agent`` node inside a workflow
graph. That shape is great for pipelines and wrong for standing assistants:
building a one-node graph, wiring a trigger and keeping it active just to
have a conversation with tools is ceremony. A module is the agent as its own
resource - system prompt, provider, tool kit, session memory - with a direct
run door.

Zero new agent machinery: the runtime instantiates the REAL AgentNode as the
engine (a headless shim) and drives the SAME loop the graph executor drives,
reusing its transport (_chat), its tool execution (_run_tool - workflow /
http / knowledge / dataset / code, sandboxed and owner-scoped exactly as in
the graph), its wire-protocol parsing and its protocol text. What the module
layer adds is persistence (the AgentModule row), session memory keyed per
module, and a trace list the API returns inline so callers can show the
loop's steps without subscribing to the event bus.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from ..db import AsyncSessionLocal
from ..engine.nodes.agent import AgentNode, ToolSpec
from ..engine.schema import NodeSpec
from ..models import AgentMemory, AgentModule
from .agent_memory import _scoped_key, append_history, load_history

# AgentMemory rows are keyed by session_key alone (v23); module sessions
# live in a per-module namespace so two modules (or a module and a graph
# agent) can never read each other's conversation.
_PREFIX = "module-"


class AgentModuleError(Exception):
    """A module problem the API answers with 400/409 - never a 500."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def validate_tools(raw: Any) -> list[dict]:
    """Parse the module's tool kit through the node's own ToolSpec."""
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise AgentModuleError("tools must be a list of tool specs")
    out: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AgentModuleError(f"tools[{i}] must be an object")
        try:
            spec = ToolSpec(**item)
        except Exception as exc:  # pydantic ValidationError - name it for the caller
            raise AgentModuleError(f"tools[{i}] is not a valid tool spec: {exc}") from exc
        if not spec.name.strip():
            raise AgentModuleError(f"tools[{i}] needs a name")
        out.append(spec.model_dump(exclude_none=True))
    names = [t["name"] for t in out]
    if len(set(names)) != len(names):
        raise AgentModuleError("tool names must be unique")
    return out


def session_storage_key(module_id: str, session_key: str) -> str:
    """The AgentMemory row key for one module session."""
    return f"{_PREFIX}{module_id}-{session_key}"


def _build_node(module: AgentModule, user_message: str) -> AgentNode:
    """A headless AgentNode carrying the module's configuration.

    validate_parameters() is bypassed (it resolves Jinja against a graph
    context); the params are built directly from the module row, so the
    shim speaks exactly what the node speaks.
    """
    params = AgentNode.ParamsModel(
        provider=module.provider or "sandbox_bridge",
        model=module.model or "",
        system_prompt=module.system_prompt or "",
        user_message=user_message,
        max_iterations=int(module.max_iterations or 5),
        temperature=float(module.temperature if module.temperature is not None else 0.4),
        credential_id=module.credential_id,
        memory="none",  # the module layer owns memory injection itself
        session_key="default",
        max_history_turns=int(module.max_history_turns or 5),
        tools=[ToolSpec(**t) for t in (module.tools or [])],
    )
    node = AgentNode.__new__(AgentNode)
    node.spec = NodeSpec(  # execute() is never called on the shim; helpers read params
        id=module.id[:80] or "module", type="ai_agent", name=(module.name or "agent")[:200])
    node.id = module.id
    node.name = module.name
    node.params = params
    return node


async def run_module(
    module: AgentModule,
    message: str,
    session_key: str,
    owner_id: str | None = None,
) -> dict[str, Any]:
    """Run one module turn: the same tool loop the graph executor runs.

    Returns the answer, the loop bookkeeping (iterations / tool_calls) and
    an inline trace of frames shaped like the node's agent_* events, so an
    API caller can render the run without touching the event bus.
    """
    if not (message or "").strip():
        raise AgentModuleError("message must not be empty")
    if module.is_active is False:
        raise AgentModuleError("module is inactive - activate it to run", status_code=409)

    node = _build_node(module, message)
    tools = {t.name: t for t in (node.params.tools or []) if t.name}  # type: ignore[union-attr]

    # A minimal execution-context stand-in: owner scoping rides owner_id
    # (dataset SQL views, workflow tools), emit stays off (the trace list
    # below replaces the event bus for module runs).
    ctx = SimpleNamespace(owner_id=owner_id, depth=0, honor_pinned=True, emit=None)

    node._context_for_creds = ctx  # noqa: SLF001 - the shim contract

    memory_key = session_storage_key(module.id, session_key or "default")
    history: list[dict] = []
    memory_used = 0
    if module.memory == "buffer":
        history = await load_history(memory_key, owner_id=owner_id)
        memory_used = len(history) // 2

    protocol = AgentNode._protocol(tools)  # noqa: SLF001 - shared, tested wire text
    messages: list[dict] = [
        {"role": "system", "content": f"{node.params.system_prompt}\n\n{protocol}"},  # type: ignore[union-attr]
        *history,
        {"role": "user", "content": message},
    ]

    trace: list[dict] = []
    tool_calls: list[dict] = []
    answer: str | None = None
    iterations = 0
    while iterations < node.params.max_iterations:  # type: ignore[union-attr]
        iterations += 1
        trace.append({"event": "iteration", "iteration": iterations})
        content = await node._chat(messages, node.params.temperature)  # noqa: SLF001
        trace.append({"event": "reply", "iteration": iterations,
                      "reply": AgentNode._preview(content, 400)})  # noqa: SLF001
        directive = AgentNode._parse_reply(content)  # noqa: SLF001
        tool_name = directive.get("tool")
        tool_args = directive.get("arguments")
        if isinstance(tool_name, dict):
            tool_args = tool_name.get("arguments", tool_args)
            tool_name = tool_name.get("name")
        if isinstance(tool_name, str) and tool_name in tools:
            tool = tools[tool_name]
            args = tool_args if isinstance(tool_args, dict) else ({"value": tool_args} if tool_args is not None else {})
            try:
                value = await node._run_tool(tool, args, ctx)  # noqa: SLF001
                result = AgentNode._truncate(value)  # noqa: SLF001
                status = "ok"
            except Exception as exc:  # noqa: BLE001 - tool feedback reaches the model
                value = None
                result = f"tool error: {exc}"
                status = "error"
            trace.append({"event": "tool_call", "iteration": iterations,
                          "tool": tool.name, "status": status})
            tool_calls.append({"tool": tool.name, "arguments": args, "status": status, "result": result})
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": f"TOOL RESULT {tool.name}: {result}"})
            continue
        answer = directive.get("answer") if isinstance(directive.get("answer"), str) else (content or "").strip()
        break

    if answer is None:
        raise AgentModuleError(
            f"agent hit the iteration cap ({node.params.max_iterations}) without a final answer")  # type: ignore[union-attr]

    if module.memory == "buffer":
        await append_history(
            memory_key, message, answer, int(module.max_history_turns or 5), owner_id=owner_id)

    trace.append({"event": "answer", "answer": answer})
    return {
        "reply": answer,
        "iterations": iterations,
        "tool_calls": tool_calls,
        "trace": trace,
        "session_key": session_key or "default",
        "memory_turns_loaded": memory_used,
    }


async def list_sessions(module: AgentModule, owner_id: str | None = None) -> list[dict]:
    """The module's conversation sessions, most recently active first."""
    # the storage key is OWNER-SCOPED by the v23 store ({owner_id}::{key}):
    # the listing must walk the same namespaced rows, never raw keys
    prefix = _scoped_key(session_storage_key(module.id, ""), owner_id)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(AgentMemory)
                .where(AgentMemory.session_key.like(f"{prefix}%"))
                .order_by(AgentMemory.updated_at.desc())
            )
        ).scalars().all()
        out = []
        for row in rows:
            out.append({
                "session_key": row.session_key[len(prefix):],
                "turns": len(row.messages or []) // 2,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            })
        return out


async def clear_session(module: AgentModule, session_key: str, owner_id: str | None = None) -> bool:
    """Drop one module session; True when a conversation was deleted."""
    from ..db import AsyncSessionLocal

    key = _scoped_key(session_storage_key(module.id, session_key or "default"), owner_id)
    async with AsyncSessionLocal() as session:
        row = await session.get(AgentMemory, key)
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True

