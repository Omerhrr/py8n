"""V19 feature tests: AI Agent node (tool-calling loop) + execution retention.

Agent tests mock the LLM transport (``AgentNode._chat``) so the wire protocol,
tool dispatch (knowledge + real sub-workflow runs), iteration cap and plain
prose fallback are all deterministic — no bridge dependency.

Retention tests exercise the policy API against the real SQLite dev DB:
age-based purge is scoped safely by backdating ONLY executions created here;
the volume cap temporarily trims histories (the smoke suite replenishes demo
executions afterwards). The policy is restored at the end of each test.

Same harness as v4-v18: httpx ASGITransport in-process, per-test asyncio.run,
uuid-suffixed names, finally-cleanup + background drain.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from app.main import app
from app.services import executor as executor_mod

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _cleanup(workflow_ids: list[str]) -> None:
    async with _client() as client:
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
    await _drain_background()


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str, payload: dict | None = None) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(80):
        res = await client.get(f"/executions/{exec_id}")
        assert res.status_code == 200, res.text
        if res.json()["status"] != "running":
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


# ------------------------------------------------------------------ helpers
def _agent_graph(tools: list[dict], user_message: str, max_iterations: int = 5) -> dict:
    return {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "name": "Trigger", "position": {"x": 0, "y": 0}, "parameters": {}},
            {
                "id": "agent",
                "type": "ai_agent",
                "name": "Agent",
                "position": {"x": 220, "y": 0},
                "parameters": {
                    "system_prompt": "You are a test agent.",
                    "user_message": user_message,
                    "max_iterations": max_iterations,
                    "tools": tools,
                },
            },
        ],
        "edges": [{"id": "e1", "source": "t", "target": "agent", "sourceHandle": "main", "targetHandle": "main"}],
    }


class _ScriptedChat:
    """Replaces AgentNode._chat with a scripted reply sequence.

    Must be a plain async FUNCTION (descriptor) so the node instance binds
    correctly — assigning a callable object would skip self-binding.
    """

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []
        self._original = None

    def install(self):
        from app.engine.nodes.agent import AgentNode

        calls, replies = self.calls, self.replies

        async def _fake_chat(agent_self, messages, temperature):
            calls.append(json.loads(json.dumps(messages)))
            return replies.pop(0) if replies else '{"answer": "script exhausted"}'

        self._original = AgentNode._chat
        AgentNode._chat = _fake_chat  # type: ignore[method-assign]

    def restore(self):
        from app.engine.nodes.agent import AgentNode

        if self._original is not None:
            AgentNode._chat = self._original  # type: ignore[method-assign]


def _agent_output(detail: dict) -> dict:
    runs = {r["node_id"]: r for r in detail["node_runs"]}
    return runs["agent"]["output"]


# ------------------------------------------------------------------ test 1
def test_agent_knowledge_tool_loop_and_prose_fallback():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    scripted = _ScriptedChat([])

    async def _go():
        async with _client() as client:
            tools = [
                {
                    "kind": "knowledge",
                    "name": "tier_table",
                    "description": "Maps customer codes to loyalty tiers",
                    "content": "code A1 = GOLD tier; code B2 = SILVER tier",
                }
            ]
            res = await client.post(
                "/workflows",
                json={"name": f"tmp v19 agent {tag}", "graph": _agent_graph(tools, "Which tier is code A1?")},
            )
            assert res.status_code == 201, res.text
            wf_ids.append(res.json()["id"])

            # --- round 1: tool call, round 2: final answer -------------
            scripted.replies = [
                '{"tool": "tier_table", "arguments": {"code": "A1"}}',
                '{"answer": "Code A1 is GOLD tier."}',
            ]
            scripted.install()
            try:
                detail = await _run_and_wait(client, wf_ids[0], {})
            finally:
                scripted.restore()
            assert detail["status"] == "success", detail.get("error")
            out = _agent_output(detail)
            assert out["answer"] == "Code A1 is GOLD tier."
            assert out["iterations"] == 2
            assert len(out["tool_calls"]) == 1
            call = out["tool_calls"][0]
            assert call["tool"] == "tier_table" and call["status"] == "ok"
            assert "GOLD" in call["result"]
            # the tool result was fed back to the model (round 2 messages)
            assert "TOOL RESULT tier_table" in scripted.calls[1][-1]["content"]

            # --- plain prose (no JSON) is the final answer in one round --
            scripted.replies = ["Just a plain sentence without any JSON."]
            scripted.install()
            try:
                detail = await _run_and_wait(client, wf_ids[0], {})
            finally:
                scripted.restore()
            out = _agent_output(detail)
            assert out["answer"] == "Just a plain sentence without any JSON."
            assert out["iterations"] == 1 and out["tool_calls"] == []

    try:
        asyncio.run(_go())
    finally:
        scripted.restore()
        asyncio.run(_cleanup(wf_ids))


# ------------------------------------------------------------------ test 2
def test_agent_workflow_tool_runs_subflow():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    scripted = _ScriptedChat([])

    async def _go():
        async with _client() as client:
            # sub-workflow: echoes the tool arguments through set_variable
            sub_graph = {
                "nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "Trigger", "position": {"x": 0, "y": 0}, "parameters": {}},
                    {
                        "id": "s",
                        "type": "set_variable",
                        "name": "Echo",
                        "position": {"x": 220, "y": 0},
                        "parameters": {"assignments": {"echo": "{{ input.payload.arguments.q }}"}, "keep_input": False},
                    },
                ],
                "edges": [{"id": "e1", "source": "t", "target": "s", "sourceHandle": "main", "targetHandle": "main"}],
            }
            res = await client.post("/workflows", json={"name": f"tmp v19 agent tool {tag}", "graph": sub_graph})
            assert res.status_code == 201, res.text
            sub_id = res.json()["id"]
            wf_ids.append(sub_id)

            tools = [{"kind": "workflow", "name": "wiki", "description": "search the wiki", "workflow_id": sub_id}]
            res = await client.post(
                "/workflows",
                json={"name": f"tmp v19 agent main {tag}", "graph": _agent_graph(tools, "Search the wiki for py8n")},
            )
            assert res.status_code == 201, res.text
            wf_ids.append(res.json()["id"])

            scripted.replies = [
                '{"tool": "wiki", "arguments": {"q": "py8n"}}',
                '{"answer": "wiki done"}',
            ]
            scripted.install()
            try:
                detail = await _run_and_wait(client, wf_ids[1], {})
            finally:
                scripted.restore()
            assert detail["status"] == "success", detail.get("error")
            out = _agent_output(detail)
            assert out["answer"] == "wiki done"
            call = out["tool_calls"][0]
            assert call["status"] == "ok"
            payload = json.loads(call["result"])
            assert payload["tool_status"] == "success"
            assert payload["output"]["echo"] == "py8n"

    try:
        asyncio.run(_go())
    finally:
        scripted.restore()
        asyncio.run(_cleanup(wf_ids))


# ------------------------------------------------------------------ test 3
def test_agent_iteration_cap_errors():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    scripted = _ScriptedChat([])

    async def _go():
        async with _client() as client:
            tools = [{"kind": "knowledge", "name": "loop", "description": "endless", "content": "nothing"}]
            res = await client.post(
                "/workflows",
                json={"name": f"tmp v19 cap {tag}", "graph": _agent_graph(tools, "loop forever", max_iterations=2)},
            )
            assert res.status_code == 201, res.text
            wf_ids.append(res.json()["id"])

            scripted.replies = ['{"tool": "loop", "arguments": {}}'] * 5
            scripted.install()
            try:
                detail = await _run_and_wait(client, wf_ids[0], {})
            finally:
                scripted.restore()
            assert detail["status"] == "error"
            assert "iteration cap" in (detail.get("error") or "")

    try:
        asyncio.run(_go())
    finally:
        scripted.restore()
        asyncio.run(_cleanup(wf_ids))


# ------------------------------------------------------------------ test 4
def test_retention_age_purge_and_policy_api():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        from app.db import AsyncSessionLocal
        from app.models import ExecutionLog

        async with _client() as client:
            # policy API: defaults visible, updates persist
            res = await client.get("/settings/retention")
            assert res.status_code == 200 and "retention_days" in res.json()
            original = res.json()

            res = await client.put("/settings/retention", json={"retention_days": 30})
            assert res.status_code == 200 and res.json()["retention_days"] == 30

            # bad policy rejected (body validation 422 / handler 400)
            res = await client.put("/settings/retention", json={"retention_days": -5})
            assert res.status_code in (400, 422)

            # fresh execution, then backdate ONLY it beyond the cutoff
            res = await client.post(
                "/workflows",
                json={
                    "name": f"tmp v19 retention {tag}",
                    "graph": _agent_graph([], "hi"),
                },
            )
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]
            wf_ids.append(wf_id)
            detail = await _run_and_wait(client, wf_id, {})
            exec_id = detail["id"]

            async with AsyncSessionLocal() as session:
                row = await session.get(ExecutionLog, exec_id)
                assert row is not None
                row.started_at = datetime.now(timezone.utc) - timedelta(days=60)
                row.finished_at = datetime.now(timezone.utc) - timedelta(days=59)
                await session.commit()

            res = await client.post("/settings/retention/purge")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["deleted_by_age"] >= 1

            res = await client.get(f"/executions/{exec_id}")
            assert res.status_code == 404  # purged

            # bookkeeping stored
            res = await client.get("/settings/retention")
            assert res.json()["last_purge_deleted"] >= 1

            # restore original policy
            res = await client.put(
                "/settings/retention",
                json={"retention_days": original["retention_days"], "max_executions_per_workflow": original["max_executions_per_workflow"]},
            )
            assert res.status_code == 200

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ------------------------------------------------------------------ test 5
def test_retention_volume_cap_keeps_newest():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            res = await client.post(
                "/workflows",
                json={
                    "name": f"tmp v19 volume {tag}",
                    "graph": _agent_graph([], "hi"),
                },
            )
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]
            wf_ids.append(wf_id)

            first = await _run_and_wait(client, wf_id, {})
            second = await _run_and_wait(client, wf_id, {})
            assert first["id"] != second["id"]

            res = await client.put("/settings/retention", json={"max_executions_per_workflow": 1})
            assert res.status_code == 200

            res = await client.post("/settings/retention/purge")
            assert res.status_code == 200, res.text

            res = await client.get(f"/executions?workflow_id={wf_id}")
            assert res.status_code == 200
            remaining = res.json()
            assert len(remaining) == 1, remaining
            assert remaining[0]["id"] == second["id"]  # newest survives

            # restore unlimited volume
            res = await client.put("/settings/retention", json={"max_executions_per_workflow": 0})
            assert res.status_code == 200

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))
