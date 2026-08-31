"""V34 feature tests: AI Agent tool-calling deepening.

Two new tool kinds on the AI Agent node:
* dataset - READ-ONLY SQL (single SELECT/WITH) over every stored dataset
  (DuckDB views named after each dataset). Guarded: non-select statements,
  multiple statements and dangerous keywords (ATTACH/COPY/...) are rejected
  and the error is fed back to the model instead of failing the run.
* code    - sandboxed Python (same restricted runtime as the Code node);
  the model gets back `result` + captured stdout, and sandbox exceptions
  are fed back as tool errors too.

Plus GET /agents - inventory of agent workflows (tools, kinds, memory
sessions) powering the /agents console - and the `data-analyst` gallery
template showcasing both new kinds.

Same harness as v4-v33: httpx ASGITransport in-process, per-test asyncio.run,
uuid-suffixed names, finally-cleanup + background drain; the LLM transport
(`AgentNode._chat`) is scripted so the whole wave runs offline.
"""

from __future__ import annotations

import asyncio
import json
import uuid

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


async def _cleanup(workflow_ids: list[str], dataset_ids: list[str]) -> None:
    async with _client() as client:
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
        for did in dataset_ids:
            try:
                await client.delete(f"/datasets/{did}")
            except Exception:
                pass
    await _drain_background()


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(120):
        res = await client.get(f"/executions/{exec_id}")
        assert res.status_code == 200, res.text
        if res.json()["status"] != "running":
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def _agent_graph(tools: list[dict], user_message: str, max_iterations: int = 5, **extra: object) -> dict:
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
                    **extra,
                },
            },
        ],
        "edges": [{"id": "e1", "source": "t", "target": "agent", "sourceHandle": "main", "targetHandle": "main"}],
    }


class _ScriptedChat:
    """Replaces AgentNode._chat with a scripted reply sequence.

    Must be a plain async FUNCTION (descriptor) so the node instance binds
    correctly - assigning a callable object would skip self-binding.
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
def test_v34_health_pin():
    """v34 keeps the app identity check; the strict pin moved to v35 (convention)."""

    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["app"] == "Py8n" and body["version"] >= "1.34.0", body

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ------------------------------------------------------------------ test 2
def test_v34_dataset_tool_answers_from_sql():
    """Agent queries a stored dataset via the dataset tool and answers."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    ds_ids: list[str] = []
    view = f"sales_{tag}"  # view_name lowercases; name is already a valid identifier
    scripted = _ScriptedChat([])

    async def _go():
        async with _client() as client:
            res = await client.post(
                "/datasets",
                json={
                    "name": f"sales_{tag}",
                    "description": "v34 dataset-tool fixture",
                    "rows": [
                        {"region": "EMEA", "amount": 120},
                        {"region": "APAC", "amount": 80},
                        {"region": "EMEA", "amount": 60},
                    ],
                },
            )
            assert res.status_code == 201, res.text
            ds_ids.append(res.json()["id"])

            tools = [{
                "kind": "dataset",
                "name": "sql_query",
                "description": "Read-only SELECT over datasets",
                "max_rows": 10,
            }]
            res = await client.post(
                "/workflows",
                json={"name": f"tmp v34 ds-agent {tag}", "graph": _agent_graph(tools, f"Total per region in {view}?")},
            )
            assert res.status_code == 201, res.text
            wf_ids.append(res.json()["id"])

            scripted.replies = [
                json.dumps({
                    "tool": "sql_query",
                    "arguments": {"sql": f"SELECT region, SUM(amount) AS total FROM {view} GROUP BY region ORDER BY total DESC"},
                }),
                '{"answer": "EMEA leads with 180."}',
            ]
            scripted.install()
            try:
                detail = await _run_and_wait(client, wf_ids[0])
            finally:
                scripted.restore()

            assert detail["status"] == "success", detail.get("error")
            out = _agent_output(detail)
            assert out["answer"] == "EMEA leads with 180."
            assert out["iterations"] == 2
            assert len(out["tool_calls"]) == 1
            call = out["tool_calls"][0]
            assert call["status"] == "ok", call
            payload = json.loads(call["result"])
            assert payload["row_count"] == 2, payload
            assert payload["returned_rows"] == 2
            assert payload["rows"][0]["region"] == "EMEA" and payload["rows"][0]["total"] == 180, payload
            assert payload["rows"][1]["region"] == "APAC" and payload["rows"][1]["total"] == 80, payload

    try:
        asyncio.run(_go())
    finally:
        scripted.restore()
        asyncio.run(_cleanup(wf_ids, ds_ids))


