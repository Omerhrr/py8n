"""V25 feature tests: Chat Trigger + the /chat endpoint (conversational workflows).

Each chat message POSTed to /api/v1/chat/{workflow_id} starts one run. The
reply comes from the last node's output (response_mode=last_node, default,
extracted to a plain-text "reply") or from a Respond to Webhook node mid-flow
(response_mode=respond_node — the v21 WebhookResponder channel reused).
Chat workflows must be ACTIVE and contain a chat_trigger node; the runner's
_pick_trigger map gained "chat" so chat runs pick the Chat Trigger even when
a Manual Trigger also sits on the canvas. session_id flows into the run so
downstream agent nodes can key their per-session memory on it.

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v24).
"""

from __future__ import annotations

import asyncio
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


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _make_workflow(client: httpx.AsyncClient, name: str, graph: dict, is_active: bool = False) -> str:
    res = await client.post("/workflows", json={"name": name, "graph": graph, "is_active": is_active})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _find_node_run(execution: dict, node_name: str) -> dict | None:
    for run in execution.get("node_runs") or []:
        if run.get("node_name") == node_name:
            return run
    return None


# ---------------------------------------------------------------------------
# 1) Definitions: chat_trigger exposed with its params (30 visible types)
# ---------------------------------------------------------------------------
def test_v25_definitions_expose_chat_trigger():
    async def _go():
        async with _client() as client:
            res = await client.get("/node-definitions")
            assert res.status_code == 200
            defs = res.json()["definitions"]
            types = [d["type"] for d in defs]
            assert len(types) == 37, f"expected 37 visible types, got {len(types)}"
            chat = next(d for d in defs if d["type"] == "chat_trigger")
            assert chat["category"] == "triggers"
            assert chat["inputs"] == []  # triggers have no input ports
            params = chat["parameters_schema"]["properties"]
            assert params["response_mode"]["options"] == ["last_node", "respond_node"]
            assert "welcome_message" in params

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# 2) Happy path: last_node mode replies with the final node output,
#    trigger output carries message + session_id, execution records chat trigger
# ---------------------------------------------------------------------------
def test_v25_chat_last_node_reply_and_execution_record():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("chat1", "chat_trigger", {"response_mode": "last_node"}, "Chat Trigger"),
                    _node(
                        "rep",
                        "set_variable",
                        {"assignments": {"reply": "Echo: {{ nodes.chat1.output.message }} (session {{ nodes.chat1.output.session_id }})"}},
                        "Reply",
                    ),
                ],
                "edges": [_edge("e1", "chat1", "rep")],
            }
            wf_id = await _make_workflow(client, f"tmp v25 chat echo {tag}", graph, is_active=True)
            wf_ids.append(wf_id)

            res = await client.post(
                f"/chat/{wf_id}",
                json={"message": "hello bot", "session_id": "sess-1"},
            )
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["status"] == "success"
            assert body["session_id"] == "sess-1"
            assert body["reply"] == "Echo: hello bot (session sess-1)"

            # the execution record exists and is attributed to the chat trigger
            res = await client.get(f"/executions/{body['execution_id']}")
            assert res.status_code == 200
            execution = res.json()
            assert execution["trigger_type"] == "chat"
            trig = _find_node_run(execution, "Chat Trigger")
            assert trig is not None and trig["status"] == "success"
            assert trig["output"]["message"] == "hello bot"
            assert trig["output"]["session_id"] == "sess-1"
            assert trig["output"]["trigger_type"] == "chat"

            # default session_id when omitted
            res = await client.post(f"/chat/{wf_id}", json={"message": "anon"})
            assert res.status_code == 200
            assert res.json()["session_id"] == "default"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 3) respond_node mode: Respond to Webhook answers the chat client mid-flow,
#    the flow continues downstream afterwards
# ---------------------------------------------------------------------------
def test_v25_chat_respond_node_mode():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("chat1", "chat_trigger", {"response_mode": "respond_node"}, "Chat Trigger"),
                    _node(
                        "rw",
                        "respond_to_webhook",
                        {"status_code": 200, "body": '{"reply": "custom: {{ nodes.chat1.output.message }}", "via": "respond"}', "content_type": "application/json"},
                        "Respond",
                    ),
                    _node("after", "set_variable", {"assignments": {"post_reply": "kept working"}}, "After"),
                ],
                "edges": [_edge("e1", "chat1", "rw"), _edge("e2", "rw", "after")],
            }
            wf_id = await _make_workflow(client, f"tmp v25 chat respond {tag}", graph, is_active=True)
            wf_ids.append(wf_id)

            res = await client.post(f"/chat/{wf_id}", json={"message": "mid", "session_id": "s"})
            assert res.status_code == 200, res.text
            assert res.json() == {"reply": "custom: mid", "via": "respond"}

            # the flow keeps running after answering — poll the execution
            for _ in range(100):
                res = await client.get("/executions", params={"workflow_id": wf_id, "limit": 1})
                assert res.status_code == 200
                payload = res.json()
                items = payload if isinstance(payload, list) else (payload.get("items") or payload.get("executions") or [])
                if items and items[0].get("status") in ("success", "error"):
                    execution = items[0]
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("execution did not finish in time")
            assert execution["status"] == "success"
            assert execution["trigger_type"] == "chat"
            # list items are summaries — fetch the detail for node runs
            res = await client.get(f"/executions/{execution['id']}")
            assert res.status_code == 200
            detail = res.json()
            after_run = _find_node_run(detail, "After")
            assert after_run is not None and after_run["status"] == "success"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 4) Guard rails: unknown id -> 404, inactive -> 409, no chat trigger -> 409,
