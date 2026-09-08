"""v93 tests - the loop is watched live.

v92 gave the operator the chair (the diff, the row receipt, the chains
drawn); v93 keeps the screens honest without anyone pressing refresh:

* THE ATTENTION FEED AUTO-REFRESHES ON business.stuck: the feed's
  refresh trigger rides the SAME live tail every other reactive surface
  rides - the owner-scoped in-process hub the WS /events/stream drains.
  This test proves the wire end to end: a subscriber queue (exactly what
  the websocket handler holds) receives business.stuck when the door
  sweeps a breach, carrying the payload keys the board needs (ref,
  process_id, instance_id, overdue_seconds, state) - and the row the
  event names is the row the attention feed serves.
* THE CHAINS GO LIVE ON THE INSTALLED SYSTEMS: GET /systems/{id} now
  resolves _RESOLVED_CHAINS against the system's bound kind="process"
  components by NAME (the same name-resolution the journeys use at fire
  time) and counts the real rows - per NODE the open + past-SLA
  instances, per LEG in_state (the source's open instances sitting in
  the fire state), fired (the target's open instances the leg opened
  itself - they carry the journey link) and overdue (the fired ones past
  the leg's SLA). A partial install shows the chain honestly: the leg's
  missing end is named, complete is false; a system with no machines
  draws no chains at all.
* THE SLA DIGEST PREVIEW INSIDE THE POLICY EDITOR: POST
  /processes/{id}/escalation-preview renders what the door would do
  RIGHT NOW under the DRAFT policy - the digest's subject + body (the
  exact strings the email adapter would carry), the per-item knock
  messages, the held episodes (acked / snoozed / one-knock-per-stint)
  and the honest delivery note (event-only / no target / no endpoint
  bound). The draft passes the SAME validator a save runs, so a broken
  draft refuses loudly BEFORE it can be saved - and the preview is pure
  read + render: no book written, no event emitted, nothing delivered.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"

MACHINE = {
    "states": ["a", "b", "done"],
    "initial": "a",
    "transitions": [
        {"name": "go", "from": "a", "to": "b"},
        {"name": "finish", "from": "b", "to": "done"},
    ],
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
        "email": f"v93-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v93 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _door(user_id: str, *, now: datetime | None = None) -> dict:
    """The escalation door at the service level - the injectable clock."""
    from app.db import AsyncSessionLocal
    from app.services import business_processes as bp_svc

    async with AsyncSessionLocal() as session:
        report = await bp_svc.escalate_stuck(session, owner_id=user_id,
                                             actor="test", now=now)
        await session.commit()
    return report


async def _attention(user_id: str, *, now: datetime | None = None) -> dict:
    """The attention feed at the service level - the same injectable clock."""
    from app.db import AsyncSessionLocal
    from app.services import business_processes as bp_svc

    async with AsyncSessionLocal() as session:
        return await bp_svc.attention_feed(session, user_id, now=now)


async def _preview(user_id: str, pid: str, policy, *,
                   now: datetime | None = None) -> dict:
    """The v93 preview at the service level - the same injectable clock."""
    from app.db import AsyncSessionLocal
    from app.services import business_processes as bp_svc

    async with AsyncSessionLocal() as session:
        return await bp_svc.escalation_preview(
            session, pid, owner_id=user_id, policy=policy, now=now)


async def _system_chains(user_id: str, sid: str, *,
                         now: datetime | None = None) -> list[dict]:
    """chains_for_system at the service level - the injectable clock."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db import AsyncSessionLocal
    from app.models import Py8nSystem
    from app.services.operators import chains_for_system

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(Py8nSystem)
            .options(selectinload(Py8nSystem.components))
            .where(Py8nSystem.id == sid))).scalar_one()
        return await chains_for_system(session, row, now=now)


# ---------------------------------------------------------------------------
# 1) the attention feed auto-refreshes on business.stuck - the live wire
# ---------------------------------------------------------------------------

