"""v99 tests - the digest pattern applied to the FILE, and the plot's
own filters riding the export.

v98 put the chain history in a file and put the same reschedule clock on
every ack surface. v99 closes the loop:

* THE CHAIN REPORT: one schedule per owner - every window the SAME
  per-leg chain history the plots export (chain_history_csv, zero
  drift, the schedule's own depth and optional chain filter) rides the
  owner's bound EMAIL endpoint as a REAL MIME attachment. The subject
  carries the scan line the digest's subject carries
  ("[py8n] Chain history - N ride(s) across M leg(s)"). Every outcome
  is an honest record on the schedule row - delivered, skipped (no
  recipient, no endpoint, an EMPTY window - the door will not email an
  empty spreadsheet, and the skip still consumes the window the way
  the digest's window elapses when the bucket stays empty) or failed
  (smtp refused). next_due advances on EVERY due attempt: a broken
  endpoint cannot become a per-tick retry storm. The manual door
  (POST /processes/chain-report/send-now) dispatches NOW regardless of
  the window - a real send is a real send. The dispatch lands
  business.chain_report_dispatched on the event door (the honest skip
  IS the record). The attachment builder: email_build_outbound gains
  real MIME parts (multipart/mixed only when a part joins - the plain
  text path is byte-for-byte untouched); any non-email provider
  refuses loud (files-by-chat is not a silent text downgrade).
* THE PLOT'S OWN FILTERS: GET /processes/chains/history.csv gains
  ``chain`` (one chain, case-insensitive - the per-chain CSV button)
  and ``leg`` ("from_name|on_state", case-insensitive - the per-leg
  CSV chip); both compose off the SAME map. An unknown chain or leg is
  an HONEST EMPTY file (header only, leg_count 0) - the absence reads
  as data, never a 404. The response names the filters it honored
  (X-Py8n-Chain-Filter / X-Py8n-Leg-Filter) and the filename carries
  the chain's slug when one is named.
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
        "email": f"v99-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v99 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


# ---------------------------------------------------------------------------
# 1. the schedule - validated loud, one per owner, removed loud
# ---------------------------------------------------------------------------

def test_v99_chain_report_schedule_roundtrip():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "sched")
            h = _auth(user["token"])

            # the honest absence - nothing configured until the owner asks
            res = await client.get("/processes/chain-report", headers=h)
            assert res.status_code == 200, res.text
            assert res.json()["schedule"] is None, res.json()

            # loud validation: no recipient, no cadence under the floor
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "not-an-email"})
            assert res.status_code == 400, res.text
            assert "recipient" in res.json()["detail"], res.json()
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test",
                                         "cadence_seconds": 60})
            assert res.status_code == 400, res.text
            assert "minutes concern" in res.json()["detail"], res.json()

            # the round-trip - the window anchors to now + cadence
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test",
                                         "cadence_seconds": 3600,
                                         "history_limit": 25,
                                         "chain": "Revenue chain"})
            assert res.status_code == 200, res.text
            sched = res.json()["schedule"]
            assert sched["enabled"] is True and sched["to"] == "ops@py8n.test", sched
            assert sched["cadence_seconds"] == 3600, sched
            assert sched["history_limit"] == 25 and sched["chain"] == "Revenue chain", sched
            assert sched["next_due"], sched
            res = await client.get("/processes/chain-report", headers=h)
            assert res.json()["schedule"]["to"] == "ops@py8n.test", res.json()

            # the update replaces (one schedule per owner - an upsert)
            # a depth beyond the map's own ceiling refuses loud at the door
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "boss@py8n.test",
                                         "cadence_seconds": 86400,
                                         "history_limit": 500})
            assert res.status_code == 422, res.text
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "boss@py8n.test",
                                         "cadence_seconds": 86400,
                                         "history_limit": 50})
            assert res.status_code == 200, res.text
            sched = res.json()["schedule"]
            assert sched["to"] == "boss@py8n.test", sched
            assert sched["history_limit"] == 50, sched
            assert sched["chain"] == "", sched

            # removing is loud both ways
            res = await client.delete("/processes/chain-report", headers=h)
            assert res.status_code == 200 and res.json()["removed"], res.text
            res = await client.delete("/processes/chain-report", headers=h)
            assert res.status_code == 400, res.text

            # send-now without a schedule: the manual door refuses loud
            res = await client.post("/processes/chain-report/send-now", headers=h)
            assert res.status_code == 400, res.text

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the dispatch - honest outcomes, the window, the event
# ---------------------------------------------------------------------------

def test_v99_chain_report_dispatch_and_window():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "ride")
            h = _auth(user["token"])

            # rides exist: the revenue chain's first leg carries a deal
            for slug in ("operations-operator", "sales-operator",
                         "finance-operator"):
                res = await client.post(f"/operators/{slug}/install",
                                        headers=h, json={})
                assert res.status_code == 200, res.text
            procs = {p["name"]: p["id"] for p in
                     (await client.get("/processes", headers=h)).json()["processes"]}
            lead_pid = procs["Lead pipeline"]
            res = await client.post(f"/processes/{lead_pid}/instances",
                                    headers=h, json={"ref": "+15559997001",
                                                     "title": "Report Deal"})
            iid = res.json()["id"]
            for move in ("reach_out", "qualify", "book_demo", "run_demo",
                         "send_proposal", "negotiate", "win"):
                res = await client.post(
                    f"/processes/{lead_pid}/instances/{iid}/advance",
                    headers=h, json={"transition": move})
                assert res.status_code == 200, res.text

            # the schedule saves; NO email endpoint bound: the honest skip
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test",
                                         "cadence_seconds": 3600})
            assert res.status_code == 200, res.text
            res = await client.post("/processes/chain-report/send-now", headers=h)
            assert res.status_code == 200, res.text
            result = res.json()["result"]
            assert result["delivery"] == "skipped", result
            assert "no email endpoint bound" in result["detail"], result
            assert result["legs"] == 2 and result["rides"] == 1, result
            sched = res.json()["schedule"]
            assert sched["last_result"]["delivery"] == "skipped", sched
            assert sched["last_sent_at"] and sched["next_due"], sched

            # the window: send-now works regardless, but the DUE walk
            # respects next_due - the schedule is not due again for an hour
            from app.db import AsyncSessionLocal
            from app.services import business_processes as bp_svc

            async with AsyncSessionLocal() as session:
                due = await bp_svc.dispatch_due_chain_reports(
                    session, user["id"])
                await session.commit()
            assert due == [], due  # the window holds

            # the injected clock passes next_due: the walk dispatches -
            # and the SECOND call at the same moment finds the window
            # already consumed (next_due advanced on every due attempt)
            base = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as session:
                from sqlalchemy import select as _select
                row = (await session.execute(
                    _select(bp_svc.ChainReportSchedule))).scalars().first()
                first = await bp_svc.dispatch_due_chain_reports(
                    session, user["id"], now=base + timedelta(seconds=3601))
                await session.commit()
            assert len(first) == 1, first
            async with AsyncSessionLocal() as session:
                again = await bp_svc.dispatch_due_chain_reports(
                    session, user["id"], now=base + timedelta(seconds=3601))
                await session.commit()
            assert again == [], again

            # an empty window consumes itself: a fresh owner schedules the
            # file with NOTHING riding the legs - the skip names it and
            # the window still advances
            user2 = await _mk_user(client, "quiet")
            h2 = _auth(user2["token"])
            res = await client.put("/processes/chain-report", headers=h2,
                                   json={"to": "quiet@py8n.test",
                                         "cadence_seconds": 3600})
            assert res.status_code == 200, res.text
            res = await client.post("/processes/chain-report/send-now", headers=h2)
            assert res.status_code == 200, res.text
            result2 = res.json()["result"]
            assert result2["delivery"] == "skipped", result2
            assert "nothing to summarize" in result2["detail"], result2
            assert result2["rides"] == 0 and result2["legs"] == 0, result2

            # the dispatches are on the record - the event door carries
            # the honest skip the same way it carries a delivery
            res = await client.get(
                "/events", params={"type": "business.chain_report_dispatched"},
                headers=h)
            evs = [e for e in res.json()["events"]
                   if e["payload"].get("rides") == 1]
            assert evs, res.json()
            payload = evs[0]["payload"]
            assert payload["delivery"] in ("skipped", "failed", "delivered"), payload
            assert payload["filename"].startswith("py8n-chain-history"), payload
            assert payload["legs"] == 2 and payload["rides"] == 1, payload

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the plot's own filters - one chain, one leg, honest empties
# ---------------------------------------------------------------------------

def test_v99_csv_filters_on_the_plot():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "filter")
            h = _auth(user["token"])

            for slug in ("operations-operator", "sales-operator",
                         "finance-operator"):
                res = await client.post(f"/operators/{slug}/install",
                                        headers=h, json={})
                assert res.status_code == 200, res.text
            procs = {p["name"]: p["id"] for p in
                     (await client.get("/processes", headers=h)).json()["processes"]}
            lead_pid = procs["Lead pipeline"]
            onboard_pid = procs["Customer onboarding"]
            onboard_iid = None
            for ref in ("+15559998001", "+15559998002"):
                res = await client.post(f"/processes/{lead_pid}/instances",
                                        headers=h, json={"ref": ref,
                                                         "title": f"Filter {ref[-2:]}"})
                iid = res.json()["id"]
                for move in ("reach_out", "qualify", "book_demo", "run_demo",
                             "send_proposal", "negotiate", "win"):
                    res = await client.post(
                        f"/processes/{lead_pid}/instances/{iid}/advance",
                        headers=h, json={"transition": move})
                    assert res.status_code == 200, res.text
                if onboard_iid is None:
                    rows = c = (await client.get(
                        f"/processes/{onboard_pid}/instances", headers=h)
                    ).json()["instances"]
                    onboard_iid = next(i["id"] for i in rows if i["ref"] == ref)
                    for move in ("provision", "train", "go_live", "hand_off"):
                        res = await client.post(
                            f"/processes/{onboard_pid}/instances/{onboard_iid}/advance",
                            headers=h, json={"transition": move})
                        assert res.status_code == 200, res.text

            # the chain filter: ONE chain in the file - case-insensitive
            res = await client.get("/processes/chains/history.csv", headers=h,
                                   params={"chain": "Revenue chain"})
            assert res.headers["x-py8n-chain-filter"] == "Revenue chain", res.headers
            assert res.headers["x-py8n-leg-count"] == "2", res.headers
            assert res.headers["x-py8n-ride-count"] == "3", res.headers
            assert "revenue-chain" in res.headers["content-disposition"], res.headers
            rows = _parse_csv(res.text)
            assert {r[0] for r in rows[1:]} == {"Revenue chain"}, rows
            res2 = await client.get("/processes/chains/history.csv", headers=h,
                                    params={"chain": "revenue CHAIN"})
            assert res2.headers["x-py8n-ride-count"] == "3", res2.headers

            # the leg filter: ONE leg - composed with the chain filter
            res = await client.get("/processes/chains/history.csv", headers=h,
                                   params={"leg": "Lead pipeline|won"})
            assert res.headers["x-py8n-leg-filter"] == "Lead pipeline|won", res.headers
            assert res.headers["x-py8n-leg-count"] == "1", res.headers
            assert res.headers["x-py8n-ride-count"] == "2", res.headers
            rows = _parse_csv(res.text)
            assert all(r[1] == "Lead pipeline" and r[2] == "won"
                       for r in rows[1:]), rows
            res = await client.get("/processes/chains/history.csv", headers=h,
                                   params={"chain": "revenue chain",
                                           "leg": "customer onboarding|HANDED_OFF"})
            assert res.headers["x-py8n-ride-count"] == "1", res.headers
            rows = _parse_csv(res.text)
            assert rows[1][9] == "+15559998001", rows  # the case that walked the chain

            # an unknown name: the HONEST EMPTY file - header only, never a 404
            res = await client.get("/processes/chains/history.csv", headers=h,
                                   params={"chain": "Ghost chain"})
            assert res.status_code == 200, res.text
            assert res.headers["x-py8n-leg-count"] == "0", res.headers
            rows = _parse_csv(res.text)
            assert len(rows) == 1, rows  # just the header - the absence reads as data

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the attachment - real MIME parts, and other providers refuse loud
# ---------------------------------------------------------------------------

def test_v99_attachment_builder():
    from app.services import channel_adapters as adapters

    # the CSV rides as a named MIME part; the message becomes multipart
    req = adapters.email_build_outbound(
        {"smtp_host": "h", "from_address": "p@x.test"}, "to@y.test",
        "the body", subject="[py8n] Chain history - 3 ride(s) across 2 leg(s)",
        attachments=[{"filename": "py8n-chain-history-20260908.csv",
                      "content": "chain,leg_from\nRevenue,Lead pipeline",
                      "maintype": "text", "subtype": "csv"}])
    assert "multipart/mixed" in req["message"], req["message"][:200]
    assert "py8n-chain-history-20260908.csv" in req["message"], req
    assert "text/csv" in req["message"], req
    assert "[py8n] Chain history" in req["subject"], req
    assert req["attachment_names"] == ["py8n-chain-history-20260908.csv"], req

    # no attachments: the plain path is EXACTLY what it always was
    plain = adapters.email_build_outbound({"smtp_host": "h"}, "to@y.test", "body")
    assert "multipart" not in plain["message"], plain
    assert plain["attachment_names"] == [], plain

    # a broken attachment refuses loud
    try:
        adapters.email_build_outbound({"smtp_host": "h"}, "t@y.test", "b",
                                      attachments=[{"content": "no name"}])
        raise SystemExit("the builder accepted an unnamed attachment")
    except ValueError:
        pass

    # a non-email provider refuses loud - files-by-chat is not a downgrade
    from app.models import ChannelEndpoint
    from app.services.channel_endpoints import deliver_outbound

    async def _refuse():
        ep = ChannelEndpoint(provider="telegram_bot_api", channel="telegram",
                             config={"bot_token": "t", "chat_prefix": "p"},
                             enabled=True)
        return await deliver_outbound(ep, "chat", "hi",
                                      attachments=[{"filename": "f.csv",
                                                    "content": "a,b"}])
    try:
        asyncio.run(_refuse())
        raise SystemExit("telegram carried an attachment silently")
    except Exception as exc:  # noqa: BLE001
        assert "attachments" in str(exc), exc


# ---------------------------------------------------------------------------
# 5. version pin
# ---------------------------------------------------------------------------

def test_v99_version_pin():
    assert settings.version == "1.105.0"
