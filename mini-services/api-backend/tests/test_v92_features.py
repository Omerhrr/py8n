"""v92 tests - the loop closes from the operator's chair.

v91 put the door's business ON the boards (the attention view, the
policy editor, the chains on the shelf); v92 closes the loop:

* THE POLICY SAVE COMES WITH A DIFF: PATCH /processes/{id}/escalation-policy
  now returns ``policy_diff`` - the before policy, the after policy and
  the exact keys that moved, both sides through the SAME validator so a
  no-op save diffs empty, a knock->digest switch names the clock keys it
  re-anchored, and a removal names every key the machine gave back. The
  business.policy_updated event carries the same ``changed`` list, so
  workflows can react to WHAT moved, not just that something did. The
  journeys ride through the save untouched (the definition is patched on
  a fresh dict - v91's promise, re-proven through the diff receipt).
* ACK/SNOOZE STRAIGHT FROM THE ATTENTION ROW: the attention row already
  carried the ack book; the board now answers it in place. The receipt
  is the SAME endpoint the machine view uses (POST
  /processes/{pid}/instances/{iid}/escalations/ack), so this test proves
  the row has everything the receipt needs: process_id, instance_id, and
  an empty acked_by that the POST fills - the row stays on the feed
  wearing the ack + the snooze it asked for.
* THE CHAINS DRAWN ON OPERATOR DETAIL: GET /operators/{slug} now names
  the chains the operator sits in (revenue / supply / care), each with
  the ordered walk of operators, the resolved legs (fire state, the
  process that opens itself, the leg's own SLA) and ``position`` - where
  this operator's node sits in the drawing. The chains RESOLVE from
  _JOURNEYS at import (a drifted walk refuses to boot), and the catalog
  cards carry the chain slugs beside the v91 legs.
"""

from __future__ import annotations

import asyncio
import sys
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

JOURNEY = {"on_state": "b",
           "open": {"process": "Invoice lifecycle",
                    "title_template": "Billing - {title}"}}

CANONICAL_KEYS = {"channel", "to", "handlers", "mode", "repeat_every_seconds",
                  "digest_every_seconds", "max_repeats", "message_template"}


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
        "email": f"v92-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v92 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1) the policy save comes with a before/after diff
# ---------------------------------------------------------------------------

