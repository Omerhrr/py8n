"""v108 tests - agent modules: the agent as its own resource.

* AGENT MODULES: until now an agent only existed as an ai_agent node inside
  a workflow graph, and /agents was a read-only inventory of those nodes. A
  module is the agent as a first-class resource - system prompt, provider,
  the SAME tool kit the node speaks (workflow / http / knowledge / dataset /
  code), session memory - runnable directly over POST /agents/modules/{id}/run
  with no workflow document at all. The runtime is the node's OWN machinery:
  services/agent_modules instantiates the real AgentNode as a headless shim
  and drives the same loop the graph executor drives (same wire protocol,
  same tool execution, same sandbox), so the graph path and the module path
  can never drift.
* THE LLM TRANSPORT IS SCRIPTED (AgentNode._chat replaced) exactly like
  v19/v34 - everything here runs offline; the tool execution is REAL (the
  code tool runs through the actual sandbox).
* SESSIONS: module memory lives in a per-module namespace of the v23
  AgentMemory store (``module-{id}-{key}``), so two modules (or a module
  and a graph agent) can never read each other's conversation; the sessions
  door lists them and a DELETE clears one.
* THE OLD DOOR STAYS: GET /agents still inventories graph agents.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.engine.nodes.agent import AgentNode  # noqa: E402
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    from app.api import _ratelimit

    _ratelimit.reset_all()
    yield
    _ratelimit.reset_all()


class _ScriptedLLM:
    """Replaces AgentNode._chat with a scripted reply sequence.

    Each reply may be a callable receiving the message list (so a test can
    assert the tool result really came back as a TOOL RESULT message).
    """

    def __init__(self, replies):
        self._replies = list(replies)
        self._original = AgentNode._chat
        self.calls = 0

    def __enter__(self):
        holder = self

        async def _fake_chat(agent_self, messages, temperature):
            holder.calls += 1
            reply = holder._replies.pop(0) if holder._replies else '{"answer": "done"}'
            if callable(reply):
                return reply(messages)
            return reply

        AgentNode._chat = _fake_chat  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc):
        AgentNode._chat = self._original  # type: ignore[method-assign]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


def _sync(coro):
    return asyncio.run(coro)


async def _mk_user(client: httpx.AsyncClient, tag: str) -> dict:
    email = f"v108-{tag}-{uuid.uuid4().hex[:6]}@py8n.test"
    res = await client.post("/auth/register", json={
        "email": email, "password": "correct-horse-battery", "name": f"v108 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _module_body(**over) -> dict:
    body = {
        "name": "Ops Assistant",
        "description": "answers with its tool kit",
        "system_prompt": "You are precise.",
        "memory": "buffer",
        "tools": [{
            "kind": "knowledge",
            "name": "handbook",
            "description": "the company handbook",
            "content": "Py8n ops rule one: the human picks the moment.",
        }],
    }
    body.update(over)
    return body


# ---------------------------------------------------------------------------
# 1. the module round: create -> run (real tool loop) -> memory -> sessions
# ---------------------------------------------------------------------------

def test_v108_agent_modules_crud_run_memory_and_sessions():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "owner")
            headers = _auth(user["token"])

            # openai_compatible without a credential refuses loudly
            r = await client.post("/agents/modules", headers=headers,
                                  json=_module_body(provider="openai_compatible"))
            assert r.status_code == 400, r.text

            # an invalid tool spec refuses loudly
            r = await client.post("/agents/modules", headers=headers,
                                  json=_module_body(tools=[{"kind": "nope", "name": "x"}]))
            assert r.status_code == 400, r.text

            r = await client.post("/agents/modules", headers=headers, json=_module_body())
            assert r.status_code == 201, r.text
            mod = r.json()
            assert mod["provider"] == "sandbox_bridge"
            assert mod["tool_kinds"] == ["knowledge"]

            # turn one: the model calls the knowledge tool, then answers
            with _ScriptedLLM([
                '{"tool": "handbook", "arguments": {}}',
                '{"answer": "Rule one: the human picks the moment."}',
            ]):
                r = await client.post(f"/agents/modules/{mod['id']}/run", headers=headers,
                                      json={"message": "what is rule one?", "session_key": "play"})
            assert r.status_code == 200, r.text
            turn1 = r.json()
            assert turn1["reply"] == "Rule one: the human picks the moment."
            assert turn1["iterations"] == 2
            assert [tc["tool"] for tc in turn1["tool_calls"]] == ["handbook"]
            assert turn1["tool_calls"][0]["status"] == "ok"
            assert turn1["memory_turns_loaded"] == 0
            kinds = [f["event"] for f in turn1["trace"]]
            assert kinds == ["iteration", "reply", "tool_call", "iteration", "reply", "answer"]
            assert turn1["trace"][-1]["answer"] == turn1["reply"]

            # turn two, same session: the buffer memory carries turn one in
            seen: dict = {}

            def _check_history(messages):
                seen["history_roles"] = [m["role"] for m in messages]
                return '{"answer": "I remember: rule one."}'

            with _ScriptedLLM([_check_history]):
                r = await client.post(f"/agents/modules/{mod['id']}/run", headers=headers,
                                      json={"message": "do you remember?", "session_key": "play"})
            assert r.status_code == 200, r.text
            turn2 = r.json()
            assert turn2["reply"] == "I remember: rule one."
            assert turn2["memory_turns_loaded"] == 1
            # system + 2 stored turns + the new user message
            assert seen["history_roles"] == ["system", "user", "assistant", "user"]

            # PATCH: rename + flip memory off; the run then loads nothing
            r = await client.patch(f"/agents/modules/{mod['id']}", headers=headers,
                                   json={"name": "Ops Assistant v2", "memory": "none"})
            assert r.status_code == 200, r.text
            assert r.json()["name"] == "Ops Assistant v2"
            with _ScriptedLLM(['{"answer": "fresh."}']):
                r = await client.post(f"/agents/modules/{mod['id']}/run", headers=headers,
                                      json={"message": "again", "session_key": "play"})
            assert r.json()["memory_turns_loaded"] == 0

            # sessions door: the play conversation is listed, then cleared
            r = await client.get(f"/agents/modules/{mod['id']}/sessions", headers=headers)
            assert r.status_code == 200, r.text
            sessions = r.json()
            assert [s["session_key"] for s in sessions] == ["play"]
            assert sessions[0]["turns"] == 2  # both buffered turns persist
            r = await client.delete(f"/agents/modules/{mod['id']}/sessions/play", headers=headers)
            assert r.json()["cleared"] is True
            r = await client.delete(f"/agents/modules/{mod['id']}/sessions/play", headers=headers)
            assert r.json()["cleared"] is False  # honest absence
            r = await client.get(f"/agents/modules/{mod['id']}/sessions", headers=headers)
            assert r.json() == []

            # DELETE removes the module; a second delete is a 404
            r = await client.delete(f"/agents/modules/{mod['id']}", headers=headers)
            assert r.json()["deleted"] == mod["id"]
            r = await client.get(f"/agents/modules/{mod['id']}", headers=headers)
            assert r.status_code == 404

    _sync(_go())


# ---------------------------------------------------------------------------
# 2. the code tool is REAL: the loop runs the sandbox and feeds the result
# ---------------------------------------------------------------------------

def test_v108_agent_module_code_tool_runs_the_sandbox():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "code")
            headers = _auth(user["token"])
            r = await client.post("/agents/modules", headers=headers, json=_module_body(
                name="Calculator",
                memory="none",
                tools=[{
                    "kind": "code", "name": "calc",
                    "description": "compute arithmetic",
                    "timeout_seconds": 5,
                }],
            ))
            assert r.status_code == 201, r.text
            mod = r.json()

            def _then_sum(messages):
                # the tool result must have come back as a TOOL RESULT message
                assert any("TOOL RESULT calc" in m["content"] for m in messages)
                return '{"answer": "42"}'

            with _ScriptedLLM([
                '{"tool": "calc", "arguments": {"code": "result = 6 * 7"}}',
                _then_sum,
            ]):
                r = await client.post(f"/agents/modules/{mod['id']}/run", headers=headers,
                                      json={"message": "what is six times seven?"})
            assert r.status_code == 200, r.text
            out = r.json()
            assert out["reply"] == "42"
            assert out["tool_calls"][0]["status"] == "ok"
            assert out["tool_calls"][0]["result"] == '{"result": 42, "stdout": ""}'

            # a sandbox violation surfaces as tool feedback, not a 500
            with _ScriptedLLM([
                '{"tool": "calc", "arguments": {"code": "import os"}}',
                '{"answer": "import refused"}',
            ]):
                r = await client.post(f"/agents/modules/{mod['id']}/run", headers=headers,
                                      json={"message": "try to escape"})
            assert r.status_code == 200, r.text
            out = r.json()
            assert out["tool_calls"][0]["status"] == "error"
            assert "sandbox" in out["tool_calls"][0]["result"].lower()

    _sync(_go())


# ---------------------------------------------------------------------------
# 3. ownership, the inactive door, and the old inventory still answers
# ---------------------------------------------------------------------------

def test_v108_agent_modules_ownership_and_the_old_inventory():
    async def _go():
        async with _client() as client:
            owner = await _mk_user(client, "own")
            stranger = await _mk_user(client, "stranger")

            r = await client.post("/agents/modules", headers=_auth(owner["token"]),
                                  json=_module_body(memory="none"))
            assert r.status_code == 201, r.text
            mod = r.json()

            # a foreign user's direct access looks nonexistent; the list hides it
            r = await client.get(f"/agents/modules/{mod['id']}", headers=_auth(stranger["token"]))
            assert r.status_code == 404
            r = await client.get("/agents/modules", headers=_auth(stranger["token"]))
            assert r.status_code == 200 and r.json() == []
            # the owner sees it on the list
            r = await client.get("/agents/modules", headers=_auth(owner["token"]))
            assert [m["id"] for m in r.json()] == [mod["id"]]

            # running an inactive module is a loud 409
            r = await client.patch(f"/agents/modules/{mod['id']}", headers=_auth(owner["token"]),
                                   json={"is_active": False})
            assert r.json()["is_active"] is False
            r = await client.post(f"/agents/modules/{mod['id']}/run", headers=_auth(owner["token"]),
                                  json={"message": "hello"})
            assert r.status_code == 409, r.text

            # a module with NO tools still answers (plain prose pass-through)
            r = await client.post("/agents/modules", headers=_auth(owner["token"]),
                                  json=_module_body(name="Bare", memory="none", tools=[]))
            mod2 = r.json()
            with _ScriptedLLM(["Just a plain prose answer, no tools."]):
                r = await client.post(f"/agents/modules/{mod2['id']}/run",
                                      headers=_auth(owner["token"]),
                                      json={"message": "speak"})
            assert r.status_code == 200
            assert r.json()["reply"] == "Just a plain prose answer, no tools."

            # THE OLD DOOR: the v34 inventory still lists graph agents
            wf = {"name": f"graph-agent-{uuid.uuid4().hex[:6]}", "isActive": False,
                  "graph": {"nodes": [{
                      "id": "a1", "type": "ai_agent", "name": "Graph Agent",
                      "position": {"x": 0, "y": 0},
                      "parameters": {"user_message": "hi", "tools": []},
                  }], "edges": []}}
            r = await client.post("/workflows", json=wf)
            assert r.status_code in (200, 201), r.text
            r = await client.get("/agents")
            agents = r.json()
            assert any("Graph Agent" in a.get("agent_nodes", []) for a in agents)

    _sync(_go())


# ---------------------------------------------------------------------------
# 4. version pin
# ---------------------------------------------------------------------------

def test_v108_version_pin():
    assert settings.version == "1.111.0"
