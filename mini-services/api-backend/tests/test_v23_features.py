"""V23 feature tests: Agent session memory + webhook authentication.

Memory: the AI Agent persists finished user/assistant turns per session key
(new agent_memories table), injects them as prior chat messages on the next
run sharing the key, isolates different keys, and trims to max_history_turns.
Auth: webhook_trigger gains header / basic modes enforced with timing-safe
comparisons BEFORE the flow runs (401, no execution created).

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v22).
"""

from __future__ import annotations

import asyncio
import base64
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


async def _cleanup(workflow_ids: list[str], memory_keys: list[str] | None = None) -> None:
    async with _client() as client:
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
    if memory_keys:
        from app.db import AsyncSessionLocal
        from app.models import AgentMemory

        async with AsyncSessionLocal() as session:
            for key in memory_keys:
                row = await session.get(AgentMemory, key)
                if row is not None:
                    await session.delete(row)
            await session.commit()
    await _drain_background()


def _node(nid: str, ntype: str, params: dict | None = None, name: str | None = None) -> dict:
    return {
        "id": nid,
        "type": ntype,
        "name": name or nid,
        "position": {"x": 0, "y": 0},
        "parameters": params or {},
    }


async def _make_workflow(client: httpx.AsyncClient, name: str, graph: dict, is_active: bool = False) -> str:
    res = await client.post("/workflows", json={"name": name, "graph": graph, "is_active": is_active})
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _run_and_wait(client: httpx.AsyncClient, workflow_id: str, payload: dict | None = None) -> dict:
    res = await client.post(f"/workflows/{workflow_id}/run", json={"payload": payload or {}})
    assert res.status_code in (200, 202), res.text
    exec_id = res.json()["execution_id"]
    for _ in range(100):
        res = await client.get(f"/executions/{exec_id}")
        assert res.status_code == 200, res.text
        if res.json()["status"] != "running":
            return res.json()
        await asyncio.sleep(0.05)
    raise AssertionError("execution did not finish in time")


def _find_node_run(execution: dict, node_name: str) -> dict | None:
    for run in execution.get("node_runs") or []:
        if run.get("node_name") == node_name:
            return run
    return None


def _agent_node(params: dict) -> dict:
    return _node("ag", "ai_agent", params, "Agent")


