"""v98 tests - the machine board's receipt catches up, and the chain
history leaves the building as a file.

v96 made the receipt a schedule on the attention rows; v97 made the
digests and the chain map TELL that story. v98 closes the loop:

* MACHINE-BOARD ACK-FORM RESCHEDULE PARITY: the machine board's inline
  ack form (v88 by + note, v89 snooze) now carries the SAME reschedule
  clock the attention rows gained in v96 - reschedule_in_minutes posts
  to the SAME endpoint, one clock per receipt (snooze AND a reschedule
  together refuse loud), and the tracked row wears the loan chips. The
  backend needed nothing new - this round proves the exact wire the
  board posts: the ack-with-reschedule receipt lands on the machine's
  own book (the same shape the attention feed's rows read).
* PER-LEG CHAIN-HISTORY CSV EXPORT: GET /processes/chains/history.csv
  renders the SAME chain map the operator-detail chain draws
  (chain_map, zero drift) as one row per traversal, the chain and the
  leg named on every row so a spreadsheet can filter or pivot per leg.
  A leg with no rides yet still ships one inventory row with the ride
  columns empty - the absence reads as data. history_limit rides the
  map's own clamp (1..50), so the file honors exactly the window the
  depth toggle chose. The response names its own shape: Content-Type
  text/csv, Content-Disposition attachment, X-Py8n-Leg-Count and
  X-Py8n-Ride-Count. A leg TWO walks both carry (two machines firing
  into the same target - a diamond the map honestly draws twice) is
  written ONCE: a pivot over this file never counts the same ride
  twice. A machine with more out-legs than one chain walk carries
  ships its leftovers under the honest "side legs" group - real legs
  with real history, never a silent drop.
"""

from __future__ import annotations

import asyncio
import csv
import io
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
        "email": f"v98-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v98 {tag}",
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


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


# ---------------------------------------------------------------------------
# 1. the CSV export - the same map the operator-detail chain draws, as rows
# ---------------------------------------------------------------------------

