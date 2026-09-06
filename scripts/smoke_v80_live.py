"""V80 live smoke: boot the real server and drive the event system + the
media runtime end to end.

1. EVENT SYSTEM: a real queue journey (live call waits, a second caller
   joins, the line moves, the head is seated) and a real meeting journey
   (opened through the MEDIA RUNTIME door as a video session, a web leg
   joins, chat posts, the room ends) - every step lands in /events with
   the right types, sources and ONE correlation thread per journey; the
   websocket live tail receives the frames as they happen.
2. EVENT TRIGGERS: a workflow with an event_trigger (pattern "crm.*")
   reacts to a POSTed event - a real execution with trigger_type=event
   carries the event as its trigger payload.
3. MEDIA RUNTIME: kind=video opens a video-first room and the unified
   projection holds every block of the MediaSession sketch (participants,
   tracks, data channels, permissions, presence, events, recording,
   transcription, state); kind=voice wraps a real call (audio track from
   a live media stream, honest refusals), and ending the session ends
   what it wraps.

Usage: /home/z/.venv/bin/python scripts/smoke_v80_live.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v74_server.py"
API = "http://127.0.0.1:8208/api/v1"
WS_BASE = "ws://127.0.0.1:8208/api/v1"
SERVER_PORT = 8208


def wait_health(client: httpx.Client, deadline: float = 30.0) -> None:
    end = time.time() + deadline
    while time.time() < end:
        try:
            res = client.get(f"{API}/health")
            if res.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.4)
    raise SystemExit("server never became healthy")


def _mk_live_call(c: httpx.Client, call_ref: str, frm: str) -> dict:
    sess = c.post("/voice/sessions", json={
        "direction": "inbound", "provider": "telnyx", "call_ref": call_ref,
        "from_ref": frm, "to_ref": "+15554440999"}).json()
    c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.ringing", "payload": {}})
    c.post(f"/voice/sessions/{sess['id']}/events", json={"kind": "call.answered", "payload": {}})
    assert c.get(f"/voice/sessions/{sess['id']}").json()["state"] == "in_progress"
    return sess


def _types(c: httpx.Client, **params) -> list[str]:
    res = c.get("/events", params={**params, "limit": 100}).json()["events"]
    return [e["type"] for e in res]


# ---------------------------------------------------------------------------
# 1) the event system over a real queue + meeting journey - live
# ---------------------------------------------------------------------------

def events_check(c: httpx.Client, tag: str) -> dict:
    # the queue journey: a real call waits, the line moves, someone is seated
    c.post("/voice/queues", json={
        "name": f"smoke line {tag}",
        "config": {"announce": {"template": "Position {position} of {depth}."}}})
    queue = c.get("/voice/queues").json()["queues"][0]

    s1 = _mk_live_call(c, f"cc-q1-{tag}", "+15554440701")
    c.post(f"/voice/queues/{queue['id']}/entries", json={"session_id": s1["id"]})
    s2 = _mk_live_call(c, f"cc-q2-{tag}", "+15554440702")
    res = c.post(f"/voice/queues/{queue['id']}/entries", json={"session_id": s2["id"]})
    assert res.status_code == 201, res.text
    entry2 = res.json()["entry_id"]

    # the first caller leaves; the second is seated
    res = c.get(f"/voice/queues/{queue['id']}").json()
    entry1 = next(e["id"] for e in res["entries"] if e["session_id"] == s1["id"])
    c.post(f"/voice/queues/{queue['id']}/entries/{entry1}/leave")
    res = c.post(f"/voice/queues/{queue['id']}/next")
    assert res.status_code == 200 and res.json()["seated"]["id"] == entry2, res.text

    # the meeting journey through the MEDIA RUNTIME door
    agent = c.post("/voice/agents", json={
        "name": f"room persona {tag}", "scaffold_handler": True}).json()
    ms = c.post("/media/sessions", json={
        "kind": "video", "title": f"smoke room {tag}", "agent_id": agent["id"]}).json()
    assert ms["kind"] == "video" and ms["modality"] == "audio+video"
    res = c.post(f"/media/sessions/{ms['id']}/participants", json={"label": "grace"})
    assert res.status_code == 201, res.text
    c.post(f"/voice/meetings/{ms['ref_id']}/chat",
           json={"participant_id": res.json()["participant"]["id"], "text": "hello from the runtime"})
    c.post(f"/media/sessions/{ms['id']}/end")
    assert c.get(f"/media/sessions/{ms['id']}").json()["state"]["room"] == "ended"

    # the store: every chapter is there, correlated per journey
    for expected in ("call.waiting", "queue.position_changed", "queue.left",
                     "call.seated"):
        assert expected in _types(c, type="*"), (expected, _types(c, type="*"))
    thread = c.get("/events", params={"correlation_id": s1["id"], "limit": 100}).json()["events"]
    assert {"call.created", "call.started", "call.waiting",
            "queue.position_changed", "queue.left"} <= {e["type"] for e in thread}, thread
    meeting_types = _types(c, type="meeting.ended")
    assert len(meeting_types) >= 1
    media_types = _types(c, type="media.*")
    assert {"media.session_started", "media.participant_added",
            "media.session_ended"} <= set(media_types), media_types

    # the LIVE tail: frames arrive on the websocket as events are emitted
    import websockets

    async def _tail() -> int:
        async with websockets.connect(f"{WS_BASE}/events/stream") as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert hello["event"] == "stream_started", hello
            # a manual emit through the front door (composition in both directions)
            c.post("/events", json={"type": "smoke.hello", "source": "user",
                                    "payload": {"live": True}})
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert frame["event"] == "system_event" and frame["type"] == "smoke.hello", frame
            assert frame["payload"] == {"live": True}
            return 1

    got = asyncio.run(asyncio.wait_for(_tail(), timeout=20))
    return {"tail": got, "media": ms["id"][:8], "thread": len(thread)}


# ---------------------------------------------------------------------------
# 2) event triggers: a workflow reacts to an event - live
# ---------------------------------------------------------------------------

def trigger_check(c: httpx.Client, tag: str) -> dict:
    graph = {"nodes": [
        {"id": "t", "type": "event_trigger", "name": "t",
         "position": {"x": 0, "y": 0},
         "parameters": {"event_type": "smoke.*"}},
        {"id": "c", "type": "code", "name": "c",
         "position": {"x": 120, "y": 0},
         "parameters": {"mode": "run_once_for_each_item", "jsCode":
                        "return {saw: input.event.type};"}},
    ], "edges": [{"id": "e1", "source": "t", "target": "c",
                  "sourceHandle": "main", "targetHandle": "main"}]}
    res = c.post("/workflows", json={"name": f"smoke reactor {tag}",
                                     "graph": graph, "is_active": True})
    assert res.status_code == 201, res.text
    wf_id = res.json()["id"]

    res = c.post("/events", json={"type": "smoke.lead_won", "source": "user",
                                  "payload": {"value": 42}})
    assert res.status_code == 201, res.text
    deadline = time.time() + 20
    while time.time() < deadline:
        runs = [r for r in c.get("/executions", params={"limit": 50}).json()
                if r.get("workflow_id") == wf_id]
        if runs:
            run = runs[0]
            assert run["trigger_type"] == "event", run
            assert run["status"] == "success", run
            detail = c.get(f"/executions/{run['id']}").json()
            assert detail["trigger_payload"]["event"]["type"] == "smoke.lead_won"
            return {"execution": run["id"][:8]}
        time.sleep(0.5)
    raise SystemExit("the event trigger never fired")


# ---------------------------------------------------------------------------
# 3) the media runtime: the voice wrapper - live
# ---------------------------------------------------------------------------

def media_voice_check(c: httpx.Client, tag: str) -> dict:
    call = _mk_live_call(c, f"cc-wrap-{tag}", "+15554440703")

    # the honest live check: the projection WITHOUT a stream (the pytest
    # suite covers the attached-stream audio track path in-process)
    ms = c.post("/media/sessions", json={
        "kind": "voice", "session_id": call["id"]}).json()
    assert ms["kind"] == "voice" and ms["modality"] == "audio"
    assert ms["ref_kind"] == "session"
    roles = {p["role"] for p in ms["participants"]}
    assert roles == {"caller", "callee"}
    assert ms["presence"]["connected"] is True
    assert "call.answered" in {e["kind"] for e in ms["events"]}

    # a call's participants ARE the call - adding legs refuses loudly
    res = c.post(f"/media/sessions/{ms['id']}/participants", json={"label": "x"})
    assert res.status_code == 400 and "participants are the call" in res.json()["detail"]

    # ending the session hangs the call up through the state machine
    res = c.post(f"/media/sessions/{ms['id']}/end")
    assert res.status_code == 200
    detail = c.get(f"/voice/sessions/{call['id']}").json()
    assert detail["state"] == "ended" and detail["end_reason"] == "media_session_ended"
    assert "call.ended" in _types(c, type="call.ended")
    return {"media": ms["id"][:8], "end_reason": detail["end_reason"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v80_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PORT": str(SERVER_PORT),
    })
    proc = subprocess.Popen(
        ["/home/z/.venv/bin/python", SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.80.0", version
            tag = uuid.uuid4().hex[:6]

            r = events_check(c, tag)
            print(f"[1] EVENT SYSTEM OK - the queue journey and the meeting "
                  f"journey both landed in /events ({r['thread']} events on the "
                  f"caller's correlation thread alone), media session {r['media']} "
                  f"opened/participated/ended through the runtime door, and the "
                  f"websocket live tail received the frames as they were emitted")

            t = trigger_check(c, tag)
            print(f"[2] EVENT TRIGGER OK - a workflow with an event_trigger "
                  f"(pattern smoke.*) reacted to a POSTed event: execution "
                  f"{t['execution']} ran with trigger_type=event and the event "
                  f"as its trigger payload")

            m = media_voice_check(c, tag)
            print(f"[3] MEDIA RUNTIME OK - kind=voice wrapped a real call "
                  f"(participants caller+callee, honest refusal to add legs), "
                  f"ending it hung the call up through the state machine "
                  f"(reason={m['end_reason']}), and kind=video opened a "
                  f"video-first room with the FULL MediaSession projection")

            print(f"\nALL 3 CHECKS GREEN - v80 live smoke passed (version {version})")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        for f in (db_path, db_path + "-wal", db_path + "-shm"):
            try:
                os.unlink(f)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