# ------------------------------------------------------------------ test 3
def test_v34_dataset_tool_readonly_guard():
    """Non-SELECT and multi-statement SQL are rejected but the run survives."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    scripted = _ScriptedChat([])

    async def _go():
        async with _client() as client:
            tools = [{"kind": "dataset", "name": "sql_query", "description": "Read-only SELECT"}]
            res = await client.post(
                "/workflows",
                json={"name": f"tmp v34 guard {tag}", "graph": _agent_graph(tools, "try to break out", max_iterations=4)},
            )
            assert res.status_code == 201, res.text
            wf_ids.append(res.json()["id"])

            scripted.replies = [
                '{"tool": "sql_query", "arguments": {"sql": "DELETE FROM sales"}}',
                '{"tool": "sql_query", "arguments": {"sql": "SELECT 1; ATTACH \'evil.db\' AS e"}}',
                '{"answer": "blocked, staying read-only."}',
            ]
            scripted.install()
            try:
                detail = await _run_and_wait(client, wf_ids[0])
            finally:
                scripted.restore()

            assert detail["status"] == "success", detail.get("error")
            out = _agent_output(detail)
            assert out["answer"] == "blocked, staying read-only."
            assert len(out["tool_calls"]) == 2
            first, second = out["tool_calls"]
            assert first["status"] == "error" and "read-only" in first["result"], first
            assert second["status"] == "error" and "statements" in second["result"], second

    try:
        asyncio.run(_go())
    finally:
        scripted.restore()
        asyncio.run(_cleanup(wf_ids, []))


# ------------------------------------------------------------------ test 4
def test_v34_code_tool_sandbox_roundtrip():
    """Sandboxed Python returns result + stdout; sandbox errors feed back."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    scripted = _ScriptedChat([])

    async def _go():
        async with _client() as client:
            tools = [{
                "kind": "code",
                "name": "py",
                "description": "Sandboxed Python; set result",
                "timeout_seconds": 5,
            }]
            res = await client.post(
                "/workflows",
                json={"name": f"tmp v34 code-agent {tag}", "graph": _agent_graph(tools, "compute 6*7")},
            )
            assert res.status_code == 201, res.text
            wf_ids.append(res.json()["id"])

            scripted.replies = [
                # first call uses the NESTED directive shape some models emit
                '{"tool": {"name": "py", "arguments": {"code": "result = 6 * 7\\nprint(\\"computing\\")"}}}',
                '{"tool": "py", "arguments": {"code": "result = 1 / 0"}}',
                '{"answer": "42; division was blocked."}',
            ]
            scripted.install()
            try:
                detail = await _run_and_wait(client, wf_ids[0])
            finally:
                scripted.restore()

            assert detail["status"] == "success", detail.get("error")
            out = _agent_output(detail)
            assert out["answer"] == "42; division was blocked."
            assert out["iterations"] == 3
            ok_call, err_call = out["tool_calls"]
            assert ok_call["status"] == "ok", ok_call
            ok_payload = json.loads(ok_call["result"])
            assert ok_payload["result"] == 42 and ok_payload["stdout"] == "computing", ok_payload
            assert err_call["status"] == "error" and "ZeroDivisionError" in err_call["result"], err_call

    try:
        asyncio.run(_go())
    finally:
        scripted.restore()
        asyncio.run(_cleanup(wf_ids, []))