def test_v92_policy_diff_receipt():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "diff")
            h = _auth(user["token"])

            # a machine born WITH a journey - the save must not disturb it
            r = await client.post("/processes", headers=h, json={
                "name": "the diff machine",
                "definition": {**MACHINE, "journeys": [JOURNEY]}})
            assert r.status_code == 201, r.text
            pid = r.json()["id"]

            # GAIN: no policy -> the first one. The before side is None
            # and every canonical key counts as moved.
            r = await client.patch(
                f"/processes/{pid}/escalation-policy", headers=h,
                json={"policy": {"channel": "email", "to": "ops@py8n.test",
                                 "repeat_every_seconds": 120,
                                 "max_repeats": 2},
                      "actor": "the board"})
            assert r.status_code == 200, r.text
            body = r.json()
            diff = body["policy_diff"]
            assert diff["before"] is None
            assert diff["after"]["channel"] == "email"
            assert diff["after"]["mode"] == "knock"
            assert diff["after"]["repeat_every_seconds"] == 120
            assert diff["changed"] == sorted(CANONICAL_KEYS), diff["changed"]
            # the process body is unchanged in shape - the diff rides beside it
            assert body["escalation_policy"]["to"] == "ops@py8n.test"
            # v91's promise, re-proven through the receipt: journeys survive
            # (the normalized shape validate_definition lands on the record)
            assert body["journeys"] == [{
                "on_state": "b",
                "open": {"process": "Invoice lifecycle", "state": None,
                         "title_template": "Billing - {title}",
                         "ref_template": "", "memory": {},
                         "due_in_seconds": None}}]

            # EDIT: knock -> digest. The switch names the rhythm AND the
            # clock keys it re-anchored (the old cadence handed back to
            # the knock default, the digest clock taken over).
            r = await client.patch(
                f"/processes/{pid}/escalation-policy", headers=h,
                json={"policy": {"channel": "email", "to": "ops@py8n.test",
                                 "mode": "digest",
                                 "digest_every_seconds": 300,
                                 "max_repeats": 2},
                      "actor": "the board"})
            assert r.status_code == 200, r.text
            diff = r.json()["policy_diff"]
            assert diff["before"]["mode"] == "knock"
            assert diff["before"]["repeat_every_seconds"] == 120
            assert diff["after"]["mode"] == "digest"
            assert diff["after"]["digest_every_seconds"] == 300
            assert diff["changed"] == ["digest_every_seconds", "mode",
                                       "repeat_every_seconds"], diff["changed"]

            # NO-OP: the same policy again - the receipt is EMPTY. Both
            # sides went through the same validator, so defaults compare
            # equal and nothing pretends to have moved.
            r = await client.patch(
                f"/processes/{pid}/escalation-policy", headers=h,
                json={"policy": {"channel": "email", "to": "ops@py8n.test",
                                 "mode": "digest",
                                 "digest_every_seconds": 300,
                                 "max_repeats": 2},
                      "actor": "the board"})
            assert r.status_code == 200, r.text
            assert r.json()["policy_diff"]["changed"] == []

            # REMOVE: the machine gives every key back.
            r = await client.patch(
                f"/processes/{pid}/escalation-policy", headers=h,
                json={"policy": None, "actor": "the board"})
            assert r.status_code == 200, r.text
            diff = r.json()["policy_diff"]
            assert diff["after"] is None
            assert diff["changed"] == sorted(CANONICAL_KEYS)
            assert r.json()["escalation_policy"] is None
            assert r.json()["escalation_summary"] == "no escalation policy"
            # the journey survived ALL FOUR saves
            assert r.json()["journeys"], "journeys must survive the saves"

            # the loud refusals never reached a save - no diff, no change
            r = await client.patch(
                f"/processes/{pid}/escalation-policy", headers=h,
                json={"policy": {"channel": "email", "wat": 1}})
            assert r.status_code == 400, r.text

            # the event door carries WHAT moved, not just that it moved
            evs = (await client.get("/events", headers=h,
                                    params={"type": "business.policy_updated"}
                                    )).json()["events"]
            changed_by_call = [e["payload"]["changed"] for e in evs]
            assert sorted(CANONICAL_KEYS) in changed_by_call
            assert ["digest_every_seconds", "mode",
                    "repeat_every_seconds"] in changed_by_call
            assert [] in changed_by_call

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2) ack/snooze straight from the attention row - the row carries the receipt
# ---------------------------------------------------------------------------

def test_v92_attention_row_answers_the_door():
    async def _go():
        from datetime import datetime, timedelta, timezone
        from app.db import AsyncSessionLocal
        from app.services import business_processes as bp_svc

        async with _client() as client:
            user = await _mk_user(client, "attention")
            h = _auth(user["token"])

            r = await client.post("/processes", headers=h, json={
                "name": "the row machine",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "email", "to": "row@py8n.test",
                    "repeat_every_seconds": 3600, "max_repeats": 3}}})
            assert r.status_code == 201, r.text
            pid = r.json()["id"]

            base = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as session:
                await bp_svc.start_instance(
                    session, pid, owner_id=user["id"], ref="ROW-1",
                    title="the overdue one", due_in_seconds=2,
                    actor="test")
                await session.commit()

            # past its SLA, on the feed, the door already knocked once
            # (honest skip - no endpoint bound, the book is still written)
            async with AsyncSessionLocal() as session:
                await bp_svc.escalate_stuck(session, owner_id=user["id"],
                                            actor="test",
                                            now=base + timedelta(seconds=3))
                await session.commit()

            async with AsyncSessionLocal() as session:
                feed = await bp_svc.attention_feed(
                    session, user["id"], now=base + timedelta(seconds=4))
            rows = [x for x in feed["attention"] if x["ref"] == "ROW-1"]
            assert len(rows) == 1, feed
            row = rows[0]
            # everything the row's inline receipt needs is on the row
            assert row["process_id"] == pid
            assert row["instance_id"]
            assert row["escalation"]["acked_by"] == ""
            assert row["escalation"]["count"] == 1
            assert row["escalation"]["last_delivery"] == "skipped"

            # the receipt straight from the row - the SAME endpoint the
            # machine view posts to, addressed by the row's own ids
            r = await client.post(
                f"/processes/{row['process_id']}/instances/{row['instance_id']}"
                f"/escalations/ack",
                headers=h, json={"by": "dana", "note": "on it",
                                 "snooze_hours": 1})
            assert r.status_code == 200, r.text

            # the row stays on the feed (still open, still past SLA)
            # wearing the ack + the snooze it asked for
            async with AsyncSessionLocal() as session:
                feed = await bp_svc.attention_feed(
                    session, user["id"], now=base + timedelta(seconds=5))
            row2 = next(x for x in feed["attention"] if x["ref"] == "ROW-1")
            assert row2["escalation"]["acked_by"] == "dana"
            assert row2["escalation"]["snooze_until"], row2["escalation"]

            # the door holds while the loan runs (the ack owns the stint)
            async with AsyncSessionLocal() as session:
                report = await bp_svc.escalate_stuck(
                    session, owner_id=user["id"], actor="test",
                    now=base + timedelta(seconds=6))
                await session.commit()
            assert all(e["ref"] != "ROW-1" for e in report["recorded"]), report

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3) the chains drawn on operator detail - the named walks, resolved
# ---------------------------------------------------------------------------

