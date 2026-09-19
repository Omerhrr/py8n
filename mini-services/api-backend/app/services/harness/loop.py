"""The harness loop (v109) - the agentic turn as a persistent state machine.

The brain is the node's OWN transport (AgentNode._chat via a headless shim
- the exact same machinery the graph executor and the v108 modules drive),
and the wire is the node's OWN strict JSON protocol ({"tool": ...} /
{"answer": ...} rendered by AgentNode._protocol). The harness adds what the
node loop cannot carry inside a graph:

* guard rails between rounds (repeat / budget / clock - see guard.py),
* the approval GATE: a sensitive tool call does not run - the turn pauses
  (status=waiting_approval), the exact message list is frozen into
  ``turn.wire``, and a fail-closed slip waits for a human decision,
* a fully persisted trace, so the console and the API can replay a turn
  (and a resumed turn continues with the SAME iteration counter).

Every tool result - ok, error, guarded, declined - reaches the model as a
``TOOL RESULT <name>: <text>`` message, exactly like the node loop; the
model can always adapt instead of crashing the turn.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...engine.nodes.agent import AgentNode
from ...engine.schema import NodeSpec
from ...models import HarnessApproval, HarnessSession, HarnessTurn
from . import tools as harness_tools
from .guard import TurnGuard

MAX_REPLY_PREVIEW = 400
MAX_FRAME_PREVIEW = 300


def _brain(session: HarnessSession, owner_id: str | None) -> tuple[AgentNode, SimpleNamespace]:
    """A headless AgentNode carrying the session's brain configuration -
    the same shim contract the v108 module runtime uses."""
    params = AgentNode.ParamsModel(
        provider=session.provider or "sandbox_bridge",
        model=session.model or "",
        system_prompt=session.system_prompt or "",
        user_message="",
        max_iterations=max(1, int(settings.harness_max_iterations)),
        temperature=float(session.temperature if session.temperature is not None else 0.4),
        credential_id=session.credential_id,
        memory="none",
        session_key="default",
        tools=[],
    )
    node = AgentNode.__new__(AgentNode)
    node.spec = NodeSpec(id=session.id[:80] or "harness", type="ai_agent",
                         name=(session.name or "harness")[:200])
    node.id = session.id
    node.name = session.name
    node.params = params
    ctx = SimpleNamespace(owner_id=owner_id, depth=0, honor_pinned=True, emit=None)
    node._context_for_creds = ctx  # noqa: SLF001 - the shim contract
    return node, ctx


def _system_message(session: HarnessSession, registry: dict) -> dict:
    protocol = AgentNode._protocol(
        {name: SimpleNamespace(name=name, description=spec["description"])
         for name, spec in harness_tools.protocol_catalogue(registry).items()})  # noqa: SLF001
    return {"role": "system", "content": f"{session.system_prompt}\n\n{protocol}"}


def history_messages(completed: list[HarnessTurn], limit: int) -> list[dict]:
    """Completed turns become the conversation the next turn remembers."""
    pairs: list[dict] = []
    for turn in completed[-max(0, int(limit)):]:
        if turn.reply:
            pairs.append({"role": "user", "content": turn.user_message})
            pairs.append({"role": "assistant", "content": turn.reply})
    return pairs


def _frame(turn: HarnessTurn, event: dict) -> None:
    turn.trace = [*(turn.trace or []), event]


def _preview(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit]


async def _execute_tool(db: AsyncSession, registry: dict, tool: harness_tools.ToolDef,
                        args: dict, owner_id: str | None) -> harness_tools.ToolResult:
    try:
        value = await tool.handler(db, owner_id, args)
        return harness_tools.ToolResult(value=value,
                                        text=AgentNode._truncate(value))  # noqa: SLF001
    except harness_tools.HarnessToolError as exc:
        return harness_tools.ToolResult(status="error", text=f"tool error: {exc}")
    except Exception as exc:  # noqa: BLE001 - tool feedback reaches the model
        return harness_tools.ToolResult(status="error",
                                        text=f"tool error: {type(exc).__name__}: {exc}")


async def drive(db: AsyncSession, session: HarnessSession, turn: HarnessTurn,
                messages: list[dict], registry: dict[str, harness_tools.ToolDef],
                guard: TurnGuard) -> HarnessTurn:
    """Run (or continue) one harness turn until a terminal state or the
    approval gate pauses it. Mutates and commits ``turn`` progressively."""
    node, _ctx = _brain(session, turn.owner_id)

    while True:
        # -- guard: budget and clock, checked BEFORE the next model round
        budget = guard.check_budget(turn.iterations)
        if not budget.ok:
            turn.status = "exhausted" if budget.kind == "budget" else "failed"
            turn.error = budget.reason
            _frame(turn, {"event": "guard_stop", "kind": budget.kind,
                          "reason": budget.reason})
            await db.commit()
            return turn
        clock = guard.check_clock()
        if not clock.ok:
            turn.status = "failed"
            turn.error = clock.reason
            _frame(turn, {"event": "guard_stop", "kind": clock.kind,
                          "reason": clock.reason})
            await db.commit()
            return turn

        turn.iterations += 1
        _frame(turn, {"event": "iteration", "iteration": turn.iterations})

        content = await node._chat(messages, node.params.temperature)  # noqa: SLF001
        _frame(turn, {"event": "reply", "iteration": turn.iterations,
                      "reply": _preview(content, MAX_REPLY_PREVIEW)})

        directive = AgentNode._parse_reply(content)  # noqa: SLF001
        tool_name = directive.get("tool")
        tool_args = directive.get("arguments")
        if isinstance(tool_name, dict):
            tool_args = tool_name.get("arguments", tool_args)
            tool_name = tool_name.get("name")

        if isinstance(tool_name, str) and tool_name not in registry:
            # a named-but-unknown tool is feedback (the model can recover),
            # exactly like the node's tool-error path
            result_text = (f"tool error: no tool named {tool_name!r} - the "
                           "harness only speaks its catalogued tools")
            turn.tool_calls = [*(turn.tool_calls or []),
                               {"tool": tool_name, "arguments": tool_args or {},
                                "status": "error", "result": result_text}]
            _frame(turn, {"event": "tool_call", "iteration": turn.iterations,
                          "tool": tool_name, "status": "error"})
            _frame(turn, {"event": "tool_result", "tool": tool_name,
                          "preview": _preview(result_text, MAX_FRAME_PREVIEW)})
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user",
                             "content": f"TOOL RESULT {tool_name}: {result_text}"})
            await db.commit()
            continue

        if isinstance(tool_name, str) and tool_name in registry:
            tool = registry[tool_name]
            args = tool_args if isinstance(tool_args, dict) else (
                {"value": tool_args} if tool_args is not None else {})

            # -- guard: verbatim repeats are a stuck loop, not a strategy
            verdict = guard.check_repeat(tool.name, args)
            if not verdict.ok:
                turn.guard_blocks += 1
                _frame(turn, {"event": "guard", "tool": tool.name,
                              "reason": verdict.reason})
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user",
                                 "content": f"TOOL RESULT {tool.name}: {verdict.reason}"})
                await db.commit()
                continue

            # -- the pre-gate check (v110): a malformed sensitive call is
            #    tool feedback at once - the human is never asked to decide
            #    a call that would not even run
            if tool.preflight is not None:
                problem = tool.preflight(args)
                if problem:
                    turn.tool_calls = [*(turn.tool_calls or []),
                                       {"tool": tool.name, "arguments": args,
                                        "status": "error", "result": problem}]
                    _frame(turn, {"event": "tool_call", "iteration": turn.iterations,
                                  "tool": tool.name, "status": "error",
                                  "preflight": True})
                    _frame(turn, {"event": "tool_result", "tool": tool.name,
                                  "preview": _preview(problem, MAX_FRAME_PREVIEW)})
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user",
                                     "content": f"TOOL RESULT {tool.name}: {problem}"})
                    await db.commit()
                    continue

            # -- the approval gate: sensitive moves wait for a human
            if tool.sensitive:
                slip = HarnessApproval(
                    turn_id=turn.id, session_id=session.id, owner_id=turn.owner_id,
                    tool=tool.name, arguments=args)
                db.add(slip)
                await db.flush()
                turn.wire = [*messages, {"role": "assistant", "content": content}]
                turn.status = "waiting_approval"
                _frame(turn, {"event": "approval_requested",
                              "approval_id": slip.id, "tool": tool.name,
                              "arguments": _preview(args, MAX_FRAME_PREVIEW),
                              "moves": tool.moves or "changes the business"})
                await db.commit()
                return turn

            result = await _execute_tool(db, registry, tool, args, turn.owner_id)
            turn.tool_calls = [*(turn.tool_calls or []),
                               {"tool": tool.name, "arguments": args,
                                "status": result.status, "result": result.text}]
            _frame(turn, {"event": "tool_call", "iteration": turn.iterations,
                          "tool": tool.name, "status": result.status})
            _frame(turn, {"event": "tool_result", "tool": tool.name,
                          "preview": _preview(result.text, MAX_FRAME_PREVIEW)})
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user",
                             "content": f"TOOL RESULT {tool.name}: {result.text}"})
            await db.commit()
            continue

        # plain prose or an explicit {"answer": ...} - the turn completes
        answer = directive.get("answer") if isinstance(directive.get("answer"), str) \
            else (content or "").strip()
        turn.reply = answer
        turn.status = "completed"
        _frame(turn, {"event": "answer", "answer": answer})
        await db.commit()
        return turn