# ---------------------------------------------------------------------------
# 1) Memory: recall on second run, isolation across keys, output bookkeeping
# ---------------------------------------------------------------------------
def test_v23_agent_memory_recall_and_isolation():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    key_a, key_b = f"mem-a-{tag}", f"mem-b-{tag}"
    seen_messages: list[list[dict]] = []

    async def _scripted_chat(agent_self, messages, temperature):
        seen_messages.append([dict(m) for m in messages])
        return 'noted {"answer": "OK, remembered."}'  # plain prose + fallback line

    from app.engine.nodes.agent import AgentNode

    original_chat = AgentNode._chat
    AgentNode._chat = _scripted_chat

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger", {"payload": {}}),
                    _agent_node(
                        {
                            "memory": "buffer",
                            "session_key": key_a,
                            "user_message": "fact {{ input.payload.n }}",
                            "max_history_turns": 5,
                        }
                    ),
                ],
                "edges": [{"id": "e1", "source": "t", "target": "ag", "sourceHandle": "main", "targetHandle": "main"}],
            }
            wf_id = await _make_workflow(client, f"tmp v23 mem A {tag}", graph)
            wf_ids.append(wf_id)

            # Run 1: no history yet
            r1 = await _run_and_wait(client, wf_id, {"n": 1})
            assert r1["status"] == "success", r1.get("error")
            out1 = _find_node_run(r1, "Agent")["output"]
            assert out1["memory_turns_loaded"] == 0, out1
            assert out1["memory_key"] == key_a

            # Run 2: same key -> run 1's turn must be injected
            r2 = await _run_and_wait(client, wf_id, {"n": 2})
            out2 = _find_node_run(r2, "Agent")["output"]
            assert out2["memory_turns_loaded"] == 1, out2
            msgs = seen_messages[-1]
            # messages: system, [history...], current user
            roles = [m["role"] for m in msgs]
            assert roles == ["system", "user", "assistant", "user"], roles
            assert msgs[1]["content"] == "fact 1"  # run 1's user message
            assert "remembered" in msgs[2]["content"]  # run 1's answer

            # Different key -> isolated (no history)
            res = await client.put(
                f"/workflows/{wf_id}",
                json={"graph": {
                    "nodes": [
                        _node("t", "manual_trigger", {"payload": {}}),
                        _agent_node(
                            {
                                "memory": "buffer",
                                "session_key": key_b,
                                "user_message": "fact {{ input.payload.n }}",
                            }
                        ),
                    ],
                    "edges": [{"id": "e1", "source": "t", "target": "ag", "sourceHandle": "main", "targetHandle": "main"}],
                }},
            )
            assert res.status_code == 200, res.text
            r3 = await _run_and_wait(client, wf_id, {"n": 3})
            out3 = _find_node_run(r3, "Agent")["output"]
            assert out3["memory_turns_loaded"] == 0, out3

            # memory=none must not touch the store
            res = await client.put(
                f"/workflows/{wf_id}",
                json={"graph": {
                    "nodes": [
                        _node("t", "manual_trigger", {"payload": {}}),
                        _agent_node({"memory": "none", "session_key": key_a, "user_message": "fact 4"}),
                    ],
                    "edges": [{"id": "e1", "source": "t", "target": "ag", "sourceHandle": "main", "targetHandle": "main"}],
                }},
            )
            assert res.status_code == 200, res.text
            r4 = await _run_and_wait(client, wf_id, {})
            out4 = _find_node_run(r4, "Agent")["output"]
            assert out4["memory_key"] is None and out4["memory_turns_loaded"] == 0

    try:
        asyncio.run(_go())
    finally:
        AgentNode._chat = original_chat
        asyncio.run(_cleanup(wf_ids, [key_a, key_b]))


# ---------------------------------------------------------------------------
# 2) Memory trim: max_history_turns caps the injected history
# ---------------------------------------------------------------------------
def test_v23_agent_memory_trim():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    key = f"mem-trim-{tag}"
    seen_counts: list[int] = []

    async def _scripted_chat(agent_self, messages, temperature):
        seen_counts.append(len(messages))
        return "plain answer"

    from app.engine.nodes.agent import AgentNode

    original_chat = AgentNode._chat
    AgentNode._chat = _scripted_chat

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("t", "manual_trigger", {"payload": {}}),
                    _agent_node(
                        {
                            "memory": "buffer",
                            "session_key": key,
                            "user_message": "msg {{ input.payload.n }}",
                            "max_history_turns": 2,
                        }
                    ),
                ],
                "edges": [{"id": "e1", "source": "t", "target": "ag", "sourceHandle": "main", "targetHandle": "main"}],
            }
            wf_id = await _make_workflow(client, f"tmp v23 mem trim {tag}", graph)
            wf_ids.append(wf_id)

            for n in (1, 2, 3, 4):
                r = await _run_and_wait(client, wf_id, {"n": n})
                assert r["status"] == "success", r.get("error")

            # messages = 1 system + min(n-1, 2) turns x 2 history msgs + 1 user -> cap 6
            assert seen_counts == [2, 4, 6, 6], seen_counts

    try:
        asyncio.run(_go())
    finally:
        AgentNode._chat = original_chat
        asyncio.run(_cleanup(wf_ids, [key]))