def test_v98_chain_history_csv_export():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "csv")
            h = _auth(user["token"])

            # nothing installed: an honest file with only the header
            res = await client.get("/processes/chains/history.csv", headers=h)
            assert res.status_code == 200, res.text
            assert res.headers["content-type"].startswith("text/csv"), res.headers
            assert "attachment" in res.headers.get("content-disposition", "")
            assert "py8n-chain-history-" in res.headers.get("content-disposition", "")
            assert res.headers["x-py8n-leg-count"] == "0"
            assert res.headers["x-py8n-ride-count"] == "0"
            rows = _parse_csv(res.text)
            assert rows == [[
                "chain", "leg_from", "leg_on_state", "leg_to", "leg_sla_seconds",
                "leg_opened", "leg_open_now", "leg_stuck", "history_truncated",
                "ref", "title", "state", "opened_at", "due_at", "is_stuck",
                "overdue_seconds", "acked_by", "snooze_remaining_seconds",
                "instance_id", "process_id", "system"]], rows

            # install the revenue chain's three departments
            for slug in ("operations-operator", "sales-operator",
                         "finance-operator"):
                res = await client.post(f"/operators/{slug}/install",
                                        headers=h, json={})
                assert res.status_code == 200, res.text

            # both revenue legs ship their inventory row BEFORE any ride
            res = await client.get("/processes/chains/history.csv", headers=h)
            assert res.headers["x-py8n-leg-count"] == "2", res.headers
            assert res.headers["x-py8n-ride-count"] == "0", res.headers
            rows = _parse_csv(res.text)
            leg_rows = {r[1]: r for r in rows[1:]}
            assert set(leg_rows) == {"Lead pipeline", "Customer onboarding"}, leg_rows
            for name, row in leg_rows.items():
                assert row[0] == "Revenue chain", row          # the chain named
                assert row[5] == "0" and row[8] == "no", row   # no rides yet
                assert all(cell == "" for cell in row[9:20]), row  # the absence reads as data
                assert row[20], row                            # v100: the machine's system named (the breakdown dimension)
            assert leg_rows["Customer onboarding"][4] == str(5 * 24 * 3600), leg_rows

            # a fresh lead WINS - the leg opens the onboarding case
            procs = {p["name"]: p["id"] for p in
                     (await client.get("/processes", headers=h)).json()["processes"]}
            lead_pid = procs["Lead pipeline"]
            res = await client.post(f"/processes/{lead_pid}/instances",
                                    headers=h, json={"ref": "+15557770101",
                                                     "title": "Csv Deal"})
            iid = res.json()["id"]
            for move in ("reach_out", "qualify", "book_demo", "run_demo",
                         "send_proposal", "negotiate", "win"):
                res = await client.post(
                    f"/processes/{lead_pid}/instances/{iid}/advance",
                    headers=h, json={"transition": move})
                assert res.status_code == 200, res.text

            # the traversal is a ROW: the leg named on every row, the ride named
            res = await client.get("/processes/chains/history.csv", headers=h)
            assert res.headers["x-py8n-leg-count"] == "2", res.headers
            assert res.headers["x-py8n-ride-count"] == "1", res.headers
            rows = _parse_csv(res.text)
            won_rows = [r for r in rows[1:] if r[1] == "Lead pipeline"]
            assert len(won_rows) == 1, won_rows
            won = won_rows[0]
            assert won[2] == "won" and won[3] == "Customer onboarding", won
            assert won[5] == "1" and won[6] == "1", won       # opened / open_now
            assert won[9] == "+15557770101", won              # the ride's ref
            assert won[10] == "Onboarding - Csv Deal", won
            assert won[11] == "kickoff", won                  # where it is now
            assert won[13] == "", won                         # this leg carries no SLA promise - the column is honest about it
            assert won[14] == "no", won                       # not stuck

            # the case walks to hand_off - the SECOND leg opens the invoice,
            # and its own 5-day SLA promise rides the file
            procs2 = {p["name"]: p["id"] for p in
                      (await client.get("/processes", headers=h)).json()["processes"]}
            onboard_pid = procs2["Customer onboarding"]
            # the opened case carries the source's ref (the fallback when the
            # leg names no ref_template) - matched HERE, on the onboarding
            # process's own roster, never on the lead's
            res = await client.get(f"/processes/{onboard_pid}/instances", headers=h)
            onboard_iid = next(i["id"] for i in res.json()["instances"]
                               if i["ref"] == "+15557770101")
            for move in ("provision", "train", "go_live", "hand_off"):
                res = await client.post(
                    f"/processes/{onboard_pid}/instances/{onboard_iid}/advance",
                    headers=h, json={"transition": move})
                assert res.status_code == 200, res.text

            res = await client.get("/processes/chains/history.csv", headers=h)
            assert res.headers["x-py8n-ride-count"] == "2", res.headers
            rows = _parse_csv(res.text)
            won = next(r for r in rows[1:] if r[1] == "Lead pipeline")
            assert won[6] == "0", won                    # the case CLOSED on handed_off - open_now tells it
            billed = next(r for r in rows[1:] if r[1] == "Customer onboarding")
            assert billed[0] == "Revenue chain", billed
            assert billed[2] == "handed_off" and billed[3] == "Invoice lifecycle", billed
            assert billed[4] == str(5 * 24 * 3600), billed    # the leg's own SLA
            assert billed[5] == "1" and billed[6] == "1", billed
            assert billed[9] == "+15557770101", billed        # the ref threading the chain
            assert billed[10] == "Billing - Onboarding - Csv Deal", billed  # {title} renders the source's OWN title - the case the chain threaded
            assert billed[13] != "", billed                   # the SLA promise: a real due_at

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the ride row carries the door's book - ack + reschedule from the board
# ---------------------------------------------------------------------------