def test_v93_attention_live_tail():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "tail")
            h = _auth(user["token"])

            r = await client.post("/processes", headers=h, json={
                "name": "the watched machine",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "email", "to": "ops@py8n.test",
                    "repeat_every_seconds": 3600, "max_repeats": 3}}})
            assert r.status_code == 201, r.text
            pid = r.json()["id"]
            r = await client.post(f"/processes/{pid}/instances", headers=h,
                                  json={"ref": "W-1", "title": "the breach",
                                        "due_in_seconds": 1})
            assert r.status_code == 201, r.text

            # a live tail subscriber - exactly what the WS handler holds
            from app.services import system_events as events_svc

            q = events_svc.subscribe(user["id"])
            base = datetime.now(timezone.utc)

            # the door sweeps the breach - the commit fans the event out
            report = await _door(user["id"], now=base + timedelta(seconds=2))
            assert report["stuck"] >= 1, report

            ev = q.get_nowait()  # put_nowait at commit - no waiting needed
            assert ev["type"] == "business.stuck", ev
            assert ev["owner_id"] == user["id"]
            assert ev["source"] == "business"
            payload = ev["payload"]
            assert payload["ref"] == "W-1"
            assert payload["process_id"] == pid
            assert payload["process_name"] == "the watched machine"
            assert payload["instance_id"] == report["recorded"][0]["instance_id"]
            assert payload["state"] == "a"
            # the payload's clock matches the door's own report
            assert payload["overdue_seconds"] >= 1
            assert payload["overdue_seconds"] == \
                report["recorded"][0]["overdue_seconds"]
            # the wrapping contract the WS endpoint speaks (the browser's
            # JSON.parse sees exactly this shape)
            wrapped = {"event": "system_event", **ev}
            assert wrapped["event"] == "system_event"
            assert wrapped["type"] == "business.stuck"

            # the row the event names is the row the feed serves - the
            # refresh lands on a feed that already carries the breach
            from app.db import AsyncSessionLocal
            from app.services import business_processes as bp_svc

            async with AsyncSessionLocal() as session:
                feed = await bp_svc.attention_feed(
                    session, user["id"], now=base + timedelta(seconds=3))
            rows = feed["attention"]
            assert len(rows) == 1, rows
            assert rows[0]["instance_id"] == payload["instance_id"]
            assert rows[0]["ref"] == "W-1"
            assert rows[0]["escalation"]["count"] == 1

            # the tail is owner-scoped: a stranger's tail hears nothing
            stranger_q = events_svc.subscribe("someone-else")
            assert stranger_q.empty()

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2) the chains live on the installed systems - drawn with real counts
# ---------------------------------------------------------------------------

def test_v93_system_chains_live_counts():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "chains")
            h = _auth(user["token"])
            base = datetime.now(timezone.utc)

            # the system the operators' install would have built
            rs = await client.post("/systems", headers=h,
                                   json={"name": "the revenue floor"})
            assert rs.status_code == 201, rs.text
            sid = rs.json()["id"]

            # two of the revenue walk's machines, bound to the system
            r = await client.post("/processes", headers=h, json={
                "name": "Lead pipeline",
                "definition": {
                    "states": ["new", "won", "archived", "lost"],
                    "initial": "new",
                    "transitions": [
                        {"name": "win", "from": "new", "to": "won"},
                        {"name": "archive", "from": "won", "to": "archived"},
                        {"name": "lose", "from": "new", "to": "lost"},
                    ],
                    "journeys": [{"on_state": "won",
                                  "open": {"process": "Customer onboarding",
                                           "title_template": "Onboarding - {title}"}}],
                }})
            assert r.status_code == 201, r.text
            lead_pid = r.json()["id"]
            r = await client.post("/processes", headers=h, json={
                "name": "Customer onboarding",
                "definition": {
                    "states": ["fresh", "handed_off", "done"],
                    "initial": "fresh",
                    "transitions": [
                        {"name": "hand_off", "from": "fresh", "to": "handed_off"},
                        {"name": "close", "from": "handed_off", "to": "done"},
                    ],
                }})
            assert r.status_code == 201, r.text
            onboard_pid = r.json()["id"]
            for comp_pid in (lead_pid, onboard_pid):
                r = await client.post(f"/systems/{sid}/components", headers=h,
                                      json={"kind": "process", "ref_id": comp_pid})
                assert r.status_code == 201, r.text

            # a lead walks to won - the journey opens the onboarding case
            r = await client.post(f"/processes/{lead_pid}/instances", headers=h,
                                  json={"ref": "L-1", "title": "Globex",
                                        "due_in_seconds": 3600})
            assert r.status_code == 201, r.text
            r = await client.post(
                f"/processes/{lead_pid}/instances/{r.json()['id']}/advance",
                headers=h, json={"transition": "win", "actor": "sales"})
            assert r.status_code == 200, r.text
            child = (r.json().get("journeys_opened") or [{}])[0]
            assert child.get("opened") is True, child

            # a second onboarding case with a 1s SLA - late under a later clock
            r = await client.post(f"/processes/{onboard_pid}/instances",
                                  headers=h,
                                  json={"ref": "OB-9", "title": "Initech",
                                        "due_in_seconds": 1})
            assert r.status_code == 201, r.text

            # the detail endpoint carries the live chains
            r = await client.get(f"/systems/{sid}", headers=h)
            assert r.status_code == 200, r.text
            chains = r.json()["chains"]
            assert [c["slug"] for c in chains] == ["revenue"]  # supply/care absent
            chain = chains[0]
            assert chain["story"]
            assert [o["slug"] for o in chain["operators"]] == \
                ["sales-operator", "operations-operator", "finance-operator"]
            assert len(chain["legs"]) == 2
            leg0, leg1 = chain["legs"]
            assert (leg0["from_process"], leg0["on_state"], leg0["opens"]) == \
                ("Lead pipeline", "won", "Customer onboarding")
            assert leg0["bound_from"] is True and leg0["bound_opens"] is True
            # v94: the leg counts carry the ack/snooze sub-counts too
            assert leg0["counts"] == {"in_state": 1, "fired": 1, "overdue": 0,
                                      "acked": 0, "snoozed": 0}
            assert (leg1["from_process"], leg1["on_state"], leg1["opens"]) == \
                ("Customer onboarding", "handed_off", "Invoice lifecycle")
            assert leg1["bound_from"] is True and leg1["bound_opens"] is False
            assert leg1["counts"]["fired"] == 0
            assert chain["complete"] is False  # finance is not installed here
            nodes = {n["process"]: n for n in chain["nodes"]}
            assert nodes["Lead pipeline"]["bound"] is True
            assert nodes["Lead pipeline"]["open"] == 1
            assert nodes["Customer onboarding"]["open"] == 2
            assert nodes["Invoice lifecycle"]["bound"] is False

            # the clock moves on: OB-9 (1s SLA) and L-1 (its own 3600s
            # promise) are both past it - each node counts its own lateness
            later = base + timedelta(days=2)
            chains2 = await _system_chains(user["id"], sid, now=later)
            chain2 = [c for c in chains2 if c["slug"] == "revenue"][0]
            nodes2 = {n["process"]: n for n in chain2["nodes"]}
            assert nodes2["Customer onboarding"]["overdue"] == 1
            assert nodes2["Lead pipeline"]["overdue"] == 1
            # the leg's fired child (no leg SLA on this leg) stays at zero
            assert chain2["legs"][0]["counts"] == {"in_state": 1, "fired": 1,
                                                   "overdue": 0,
                                                   "acked": 0, "snoozed": 0}

            # a system that binds no machines draws no chains - an honest
            # absence, never an empty diagram
            rs = await client.post("/systems", headers=h,
                                   json={"name": "the empty floor"})
            empty_sid = rs.json()["id"]
            r = await client.get(f"/systems/{empty_sid}", headers=h)
            assert r.status_code == 200, r.text
            assert r.json()["chains"] == []

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3) the SLA digest preview inside the policy editor
# ---------------------------------------------------------------------------

