"""v94 tests - the chain nodes answer for their people, the machine keeps
a memory of the door's rhythm.

v93 made the boards watch the door (the live tail, the live chains, the
preview); v94 closes the last two gaps the repo worklog named:

* ACK/SNOOZE SURFACED ON THE CHAIN NODES: the systems page's drawn walk
  now carries the door's book ON the nodes - per node ``acked`` /
  ``snoozed`` count the overdue instances the door is holding (the ack
  holds the DOOR, never the clock - the row stays past SLA either way),
  and ``overdue_instances`` names them (most overdue first, capped at 5)
  each with the same escalation receipt the attention feed serves
  (count, acked_by, snooze_until, snooze_active). The legs carry the same
  sub-counts among their fired children. The receipt is posted to the
  SAME ack endpoint the attention rows use, addressed by the row's own
  ids - the board re-reads and the node wears the ack. A snooze is a
  LOAN: once snooze_until has passed ``snooze_active`` reads False while
  the ack itself still shows (the door re-knocks; the take remains).
* AN ESCALATION-HISTORY SPARKLINE PER MACHINE: process analytics now
  derive ``escalation_history`` - 14 day buckets (window NAMED so a
  quiet stretch reads as data, not absence) counting the door's
  knocks/moves ('escalated' + the machine's own 'escalate'), the team's
  receipts (acknowledgements) and the summaries (digests) straight from
  the transition log's own timestamps. Older-than-window rows are
  honestly out; empty days are zero-filled, never missing.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"

LEAD_MACHINE = {
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
}

ONBOARD_MACHINE = {
    "states": ["fresh", "handed_off", "done"],
    "initial": "fresh",
    "transitions": [
        {"name": "hand_off", "from": "fresh", "to": "handed_off"},
        {"name": "close", "from": "handed_off", "to": "done"},
    ],
    # v94: a knock policy so the door's episode book carries count=1
    # (event-only - no endpoint bound, the delivery skips honestly and
    # the book still remembers the attempt)
    "escalation_policy": {"mode": "knock", "repeat_every_seconds": 3600,
                          "max_repeats": 3},
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
        "email": f"v94-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v94 {tag}",
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


async def _analytics(user_id: str, pid: str, *,
                     now: datetime | None = None) -> dict:
    """process_analytics at the service level - the injectable clock."""
    from app.db import AsyncSessionLocal
    from app.services import business_processes as bp_svc

    async with AsyncSessionLocal() as session:
        return await bp_svc.process_analytics(session, pid, user_id, now=now)


async def _backfill_log(pid: str, iid: str, owner_id: str | None, *,
                        transition: str, days_ago: float) -> None:
    """A transition-log row stamped INTO THE PAST - the log IS the
    history, so the sparkline's older buckets are exercisable without
    waiting days for a scheduler."""
    from app.db import AsyncSessionLocal
    from app.models import BusinessProcessTransitionLog

    async with AsyncSessionLocal() as session:
        session.add(BusinessProcessTransitionLog(
            process_id=pid, instance_id=iid, owner_id=owner_id,
            from_state="a", to_state="a", transition=transition,
            actor="test", note="backfilled for the sparkline",
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago)))
        await session.commit()


# ---------------------------------------------------------------------------
# 1) ack/snooze surfaced on the chain nodes
# ---------------------------------------------------------------------------

def test_v94_chain_nodes_ack_snooze():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "nodes")
            h = _auth(user["token"])
            base = datetime.now(timezone.utc)

            # the revenue floor: two of the walk's machines on one system
            rs = await client.post("/systems", headers=h,
                                   json={"name": "the v94 floor"})
            assert rs.status_code == 201, rs.text
            sid = rs.json()["id"]
            r = await client.post("/processes", headers=h, json={
                "name": "Lead pipeline", "definition": LEAD_MACHINE})
            assert r.status_code == 201, r.text
            lead_pid = r.json()["id"]
            r = await client.post("/processes", headers=h, json={
                "name": "Customer onboarding", "definition": ONBOARD_MACHINE})
            assert r.status_code == 201, r.text
            onboard_pid = r.json()["id"]
            for comp_pid in (lead_pid, onboard_pid):
                r = await client.post(f"/systems/{sid}/components", headers=h,
                                      json={"kind": "process", "ref_id": comp_pid})
                assert r.status_code == 201, r.text

            # the lead walks to won - the journey opens the onboarding case
            r = await client.post(f"/processes/{lead_pid}/instances", headers=h,
                                  json={"ref": "L-1", "title": "Globex",
                                        "due_in_seconds": 60})
            assert r.status_code == 201, r.text
            lead_iid = r.json()["id"]
            r = await client.post(
                f"/processes/{lead_pid}/instances/{lead_iid}/advance",
                headers=h, json={"transition": "win", "actor": "sales"})
            assert r.status_code == 200, r.text
            assert (r.json().get("journeys_opened") or [{}])[0].get("opened") is True

            # a second onboarding case with a 1s SLA - late within the hour
            r = await client.post(f"/processes/{onboard_pid}/instances",
                                  headers=h,
                                  json={"ref": "OB-9", "title": "Initech",
                                        "due_in_seconds": 1})
            assert r.status_code == 201, r.text
            ob_iid = r.json()["id"]
            time.sleep(1.2)

            # the door sweeps at base+1h: OB-9 (policy-carrying) gets a
            # knock attempt ON THE BOOK; L-1 (policy-less) is recorded the
            # v85 way (a no-move escalated row, the book stays empty)
            later1 = base + timedelta(hours=1)
            report = await _door(user["id"], now=later1)
            rec_refs = {e["ref"] for e in report["recorded"]}
            assert rec_refs == {"OB-9", "L-1"}, report

            # the human takes OB-9 from the node's row - the SAME endpoint
            # the attention rows and the machine view post to, addressed by
            # the row's own ids, with a 4h loan
            r = await client.post(
                f"/processes/{onboard_pid}/instances/{ob_iid}/escalations/ack",
                headers=h, json={"by": "ops", "note": "taking it",
                                 "snooze_hours": 4})
            assert r.status_code == 200, r.text

            # the chain nodes carry the door's book at the same clock
            chains = await _system_chains(user["id"], sid, now=later1)
            assert [c["slug"] for c in chains] == ["revenue"]
            nodes = {n["process"]: n for n in chains[0]["nodes"]}
            ob_node = nodes["Customer onboarding"]
            assert ob_node["open"] == 2 and ob_node["overdue"] == 1, ob_node
            assert ob_node["acked"] == 1 and ob_node["snoozed"] == 1, ob_node
            # the node names WHO it holds - the same receipt the feed serves
            listing = ob_node["overdue_instances"]
            assert len(listing) == 1, listing
            row = listing[0]
            assert row["process_id"] == onboard_pid
            assert row["instance_id"] == ob_iid
            assert row["ref"] == "OB-9" and row["state"] == "fresh"
            assert row["overdue_seconds"] > 0
            assert row["escalation"]["count"] == 1, row
            assert row["escalation"]["acked_by"] == "ops", row
            assert row["escalation"]["snooze_until"], row
            assert row["escalation"]["snooze_active"] is True, row

            # the policy-less node shows the contrast honestly: knocked
            # (the v85 record), never held by a human, nothing snoozed
            lead_node = nodes["Lead pipeline"]
            assert lead_node["overdue"] == 1, lead_node
            assert lead_node["acked"] == 0 and lead_node["snoozed"] == 0
            assert len(lead_node["overdue_instances"]) == 1
            assert lead_node["overdue_instances"][0]["ref"] == "L-1"
            assert lead_node["overdue_instances"][0]["escalation"] == \
                {"count": 0, "last_delivery": "", "acked_by": "",
                 "snooze_until": "", "snooze_active": False}

            # the leg's fired children carry the same sub-counts (this
            # leg's child has no leg SLA, so nothing is overdue on it)
            leg0 = chains[0]["legs"][0]
            assert leg0["counts"] == {"in_state": 1, "fired": 1, "overdue": 0,
                                      "acked": 0, "snoozed": 0}, leg0

            # the LOAN runs out: at base+5h the snooze has passed - the
            # ack itself still shows (the take remains), snoozed drops
            chains2 = await _system_chains(user["id"], sid,
                                           now=base + timedelta(hours=5))
            nodes2 = {n["process"]: n for n in chains2[0]["nodes"]}
            assert nodes2["Customer onboarding"]["acked"] == 1
            assert nodes2["Customer onboarding"]["snoozed"] == 0
            got = nodes2["Customer onboarding"]["overdue_instances"][0]
            assert got["escalation"]["acked_by"] == "ops"
            assert got["escalation"]["snooze_active"] is False

            # re-acking without a snooze replaces the loan with ownership
            r = await client.post(
                f"/processes/{onboard_pid}/instances/{ob_iid}/escalations/ack",
                headers=h, json={"by": "ops-lead", "note": "mine now"})
            assert r.status_code == 200, r.text
            chains3 = await _system_chains(user["id"], sid, now=later1)
            nodes3 = {n["process"]: n for n in chains3[0]["nodes"]}
            assert nodes3["Customer onboarding"]["acked"] == 1
            assert nodes3["Customer onboarding"]["snoozed"] == 0
            got3 = nodes3["Customer onboarding"]["overdue_instances"][0]
            assert got3["escalation"]["acked_by"] == "ops-lead"
            assert got3["escalation"]["snooze_until"] == ""
            assert got3["escalation"]["snooze_active"] is False

            # the API wire (the real clock the board rides) carries the
            # same shape: OB-9 is late NOW, still listed, wearing the ack
            r = await client.get(f"/systems/{sid}", headers=h)
            assert r.status_code == 200, r.text
            api_nodes = {n["process"]: n
                         for n in r.json()["chains"][0]["nodes"]}
            api_ob = api_nodes["Customer onboarding"]
            assert api_ob["overdue"] == 1 and api_ob["acked"] == 1
            assert api_ob["snoozed"] == 0  # the loan was replaced
            assert api_ob["overdue_instances"][0]["instance_id"] == ob_iid
            # the lead is not late on the REAL clock yet - honest absence
            assert api_nodes["Lead pipeline"]["overdue"] == 0
            assert api_nodes["Lead pipeline"]["overdue_instances"] == []

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2) the escalation-history sparkline per machine
# ---------------------------------------------------------------------------

def test_v94_escalation_history():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "spark")
            h = _auth(user["token"])

            r = await client.post("/processes", headers=h, json={
                "name": "Spark machine",
                "definition": {"states": ["a", "done"], "initial": "a",
                               "transitions": [
                                   {"name": "finish", "from": "a", "to": "done"}]}})
            assert r.status_code == 201, r.text
            pid = r.json()["id"]
            iids = []
            for ref in ("SP-1", "SP-2"):
                r = await client.post(f"/processes/{pid}/instances", headers=h,
                                      json={"ref": ref, "title": f"the {ref} breach",
                                            "due_in_seconds": 1})
                assert r.status_code == 201, r.text
                iids.append(r.json()["id"])
            time.sleep(1.2)

            # the door knocks both (no policy - the v85 record), the human
            # takes one: three rows TODAY, straight from the real door
            report = await _door(user["id"])
            assert {e["ref"] for e in report["recorded"]} == {"SP-1", "SP-2"}
            r = await client.post(
                f"/processes/{pid}/instances/{iids[0]}/escalations/ack",
                headers=h, json={"by": "ops", "note": "on it"})
            assert r.status_code == 200, r.text

            # the past is on the record too - rows stamped into their own
            # days: a door knock 10d back, the machine's own escalate move
            # 5d back, a digest receipt 3d back, and one OUTSIDE the window
            await _backfill_log(pid, iids[0], user["id"],
                                transition="escalated", days_ago=10)
            await _backfill_log(pid, iids[0], user["id"],
                                transition="escalate", days_ago=5)
            await _backfill_log(pid, iids[1], user["id"],
                                transition="escalation_digest", days_ago=3)
            await _backfill_log(pid, iids[0], user["id"],
                                transition="escalated", days_ago=20)

            a = await _analytics(user["id"], pid)
            hist = a["escalation_history"]
            assert hist["window_days"] == 14
            days = hist["days"]
            assert len(days) == 14
            # chronological, oldest first, dates are the day keys
            assert days[0]["date"] < days[-1]["date"]
            assert [d["date"] for d in days] == sorted(d["date"] for d in days)

            def _bucket(offset_days: int) -> dict:
                want = ((datetime.now(timezone.utc)
                         - timedelta(days=offset_days)).date().isoformat())
                return next(d for d in days if d["date"] == want)

            today = _bucket(0)
            assert today["escalations"] == 2, today      # the door's knocks
            assert today["acknowledgements"] == 1, today  # the human's receipt
            assert today["digests"] == 0
            assert _bucket(10)["escalations"] == 1
            assert _bucket(5)["escalations"] == 1         # the machine's own move
            assert _bucket(3)["digests"] == 1
            # the empty days are zero-filled, never missing
            quiet = _bucket(4)
            assert quiet == {"date": quiet["date"], "escalations": 0,
                             "acknowledgements": 0, "digests": 0, "total": 0}
            # the 20d-ago row is honestly outside the named window
            assert ((datetime.now(timezone.utc)
                     - timedelta(days=20)).date().isoformat()
                    not in {d["date"] for d in days})
            # every bucket's total adds up
            assert all(d["total"] == d["escalations"] + d["acknowledgements"]
                       + d["digests"] for d in days)
            # the top-line counters still read the whole log - including
            # the 20d-ago knock the sparkline window honestly leaves out
            assert a["escalations"] == 5 and a["acknowledgements"] == 1
            assert a["digests"] == 1

            # the API wire (the board's own call) carries the same shape
            r = await client.get(f"/processes/{pid}/analytics", headers=h)
            assert r.status_code == 200, r.text
            wire = r.json()["escalation_history"]
            assert wire["window_days"] == 14 and len(wire["days"]) == 14
            assert wire["days"][-1]["acknowledgements"] >= 1

            # a machine nobody knocked shows an honest all-zero window
            r = await client.post("/processes", headers=h, json={
                "name": "Quiet machine",
                "definition": {"states": ["a", "done"], "initial": "a",
                               "transitions": [
                                   {"name": "finish", "from": "a", "to": "done"}]}})
            assert r.status_code == 201, r.text
            a2 = await _analytics(user["id"], r.json()["id"])
            assert a2["escalation_history"]["window_days"] == 14
            assert all(d["total"] == 0
                       for d in a2["escalation_history"]["days"])

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3) the version pin
# ---------------------------------------------------------------------------

def test_v94_version_pin():
    assert settings.version == "1.95.0"