#    missing message -> 422 (no execution created)
# ---------------------------------------------------------------------------
def test_v25_chat_guard_rails():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            res = await client.post(f"/chat/definitely-not-{tag}", json={"message": "x"})
            assert res.status_code == 404

            chat_graph = {
                "nodes": [_node("chat1", "chat_trigger", {}, "Chat Trigger")],
                "edges": [],
            }
            inactive = await _make_workflow(client, f"tmp v25 inactive {tag}", chat_graph, is_active=False)
            wf_ids.append(inactive)
            res = await client.post(f"/chat/{inactive}", json={"message": "x"})
            assert res.status_code == 409
            assert "inactive" in res.json()["detail"]

            webhook_only = {
                "nodes": [_node("wh", "webhook_trigger", {}, "Webhook")],
                "edges": [],
            }
            no_chat = await _make_workflow(client, f"tmp v25 no chat {tag}", webhook_only, is_active=True)
            wf_ids.append(no_chat)
            res = await client.post(f"/chat/{no_chat}", json={"message": "x"})
            assert res.status_code == 409
            assert "no Chat Trigger" in res.json()["detail"]

            # validation: message is required (422, no run started)
            res = await client.post(f"/chat/{inactive}", json={"session_id": "s"})
            assert res.status_code == 422

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 5) Deterministic trigger pick: with BOTH a Manual and a Chat Trigger on the
#    canvas, a chat run starts from the Chat Trigger (manual is skipped)
# ---------------------------------------------------------------------------
def test_v25_chat_trigger_picked_over_manual():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("man", "manual_trigger", {"payload": {}}, "Manual Trigger"),
                    _node("chat1", "chat_trigger", {"response_mode": "last_node"}, "Chat Trigger"),
                    _node("rep", "set_variable", {"assignments": {"reply": "{{ nodes.chat1.output.message }}"}}, "Reply"),
                ],
                "edges": [_edge("e1", "chat1", "rep")],
            }
            wf_id = await _make_workflow(client, f"tmp v25 dual triggers {tag}", graph, is_active=True)
            wf_ids.append(wf_id)

            res = await client.post(f"/chat/{wf_id}", json={"message": "pick me", "session_id": "s"})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["status"] == "success"
            assert body["reply"] == "pick me"  # set_variable emits {reply: ...}; extract_reply keys on "reply"

            res = await client.get(f"/executions/{body['execution_id']}")
            execution = res.json()
            manual_run = _find_node_run(execution, "Manual Trigger")
            assert manual_run is not None
            assert manual_run["status"] == "skipped"  # "trigger not fired"
            chat_run = _find_node_run(execution, "Chat Trigger")
            assert chat_run is not None and chat_run["status"] == "success"

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 6) Session synergy: agent memory keyed on the chat session_id — same session
#    recalls the earlier turn, a different session does not
# ---------------------------------------------------------------------------
def test_v25_chat_session_memory_synergy():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    mem_key_base = f"chat-mem-{tag}"
    seen_messages: list[list[dict]] = []

    async def _scripted_chat(agent_self, messages, temperature):
        seen_messages.append([dict(m) for m in messages])
        return 'noted {"answer": "got it"}'

    from app.engine.nodes.agent import AgentNode

    original_chat = AgentNode._chat
    AgentNode._chat = _scripted_chat

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    _node("chat1", "chat_trigger", {"response_mode": "last_node"}, "Chat Trigger"),
                    _node(
                        "ag",
                        "ai_agent",
                        {
                            "memory": "buffer",
                            "session_key": f"{mem_key_base}-{{{{ nodes.chat1.output.session_id }}}}",
                            "user_message": "{{ nodes.chat1.output.message }}",
                            "max_history_turns": 5,
                        },
                        "Agent",
                    ),
                ],
                "edges": [_edge("e1", "chat1", "ag")],
            }
            wf_id = await _make_workflow(client, f"tmp v25 chat memory {tag}", graph, is_active=True)
            wf_ids.append(wf_id)

            # run 1 — session alpha: no prior turns
            res = await client.post(f"/chat/{wf_id}", json={"message": "my name is Ada", "session_id": "alpha"})
            assert res.status_code == 200, res.text
            first = res.json()
            assert first["status"] == "success"
            assert len(seen_messages) == 1
            assert sum(1 for m in seen_messages[0] if m["role"] == "user") == 1

            # run 2 — same session: the prior turn is injected
            res = await client.post(f"/chat/{wf_id}", json={"message": "what is my name?", "session_id": "alpha"})
            assert res.status_code == 200
            assert len(seen_messages) == 2
            roles2 = [m["role"] for m in seen_messages[1]]
            assert roles2.count("user") == 2  # prior "my name is Ada" + current
            prior_user = [m["content"] for m in seen_messages[1] if m["role"] == "user"][0]
            assert "Ada" in prior_user

            # run 3 — different session: isolated, no recall
            res = await client.post(f"/chat/{wf_id}", json={"message": "hello", "session_id": "beta"})
            assert res.status_code == 200
            assert len(seen_messages) == 3
            assert sum(1 for m in seen_messages[2] if m["role"] == "user") == 1

    try:
        asyncio.run(_go())
    finally:
        AgentNode._chat = original_chat
        asyncio.run(_cleanup(wf_ids, memory_keys=[f"{mem_key_base}-alpha", f"{mem_key_base}-beta"]))