def test_v98_csv_ride_wears_the_receipt():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "receipt")
            h = _auth(user["token"])

            # a two-department chain with a SHORT leg SLA - the ride goes
            # stuck on the real clock without waiting days
            res = await client.post("/processes", headers=h, json={
                "name": "Alpha pipeline",
                "definition": {**MACHINE, "journeys": [
                    {"on_state": "b",
                     "open": {"process": "Beta intake",
                              "title_template": "Intake - {title}",
                              "ref_template": "{ref}",
                              "due_in_seconds": 1}}]}})
            assert res.status_code == 201, res.text
            res = await client.post("/processes", headers=h, json={
                "name": "Beta intake", "definition": MACHINE})
            assert res.status_code == 201, res.text

            procs = {p["name"]: p["id"] for p in
                     (await client.get("/processes", headers=h)).json()["processes"]}
            alpha_pid, beta_pid = procs["Alpha pipeline"], procs["Beta intake"]
            beta_iids = []
            for ref, title in (("AL-1", "Ride Deal"), ("AL-2", "Second Ride")):
                res = await client.post(f"/processes/{alpha_pid}/instances",
                                        headers=h, json={"ref": ref,
                                                         "title": title})
                iid = res.json()["id"]
                res = await client.post(
                    f"/processes/{alpha_pid}/instances/{iid}/advance",
                    headers=h, json={"transition": "go"})
                assert res.status_code == 200, res.text
                assert res.json()["journeys_opened"], res.text  # the leg opened Beta
                res = await client.get(f"/processes/{beta_pid}/instances", headers=h)
                beta_iids.append(next(i["id"] for i in res.json()["instances"]
                                      if i["ref"] == ref))

            base = datetime.now(timezone.utc)
            report = await _door(user["id"], now=base + timedelta(seconds=10))
            assert report["stuck"] >= 2, report  # both intakes past their 1s SLA
            # the chain map reads the REAL clock (the door's sweep rode the
            # injected one) - the intakes fall due 1s after they opened, so
            # real time has to pass the moment before the map reads stuck
            await asyncio.sleep(2.0)

            # the machine board's OWN receipt: ack with the v96 reschedule
            # clock - the SAME endpoint, the SAME one-clock-per-receipt
            res = await client.post(
                f"/processes/{beta_pid}/instances/{beta_iids[0]}/escalations/ack",
                headers=h, json={"by": "raf", "note": "calling now",
                                 "reschedule_in_minutes": 30})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["ack"]["by"] == "raf", body
            assert body["ack"]["reschedule_at"], body
            # one clock per receipt - the door refuses to guess
            res = await client.post(
                f"/processes/{beta_pid}/instances/{beta_iids[0]}/escalations/ack",
                headers=h, json={"by": "raf", "snooze_hours": 1,
                                 "reschedule_in_minutes": 30})
            assert res.status_code == 400, res.text

            # the CSV's ride rows wear the book - the acked one AND the open one
            res = await client.get("/processes/chains/history.csv", headers=h)
            assert res.headers["x-py8n-leg-count"] == "1", res.headers
            assert res.headers["x-py8n-ride-count"] == "2", res.headers
            rows = _parse_csv(res.text)
            assert len(rows) == 3, rows  # header + two rides (one leg, no inventory row)
            for ride in rows[1:]:
                assert ride[0] == "Chain via Alpha pipeline", ride
                assert ride[1] == "Alpha pipeline" and ride[2] == "b", ride
                assert ride[3] == "Beta intake", ride
                assert ride[4] == "1", ride                 # the leg's own SLA seconds
                assert ride[11] == "a", ride                # where the intake sits
                assert ride[14] == "yes", ride              # is_stuck - the ack holds the DOOR, never the clock
                assert int(ride[15]) > 0, ride              # overdue_seconds
            acked = next(r for r in rows[1:] if r[9] == "AL-1")
            assert acked[16] == "raf", acked                # acked_by, on the record
            open_ride = next(r for r in rows[1:] if r[9] == "AL-2")
            assert open_ride[16] == "", open_ride           # the door still watches

            # the clamp honors the window: depth 1 keeps exactly one ride
            # per leg (the map's own clamp, 1..50)
            res = await client.get("/processes/chains/history.csv",
                                   headers=h, params={"history_limit": 1})
            assert res.headers["x-py8n-ride-count"] == "1", res.headers
            rows1 = _parse_csv(res.text)
            assert len(rows1) == 2, rows1  # header + one ride
            assert rows1[1][8] == "yes", rows1             # history_truncated - honesty rides the file too

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. side legs - a machine with more out-legs than one walk carries
# ---------------------------------------------------------------------------