def test_v92_operator_detail_chains():
    async def _go():
        from app.services.operators import _JOURNEYS, _RESOLVED_CHAINS

        async with _client() as client:
            # the walk resolves against the REAL journeys - no drift
            resolved = {(c["slug"], l["from_process"], l["on_state"]):
                        (l["opens"], l["due_in_seconds"])
                        for c in _RESOLVED_CHAINS for l in c["legs"]}
            assert len(resolved) == 5, resolved  # 3 chains, 5 legs
            for (slug_, src, state), (opens, due) in resolved.items():
                hits = [j for j in _JOURNEYS[src] if j["on_state"] == state]
                assert len(hits) == 1
                assert hits[0]["open"]["process"] == opens
                assert hits[0]["open"].get("due_in_seconds") == due

            r = await client.get("/operators/sales-operator")
            assert r.status_code == 200, r.text
            detail = r.json()
            chains = detail["chains"]
            assert [c["slug"] for c in chains] == ["revenue"]
            rev = chains[0]
            assert rev["position"] == 0  # the chain starts here
            assert [o["slug"] for o in rev["operators"]] == [
                "sales-operator", "operations-operator", "finance-operator"]
            legs = rev["legs"]
            assert [(l["from_process"], l["on_state"], l["opens"])
                    for l in legs] == [
                ("Lead pipeline", "won", "Customer onboarding"),
                ("Customer onboarding", "handed_off", "Invoice lifecycle")]
            assert legs[0]["from_operator"] == "sales-operator"
            assert legs[0]["opens_operator"] == "operations-operator"
            assert legs[0]["due_in_seconds"] is None
            assert legs[1]["opens_operator"] == "finance-operator"
            assert legs[1]["due_in_seconds"] == 5 * 24 * 3600

            # finance TERMINATES all three chains
            r = await client.get("/operators/finance-operator")
            fin = {c["slug"]: c for c in r.json()["chains"]}
            assert set(fin) == {"revenue", "supply", "care"}
            assert [fin["revenue"]["position"], fin["supply"]["position"],
                    fin["care"]["position"]] == [2, 2, 1]
            assert all(c["operators"][-1]["slug"] == "finance-operator"
                       for c in fin.values())

            # clinic STARTS care; meeting sits in no chain
            r = await client.get("/operators/clinic-operator")
            care = r.json()["chains"]
            assert [c["slug"] for c in care] == ["care"]
            assert care[0]["position"] == 0
            r = await client.get("/operators/meeting-operator")
            assert r.json()["chains"] == []

            # the shelf cards name their chains beside the v91 legs
            res = await client.get("/operators")
            ops = {o["slug"]: o for o in res.json()["operators"]}
            assert ops["finance-operator"]["chains"] == \
                ["revenue", "supply", "care"]
            assert ops["sales-operator"]["chains"] == ["revenue"]
            assert ops["meeting-operator"]["chains"] == []
            total = sum(len(o["journeys"]) for o in ops.values())
            assert total == 10, total  # the v91 legs are untouched

            # unknown operators still refuse loud (no chains to give)
            r = await client.get("/operators/no-such-operator")
            assert r.status_code == 404, r.text

    _sync(_wrap(_go()))


def test_v92_version():
    assert settings.version == "1.103.0"
