"""v91 tests - the boards catch up with the door.

v85-v90 built the escalation door and the cross-department chains; the
operator still had to live in the API to see what the door was doing.
v91 closes that gap:

* THE OVERDUE-ATTENTION VIEW: GET /processes/attention - every OPEN
  instance past its SLA across ALL machines, most-overdue first, each
  row carrying the door's escalation book (attempts, last delivery, the
  ack), the machine's own policy line, and the journey-leg flag. Closed
  entities are not asking for attention; owner scoping is like every
  read; the clock compares in Python (the SQLite naive/aware trap).
* ESCALATION-POLICY EDITING FROM /processes: PATCH
  /processes/{id}/escalation-policy - the SAME loud validation the
  definition carries (unknown keys, unknown channels, the two clocks,
  to-vs-handlers), policy=None removes it, and because the door reads
  the policy fresh at every sweep the new rhythm rules the NEXT tick -
  cadence changes AND knock <-> digest switches - while the running
  instances and their episodes are untouched. business.policy_updated
  rides the event door so workflows can react to the re-tune.
* JOURNEY VISIBILITY ON THE SHELF: GET /operators carries the
  cross-department legs per operator card - 'out' (this operator's
  machine opens the next department's leg) and 'in' (who feeds it) -
  so the chains are visible BEFORE the install, not only after.
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
    "states": ["a", "b", "done", "dead"],
    "initial": "a",
    "transitions": [
        {"name": "go", "from": "a", "to": "b"},
        {"name": "finish", "from": "b", "to": "done"},
        {"name": "kill", "from": "a", "to": "dead"},
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
        "email": f"v91-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v91 {tag}",
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


async def _attention(user_id: str, *, now: datetime | None = None,
                     limit: int = 200) -> dict:
    """The attention feed at the service level - the same injectable clock."""
    from app.db import AsyncSessionLocal
    from app.services import business_processes as bp_svc

    async with AsyncSessionLocal() as session:
        return await bp_svc.attention_feed(session, user_id, limit=limit, now=now)


# ---------------------------------------------------------------------------
# 1) the policy editor - the same loud validation, the same door
# ---------------------------------------------------------------------------

def test_v91_policy_edit_roundtrip():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "policy")
            h = _auth(user["token"])

            definition = {**MACHINE, "journeys": [
                {"on_state": "b", "open": {"process": "The next machine",
                                           "title_template": "Next - {title}"}}]}
            r = await client.post("/processes", headers=h, json={
                "name": "edited machine", "definition": definition})
            assert r.status_code == 201, r.text
            pid = r.json()["id"]

            # a machine can be born without a policy and GAIN one later
            assert r.json()["escalation_summary"] == "no escalation policy"
            r = await client.patch(
                f"/processes/{pid}/escalation-policy", headers=h,
                json={"policy": {"channel": "email", "to": "ops@py8n.test",
                                 "repeat_every_seconds": 120,
                                 "max_repeats": 2},
                      "actor": "the board"})
            assert r.status_code == 200, r.text
            body = r.json()
            pol = body["escalation_policy"]
            assert pol["channel"] == "email" and pol["to"] == "ops@py8n.test"
            assert pol["repeat_every_seconds"] == 120 and pol["max_repeats"] == 2
            assert pol["mode"] == "knock"
            assert "email" in body["escalation_summary"]
            # the journeys survive the edit - the definition is patched, not rebuilt
            assert body["journeys"] and body["journeys"][0]["on_state"] == "b"

            # persisted: a fresh read carries the new rhythm
            r = await client.get(f"/processes/{pid}", headers=h)
            assert r.json()["escalation_policy"]["repeat_every_seconds"] == 120

            # the SAME loud validation the definition carries
            r = await client.patch(
                f"/processes/{pid}/escalation-policy", headers=h,
                json={"policy": {"channel": "fax"}})
            assert r.status_code == 400 and "fax" in r.json()["detail"]
            r = await client.patch(
                f"/processes/{pid}/escalation-policy", headers=h,
                json={"policy": {"channel": "email", "wat": 1}})
            assert r.status_code == 400 and "unknown key" in r.json()["detail"]
            r = await client.patch(
                f"/processes/{pid}/escalation-policy", headers=h,
                json={"policy": {"channel": "email", "mode": "knock",
                                 "repeat_every_seconds": 120,
                                 "digest_every_seconds": 60}})
            assert r.status_code == 400 and \
                "only means something with mode='digest'" in r.json()["detail"]

            # knock -> digest - the switch the editor offers
            r = await client.patch(
                f"/processes/{pid}/escalation-policy", headers=h,
                json={"policy": {"channel": "email", "to": "ops@py8n.test",
                                 "mode": "digest",
                                 "digest_every_seconds": 86400}})
            assert r.status_code == 200, r.text
            assert r.json()["escalation_policy"]["mode"] == "digest"
            assert "digest" in r.json()["escalation_summary"]

            # policy=None removes it - the machine falls back to the v85
            # semantics (one knock per stint, event-only); journeys survive
            r = await client.patch(
                f"/processes/{pid}/escalation-policy", headers=h,
                json={"policy": None, "actor": "the board"})
            assert r.status_code == 200, r.text
            assert r.json()["escalation_policy"] is None
            assert r.json()["escalation_summary"] == "no escalation policy"
            assert r.json()["journeys"], "the journey must survive the removal"

            # every re-tune is on the record
            res = await client.get("/events", headers=h,
                                   params={"type": "business.policy_updated"})
            evs = res.json()["events"]
            assert len(evs) == 3, evs
            assert {e["payload"]["escalation_summary"] for e in evs} == {
                "stuck -> email (x3, every 120s) -> ops@py8n.test",
                "stuck -> daily digest over email (x4) -> ops@py8n.test",
                "no escalation policy",
            }
            assert evs[0]["payload"]["process_id"] == pid

            # an unknown process is refused, loudly
            r = await client.patch(
                "/processes/no-such-machine/escalation-policy", headers=h,
                json={"policy": {"channel": "email"}})
            assert r.status_code == 400

    _sync(_wrap(_go()))


def test_v91_policy_edit_rules_the_next_tick():
    """The promise the editor makes: the door reads the policy fresh at
    every sweep, so the new cadence (and the knock -> digest switch)
    rules the NEXT tick - the running episode continues, untouched."""

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "door")
            h = _auth(user["token"])
            base = datetime.now(timezone.utc)

            r = await client.post("/processes", headers=h, json={
                "name": "re-tuned machine",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "email", "to": "ops@py8n.test",
                    "repeat_every_seconds": 3600, "max_repeats": 5}}})
            assert r.status_code == 201, r.text
            pid = r.json()["id"]
            r = await client.post(f"/processes/{pid}/instances", headers=h,
                                  json={"ref": "RT-1", "title": "the slow one",
                                        "due_in_seconds": 1})
            assert r.status_code == 201, r.text

            # attempt 1 at T0 = base+2 (past the 1s SLA; honest skip - no
            # endpoint bound, on the record)
            report = await _door(user["id"], now=base + timedelta(seconds=2))
            assert len(report["recorded"]) == 1, report
            assert report["recorded"][0]["attempt"] == 1

            # T0+600: the OLD cadence (3600s) holds the door - too soon
            report = await _door(user["id"], now=base + timedelta(seconds=602))
            assert report["escalated"] == [] and report["recorded"] == []
            assert any(x["reason"] == "too_soon" for x in report["held"]), report

            # the board re-tunes the cadence to 60s
            r = await client.patch(
                f"/processes/{pid}/escalation-policy", headers=h,
                json={"policy": {"channel": "email", "to": "ops@py8n.test",
                                 "repeat_every_seconds": 60,
                                 "max_repeats": 5}})
            assert r.status_code == 200, r.text

            # T0+660: the NEW cadence rules - attempt 2 fires (660s into an
            # episode the OLD 3600s cadence would still be holding)
            report = await _door(user["id"], now=base + timedelta(seconds=662))
            assert len(report["recorded"]) == 1, report
            assert report["recorded"][0]["attempt"] == 2, report

            # knock -> digest, live mid-episode: the attempt-2 bookkeeping
            # is kept, the machine joins the summary rhythm
            r = await client.patch(
                f"/processes/{pid}/escalation-policy", headers=h,
                json={"policy": {"channel": "email", "to": "ops@py8n.test",
                                 "mode": "digest",
                                 "digest_every_seconds": 60}})
            assert r.status_code == 200, r.text

            # T0+1300: the candidate joins a bucket (pending_since = now;
            # the window has not elapsed yet) - nothing is sent
            report = await _door(user["id"], now=base + timedelta(seconds=1302))
            assert report["digest"]["sent"] == [], report
            assert len(report["digest"]["pending"]) == 1, report

            # T0+1361: the window elapses - ONE summary covers the item
            report = await _door(user["id"], now=base + timedelta(seconds=1363))
            sent = report["digest"]["sent"]
            assert len(sent) == 1 and sent[0]["items"] == 1, report
            assert sent[0]["delivery"] == "skipped"  # no endpoint - honest

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2) the overdue-attention view - every machine, one panel
# ---------------------------------------------------------------------------

def test_v91_attention_feed():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "attention")
            h = _auth(user["token"])
            base = datetime.now(timezone.utc)

            ra = await client.post("/processes", headers=h, json={
                "name": "machine A",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "email", "to": "a@py8n.test",
                    "repeat_every_seconds": 3600, "max_repeats": 3}}})
            assert ra.status_code == 201, ra.text
            pa = ra.json()["id"]
            rb = await client.post("/processes", headers=h, json={
                "name": "machine B", "definition": MACHINE})
            assert rb.status_code == 201, rb.text
            pb = rb.json()["id"]

            # A1: overdue, and the door has already knocked once
            r = await client.post(f"/processes/{pa}/instances", headers=h,
                                  json={"ref": "A-1", "title": "the overdue one",
                                        "due_in_seconds": 1})
            assert r.status_code == 201, r.text
            # A2: fresh - its SLA still holds
            r = await client.post(f"/processes/{pa}/instances", headers=h,
                                  json={"ref": "A-2", "due_in_seconds": 3600})
            assert r.status_code == 201, r.text
            # B2: overdue but CLOSED - a terminal state is not asking
            r = await client.post(f"/processes/{pb}/instances", headers=h,
                                  json={"ref": "B-2", "due_in_seconds": 1})
            iid_b2 = r.json()["id"]
            r = await client.post(
                f"/processes/{pb}/instances/{iid_b2}/advance", headers=h,
                json={"transition": "go"})
            assert r.status_code == 200, r.text
            r = await client.post(
                f"/processes/{pb}/instances/{iid_b2}/advance", headers=h,
                json={"transition": "finish"})
            assert r.status_code == 200, r.text

            # the door knocks once on A1 (honest skip - no endpoint);
            # B2 is terminal - closed entities get no knock either
            report = await _door(user["id"], now=base + timedelta(seconds=2))
            assert len(report["recorded"]) == 1, report

            # B1: overdue, born from a cross-operator hand-off, and the
            # door has NEVER knocked here (it was born after the sweep)
            r = await client.post(f"/processes/{pb}/instances", headers=h,
                                  json={"ref": "B-1", "title": "the hand-off",
                                        "due_in_seconds": 1,
                                        "context": {"journey": {
                                            "from_process": "Lead pipeline",
                                            "from_state": "won"}}})
            assert r.status_code == 201, r.text

            feed = await _attention(user["id"],
                                    now=base + timedelta(seconds=3))
            rows = feed["attention"]
            assert feed["count"] == 2 and feed["machines"] == 2, feed
            # most overdue first (A-1's due_at is the older of the two)
            assert [r["ref"] for r in rows] == ["A-1", "B-1"], rows

            a1 = rows[0]
            assert a1["process_name"] == "machine A"
            assert a1["state"] == "a" and a1["overdue_seconds"] >= 2
            assert a1["escalation"]["count"] == 1
            assert a1["escalation"]["last_delivery"] == "skipped"
            assert a1["escalation"]["acked_by"] == ""
            assert "email" in a1["escalation_summary"]
            assert a1["journey_leg"] is False

            b1 = rows[1]
            assert b1["process_name"] == "machine B"
            assert b1["journey_leg"] is True
            assert b1["escalation"] is None  # the door never knocked here
            assert b1["escalation_summary"] == "no escalation policy"

            # the ack shows on the row - acknowledged, not resolved: the
            # entity is still open past its SLA and still on the feed
            insts = (await client.get(f"/processes/{pa}/instances",
                                      headers=h)).json()["instances"]
            a1_id = next(x["id"] for x in insts if x["ref"] == "A-1")
            r = await client.post(
                f"/processes/{pa}/instances/{a1_id}/escalations/ack",
                headers=h, json={"by": "dana", "note": "calling now"})
            assert r.status_code == 200, r.text
            feed = await _attention(user["id"],
                                    now=base + timedelta(seconds=4))
            a1b = next(x for x in feed["attention"] if x["ref"] == "A-1")
            assert a1b["escalation"]["acked_by"] == "dana"

            # the limit keeps the panel humane (most overdue wins)
            feed = await _attention(user["id"], limit=1,
                                    now=base + timedelta(seconds=4))
            assert [x["ref"] for x in feed["attention"]] == ["A-1"]

            # owner scoping - another owner's feed is their own
            other = await _mk_user(client, "other")
            feed = await _attention(other["id"],
                                    now=base + timedelta(seconds=4))
            assert feed["attention"] == [] and feed["count"] == 0

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3) the chains on the shelf - journey legs on the operator cards
# ---------------------------------------------------------------------------

def test_v91_catalog_journeys():
    async def _go():
        async with _client() as client:
            res = await client.get("/operators")
            assert res.status_code == 200, res.text
            ops = {o["slug"]: o for o in res.json()["operators"]}

            sales = ops["sales-operator"]["journeys"]
            assert any(j["direction"] == "out" and
                       j["from_process"] == "Lead pipeline" and
                       j["from_operator"] == "sales-operator" and
                       j["on_state"] == "won" and
                       j["opens"] == "Customer onboarding" for j in sales), sales

            fin = ops["finance-operator"]["journeys"]
            fin_in = {(j["direction"], j["from_process"], j["opens"])
                      for j in fin}
            assert fin_in == {
                ("in", "Customer onboarding", "Invoice lifecycle"),
                ("in", "Delivery pipeline", "Invoice lifecycle"),
                ("in", "Appointment journey", "Invoice lifecycle"),
            }, fin

            ops_op = ops["operations-operator"]["journeys"]
            assert any(j["direction"] == "out" and
                       j["from_process"] == "Customer onboarding" and
                       j["on_state"] == "handed_off" and
                       j["opens"] == "Invoice lifecycle" for j in ops_op)
            assert any(j["direction"] == "in" and
                       j["from_process"] == "Lead pipeline" for j in ops_op)

            log = ops["logistics-operator"]["journeys"]
            assert any(j["direction"] == "in" and
                       j["from_process"] == "Purchase lifecycle" for j in log)
            assert any(j["direction"] == "out" and
                       j["from_process"] == "Delivery pipeline" and
                       j["opens"] == "Invoice lifecycle" for j in log)
            # the opened legs carry their own SLA promise, surfaced on the card
            out_leg = next(j for j in log if j["direction"] == "out")
            assert out_leg["due_in_seconds"] == 5 * 24 * 3600

            # every leg shows exactly twice - out on the source's card,
            # in on the target's card (5 chains, v90's full set)
            total = sum(len(o["journeys"]) for o in ops.values())
            assert total == 10, total

    _sync(_wrap(_go()))


def test_v91_version():
    assert settings.version == "1.100.0"
