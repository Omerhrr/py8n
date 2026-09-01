"""V40 feature tests: dataset row previews inside the live agent trace.

New machinery:
    AgentNode._run_tool now returns the STRUCTURED tool value; stringification
    moved to the caller (the loop) so the same value can feed both the model
    feedback (truncated JSON, unchanged) and a compact preview for the trace.
    agent_tool_result frames gain an optional "data" object for dataset-shaped
    payloads: {columns (8 max), rows (first 3, cells capped at 60 chars),
    total_rows, rows_shown, columns_shown, columns_total}.
    The /chat/{id}/stream bridge forwards "data" on the agent SSE frame and the
    Agent Console renders it as a real mini table instead of a JSON blob.

Same harness as v4-v39: httpx ASGITransport in-process, per-test asyncio.run,
uuid-suffixed data, finally-cleanup + background drain; ``AgentNode._chat``
is scripted so the whole wave runs offline.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx

from app.main import app

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod

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
        AgentNode._chat = _fake_chat  # type: ignore[(method-assign]

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
def test_v40_health_pin():
    """Strict version pin lives in the latest wave only (convention)."""

    async def _go():
        async with _client() as client:
            res = await client.get("/health")
            assert res.status_code == 200, res.text
            return res.json()

    body = asyncio.run(_go())
    assert body["app"] == "Py8n"
    assert body["version"] == "1.40.0", f"expected strict pin 1.40.0, got {body['version']}"


# ------------------------------------------------------------------ test 2
def test_v40_dataset_rows_stream_in_trace():
    """A dataset tool call streams its tool_result with a structured row
    preview: columns + first rows + totals, end to end through SSE."""
    tag = uuid.uuid4().hex[:8]
    rows = [
        {"city": "lima", "temp": 19},
        {"city": "oslo", "temp": -3},
        {"city": "cairo", "temp": 35},
        {"city": "kyiv", "temp": 9},
    ]
    scripted = _ScriptedChat([
        '{"tool": "cities", "arguments": {"sql": "SELECT city, temp FROM ' + f"ds_{tag}" + '"}}',
        '{"answer": "preview done"}',
    ])
    wf_ids: list[str] = []
    ds_ids: list[str] = []
    try:
        scripted.install()

        async def _go():
            async with _client() as client:
                res = await client.post("/datasets", json={"name": f"ds_{tag}", "rows": rows})
                assert res.status_code == 201, res.text
                ds = res.json()
                ds_ids.append(ds["id"])

                graph = _agent_graph(
                    [{"kind": "dataset", "name": "cities", "description": "query the cities table", "max_rows": 25}],
                    "Which cities are in the table?",
                )
                res = await client.post("/workflows", json={"name": f"tmp v40 preview {tag}", "graph": graph})
                assert res.status_code == 201, res.text
                wf = res.json()
                wf_ids.append(wf["id"])
                res = await client.post(f"/workflows/{wf['id']}/activate")
                assert res.status_code == 200, res.text

                status, ctype, body = await _collect_stream(client, wf["id"], "Which cities?", "v40-sess")
                assert status == 200, body
                assert ctype.startswith("text/event-stream"), ctype
                frames = _parse_sse(body)
                agent_frames = [d for e, d in frames if e == "agent"]
                phases = [d.get("phase") for d in agent_frames]
                assert phases == [
                    "iteration", "reply", "tool_call", "tool_result",
                    "iteration", "reply", "answer",
                ], phases

                result = agent_frames[3]
                assert result["tool"] == "cities" and result["status"] == "ok"
                # v40: the structured row preview rides the SAME frame
                data = result.get("data")
                assert isinstance(data, dict), result
                assert data["columns"] == ["city", "temp"]
                assert data["total_rows"] == 4
                assert data["rows_shown"] == 3
                assert data["columns_total"] == 2 and data["columns_shown"] == 2
                assert data["rows"][0] == ["lima", "19"]
                assert data["rows"][2] == ["cairo", "35"]
                # the string preview is still present for the raw view
                assert "lima" in result["preview"]
                # the model still got its JSON feedback (unchanged behaviour)
                model_msgs = scripted.calls[-1]
                tool_feedback = [m for m in model_msgs if str(m.get("content", "")).startswith("TOOL RESULT")]
                assert tool_feedback and "oslo" in tool_feedback[0]["content"]

                done = frames[-1][1]
                assert done["status"] == "success" and done["reply"] == "preview done"

        asyncio.run(_go())
    finally:
        scripted.restore()
        asyncio.run(_cleanup(wf_ids, ds_ids))


# ------------------------------------------------------------------ test 3
def test_v40_data_preview_shapes():
    """_data_preview is picky: only dataset-shaped payloads qualify; caps apply."""
    from app.engine.nodes.agent import AgentNode

    # non-dict payloads never preview
    assert AgentNode._data_preview(None) is None
    assert AgentNode._data_preview("plain text") is None
    assert AgentNode._data_preview({"result": 42, "stdout": "42"}) is None  # code tool shape
    # missing columns or rows -> None
    assert AgentNode._data_preview({"columns": ["a"]}) is None
    assert AgentNode._data_preview({"rows": [{"a": 1}]}) is None
    assert AgentNode._data_preview({"columns": [], "rows": []}) is None

    # happy path: caps + cell truncation + fallback totals
    long = "x" * 200
    value = {
        "columns": [f"c{i}" for i in range(12)],
        "rows": [{f"c{i}": long for i in range(12)} for _ in range(5)],
        "row_count": 99,
        "duration_ms": 3,
    }
    preview = AgentNode._data_preview(value)
    assert preview is not None
    assert len(preview["columns"]) == 8 and preview["columns_total"] == 12
    assert len(preview["rows"]) == 3 and preview["total_rows"] == 99 and preview["rows_shown"] == 3
    assert all(len(cell) == 60 for row in preview["rows"] for cell in row)

    # row_count absent -> falls back to len(rows); non-numeric -> fallback too
    assert AgentNode._data_preview({"columns": ["a"], "rows": [{"a": 1}]})["total_rows"] == 1
    assert AgentNode._data_preview({"columns": ["a"], "rows": [], "row_count": "many"})["total_rows"] == 0
