"""V80 feature tests: the real-time event system and the media runtime.

* EVENT SYSTEM: every real-time primitive emits into one event store
  (system_events) - calls (created/started/ended), queues (waiting/seated/
  left/position_changed), SMS (sent/received), meetings (participant.joined,
  chat.posted, hand.raised, floor.granted, meeting.ended), video (track
  published/unpublished), recordings (started/ready), media (session
  started/ended) and the manual door (POST /events). Events are
  owner-scoped, filterable (type prefix/pattern, source, session,
  correlation) and carry the user's exact shape: source, type, actor,
  target, timestamp, payload, correlation_id, session_id.
* EVENT TRIGGERS: a workflow with an event_trigger node (type pattern
  "crm.*") is dispatched fire-and-forget when a matching event lands -
  the run is a real execution with trigger_type=event and the event as
  the trigger payload; non-matching events dispatch nothing.
* MEDIA RUNTIME: one MediaSession abstraction over calls and rooms -
  kind=voice wraps a call, kind=video opens a video-first room, kind=
  meeting an audio room - with the FULL unified projection (participants,
  audio/video/screen tracks, data channels, permissions, presence,
  events, recording, transcription, state) derived at read time from the
  underlying primitive. Ending the session ends the wrapped primitive;
  adding legs to a call refuses honestly.

Runs the FastAPI app in-process (httpx ASGITransport). No network egress.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.main import app

API = "http://testserver/api/v1"


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    from app.api import _ratelimit

    _ratelimit.reset_all()
    yield
    _ratelimit.reset_all()


@pytest.fixture(autouse=True)
def _clean_event_subscribers():
    """The live-tail hub is process state - keep scenarios isolated."""
    from app.services import system_events as events_svc

    events_svc._subscribers.clear()
    yield
    events_svc._subscribers.clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod
    from app.services import system_events as events_svc

    # the after-commit dispatch tasks first (they spawn the executor's runs)
    for _ in range(5):
        tasks = [t for t in events_svc._DISPATCH_TASKS if not t.done()]
        if not tasks:
            break
        await asyncio.gather(*tasks, return_exceptions=True)
    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _sync(coro):
    return asyncio.run(coro)


async def _wrap(coro):
    try:
        return await coro
    finally:
        await _drain_background()


async def _mk_user(client: httpx.AsyncClient, tag: str, n: int = 1) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v80-{tag}-u{n}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v80 u{n} {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid,
            "position": {"x": 0, "y": 0}, "parameters": params or {}}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target,
            "sourceHandle": "main", "targetHandle": "main"}


async def _mk_live_session(client: httpx.AsyncClient, headers: dict,
                           frm: str = "+15550001111") -> dict:
    res = await client.post("/voice/sessions", headers=headers, json={
        "direction": "inbound", "provider": "telnyx", "call_ref": f"cc-{frm}",
        "from_ref": frm, "to_ref": "+15550002222"})
    assert res.status_code == 201, res.text
    sid = res.json()["id"]
    res = await client.post(f"/voice/sessions/{sid}/events", headers=headers,
                            json={"kind": "call.ringing", "payload": {}})
    assert res.status_code == 200, res.text
    res = await client.post(f"/voice/sessions/{sid}/events", headers=headers,
                            json={"kind": "call.answered", "payload": {}})
    assert res.status_code == 200, res.text
    res = await client.get(f"/voice/sessions/{sid}", headers=headers)
    return res.json()


async def _mk_agent(client: httpx.AsyncClient, headers: dict, name: str) -> dict:
    res = await client.post("/voice/agents", headers=headers, json={
        "name": name, "scaffold_handler": True})
    assert res.status_code == 201, res.text
    return res.json()


# ---------------------------------------------------------------------------
# 1. emit / list / scope / validation
# ---------------------------------------------------------------------------

def test_v80_event_emit_list_scope_validation():
    async def _go():
        async with _client() as client:
            a = await _mk_user(client, "ev")
            b = await _mk_user(client, "ev", n=2)
            ha, hb = _auth(a["token"]), _auth(b["token"])

            # the manual door: an operator (or a workflow's http node) emits
            res = await client.post("/events", headers=ha, json={
                "type": "crm.lead_won", "source": "user",
                "actor": "acme-crm", "target_type": "lead", "target_id": "L-1",
                "payload": {"value": 4200, "owner": "kim"},
                "correlation_id": "journey-1"})
            assert res.status_code == 201, res.text
            ev = res.json()
            assert ev["type"] == "crm.lead_won" and ev["source"] == "user"
            assert ev["actor"] == "acme-crm" and ev["target_id"] == "L-1"
            assert ev["payload"]["value"] == 4200
            assert ev["correlation_id"] == "journey-1"
            assert ev["created_at"] and ev["id"]

            # owner scoping: B sees nobody else's events
            res = await client.get("/events", headers=hb)
            assert res.status_code == 200
            assert res.json()["events"] == []
            res = await client.get("/events", headers=ha)
            types = [e["type"] for e in res.json()["events"]]
            assert "crm.lead_won" in types

            # a second event with the same session correlation
            res = await client.post("/events", headers=ha, json={
                "type": "crm.email_sent", "source": "user",
                "correlation_id": "journey-1"})
            assert res.status_code == 201

            # type prefix + pattern + source + correlation filters
            res = await client.get("/events", headers=ha,
                                   params={"type": "crm."})
            assert len(res.json()["events"]) == 2
            res = await client.get("/events", headers=ha,
                                   params={"type": "crm.*", "source": "user"})
            assert len(res.json()["events"]) == 2
            res = await client.get("/events", headers=ha,
                                   params={"type": "crm.*", "source": "queue"})
            assert res.json()["events"] == []
            res = await client.get("/events", headers=ha,
                                   params={"correlation_id": "journey-1"})
            assert len(res.json()["events"]) == 2

            # one event, fetched by id
            res = await client.get(f"/events/{ev['id']}", headers=ha)
            assert res.status_code == 200 and res.json()["id"] == ev["id"]
            # B cannot fetch A's event by id
            res = await client.get(f"/events/{ev['id']}", headers=hb)
            assert res.status_code == 404

            # validation refuses loudly: undotted, unknown source;
            # uppercase is NORMALIZED to lowercase (kinds/modes lowercase too)
            res = await client.post("/events", headers=ha, json={
                "type": "undotted", "source": "user"})
            assert res.status_code == 400 and "dotted" in res.json()["detail"]
            res = await client.post("/events", headers=ha, json={
                "type": "CRM.LEAD_WON", "source": "user"})
            assert res.status_code == 201
            assert res.json()["type"] == "crm.lead_won"
            res = await client.post("/events", headers=ha, json={
                "type": "crm.lead_won", "source": "facebook"})
            assert res.status_code == 400 and "not an event source" in res.json()["detail"]

            # the contract endpoint documents the catalog
            res = await client.get("/events/contracts")
            assert res.status_code == 200
            contracts = res.json()
            assert "voice" in contracts["sources"]
            assert "call.waiting" in contracts["types_emitted"]["queue"]
            assert "participant.joined" in contracts["types_emitted"]["meeting"]
            assert contracts["trigger_node"]["type"] == "event_trigger"

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the live tail hub
# ---------------------------------------------------------------------------

def test_v80_event_live_tail():
    async def _go():
        from app.services import system_events as events_svc

        async def _flow():
            async with _client() as client:
                user = await _mk_user(client, "tail")
                h = _auth(user["token"])
                q = events_svc.subscribe(user["id"])
                try:
                    # someone ELSE's event never arrives on this tail
                    other = await _mk_user(client, "tail", n=2)
                    await client.post("/events", headers=_auth(other["token"]),
                                      json={"type": "noise.event", "source": "user"})
                    await client.post("/events", headers=h, json={
                        "type": "tail.hello", "source": "user",
                        "payload": {"n": 1}})
                    frame = await asyncio.wait_for(q.get(), timeout=5)
                    assert frame["type"] == "tail.hello"
                    assert frame["payload"] == {"n": 1}
                    assert frame["owner_id"] == user["id"]
                    assert events_svc.live_subscriber_count() == 1
                finally:
                    events_svc.unsubscribe(user["id"], q)
                    assert events_svc.live_subscriber_count() == 0

        await asyncio.wait_for(_flow(), timeout=30)

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. event triggers: workflows react to events
# ---------------------------------------------------------------------------

def test_v80_event_trigger_dispatch():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "trig")
            h = _auth(user["token"])

            # the reacting workflow: event_trigger (pattern crm.*) -> code
            graph = {"nodes": [
                _node("t", "event_trigger", {"event_type": "crm.*"}),
                _node("c", "code",
                      {"mode": "run_once_for_each_item", "jsCode":
                       "return {saw: input.event.type, value: input.event.payload.value};"}),
            ], "edges": [_edge("e1", "t", "c")]}
            res = await client.post("/workflows", headers=h, json={
                "name": "react to crm", "graph": graph, "is_active": True})
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]

            # a matching event dispatches the workflow
            res = await client.post("/events", headers=h, json={
                "type": "crm.lead_won", "source": "user",
                "payload": {"value": 4200}})
            assert res.status_code == 201, res.text
            await _drain_background()
            res = await client.get("/executions", headers=h, params={"limit": 20})
            runs = [r for r in res.json() if r.get("workflow_id") == wf_id]
            assert len(runs) == 1, res.text
            run = runs[0]
            assert run["trigger_type"] == "event"
            assert run["status"] == "success", run.get("error")
            # the run's trigger payload IS the event
            res = await client.get(f"/executions/{run['id']}", headers=h)
            assert res.status_code == 200
            assert res.json()["trigger_payload"]["event"]["type"] == "crm.lead_won"
            assert res.json()["trigger_payload"]["event"]["payload"] == {"value": 4200}

            # a NON-matching event dispatches nothing
            res = await client.post("/events", headers=h, json={
                "type": "other.thing", "source": "user"})
            assert res.status_code == 201
            await _drain_background()
            res = await client.get("/executions", headers=h, params={"limit": 20})
            runs = [r for r in res.json() if r.get("workflow_id") == wf_id]
            assert len(runs) == 1

            # an inactive workflow never reacts
            res = await client.post("/workflows", headers=h, json={
                "name": "sleepy", "graph": graph, "is_active": False})
            assert res.status_code == 201
            res = await client.post("/events", headers=h, json={
                "type": "crm.refund", "source": "user"})
            assert res.status_code == 201
            await _drain_background()
            res = await client.get("/executions", headers=h, params={"limit": 20})
            runs = [r for r in res.json()
                    if r.get("workflow_name") == "sleepy"]
            assert runs == []

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the call's life lands in the event system
# ---------------------------------------------------------------------------

def test_v80_call_lifecycle_events():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "call")
            h = _auth(user["token"])

            session = await _mk_live_session(client, h, frm="+15550003333")
            sid = session["id"]

            res = await client.post(f"/voice/sessions/{sid}/events", headers=h,
                                    json={"kind": "hangup", "payload": {"reason": "bye"}})
            assert res.status_code == 200

            res = await client.get("/events", headers=h,
                                   params={"session_id": sid, "limit": 50})
            events = res.json()["events"]
            types = [e["type"] for e in events]
            # created -> started (answered) -> ended, all correlated to the call
            assert types == ["call.ended", "call.started", "call.created"], types
            ended = events[0]
            assert ended["source"] == "voice"
            assert ended["payload"]["end_reason"] == "bye"
            assert ended["correlation_id"] == sid
            assert ended["target_type"] == "session" and ended["target_id"] == sid
            # every event of the journey shares the correlation thread
            assert {e["correlation_id"] for e in events} == {sid}

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5. the queue journey: waiting, position changes, leaving
# ---------------------------------------------------------------------------

def test_v80_queue_journey_events():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "queue")
            h = _auth(user["token"])
            await _mk_agent(client, h, "line agent")

            res = await client.post("/voice/queues", headers=h, json={
                "name": "v80 line",
                "config": {"announce": {"template": "Position {position}."}}})
            assert res.status_code == 201, res.text
            queue = res.json()

            s1 = await _mk_live_session(client, h, frm="+15550004444")
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": s1["id"]})
            assert res.status_code == 201, res.text
            entry_id = res.json()["entry_id"]

            res = await client.get("/events", headers=h,
                                   params={"correlation_id": s1["id"],
                                           "limit": 50})
            types = [e["type"] for e in res.json()["events"]]
            # the join pass announced position 1 (due) - waiting + position event
            assert "call.waiting" in types
            assert "queue.position_changed" in types
            waiting = next(e for e in res.json()["events"]
                           if e["type"] == "call.waiting")
            assert waiting["payload"]["position"] == 1
            assert waiting["payload"]["queue_id"] == queue["id"]
            assert waiting["session_id"] == s1["id"]
            # the waiting event's source is the queue primitive
            assert waiting["source"] == "queue"

            # a second caller joins; the first moves? no - FIFO stays, but
            # leaving announces the line moved
            s2 = await _mk_live_session(client, h, frm="+15550005555")
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": s2["id"]})
            assert res.status_code == 201, res.text
            entry2 = res.json()["entry_id"]

            res = await client.post(
                f"/voice/queues/{queue['id']}/entries/{entry_id}/leave",
                headers=h)
            assert res.status_code == 200, res.text

            res = await client.post(f"/voice/queues/{queue['id']}/next",
                                    headers=h)
            assert res.status_code == 200, res.text
            assert res.json()["seated"]["id"] == entry2

            res = await client.get("/events", headers=h,
                                   params={"type": "call.seated", "limit": 50})
            seated_list = res.json()["events"]
            assert len(seated_list) == 1
            seated = seated_list[0]
            assert seated["session_id"] == s2["id"]
            assert seated["payload"]["queue_id"] == queue["id"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 6. the meeting journey: join, chat, hand, floor, end
# ---------------------------------------------------------------------------

def test_v80_meeting_event_journey():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "meet")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "room agent")

            res = await client.post("/voice/meetings", headers=h, json={
                "title": "v80 room", "agent_id": agent["id"]})
            assert res.status_code == 201, res.text
            meeting = res.json()

            res = await client.post(f"/voice/meetings/{meeting['id']}/join",
                                    headers=h, json={"label": "alice"})
            assert res.status_code == 200, res.text
            alice = res.json()["participant"]
            assert alice["session_id"]

            res = await client.post(f"/voice/meetings/{meeting['id']}/chat",
                                    headers=h, json={"text": "hello room",
                                                     "participant_id": alice["id"]})
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/meetings/{meeting['id']}/hand",
                                    headers=h, json={"participant_id": alice["id"]})
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/meetings/{meeting['id']}/floor",
                                    headers=h, json={"mode": "directed",
                                                     "participant_id": alice["id"]})
            assert res.status_code == 200, res.text
            res = await client.post(f"/voice/meetings/{meeting['id']}/end",
                                    headers=h)
            assert res.status_code == 200, res.text

            res = await client.get("/events", headers=h,
                                   params={"type": "*", "limit": 100})
            events = res.json()["events"]
            types = [e["type"] for e in events]
            for expected in ("participant.joined", "chat.posted", "hand.raised",
                             "floor.granted", "meeting.ended"):
                assert expected in types, types
            joined = next(e for e in events if e["type"] == "participant.joined")
            assert joined["actor"] == "alice"
            assert joined["payload"]["meeting_id"] == meeting["id"]
            assert joined["session_id"] == alice["session_id"]
            assert joined["correlation_id"] == meeting["id"]
            ended = next(e for e in events if e["type"] == "meeting.ended")
            assert ended["payload"]["title"] == "v80 room"
            # the meeting's own correlation thread ties its events together
            meet_thread = [e for e in events
                           if e["correlation_id"] == meeting["id"]]
            assert {e["type"] for e in meet_thread} >= {
                "participant.joined", "chat.posted", "hand.raised",
                "floor.granted", "meeting.ended"}

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 7. recording events: started + ready with artifact pointers
# ---------------------------------------------------------------------------

def test_v80_recording_events():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "rec")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "recorded room")

            res = await client.post("/voice/meetings", headers=h, json={
                "title": "recorded", "agent_id": agent["id"]})
            meeting = res.json()
            res = await client.post(f"/voice/meetings/{meeting['id']}/join",
                                    headers=h, json={"label": "note taker"})
            assert res.status_code == 200

            res = await client.post(f"/voice/meetings/{meeting['id']}/recordings",
                                    headers=h, json={"name": "weekly sync"})
            assert res.status_code == 201, res.text
            rec = res.json()
            res = await client.post(
                f"/voice/meetings/{meeting['id']}/recordings/{rec['id']}/stop",
                headers=h)
            assert res.status_code == 200, res.text

            res = await client.get("/events", headers=h,
                                   params={"type": "recording.*"})
            events = res.json()["events"]
            types = [e["type"] for e in events]
            assert types == ["recording.ready", "recording.started"], types
            ready = events[0]
            assert ready["payload"]["meeting_id"] == meeting["id"]
            assert ready["payload"]["transcript_artifact_id"]
            assert ready["payload"]["stop_reason"]
            assert ready["correlation_id"] == meeting["id"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 8. the media runtime: a video session IS a room with a picture
# ---------------------------------------------------------------------------

def test_v80_media_session_video():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "media")
            h = _auth(user["token"])
            agent = await _mk_agent(client, h, "video room agent")

            # kind=video opens a video-first room through the runtime door
            res = await client.post("/media/sessions", headers=h, json={
                "kind": "video", "title": "standup", "agent_id": agent["id"]})
            assert res.status_code == 201, res.text
            ms = res.json()
            assert ms["kind"] == "video" and ms["modality"] == "audio+video"
            assert ms["ref_kind"] == "meeting" and ms["ref_id"]
            assert ms["state"]["room"] == "active"
            # the FULL unified projection, every key of the sketch present
            for key in ("participants", "tracks", "data_channels", "permissions",
                        "presence", "events", "recording", "transcription",
                        "state", "tracks"):
                assert key in ms, key
            assert set(ms["tracks"].keys()) >= {"audio", "video", "screen"}
            assert ms["presence"]["joined"] == 0
            assert ms["data_channels"]["chat"]["messages"] == 0
            assert ms["permissions"]["floor"]["mode"] == "auto"

            # add a leg through the runtime door
            res = await client.post(f"/media/sessions/{ms['id']}/participants",
                                    headers=h, json={"label": "bob"})
            assert res.status_code == 201, res.text
            pid = res.json()["participant"]["id"]
            view = res.json()["media_session"]
            assert view["presence"]["joined"] == 1
            assert view["participants"][0]["label"] == "bob"
            # the leg's audio track shows (web leg with a live session)
            assert len(view["tracks"]["audio"]) == 1
            assert view["tracks"]["audio"][0]["participant_id"] == pid

            # the session's birth is in the event system
            res = await client.get("/events", headers=h,
                                   params={"type": "media.*"})
            types = [e["type"] for e in res.json()["events"]]
            assert "media.session_started" in types
            assert "media.participant_added" in types
            # the leg's own join came through the meeting primitive
            res = await client.get("/events", headers=h,
                                   params={"type": "participant.joined"})
            joined = res.json()["events"]
            assert len(joined) == 1 and joined[0]["actor"] == "bob"

            # publishing a camera through the v78 primitive shows in the view
            res = await client.post(
                f"/voice/meetings/{view['ref_id']}/video/publish",
                headers=h, json={"participant_id": pid, "track_id": "cam-1",
                                 "kind": "camera"})
            assert res.status_code == 200, res.text
            res = await client.get(f"/media/sessions/{ms['id']}", headers=h)
            assert len(res.json()["tracks"]["video"]) == 1
            assert res.json()["tracks"]["video"][0]["track_id"] == "cam-1"

            # ending the runtime object ends the room (and the leg's call)
            res = await client.post(f"/media/sessions/{ms['id']}/end", headers=h)
            assert res.status_code == 200, res.text
            assert res.json()["ended"] is True
            res = await client.get(f"/media/sessions/{ms['id']}", headers=h)
            assert res.json()["state"]["room"] == "ended"
            assert res.json()["state"]["media"] == "ended"

            res = await client.get("/events", headers=h,
                                   params={"type": "media.*"})
            types = [e["type"] for e in res.json()["events"]]
            assert "media.session_ended" in types
            # ending the runtime object ended the ROOM through its own door
            res = await client.get("/events", headers=h,
                                   params={"type": "meeting.ended"})
            assert len(res.json()["events"]) == 1

            # ending twice refuses honestly
            res = await client.post(f"/media/sessions/{ms['id']}/end", headers=h)
            assert res.status_code == 400

            # the list shows the ended session
            res = await client.get("/media/sessions", headers=h,
                                   params={"state": "ended"})
            assert any(s["id"] == ms["id"] for s in res.json()["media_sessions"])

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 9. the media runtime: kind=voice wraps a CALL, and says so
# ---------------------------------------------------------------------------

def test_v80_media_session_voice():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "mvoice")
            h = _auth(user["token"])

            session = await _mk_live_session(client, h, frm="+15550006666")
            res = await client.post("/media/sessions", headers=h, json={
                "kind": "voice", "session_id": session["id"]})
            assert res.status_code == 201, res.text
            ms = res.json()
            assert ms["kind"] == "voice" and ms["modality"] == "audio"
            assert ms["ref_kind"] == "session" and ms["ref_id"] == session["id"]
            # the call's two sides are the participants
            roles = {p["role"] for p in ms["participants"]}
            assert roles == {"caller", "callee"}
            assert ms["participants"][0]["state"] == "in_progress"
            assert ms["presence"]["connected"] is True
            # the call's event timeline rides the projection
            kinds = {e["kind"] for e in ms["events"]}
            assert "call.answered" in kinds

            # a call's participants ARE the call - adding legs refuses
            res = await client.post(f"/media/sessions/{ms['id']}/participants",
                                    headers=h, json={"label": "intruder"})
            assert res.status_code == 400
            assert "participants are the call" in res.json()["detail"]

            # ending the session hangs the call up through the state machine
            res = await client.post(f"/media/sessions/{ms['id']}/end", headers=h)
            assert res.status_code == 200
            res = await client.get(f"/voice/sessions/{session['id']}", headers=h)
            assert res.json()["state"] == "ended"
            assert res.json()["end_reason"] == "media_session_ended"

            # kind=voice without a session refuses loudly
            res = await client.post("/media/sessions", headers=h, json={
                    "kind": "voice"})
            assert res.status_code == 400
            assert "session_id" in res.json()["detail"]
            res = await client.post("/media/sessions", headers=h, json={
                    "kind": "webinar"})
            assert res.status_code == 400 and "kind must be" in res.json()["detail"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 10. the audio of a media-websocket stream shows as a REAL track
# ---------------------------------------------------------------------------

def test_v80_media_session_voice_track_from_stream():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "mtrack")
            h = _auth(user["token"])

            session = await _mk_live_session(client, h, frm="+15550007777")
            # simulate an attached media stream (what the websocket handler
            # records on the session's context when a stream is live)
            from app.db import AsyncSessionLocal
            from app.models import VoiceSession

            async with AsyncSessionLocal() as db:
                row = await db.get(VoiceSession, session["id"])
                row.context = {**(row.context or {}),
                               "media": {"stream_sid": "MS-v80", "chunks": 250,
                                         "audio_ms": 5000.0}}
                db.add(row)
                await db.commit()

            res = await client.post("/media/sessions", headers=h, json={
                "kind": "voice", "session_id": session["id"]})
            ms = res.json()
            assert len(ms["tracks"]["audio"]) == 1
            track = ms["tracks"]["audio"][0]
            assert track["transport"] == "media_websocket"
            assert track["stream_sid"] == "MS-v80"
            assert track["audio_ms"] == 5000.0

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 11. the SMS backchannel's story rides the event system
# ---------------------------------------------------------------------------

def test_v80_sms_events_via_backchannel():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "sms")
            h = _auth(user["token"])

            # the any-gateway SMS channel (no outbound credentials - the
            # sends are honestly skipped records, still real events)
            res = await client.post("/channels/endpoints", headers=h, json={
                "name": "any-gateway v80", "provider": "generic_sms",
                "config": {"secret": "sms-sekrit-v80"}})
            assert res.status_code == 201, res.text
            ep = res.json()

            def _sms_sign(raw: bytes) -> str:
                from app.services import channel_adapters as adapters

                return adapters.generic_sms_sign("sms-sekrit-v80", raw)

            async def _sms_post(payload: dict) -> dict:
                body = json.dumps(payload).encode()
                assert ep["webhook_url"].startswith("/api/v1/")
                return await client.post(
                    ep["webhook_url"][len("/api/v1"):], content=body,
                    headers={"content-type": "application/json",
                             "x-py8n-signature": _sms_sign(body)})

            # the support line: SMS backchannel + auto-answer (abandon)
            res = await client.post("/voice/queues", headers=h, json={
                "name": "textback line",
                "config": {"sms": {"enabled": True, "channel_id": ep["id"],
                                   "template": "Position {position} in {queue_name}.",
                                   "auto_answer": {"enabled": True,
                                                   "keyword": "1",
                                                   "action": "abandon"}}}})
            assert res.status_code == 201, res.text
            queue = res.json()

            session = await _mk_live_session(client, h, frm="+15550008888")
            res = await client.post(f"/voice/queues/{queue['id']}/entries",
                                    headers=h, json={"session_id": session["id"]})
            assert res.status_code == 201, res.text

            # the position SMS the join pass sent is in the event system
            # (delivery is the honest skipped - this gateway holds no creds)
            res = await client.get("/events", headers=h,
                                   params={"type": "sms.*"})
            sent = [e for e in res.json()["events"] if e["type"] == "sms.sent"]
            assert len(sent) == 1
            assert sent[0]["payload"]["to"] == "+15550008888"
            assert sent[0]["payload"]["delivery"] == "skipped"
            assert sent[0]["session_id"] == session["id"]

            # the caller replies "1" - the auto-answer intercepts, the line
            # releases the caller, the confirmation text goes back
            res = await _sms_post({"from": "+15550008888", "text": " 1 "})
            assert res.status_code == 200, res.text
            out = res.json()
            assert out["received"] == 1
            handled = out["handled"][0]
            assert handled["auto_answer"] is True
            assert handled["action"]["done"] is True

            res = await client.get("/events", headers=h,
                                   params={"type": "sms.received"})
            events = res.json()["events"]
            assert len(events) == 1
            got = events[0]
            assert got["payload"]["keyword"] == "1"
            assert got["payload"]["auto_answer_action"] == "abandon"
            assert got["session_id"] == session["id"]
            # the confirmation text is a second, honest sms.sent
            res = await client.get("/events", headers=h,
                                   params={"type": "sms.*"})
            assert len([e for e in res.json()["events"]
                        if e["type"] == "sms.sent"]) == 2
            # the whole journey hangs on one correlation thread: the wait,
            # the texts both ways, the line moving
            res = await client.get("/events", headers=h,
                                   params={"correlation_id": session["id"],
                                           "limit": 100})
            thread_types = {e["type"] for e in res.json()["events"]}
            assert {"call.waiting", "sms.sent", "sms.received",
                    "queue.left"} <= thread_types, thread_types

    _sync(_wrap(_go()))
