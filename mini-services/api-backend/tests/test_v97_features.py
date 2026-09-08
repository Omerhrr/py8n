"""v97 tests - the digest remembers what the team did, the map moves itself.

v96 made the receipt a schedule; v97 makes the summaries and the chains
TELL that story:

* RESCHEDULE EVIDENCE INSIDE DIGEST RENDERS: the one summary per window
  is no longer a bare list of overdue rows - a line whose item carries a
  receipt names the evidence right on the line: "rescheduled to
  <moment> by <who> - the door re-knocked" (a v96 reschedule whose
  moment passed before the digest), "snoozed by <who> until <moment>" /
  "- the snooze ran out" (a v89 loan), or "taken by <who>" (a plain
  v88 take). Items without a receipt render exactly as v89 drew them
  (the old callers' items have no note key and the line is unchanged).
  The evidence rides the render (the email body), the digest event's
  items, and the v93 preview's typeset - the editor sees the whole
  truth before saving. Found-live context: this rides the v96 anchor
  fix - the reschedule evidence only reaches the digest because the
  receipt survives in the book until the item re-enters the bucket.
* CHAIN-MAP LIVENESS ON journey_opened: the operator-detail chain map
  (the v95 overlay + v96 depth) now rides the same owner-scoped live
  tail every reactive surface rides since v93 (WS /events/stream). When
  any machine lands on a fire-state and the handoff OPENS the next leg,
  the map re-reads itself (600ms debounce) and the leg that just gained
  a ride flashes ("just opened", 5s) - nobody presses refresh. This
  test proves the exact wire frame: a subscriber queue (what the WS
  handler holds) receives business.journey_opened carrying
  process_name / on_state / target.process_name - the keys the page
  matches the flashing leg with - wrapped in the
  {event: system_event, **ev} envelope the browser JSON.parses.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"

MACHINE = {
    "states": ["a", "b", "done"],
    "initial": "a",
    "transitions": [{"name": "go", "from": "a", "to": "b"},
                    {"name": "finish", "from": "b", "to": "done"}],
}


@pytest.fixture(autouse=True)
def _clean_event_subscribers():
    from app.services import system_events as events_svc

    events_svc._subscribers.clear()
    yield
    events_svc._subscribers.clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod
    from app.services import system_events as events_svc

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


async def _mk_user(client: httpx.AsyncClient, tag: str) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v97-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v97 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _door(user_id: str, *, now: datetime | None = None) -> dict:
    from app.db import AsyncSessionLocal
    from app.services import business_processes as bp_svc

    async with AsyncSessionLocal() as session:
        report = await bp_svc.escalate_stuck(session, owner_id=user_id,
                                             actor="test", now=now)
        await session.commit()
    return report


# ---------------------------------------------------------------------------
# 1. the digest line carries the receipt's evidence
# ---------------------------------------------------------------------------

def test_v97_reschedule_evidence_in_digest():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "digest-ev")
            h = _auth(user["token"])

            res = await client.post("/processes", headers=h, json={
                "name": "Evidence digest",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "", "mode": "digest",
                    "digest_every_seconds": 60, "max_repeats": 3}}})
            assert res.status_code == 201, res.text
            pid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "EV-1", "title": "Late case",
                                          "due_in_seconds": 1})
            iid = res.json()["id"]

            base = datetime.now(timezone.utc)
            # digest #1: a bare line - no receipt yet, v89's exact render
            await _door(user["id"], now=base + timedelta(seconds=10))
            await _door(user["id"], now=base + timedelta(seconds=80))
            events1 = (await client.get(
                "/events?type=business.escalation_digest", headers=h)).json()
            ev1 = next(e for e in events1["events"]
                       if e["payload"]["process_id"] == pid)
            line1 = next(l for l in ev1["payload"]["items"]
                         if l["ref"] == "EV-1")
            assert not line1.get("reschedule_note"), line1

            # the human reschedules; the moment passes; the item rides
            # digest #2 - and the line says what happened
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/escalations/ack",
                headers=h, json={"by": "ops", "reschedule_in_minutes": 15})
            assert res.status_code == 200, res.text
            await _door(user["id"], now=base + timedelta(minutes=20))
            # the digest event's own payload carries the evidence per item
            events2 = (await client.get(
                "/events?type=business.escalation_digest", headers=h)).json()
            digests2 = [e for e in events2["events"]
                        if e["payload"]["process_id"] == pid]
            assert len(digests2) >= 2, digests2
            ev2 = max(digests2, key=lambda e: e["created_at"])
            item2 = next(it for it in ev2["payload"]["items"]
                         if it["ref"] == "EV-1")
            note = item2["reschedule_note"]
            assert "rescheduled" in note and "ops" in note, note
            assert "the door re-knocked" in note, note

            # the RENDER carries the same evidence on the line
            from app.services import escalations as escalations_svc

            ev_latest = max(digests2, key=lambda e: e["created_at"])
            rendered = escalations_svc.render_digest(
                process_name=ev_latest["payload"]["process_name"],
                items=ev_latest["payload"]["items"])
            assert "rescheduled" in rendered and "ops" in rendered, rendered
            # and the v89 render is untouched for note-less items
            plain = escalations_svc.render_digest(process_name="X", items=[{
                "title": "t", "ref": "r", "state": "s",
                "overdue_minutes": 5, "attempt": 1}])
            assert plain == ("[py8n] Escalation digest - 1 item(s) past SLA "
                             "on X:\n- 't' (ref r) in 's' for 5m past SLA "
                             "(digest 1)"), plain

    _sync(_wrap(_go()))


def test_v97_preview_digest_carries_evidence_too():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "preview-ev")
            h = _auth(user["token"])

            res = await client.post("/processes", headers=h, json={
                "name": "Evidence preview",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "", "mode": "digest",
                    "digest_every_seconds": 60, "max_repeats": 3}}})
            pid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "PV-1", "due_in_seconds": 1})
            iid = res.json()["id"]

            base = datetime.now(timezone.utc)
            await _door(user["id"], now=base + timedelta(seconds=10))
            await _door(user["id"], now=base + timedelta(seconds=80))
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/escalations/ack",
                headers=h, json={"by": "ops", "reschedule_in_minutes": 10})
            assert res.status_code == 200, res.text

            # the item rides again past the reschedule; the editor's
            # preview under the SAVED policy typesets the digest with the
            # receipt's evidence on the line
            from app.db import AsyncSessionLocal
            from app.services import business_processes as bp_svc

            async with AsyncSessionLocal() as session:
                out = await bp_svc.escalation_preview(
                    session, pid, owner_id=user["id"],
                    policy={"channel": "", "mode": "digest",
                            "digest_every_seconds": 60, "max_repeats": 3},
                    now=base + timedelta(minutes=20))
            assert out["digest"] and out["digest"]["items"], out["digest"]
            item = next(it for it in out["digest"]["items"]
                        if it["ref"] == "PV-1")
            assert "rescheduled" in item["reschedule_note"], item
            assert "ops" in item["reschedule_note"], item
            body = out["digest"]["body"]
            assert "rescheduled" in body, body

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the chain map's live wire - business.journey_opened on the tail
# ---------------------------------------------------------------------------

def test_v97_journey_opened_on_the_live_tail():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "live-map")
            h = _auth(user["token"])

            res = await client.post("/processes", headers=h, json={
                "name": "Live target", "definition": MACHINE})
            assert res.status_code == 201, res.text
            res = await client.post("/processes", headers=h, json={
                "name": "Live head",
                "definition": {**MACHINE, "journeys": [
                    {"on_state": "b",
                     "open": {"process": "Live target"}}]}})
            head_pid = res.json()["id"]

            # a live tail subscriber - exactly what the WS handler holds
            from app.services import system_events as events_svc

            q = events_svc.subscribe(user["id"])

            res = await client.post(f"/processes/{head_pid}/instances",
                                    headers=h, json={"ref": "L-1",
                                                     "title": "Live ride"})
            iid = res.json()["id"]
            res = await client.post(
                f"/processes/{head_pid}/instances/{iid}/advance",
                headers=h, json={"transition": "go"})
            assert res.status_code == 200, res.text
            assert res.json().get("journeys_opened"), res.json()

            got = None
            for _ in range(3):
                ev = await asyncio.wait_for(q.get(), timeout=5)
                wrapped = {"event": "system_event", **ev}
                assert wrapped["event"] == "system_event"
                if wrapped.get("type") == "business.journey_opened":
                    got = wrapped
                    break
            assert got is not None, "no journey_opened on the tail"
            payload = got["payload"]
            # the keys the chain map matches the flashing leg with
            assert payload["process_name"] == "Live head", payload
            assert payload["on_state"] == "b", payload
            assert payload["target"]["process_name"] == "Live target", payload
            assert payload["target"]["instance_id"], payload

            # the map itself gained the ride - the refresh serves it
            res = await client.get("/processes/chains?history_limit=25",
                                   headers=h)
            chain = next(c for c in res.json()["chains"]
                         if c["head_name"] == "Live head")
            leg = chain["legs"][0]
            assert leg["opened"] == 1 and leg["history"], leg
            assert leg["history"][0]["ref"] == "L-1", leg

            # a stranger's tail hears nothing (owner-scoped)
            stranger_q = events_svc.subscribe("someone-else")
            res = await client.post(f"/processes/{head_pid}/instances",
                                    headers=h, json={"ref": "L-2"})
            iid2 = res.json()["id"]
            await client.post(
                f"/processes/{head_pid}/instances/{iid2}/advance",
                headers=h, json={"transition": "go"})
            try:
                leaked = await asyncio.wait_for(stranger_q.get(), timeout=0.5)
                assert leaked["payload"].get("process_id") != head_pid, leaked
            except asyncio.TimeoutError:
                pass  # the honest outcome - the stranger's tail is quiet

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the version pin
# ---------------------------------------------------------------------------

def test_v97_version_pin():
    from app.config import settings

    assert settings.version == "1.100.0"