# ------------------------------------------------------------------ test 5
def test_v34_agents_inventory_and_template():
    """GET /agents lists agent workflows (kinds, memory) and skips plain ones;
    the data-analyst gallery template ships and installs cleanly."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    agent_id: str | None = None
    plain_id: str | None = None

    async def _go():
        nonlocal agent_id, plain_id
        async with _client() as client:
            tools = [
                {"kind": "dataset", "name": "sql_query", "description": "read-only sql"},
                {"kind": "code", "name": "py", "description": "sandbox"},
            ]
            res = await client.post(
                "/workflows",
                json={
                    "name": f"tmp v34 inventory {tag}",
                    "graph": _agent_graph(tools, "hi", memory="buffer", session_key="s1"),
                },
            )
            assert res.status_code == 201, res.text
            agent_id = res.json()["id"]
            wf_ids.append(agent_id)

            res = await client.post(
                "/workflows",
                json={
                    "name": f"tmp v34 plain {tag}",
                    "graph": {"nodes": [
                        {"id": "t", "type": "manual_trigger", "name": "T", "position": {"x": 0, "y": 0}, "parameters": {}},
                    ], "edges": []},
                },
            )
            assert res.status_code == 201, res.text
            plain_id = res.json()["id"]
            wf_ids.append(plain_id)

            res = await client.get("/agents")
            assert res.status_code == 200, res.text
            agents = res.json()
            mine = next((a for a in agents if a["id"] == agent_id), None)
            assert mine is not None, "agent workflow missing from /agents"
            assert mine["tool_kinds"] == ["code", "dataset"], mine
            assert {t["name"] for t in mine["tools"]} == {"sql_query", "py"}, mine
            assert mine["memory_sessions"] == ["s1"], mine
            assert mine["node_count"] == 2
            assert all(a["id"] != plain_id for a in agents), "plain workflow leaked into /agents"

            # gallery: data-analyst present, metadata complete, kinds correct
            res = await client.get("/templates")
            assert res.status_code == 200, res.text
            templates = res.json()
            da = next((t for t in templates if t["id"] == "data-analyst"), None)
            assert da is not None, "data-analyst template missing"
            assert da["badge"] == "Agent" and da["accent"], da
            assert "ai_agent" in da["node_types"] and da["node_count"] == 2, da

            res = await client.get("/templates/data-analyst")
            assert res.status_code == 200, res.text
            detail = res.json()
            agent_node = next(n for n in detail["graph"]["nodes"] if n["type"] == "ai_agent")
            kinds = {t["kind"] for t in agent_node["parameters"]["tools"]}
            assert kinds == {"dataset", "code"}, agent_node

            # install with a custom name
            res = await client.post("/templates/data-analyst/use", json={"name": f"SMOKE34 Analyst {tag}"})
            assert res.status_code == 201, res.text
            installed = res.json()
            wf_ids.append(installed["id"])
            assert installed["name"] == f"SMOKE34 Analyst {tag}" and installed["is_active"] is False

            # and it shows up in the inventory too
            res = await client.get("/agents")
            assert any(a["id"] == installed["id"] for a in res.json())

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids, []))


# ------------------------------------------------------------------ test 6
def test_v34_agent_only_workflow_is_chat_able():
    """v34: /chat works for agent-only workflows (no chat_trigger) - the
    console can drive manual-trigger agents; reply comes from the agent."""
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    scripted = _ScriptedChat([])

    async def _go():
        async with _client() as client:
            tools = [{"kind": "knowledge", "name": "facts", "description": "static facts", "content": "HQ is Lisbon."}]
            res = await client.post(
                "/workflows",
                json={"name": f"tmp v34 chat-agent {tag}", "graph": _agent_graph(tools, "User says: {{ nodes.t.output.payload.message }}")},
            )
            assert res.status_code == 201, res.text
            wf_ids.append(res.json()["id"])

            # a workflow with NEITHER chat_trigger nor agent must still 409
            res = await client.post(
                "/workflows",
                json={"name": f"tmp v34 plain2 {tag}", "graph": {"nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "T", "position": {"x": 0, "y": 0}, "parameters": {}},
                ], "edges": []}},
            )
            assert res.status_code == 201, res.text
            plain_id = res.json()["id"]
            wf_ids.append(plain_id)
            res = await client.post(f"/chat/{plain_id}", json={"message": "hi"})
            assert res.status_code == 409, res.text
            assert "AI Agent" in res.json()["detail"], res.text

            scripted.replies = ['{"answer": "Hello from the analyst."}']
            scripted.install()
            try:
                res = await client.post(f"/chat/{wf_ids[0]}", json={"message": "hello agent", "session_id": "s34"})
            finally:
                scripted.restore()
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["status"] == "success" and body["reply"] == "Hello from the analyst.", body
            assert body["session_id"] == "s34"

    try:
        asyncio.run(_go())
    finally:
        scripted.restore()
        asyncio.run(_cleanup(wf_ids, []))
