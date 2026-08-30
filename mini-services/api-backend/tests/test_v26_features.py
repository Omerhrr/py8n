"""V26 feature tests: chat progress streaming (SSE).

POST /chat/{workflow_id}/stream validates first (JSON 404/409/422), then
answers with text/event-stream frames while the flow runs:
  start {execution_id, session_id} -> node* (started/finished per node with
  durations) -> done {reply, output} | error | timeout.
The pump subscribes to the event bus BEFORE the flow task is created, so no
event is lost. On timeout the SSE closes with a timeout frame while the flow
keeps running in the background (no zombie "running" executions from the
client's perspective — the run completes and is recorded normally).

Runs the FastAPI app in-process via httpx ASGITransport (same harness as v4-v25).
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
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API, timeout=30.0)


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


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    """Parse an SSE wire dump into [(event, data_json)] pairs."""
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
    """POST the stream endpoint, read the whole body, return (status, content_type, body)."""
    async with client.stream("POST", f"/chat/{workflow_id}/stream", json={"message": message, "session_id": session_id}) as res:
        chunks: list[bytes] = []
        async for chunk in res.aiter_bytes():
            chunks.append(chunk)
        body = b"".join(chunks).decode("utf-8")
        return res.status_code, res.headers.get("content-type", ""), body


# ---------------------------------------------------------------------------
# 1) Happy path: start -> node frames for both nodes (started + finished with
#    durations, in order) -> done with the extracted reply
# ---------------------------------------------------------------------------
def test_v26_stream_progress_and_done():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    try:

        async def _go():
            async with _client() as client:
                graph = {
                    "nodes": [
                        _node("chat1", "chat_trigger", {"response_mode": "last_node"}, "Chat Trigger"),
                        _node("step1", "set_variable", {"assignments": {"mid": "1"}}, "Step One"),
                        _node("step2", "set_variable", {"assignments": {"reply": "final: {{ nodes.chat1.output.message }}"}}, "Step Two"),
                    ],
                    "edges": [_edge("e1", "chat1", "step1"), _edge("e2", "step1", "step2")],
                }
                wf_id = await _make_workflow(client, f"tmp v26 stream {tag}", graph, is_active=True)
                wf_ids.append(wf_id)

                status, ctype, body = await _collect_stream(client, wf_id, "hello stream", "sess-a")
                assert status == 200, body
                assert ctype.startswith("text/event-stream"), ctype
                frames = _parse_sse(body)
                names = [e for e, _ in frames]

                assert names[0] == "start"
                start = frames[0][1]
                assert start["session_id"] == "sess-a" and start["execution_id"]

                node_frames = [d for e, d in frames if e == "node"]
                # 3 nodes x (started + finished) = 6 node frames, in topological order
                assert len(node_frames) == 6, node_frames
                order = [d["node_name"] for d in node_frames]
                assert order == ["Chat Trigger", "Chat Trigger", "Step One", "Step One", "Step Two", "Step Two"]
                started, finished = order[0::2], order[1::2]
                assert started == finished == ["Chat Trigger", "Step One", "Step Two"]
                for d in node_frames:
                    assert d["status"] in ("running", "success")
                durations = [d.get("duration_ms") for d in node_frames[1::2]]
                assert all(isinstance(x, int) for x in durations), durations

                assert names[-1] == "done"
                done = frames[-1][1]
                assert done["status"] == "success"
                assert done["reply"] == "final: hello stream"
                assert done["session_id"] == "sess-a"
                assert done["execution_id"] == start["execution_id"]

                # the run is recorded like any chat execution
                res = await client.get(f"/executions/{start['execution_id']}")
                assert res.status_code == 200
                assert res.json()["trigger_type"] == "chat"

        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 2) Guards fire as normal JSON BEFORE any stream is established
# ---------------------------------------------------------------------------
def test_v26_stream_guards():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    try:

        async def _go():
            async with _client() as client:
                res = await client.post(f"/chat/definitely-not-{tag}/stream", json={"message": "x"})
                assert res.status_code == 404

                chat_graph = {"nodes": [_node("chat1", "chat_trigger", {}, "Chat Trigger")], "edges": []}
                inactive = await _make_workflow(client, f"tmp v26 inactive {tag}", chat_graph, is_active=False)
                wf_ids.append(inactive)
                res = await client.post(f"/chat/{inactive}/stream", json={"message": "x"})
                assert res.status_code == 409
                assert "inactive" in res.json()["detail"]
                assert "text/event-stream" not in res.headers.get("content-type", "")

                webhook_only = {"nodes": [_node("wh", "webhook_trigger", {}, "Webhook")], "edges": []}
                no_chat = await _make_workflow(client, f"tmp v26 no chat {tag}", webhook_only, is_active=True)
                wf_ids.append(no_chat)
                res = await client.post(f"/chat/{no_chat}/stream", json={"message": "x"})
                assert res.status_code == 409
                assert "no Chat Trigger" in res.json()["detail"]

                res = await client.post(f"/chat/{inactive}/stream", json={"session_id": "s"})
                assert res.status_code == 422

        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 3) respond_node mode: the custom mid-flow body lands in the done frame
# ---------------------------------------------------------------------------
def test_v26_stream_respond_node_mode():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    try:

        async def _go():
            async with _client() as client:
                graph = {
                    "nodes": [
                        _node("chat1", "chat_trigger", {"response_mode": "respond_node"}, "Chat Trigger"),
                        _node("rw", "respond_to_webhook",
                              {"status_code": 200, "content_type": "application/json",
                               "body": '{"reply": "custom: {{ nodes.chat1.output.message }}"}'}, "Respond"),
                    ],
                    "edges": [_edge("e1", "chat1", "rw")],
                }
                wf_id = await _make_workflow(client, f"tmp v26 respond {tag}", graph, is_active=True)
                wf_ids.append(wf_id)

                status, _ctype, body = await _collect_stream(client, wf_id, "mid", "sess-b")
                assert status == 200, body
                frames = _parse_sse(body)
                assert frames[-1][0] == "done"
                done = frames[-1][1]
                assert done["reply"] == "custom: mid"
                assert done["status"] == "success"

        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 4) Error path: a failing node surfaces as an error frame
# ---------------------------------------------------------------------------
def test_v26_stream_error_frame():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    try:

        async def _go():
            async with _client() as client:
                graph = {
                    "nodes": [
                        _node("chat1", "chat_trigger", {"response_mode": "last_node"}, "Chat Trigger"),
                        _node("boom", "stop_and_error",
                              {"error_message": "no {{ nodes.chat1.output.message }} allowed", "error_type": "ChatPolicy"},
                              "Boom"),
                    ],
                    "edges": [_edge("e1", "chat1", "boom")],
                }
                wf_id = await _make_workflow(client, f"tmp v26 error {tag}", graph, is_active=True)
                wf_ids.append(wf_id)

                status, _ctype, body = await _collect_stream(client, wf_id, "spam", "sess-c")
                assert status == 200, body  # the STREAM is fine; the run failed
                frames = _parse_sse(body)
                assert frames[-1][0] == "error"
                err = frames[-1][1]
                assert "ChatPolicy" in err["error"] and "no spam allowed" in err["error"]
                assert err["execution_id"]

        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))


# ---------------------------------------------------------------------------
# 5) Timeout: bounded stream closes with a timeout frame; the flow keeps
#    running and eventually records normally
# ---------------------------------------------------------------------------
def test_v26_stream_timeout_flow_keeps_running():
    tag = uuid.uuid4().hex[:8]
    wf_ids: list[str] = []
    try:

        async def _go():
            from app.config import settings

            async with _client() as client:
                graph = {
                    "nodes": [
                        _node("chat1", "chat_trigger", {"response_mode": "last_node"}, "Chat Trigger"),
                        _node("slow", "delay", {"duration_seconds": 4}, "Slow"),
                    ],
                    "edges": [_edge("e1", "chat1", "slow")],
                }
                wf_id = await _make_workflow(client, f"tmp v26 timeout {tag}", graph, is_active=True)
                wf_ids.append(wf_id)

                original = settings.webhook_wait_seconds
                settings.webhook_wait_seconds = 1
                try:
                    status, _ctype, body = await _collect_stream(client, wf_id, "slow poke", "sess-d")
                finally:
                    settings.webhook_wait_seconds = original
                assert status == 200, body
                frames = _parse_sse(body)
                assert frames[-1][0] == "timeout"
                assert frames[-1][1]["after_seconds"] == 1
                # at least the trigger started before the cutoff
                assert any(e == "node" for e, _ in frames)

                # the flow finishes in the background and is recorded
                for _ in range(80):
                    res = await client.get("/executions", params={"workflow_id": wf_id, "limit": 1})
                    payload = res.json()
                    items = payload if isinstance(payload, list) else payload.get("items") or []
                    if items and items[0].get("status") in ("success", "error"):
                        execution = items[0]
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("background flow did not finish")
                assert execution["status"] == "success"

        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(wf_ids))