def test_v98_side_legs_group():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "sides")
            h = _auth(user["token"])

            # HUB fires TWO legs (one journey per state is the rule - two
            # states, two legs); the chain walk rides the FIRST out-leg,
            # the second is honest history on a real leg - never a drop
            res = await client.post("/processes", headers=h, json={
                "name": "Hub",
                "definition": {
                    "states": ["start", "mid", "end", "closed"],
                    "initial": "start",
                    "transitions": [
                        {"name": "go_mid", "from": "start", "to": "mid"},
                        {"name": "go_end", "from": "mid", "to": "end"},
                        {"name": "close", "from": "end", "to": "closed"}],
                    "journeys": [
                        {"on_state": "mid",
                         "open": {"process": "Spoke one",
                                  "title_template": "One - {title}"}},
                        {"on_state": "end",
                         "open": {"process": "Spoke two",
                                  "title_template": "Two - {title}"}}]}})
            assert res.status_code == 201, res.text
            for name in ("Spoke one", "Spoke two"):
                res = await client.post("/processes", headers=h, json={
                    "name": name, "definition": MACHINE})
                assert res.status_code == 201, res.text

            # the map: the walk carries the mid leg, the end leg is a side leg
            res = await client.get("/processes/chains", headers=h)
            out = res.json()
            names = [c["name"] for c in out["chains"]]
            assert "Chain via Hub" in names and "side legs" in names, names
            main = next(c for c in out["chains"] if c["name"] == "Chain via Hub")
            side = next(c for c in out["chains"] if c["name"] == "side legs")
            assert [(l["from_name"], l["on_state"], l["to_name"])
                    for l in main["legs"]] == [("Hub", "mid", "Spoke one")], main
            assert [(l["from_name"], l["on_state"], l["to_name"])
                    for l in side["legs"]] == [("Hub", "end", "Spoke two")], side

            # both legs ride: land on mid, then on end
            procs = {p["name"]: p["id"] for p in
                     (await client.get("/processes", headers=h)).json()["processes"]}
            hub_pid = procs["Hub"]
            res = await client.post(f"/processes/{hub_pid}/instances",
                                    headers=h, json={"ref": "HUB-1",
                                                     "title": "Hub Deal"})
            iid = res.json()["id"]
            for move in ("go_mid", "go_end"):
                res = await client.post(
                    f"/processes/{hub_pid}/instances/{iid}/advance",
                    headers=h, json={"transition": move})
                assert res.status_code == 200, res.text

            # the CSV names the side leg's chain on every row - the ride
            # is in the file under "side legs", never vanishing
            res = await client.get("/processes/chains/history.csv", headers=h)
            assert res.headers["x-py8n-leg-count"] == "2", res.headers
            assert res.headers["x-py8n-ride-count"] == "2", res.headers
            rows = _parse_csv(res.text)
            mid_rows = [r for r in rows[1:] if r[2] == "mid"]
            end_rows = [r for r in rows[1:] if r[2] == "end"]
            assert len(mid_rows) == 1 and len(end_rows) == 1, rows
            assert mid_rows[0][0] == "Chain via Hub", mid_rows
            assert end_rows[0][0] == "side legs", end_rows
            assert end_rows[0][9] == "HUB-1", end_rows

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. version pin
# ---------------------------------------------------------------------------

def test_v98_version_pin():
    assert settings.version == "1.101.0"