def test_v93_policy_preview():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "preview")
            h = _auth(user["token"])
            base = datetime.now(timezone.utc)

            # the machine carries a STORED knock policy so the door writes
            # episode books (an ack needs one); the preview below still runs
            # on DRAFTS - the stored policy only gives the door its history
            r = await client.post("/processes", headers=h, json={
                "name": "the preview machine",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "email", "to": "ops@py8n.test",
                    "repeat_every_seconds": 3600, "max_repeats": 3}}})
            assert r.status_code == 201, r.text
            pid = r.json()["id"]
            for ref, title in (("P-1", "the first"), ("P-2", "the second")):
                r = await client.post(f"/processes/{pid}/instances", headers=h,
                                      json={"ref": ref, "title": title,
                                            "due_in_seconds": 1})
                assert r.status_code == 201, r.text
            # a third entity with NO SLA promise - the door never watches it
            r = await client.post(f"/processes/{pid}/instances", headers=h,
                                  json={"ref": "P-3", "title": "no promise"})
            assert r.status_code == 201, r.text

            # the door knocks once on both promised entities (honest skips -
            # no email endpoint is bound), writing their episode books
            report = await _door(user["id"], now=base + timedelta(seconds=2))
            assert len(report["recorded"]) == 2, report

            # Dana takes P-2 (the receipt the door honors for the stint)
            insts = (await client.get(f"/processes/{pid}/instances",
                                      headers=h)).json()["instances"]
            p2 = next(i for i in insts if i["ref"] == "P-2")
            r = await client.post(
                f"/processes/{pid}/instances/{p2['id']}/escalations/ack",
                headers=h, json={"by": "Dana", "note": "on it",
                                 "snooze_hours": 2})
            assert r.status_code == 200, r.text

            # a FOURTH entity born after the door's sweep - no book, no log
            r = await client.post(f"/processes/{pid}/instances", headers=h,
                                  json={"ref": "P-4", "title": "the newborn",
                                        "due_in_seconds": 1})
            assert r.status_code == 201, r.text

            later = base + timedelta(seconds=60)

            # --- the digest draft: the ONE summary, typeset ---------------
            draft_digest = {"channel": "email", "to": "ops@py8n.test",
                            "mode": "digest", "digest_every_seconds": 86400,
                            "max_repeats": 3}
            out = await _preview(user["id"], pid, draft_digest, now=later)
            assert out["overdue_count"] == 3, out
            assert out["mode"] == "digest"
            assert out["policy_line"] == \
                "stuck -> daily digest over email (x4) -> ops@py8n.test"
            d = out["digest"]
            assert d["subject"] == ("[py8n] Escalation digest - the preview "
                                    "machine (2 item(s) past SLA)")
            assert "P-1" in d["body"] and "P-4" in d["body"]
            assert "'the first'" in d["body"] and "'the newborn'" in d["body"]
            assert len(d["items"]) == 2
            assert d["candidates"] == 2
            assert d["window_seconds"] == 86400
            assert d["due"] is False        # the window has not elapsed
            assert d["next_in_seconds"] == 86400
            assert out["messages"] == []    # digest mode knocks nobody
            assert len(out["held"]) == 1    # P-2, the acknowledged one
            assert out["held"][0]["reason"] == "acknowledged"
            assert out["held"][0]["acked_by"] == "Dana"
            # delivery honesty: channel + target set, but no email endpoint
            assert out["would_deliver"] is False
            assert "would skip - no email channel endpoint bound" in out["delivery_note"]

            # --- the knock draft: the cadence honesty --------------------
            draft_knock = {"channel": "email", "to": "ops@py8n.test",
                           "repeat_every_seconds": 3600, "max_repeats": 3}
            out = await _preview(user["id"], pid, draft_knock, now=later)
            assert out["mode"] == "knock"
            assert out["digest"] is None
            # P-1: knocked 58s ago, the 3600s cadence holds it (too_soon)
            too_soon = next(x for x in out["held"]
                            if x["ref"] == "P-1")
            assert too_soon["reason"] == "too_soon"
            assert too_soon["next_in_seconds"] > 3500
            # P-2: Dana's ack owns the stint
            acked = next(x for x in out["held"] if x["ref"] == "P-2")
            assert acked["reason"] == "acknowledged"
            # P-4: no book, no log - the door would knock it right now
            assert [m["ref"] for m in out["messages"]] == ["P-4"]
            first = out["messages"][0]
            assert first["attempt"] == 1
            assert first["to"] == "ops@py8n.test"
            assert first["subject"] == ("[py8n] the preview machine: 'the newborn' "
                                        "(ref P-4) past SLA in 'a' (attempt 1)")
            assert "past its SLA" in first["message"] and "P-4" in first["message"]

            # --- the no-policy draft: the v85 semantics, event-only ------
            out = await _preview(user["id"], pid, None, now=later)
            assert out["policy"] is None
            assert out["mode"] == "event-only"
            assert out["policy_line"] == "no escalation policy"
            # P-1: the door already escalated this stint - held, not repeated
            assert next(x["reason"] for x in out["held"] if x["ref"] == "P-1") \
                == "already_escalated_this_stint"
            assert next(x["reason"] for x in out["held"] if x["ref"] == "P-2") \
                == "acknowledged"
            assert [m["ref"] for m in out["messages"]] == ["P-4"]
            assert out["messages"][0]["attempt"] == 1
            assert out["would_deliver"] is False
            assert "no policy" in out["delivery_note"]

            # --- the preview is pure read + render: nothing was written --
            insts = (await client.get(f"/processes/{pid}/instances",
                                      headers=h)).json()["instances"]
            p4 = next(i for i in insts if i["ref"] == "P-4")
            assert "escalations" not in (p4["context"] or {})  # no book written
            p1 = next(i for i in insts if i["ref"] == "P-1")
            assert (p1["context"]["escalations"]["count"]) == 1  # door's only
            r = await client.get("/events", headers=h,
                                 params={"type": "business.escalation_digest"})
            assert r.status_code == 200
            assert r.json()["events"] == []  # no digest event emitted
            r = await client.get("/events", headers=h,
                                 params={"type": "business.escalated"})
            assert len(r.json()["events"]) == 2  # only the door's own two

            # --- the API plumbing: same validator, loud refusals ---------
            r = await client.post(f"/processes/{pid}/escalation-preview",
                                  headers=h, json={"policy": {}})
            assert r.status_code == 200
            assert r.json()["policy"] is None  # {} = no policy, like a save
            r = await client.post(f"/processes/{pid}/escalation-preview",
                                  headers=h,
                                  json={"policy": {"channel": "carrier-pigeon"}})
            assert r.status_code == 400
            assert "carrier-pigeon" in r.json()["detail"]
            r = await client.post("/processes/no-such-machine/escalation-preview",
                                  headers=h, json={"policy": None})
            # the policy family speaks 400 for an unknown machine (the same
            # voice PATCH /escalation-policy uses)
            assert r.status_code == 400

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4) the version pin
# ---------------------------------------------------------------------------

def test_v93_version_pin():
    assert settings.version == "1.98.0"
