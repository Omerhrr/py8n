"""V36 feature tests: live agent streaming (SSE trace of the tool loop).

The AI Agent node now publishes fine-grained ``agent_*`` events onto the
execution bus while it thinks:

    agent_iteration  {iteration, max_iterations}      - loop round begins
    agent_reply      {iteration, reply}               - the model's raw reply
    agent_tool_call  {iteration, tool, arguments}     - before the tool runs
    agent_tool_result{iteration, tool, status, preview} - after it finishes
    agent_answer     {answer}                         - final answer

POST /chat/{id}/stream translates them into ``event: agent`` SSE frames
({phase, ...}) so the Agent Console renders the trace LIVE. The runner wires
``context.emit`` for every run; absent/bus-dead emits can never fail a node.

Same harness as v4-v35: httpx ASGITransport in-process, per-test asyncio.run,
uuid-suffixed names, finally-cleanup + background drain; ``AgentNode._chat``
is scripted so the whole wave runs offline.
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


async def _cleanup(workflow_ids: list[str]) -> None:
    async with _client() as client:
        for wid in workflow_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
    await _drain_background()


class _ScriptedChat:
    """Replaces AgentNode._chat with a scripted reply sequence (v34 pattern)."""

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


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        event_name, data_line = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_name = line[7:].strip()
            elif line.startswith("data: "):
                data_line = line[6:]
        if event_name and data_line:
            frames.append((event_name, json.loads(data_line)))
    return frames


async def _collect_stream(client: httpx.AsyncClient, workflow_id: str, message: str, session_id: str) -> tuple[int, str, str]:
    async with client.stream("POST", f"/chat/{workflow_id}/stream", json={"message": message, "session_id": session_id}) as res:
        chunks: list[bytes] = []
        async for chunk in res.aiter_bytes():
            chunks.append(chunk)
        return res.status_code, res.headers.get("content-type", ""), b"".join(chunks).decode()


# ------------------------------------------------------------------ test 1
def test_v36_health_pin():
    """Strict version pin lives in the latest wave only (v36 convention)."""

    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["app"] == "Py8n" and body["version"] == "1.36.0", body

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())


# ------------------------------------------------------------------ test 2
def test_v36_agent_stream_frames():
    """The stream endpoint emits the full live trace for a tool-calling turn.

    Scripted loop: iteration 1 calls a knowledge tool, iteration 2 answers.
    Expected agent frame phases: iteration, reply, tool_call, tool_result,
    iteration, reply, answer - then the terminal done frame carries the reply.
    """
    tag = uuid.uuid4().hex[:8]
    scripted = _ScriptedChat([
        '{"tool": "lookup", "arguments": {"q": "py8n"}}',
        '{"answer": "streamed ok"}',
    ])
    wf_ids: list[str] = []
    try:
        scripted.install()

        async def _go():
            async with _client() as client:
                graph = _agent_graph(
                    [{"kind": "knowledge", "name": "lookup", "description": "static facts", "content": "Py8n is a Python Data OS"}],
                    "What is Py8n?",
                )
                res = await client.post("/workflows", json={"name": f"tmp v36 stream {tag}", "graph": graph})
                assert res.status_code == 201, res.text
                wf = res.json()
                wf_ids.append(wf["id"])
                res = await client.post(f"/workflows/{wf['id']}/activate")
                assert res.status_code == 200, res.text

                status, ctype, body = await _collect_stream(client, wf["id"], "What is Py8n?", "v36-sess")
                assert status == 200, body
                assert ctype.startswith("text/event-stream"), ctype
                frames = _parse_sse(body)
                names = [e for e, _ in frames]

                assert names[0] == "start"
                assert names[-1] == "done"
                agent_frames = [d for e, d in frames if e == "agent"]
                phases = [d.get("phase") for d in agent_frames]
                assert phases == [
                    "iteration", "reply", "tool_call", "tool_result",
                    "iteration", "reply", "answer",
                ], phases
                # iteration framing
                assert agent_frames[0]["iteration"] == 1 and agent_frames[0]["max_iterations"] == 5
                assert agent_frames[4]["iteration"] == 2
                # the model's raw reply streams before any tool runs
                assert "lookup" in agent_frames[1]["reply"]
                # tool call frame carries the tool + its arguments
                call = agent_frames[2]
                assert call["tool"] == "lookup" and "py8n" in call["arguments"]
                # tool result frame carries ok + the knowledge content
                result = agent_frames[3]
                assert result["tool"] == "lookup" and result["status"] == "ok"
                assert "Python Data OS" in result["preview"]
                # final answer frame + done frame agree
                assert agent_frames[-1]["answer"] == "streamed ok"
                done = frames[-1][1]
                assert done["status"] == "success"
                assert done["reply"] == "streamed ok"
                assert done["execution_id"] == frames[0][1]["execution_id"]
                # every agent frame carries the execution id for correlation
                assert all(d.get("execution_id") for d in agent_frames)

        asyncio.run(_go())
    finally:
        scripted.restore()
        asyncio.run(_cleanup(wf_ids))


# ------------------------------------------------------------------ test 3
def test_v36_editor_run_unaffected():
    """The editor Run path (same emit machinery) still succeeds and records
    the full tool_calls + answer on the node output - live events are
    additive, never breaking."""
    tag = uuid.uuid4().hex[:8]
    scripted = _ScriptedChat(['{"answer": "plain run fine"}'])
    wf_ids: list[str] = []
    try:
        scripted.install()

        async def _go():
            async with _client() as client:
                graph = _agent_graph([], "hello")
                res = await client.post("/workflows", json={"name": f"tmp v36 run {tag}", "graph": graph})
                assert res.status_code == 201, res.text
                wf = res.json()
                wf_ids.append(wf["id"])
                res = await client.post(f"/workflows/{wf['id']}/run", json={"payload": {}})
                assert res.status_code in (200, 202), res.text
                exec_id = res.json()["execution_id"]
                for _ in range(120):
                    res = await client.get(f"/executions/{exec_id}")
                    assert res.status_code == 200, res.text
                    if res.json()["status"] != "running":
                        break
                    await asyncio.sleep(0.05)
                detail = res.json()
                assert detail["status"] == "success", detail.get("error")
                runs = {r["node_id"]: r for r in detail["node_runs"]}
                out = runs["agent"]["output"]
                assert out["answer"] == "plain run fine"
                assert out["iterations"] == 1 and out["tool_calls"] == []

        asyncio.run(_go())
    finally:
        scripted.restore()
        asyncio.run(_cleanup(wf_ids))


# ------------------------------------------------------------------ test 4
def test_v36_stream_rejects_non_agent_workflow():
    """A workflow with neither chat trigger nor agent gets the same JSON 409
    on the stream endpoint (validation happens BEFORE any stream starts)."""
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            graph = {
                "nodes": [
                    {"id": "t", "type": "manual_trigger", "name": "T", "position": {"x": 0, "y": 0}, "parameters": {}},
                    {"id": "s", "type": "set_variable", "name": "S", "position": {"x": 200, "y": 0}, "parameters": {"assignments": {"x": "1"}}},
                ],
                "edges": [{"id": "e", "source": "t", "target": "s", "sourceHandle": "main", "targetHandle": "main"}],
            }
            res = await client.post("/workflows", json={"name": f"tmp v36 plain {tag}", "graph": graph})
            assert res.status_code == 201, res.text
            wf = res.json()
            try:
                res = await client.post(f"/workflows/{wf['id']}/activate")
                assert res.status_code == 200, res.text
                status, ctype, body = await _collect_stream(client, wf["id"], "hi", "sess")
                assert status == 409, (status, body[:200])
                assert "no Chat Trigger or AI Agent" in body
            finally:
                await client.delete(f"/workflows/{wf['id']}")

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_drain_background())
