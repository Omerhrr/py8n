"""v96 tests - the human picks the moment, the grid opens, the history deepens.

v92 put the receipt on the attention row; v96 makes it a SCHEDULE:

* ACK-AND-RESCHEDULE ON ATTENTION ROWS: the v88 receipt may carry
  reschedule_in_minutes - the door's next knock lands at an EXPLICIT
  moment (reschedule_at on the receipt) instead of a duration-shaped
  snooze. The gates hold until the stamp passes, then re-knock on the
  episode's own cadence (attempt count continuing); in digest mode a
  rescheduled take holds the item OUT of the buckets until the moment
  passes, then it rides the next digest. One clock per receipt:
  snooze_hours AND a reschedule together refuse loud. The attention row
  wears the evidence (reschedule_at + how long it still holds).
* DRILL-DOWN FROM HEATMAP CELLS INTO A MACHINE'S DAY: the v95 grid says
  WHERE the door pressed; GET /processes/escalation-history/{pid}/{day}
  says WHAT happened - every escalation row the door wrote on that
  machine between the day's midnights (knocks/moves, acks, digest
  receipts) with the entity each belongs to. A badly shaped day refuses
  loud before the machine is even loaded.
* DEEPER CHAIN HISTORY (BEYOND 5 TRAVERSALS): the v95 chain map drew the
  5 most recent rides per leg - a leg that has ridden for months lost
  its older rides. /processes/chains?history_limit=N stretches the
  window (clamped 1..50) and each leg names whether the list was cut
  (history_truncated), so the operator-detail chain can offer "go
  deeper" honestly.
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
        "email": f"v96-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v96 {tag}",
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
    from app.db import AsyncSessionLocal
    from app.services import business_processes as bp_svc

    async with AsyncSessionLocal() as session:
        return await bp_svc.attention_feed(session, user_id, now=now)


async def _chains(user_id: str, *, history_limit: int | None = None) -> dict:
    from app.db import AsyncSessionLocal
    from app.services import business_processes as bp_svc

    async with AsyncSessionLocal() as session:
        if history_limit is None:
            return await bp_svc.chain_map(session, user_id)
        return await bp_svc.chain_map(session, user_id,
                                      history_limit=history_limit)


# ---------------------------------------------------------------------------
# 1. ack-and-reschedule - the receipt names the door's next knock
# ---------------------------------------------------------------------------

def test_v96_ack_and_reschedule():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "resched")
            h = _auth(user["token"])

            res = await client.post("/processes", headers=h, json={
                "name": "Resched machine",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "", "to": "",
                    "repeat_every_seconds": 60, "max_repeats": 3}}})
            assert res.status_code == 201, res.text
            pid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "R-1", "due_in_seconds": 1})
            assert res.status_code == 201, res.text
            iid = res.json()["id"]

            base = datetime.now(timezone.utc)
            # attempt 1 - the door knocks
            report = await _door(user["id"], now=base + timedelta(seconds=10))
            assert report["recorded"] or report["escalated"], report

            # the receipt with an EXPLICIT next-knock moment
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/escalations/ack",
                headers=h, json={"by": "ops", "note": "moved to friday",
                                 "reschedule_in_minutes": 30})
            assert res.status_code == 200, res.text
            ack = res.json()["ack"]
            assert ack["reschedule_in_minutes"] == 30, ack
            assert ack["reschedule_at"], ack

            # while the reschedule holds, the door is quiet - and the hold
            # names the stamp, not a snooze
            report = await _door(user["id"],
                                 now=base + timedelta(minutes=11))
            held = [x for x in report["held"] if x["instance_id"] == iid]
            assert held and held[0]["reason"] == "acknowledged", report
            assert held[0]["reschedule_at"] == ack["reschedule_at"], held
            assert held[0]["reschedule_remaining_seconds"] > 0, held
            assert "snooze_until" not in held[0], held

            # the attention row wears the evidence
            feed = await _attention(user["id"], now=base + timedelta(minutes=12))
            row = next(r for r in feed["attention"] if r["instance_id"] == iid)
            assert row["escalation"]["reschedule_at"] == ack["reschedule_at"], row
            assert row["escalation"]["reschedule_remaining_seconds"] > 0, row
            assert row["escalation"]["acked_by"] == "ops", row

            # the moment passes - the door RE-KNOCKS (attempt 2, same episode)
            report = await _door(user["id"],
                                 now=base + timedelta(minutes=31))
            knocks = [x for x in report["recorded"] + report["escalated"]
                      if x["instance_id"] == iid]
            assert knocks and knocks[0]["attempt"] == 2, report

            # one clock per receipt: snooze AND reschedule refuse loud
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/escalations/ack",
                headers=h, json={"by": "ops", "snooze_hours": 1,
                                 "reschedule_in_minutes": 30})
            assert res.status_code == 400, res.text
            assert "ONE loan" in res.json()["detail"], res.text
            # a negative reschedule refuses at the door of the API too
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/escalations/ack",
                headers=h, json={"by": "ops", "reschedule_in_minutes": -5})
            assert res.status_code == 422, res.text

    _sync(_wrap(_go()))


def test_v96_reschedule_holds_the_digest_bucket():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "resched-digest")
            h = _auth(user["token"])

            res = await client.post("/processes", headers=h, json={
                "name": "Resched digest",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "", "mode": "digest",
                    "digest_every_seconds": 60, "max_repeats": 3}}})
            assert res.status_code == 201, res.text
            pid = res.json()["id"]
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "RD-1", "due_in_seconds": 1})
            iid = res.json()["id"]

            base = datetime.now(timezone.utc)
            # the window walks once - digest #1 lists the item
            await _door(user["id"], now=base + timedelta(seconds=10))
            await _door(user["id"], now=base + timedelta(seconds=80))
            # the human takes it WITH an explicit reschedule
            res = await client.post(
                f"/processes/{pid}/instances/{iid}/escalations/ack",
                headers=h, json={"by": "ops",
                                 "reschedule_in_minutes": 90})
            assert res.status_code == 200, res.text

            # past the digest window but INSIDE the reschedule: the item
            # is held out of the bucket - no second summary
            report = await _door(user["id"], now=base + timedelta(minutes=30))
            assert report["digest"]["sent"] == [], report["digest"]
            held = [x for x in report["held"] if x["instance_id"] == iid]
            assert held and held[0]["reason"] == "acknowledged", report
            assert held[0]["reschedule_at"], held

            # the reschedule passes - the item rides the next digest
            report = await _door(user["id"], now=base + timedelta(minutes=95))
            sent = report["digest"]["sent"]
            assert sent and sent[0]["process_id"] == pid, report["digest"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the drill-down - one heatmap cell opened: the machine's day
# ---------------------------------------------------------------------------

def test_v96_heatmap_day_drill_down():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "drill")
            h = _auth(user["token"])

            res = await client.post("/processes", headers=h, json={
                "name": "Drill machine",
                "definition": {**MACHINE, "escalation_policy": {
                    "channel": "", "to": "",
                    "repeat_every_seconds": 60, "max_repeats": 3}}})
            pid = res.json()["id"]
            ids = {}
            for ref in ("D-1", "D-2"):
                res = await client.post(f"/processes/{pid}/instances",
                                        headers=h,
                                        json={"ref": ref, "due_in_seconds": 1})
                ids[ref] = res.json()["id"]
            base = datetime.now(timezone.utc)
            await _door(user["id"], now=base + timedelta(seconds=10))
            # one human ack lands in the day too - WITH a reschedule stamp
            res = await client.post(
                f"/processes/{pid}/instances/{ids['D-1']}/escalations/ack",
                headers=h, json={"by": "ops", "reschedule_in_minutes": 15})
            assert res.status_code == 200, res.text

            today = base.date().isoformat()
            res = await client.get(
                f"/processes/escalation-history/{pid}/{today}", headers=h)
            assert res.status_code == 200, res.text
            day = res.json()
            assert day["process_id"] == pid and day["day"] == today, day
            assert day["counts"]["escalations"] == 2, day
            assert day["counts"]["acks"] == 1, day
            assert day["total"] == 3, day
            kinds = {r["kind"] for r in day["rows"]}
            assert kinds == {"escalation", "ack"}, day
            ack_rows = [r for r in day["rows"] if r["kind"] == "ack"]
            assert ack_rows[0]["ref"] == "D-1", day
            assert ack_rows[0]["actor"] == "ops", day
            assert ack_rows[0]["reschedule_at"], day
            # every row names its entity
            assert all(r["ref"] in ("D-1", "D-2") for r in day["rows"]), day
            # chronological order
            ats = [r["at"] for r in day["rows"]]
            assert ats == sorted(ats), ats

            # a quiet day reads as data, not absence
            res = await client.get(
                f"/processes/escalation-history/{pid}/2020-01-01", headers=h)
            assert res.status_code == 200, res.text
            assert res.json()["total"] == 0 and res.json()["rows"] == [], res.json()

            # a badly shaped day refuses loud BEFORE the machine is loaded
            res = await client.get(
                f"/processes/escalation-history/{pid}/not-a-day", headers=h)
            assert res.status_code == 400, res.text
            assert "not an ISO date" in res.json()["detail"], res.text
            # an unknown machine hides (404), the same as every other read
            res = await client.get(
                f"/processes/escalation-history/nope/{today}", headers=h)
            assert res.status_code == 404, res.text

            # the grid itself still stands beside the drill-down
            res = await client.get("/processes/escalation-history?days=3",
                                   headers=h)
            grid = res.json()
            mine = next(m for m in grid["machines"] if m["process_id"] == pid)
            assert mine["cells"][today]["escalations"] == 2, mine

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. deeper chain history - the traversal window stretches beyond 5
# ---------------------------------------------------------------------------

def test_v96_chain_history_depth():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "depth")
            h = _auth(user["token"])

            res = await client.post("/processes", headers=h, json={
                "name": "Depth target", "definition": MACHINE})
            assert res.status_code == 201, res.text
            res = await client.post("/processes", headers=h, json={
                "name": "Depth head",
                "definition": {**MACHINE, "journeys": [
                    {"on_state": "b",
                     "open": {"process": "Depth target"}}]}})
            head_pid = res.json()["id"]

            # seven rides across the leg
            for i in range(7):
                res = await client.post(
                    f"/processes/{head_pid}/instances", headers=h,
                    json={"ref": f"H-{i}", "title": f"ride {i}"})
                iid = res.json()["id"]
                res = await client.post(
                    f"/processes/{head_pid}/instances/{iid}/advance",
                    headers=h, json={"transition": "go"})
                assert res.status_code == 200, res.text
                assert res.json().get("journeys_opened"), res.json()

            # the DEFAULT window is still the recent 5 - with the cut named
            leg = _chain_leg(await _chains(user["id"]))
            assert leg["opened"] == 7, leg
            assert len(leg["history"]) == 5, leg
            assert leg["history_truncated"] is True, leg
            assert leg["history_limit"] == 5, leg

            # the deeper window: all seven rides, no cut
            leg = _chain_leg(await _chains(user["id"], history_limit=25))
            assert len(leg["history"]) == 7, leg
            assert leg["history_truncated"] is False, leg
            assert leg["history_limit"] == 25, leg

            # the clamp holds on both ends
            leg = _chain_leg(await _chains(user["id"], history_limit=0))
            assert len(leg["history"]) == 1, leg
            leg = _chain_leg(await _chains(user["id"], history_limit=1000))
            assert len(leg["history"]) == 7 and leg["history_limit"] == 50, leg

            # the API wire carries the same depth
            async with _client() as c2:
                res = await c2.get("/processes/chains?history_limit=25",
                                   headers=h)
                leg = _chain_leg(res.json())
                assert len(leg["history"]) == 7, leg
                res = await c2.get("/processes/chains", headers=h)
                leg = _chain_leg(res.json())
                assert len(leg["history"]) == 5, leg

    def _chain_leg(map_out: dict) -> dict:
        chain = next(c for c in map_out["chains"]
                     if c["head_name"] == "Depth head")
        return chain["legs"][0]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the version pin
# ---------------------------------------------------------------------------

def test_v96_version_pin():
    from app.config import settings

    assert settings.version == "1.98.0"