# ---------------------------------------------------------------------------
# 3) Webhook auth - header mode: 401s do NOT run the flow
# ---------------------------------------------------------------------------
def test_v23_webhook_auth_header():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node(
                        "h",
                        "webhook_trigger",
                        {
                            "response_mode": "immediately",
                            "auth_mode": "header",
                            "auth_header_name": "X-Test-Token",
                            "auth_header_value": "s3cret",
                        },
                    ),
                    _node("s", "set_variable", {"assignments": {"ok": "1"}, "keep_input": False}),
                ],
                "edges": [{"id": "e1", "source": "h", "target": "s", "sourceHandle": "main", "targetHandle": "main"}],
            }
            wf_id = await _make_workflow(client, f"tmp v23 hook header {tag}", graph)
            wf_ids.append(wf_id)
            res = await client.post(f"/workflows/{wf_id}/activate")
            assert res.status_code == 200, res.text

            url = f"/webhooks/{wf_id}"
            # missing header -> 401, no execution
            res = await client.post(url, json={"ping": 1})
            assert res.status_code == 401, res.text
            # wrong value -> 401
            res = await client.post(url, json={"ping": 1}, headers={"X-Test-Token": "wrong"})
            assert res.status_code == 401, res.text
            # correct -> 202
            res = await client.post(url, json={"ping": 1}, headers={"X-Test-Token": "s3cret"})
            assert res.status_code == 202, res.text
            await _drain_background()

            # exactly ONE execution was created (the authorized call)
            res = await client.get("/executions", params={"workflow_id": wf_id, "limit": 10})
            assert len(res.json()) == 1, res.json()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 4) Webhook auth - basic mode incl. malformed header
# ---------------------------------------------------------------------------
def test_v23_webhook_auth_basic():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    def _basic(user: str, pwd: str) -> str:
        return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node(
                        "h",
                        "webhook_trigger",
                        {
                            "response_mode": "immediately",
                            "auth_mode": "basic",
                            "auth_user": "svc",
                            "auth_pass": "pw123",
                        },
                    ),
                    _node("s", "set_variable", {"assignments": {"ok": "1"}, "keep_input": False}),
                ],
                "edges": [{"id": "e1", "source": "h", "target": "s", "sourceHandle": "main", "targetHandle": "main"}],
            }
            wf_id = await _make_workflow(client, f"tmp v23 hook basic {tag}", graph)
            wf_ids.append(wf_id)
            res = await client.post(f"/workflows/{wf_id}/activate")
            assert res.status_code == 200, res.text

            url = f"/webhooks/{wf_id}"
            # no auth header -> 401
            res = await client.post(url, json={"ping": 1})
            assert res.status_code == 401 and "Basic auth required" in res.json()["detail"], res.text
            # malformed base64 -> 401
            res = await client.post(url, json={"ping": 1}, headers={"Authorization": "Basic !!!notb64!!!"})
            assert res.status_code == 401 and "Malformed" in res.json()["detail"], res.text
            # wrong password -> 401
            res = await client.post(url, json={"ping": 1}, headers={"Authorization": _basic("svc", "nope")})
            assert res.status_code == 401, res.text
            # correct -> 202
            res = await client.post(url, json={"ping": 1}, headers={"Authorization": _basic("svc", "pw123")})
            assert res.status_code == 202, res.text
            await _drain_background()

            res = await client.get("/executions", params={"workflow_id": wf_id, "limit": 10})
            assert len(res.json()) == 1, res.json()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 5) Definitions expose the v23 params
# ---------------------------------------------------------------------------
def test_v23_definitions():
    async def _go():
        async with _client() as client:
            res = await client.get("/node-definitions")
            defs = {d["type"]: d for d in res.json()["definitions"]}
            agent_props = defs["ai_agent"]["parameters_schema"]["properties"]
            hook_props = defs["webhook_trigger"]["parameters_schema"]["properties"]
            for k in ("memory", "session_key", "max_history_turns"):
                assert k in agent_props, k
            assert set(agent_props["memory"]["options"]) == {"none", "buffer"}
            for k in ("auth_mode", "auth_header_name", "auth_header_value", "auth_user", "auth_pass"):
                assert k in hook_props, k
            assert set(hook_props["auth_mode"]["options"]) == {"none", "header", "basic"}

    asyncio.run(_go())
