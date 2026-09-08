"""v100 tests - the report rides to a LIST of names, and the report
breaks down BY system.

v99 put the chain-history file on a cadence and let the plot's own
filters ride the export. v100 widens both doors:

* MULTI-RECIPIENT REPORTS: the schedule's ``to`` carries a recipient
  LIST (commas, semicolons and newlines all separate - an address never
  contains any of them); save-time normalization strips, dedupes
  (case-insensitive, first spelling wins) and enforces a loud ceiling
  (8 - a report is a staff brief, not a mailing list). One dispatch =
  ONE envelope carrying EVERY name: the To header names them all, the
  SMTP conversation speaks one RCPT TO per name, the single attachment
  rides to the whole list on ONE send. The honest receipts carry the
  count (the schedule's last_result and the event door both name how
  many the envelope held).
* REPORT FILTERS BROKEN DOWN BY SYSTEM: the map's legs now name the
  systems their firing machine binds; the CSV grows the ``system``
  column (the spreadsheet's own breakdown dimension, "|"-joined for
  machines bound to several systems) and the endpoint grows the
  ``system`` filter (a leg rides in when the machine FIRING it binds
  that system - case-insensitive, composing with chain/leg, an unknown
  name an honest empty file, the filter echoed in the headers and the
  filename). The schedule grows the same scope (the report covers only
  the machines that system binds) and the email body breaks the rides
  down BY system - the report's own summary of the file it carries. A
  ride on a machine bound to two systems lands in both buckets, the way
  a pivot over the file would count it.

The real-wire proof rides the dev SMTP sink (the same REAL SMTP server
the v90 digests rode): one message, two RCPT TO envelopes, the
attachment parsed back out of the wire naming the ride.
"""

from __future__ import annotations

import asyncio
import csv
import email
import email.policy
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from dev_smtp_sink import SmtpDevSink  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"


@pytest.fixture()
def sink():
    s = SmtpDevSink().start()
    yield s
    s.stop()


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
        "email": f"v100-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v100 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


async def _mk_email_endpoint(client: httpx.AsyncClient, h: dict,
                             sink: SmtpDevSink) -> dict:
    """A REAL email channel endpoint whose SMTP out is the dev sink."""
    res = await client.post("/channels/endpoints", headers=h, json={
        "name": "v100 inbox",
        "provider": "email_inbound",
        "config": {"secret": "v100-secret",
                   "smtp_host": "127.0.0.1", "smtp_port": str(sink.port),
                   "smtp_user": "v100", "smtp_pass": "v100",
                   "from_address": "py8n@py8n.test"}})
    assert res.status_code == 201, res.text
    return res.json()


async def _install_revenue_chain(client: httpx.AsyncClient, h: dict) -> dict:
    """The three revenue departments installed; the map's own node data
    returns the systems each machine binds (the filter reads the same)."""
    for slug in ("operations-operator", "sales-operator", "finance-operator"):
        res = await client.post(f"/operators/{slug}/install", headers=h,
                                json={})
        assert res.status_code == 200, res.text
    procs = {p["name"]: p["id"] for p in
             (await client.get("/processes", headers=h)).json()["processes"]}
    chains = (await client.get("/processes/chains", headers=h)).json()
    node_systems: dict[str, list] = {}
    for node in (chains.get("nodes") or {}).values():
        node_systems[node["name"]] = node.get("systems") or []
    return {"procs": procs, "node_systems": node_systems}


async def _ride_leg_one(client: httpx.AsyncClient, h: dict, lead_pid: str,
                        ref: str, title: str) -> None:
    res = await client.post(f"/processes/{lead_pid}/instances", headers=h,
                            json={"ref": ref, "title": title})
    iid = res.json()["id"]
    for move in ("reach_out", "qualify", "book_demo", "run_demo",
                 "send_proposal", "negotiate", "win"):
        res = await client.post(
            f"/processes/{lead_pid}/instances/{iid}/advance", headers=h,
            json={"transition": move})
        assert res.status_code == 200, res.text


# ---------------------------------------------------------------------------
# 1. the recipient LIST - normalized, deduped, ceilinged loud
# ---------------------------------------------------------------------------

def test_v100_recipient_list_roundtrip():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "list")
            h = _auth(user["token"])

            # commas, semicolons AND newlines separate; order survives;
            # the schedule stores the normalized comma-joined list
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test, boss@py8n.test;"
                                               " crew@py8n.test\nextra@py8n.test",
                                         "cadence_seconds": 3600})
            assert res.status_code == 200, res.text
            sched = res.json()["schedule"]
            assert sched["to"] == ("ops@py8n.test, boss@py8n.test, "
                                   "crew@py8n.test, extra@py8n.test"), sched
            assert sched["recipients"] == ["ops@py8n.test", "boss@py8n.test",
                                           "crew@py8n.test", "extra@py8n.test"], sched
            assert sched["recipient_count"] == 4, sched

            # duplicates collapse case-insensitively, first spelling wins
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "A@X.test, a@x.test , A@x.test",
                                         "cadence_seconds": 3600})
            assert res.status_code == 200, res.text
            sched = res.json()["schedule"]
            assert sched["recipients"] == ["A@X.test"], sched
            assert sched["recipient_count"] == 1, sched

            # a name without an @ refuses loud, naming the offender
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test, not-an-email",
                                         "cadence_seconds": 3600})
            assert res.status_code == 400, res.text
            assert "not-an-email" in res.json()["detail"], res.json()

            # the ceiling is loud: a report is a staff brief
            nine = ", ".join(f"r{i}@py8n.test" for i in range(9))
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": nine, "cadence_seconds": 3600})
            assert res.status_code == 400, res.text
            assert "at most 8" in res.json()["detail"], res.json()
            eight = ", ".join(f"r{i}@py8n.test" for i in range(8))
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": eight, "cadence_seconds": 3600})
            assert res.status_code == 200, res.text
            assert res.json()["schedule"]["recipient_count"] == 8, res.json()

            # no name at all: the file has nowhere to land
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": " , ;", "cadence_seconds": 3600})
            assert res.status_code == 400, res.text
            assert "recipient" in res.json()["detail"], res.json()

            # the GET echoes the parsed list
            res = await client.get("/processes/chain-report", headers=h)
            assert res.json()["schedule"]["recipient_count"] == 8, res.json()

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the dispatch rides ONE envelope to EVERY name - over the REAL wire
# ---------------------------------------------------------------------------

def test_v100_multi_recipient_dispatch_on_the_wire(sink):
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "wire")
            h = _auth(user["token"])
            await _mk_email_endpoint(client, h, sink)
            installed = await _install_revenue_chain(client, h)
            lead_pid = installed["procs"]["Lead pipeline"]
            await _ride_leg_one(client, h, lead_pid, "+15551000101",
                                "List Deal")

            # the schedule: TWO names on one report
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test, boss@py8n.test",
                                         "cadence_seconds": 3600,
                                         "history_limit": 5})
            assert res.status_code == 200, res.text
            next_due = res.json()["schedule"]["next_due"]

            # the window holds on the real door: the tick dispatches nothing
            res = await client.post("/scheduler/escalations/tick", json={})
            assert res.status_code == 200, res.text
            assert res.json()["chain_report"] == [], res.json()
            assert sink.count == 0, sink.messages

            # the manual door: ONE envelope, EVERY name, ONE attachment
            res = await client.post("/processes/chain-report/send-now",
                                    headers=h)
            assert res.status_code == 200, res.text
            result = res.json()["result"]
            assert result["delivery"] == "delivered", result
            assert result["recipients"] == 2, result
            assert result["to"] == "ops@py8n.test, boss@py8n.test", result
            assert result["legs"] == 2 and result["rides"] == 1, result
            assert sink.count == 1, sink.messages

            # the wire: one message, TWO RCPT TO entries on the envelope
            wire = sink.last()
            assert wire["to"] == ["ops@py8n.test", "boss@py8n.test"], wire["to"]
            msg = email.message_from_bytes(wire["data"],
                                           policy=email.policy.default)
            tos = [a.addr_spec for a in msg["To"].addresses]
            assert tos == ["ops@py8n.test", "boss@py8n.test"], tos
            assert ("[py8n] Chain history - 1 ride(s) across 2 leg(s)"
                    in str(msg["Subject"])), msg["Subject"]
            # the body carries the multi-recipient receipt
            assert "Recipients: 2" in wire["text"], wire["text"]

            # the attachment parses back out of the wire, ride named,
            # the system column present (the breakdown dimension)
            attachment = next(p for p in msg.iter_attachments()
                              if p.get_filename() == result["filename"])
            assert attachment.get_content_type() == "text/csv", attachment
            rows = _parse_csv(attachment.get_content())
            assert rows[0][-1] == "system", rows[0]
            won = next(r for r in rows[1:] if r[1] == "Lead pipeline")
            assert won[9] == "+15551000101", won
            assert won[20], won  # the machine's systems named on the row

            # the schedule row stamps the delivery; the window advanced
            sched = (await client.get("/processes/chain-report",
                                      headers=h)).json()["schedule"]
            assert sched["last_result"]["delivery"] == "delivered", sched
            assert sched["last_result"]["recipients"] == 2, sched
            assert sched["next_due"] > next_due, (sched["next_due"], next_due)

            # the event door names the count too
            evs = (await client.get("/events", params={
                "type": "business.chain_report_dispatched"})).json()
            mine = [e for e in evs["events"]
                    if e["payload"]["filename"] == result["filename"]]
            assert mine and mine[0]["payload"]["recipients"] == 2, evs

            # the due walk, on the injected clock, rides the SAME envelope:
            # the window elapses and ONE send covers the list again
            base = datetime.now(timezone.utc)
            from app.db import AsyncSessionLocal
            from app.services import business_processes as bp_svc

            async with AsyncSessionLocal() as session:
                due = await bp_svc.dispatch_due_chain_reports(
                    session, user["id"], now=base + timedelta(seconds=3601))
                await session.commit()
            assert len(due) == 1 and due[0]["recipients"] == 2, due
            assert sink.count == 2, sink.messages
            assert sink.last()["to"] == ["ops@py8n.test",
                                         "boss@py8n.test"], sink.last()["to"]

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the report filters broken down by system - the column, the filter,
#    the honest empty, the pivot arithmetic
# ---------------------------------------------------------------------------

def test_v100_system_filter_on_the_csv():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "sysslice")
            h = _auth(user["token"])
            installed = await _install_revenue_chain(client, h)
            lead_pid = installed["procs"]["Lead pipeline"]
            lead_systems = installed["node_systems"]["Lead pipeline"]
            assert lead_systems, installed["node_systems"]
            lead_sys = lead_systems[0]

            # two rides on leg 1; the second deal then walks ON to leg 2
            # (the onboarding case rides hand_off)
            await _ride_leg_one(client, h, lead_pid, "+15551000201", "Sys A")
            await _ride_leg_one(client, h, lead_pid, "+15551000202", "Sys B")
            onboard_pid = installed["procs"]["Customer onboarding"]
            rows = (await client.get(f"/processes/{onboard_pid}/instances",
                                     headers=h)).json()["instances"]
            onboard_iid = next(i["id"] for i in rows
                               if i["ref"] == "+15551000202")
            for move in ("provision", "train", "go_live", "hand_off"):
                res = await client.post(
                    f"/processes/{onboard_pid}/instances/{onboard_iid}/advance",
                    headers=h, json={"transition": move})
                assert res.status_code == 200, res.text

            # the system filter: the legs the system's machines FIRE -
            # the lead machine fires leg 1 only (leg 2 belongs to the
            # onboarding machine's system); case-insensitive, echoed in
            # the headers, the slug in the filename
            res = await client.get("/processes/chains/history.csv", headers=h,
                                   params={"system": lead_sys})
            assert res.status_code == 200, res.text
            assert res.headers["x-py8n-system-filter"] == lead_sys, res.headers
            assert res.headers["x-py8n-leg-count"] == "1", res.headers
            assert res.headers["x-py8n-ride-count"] == "2", res.headers
            assert "-sys-" in res.headers["content-disposition"], res.headers
            rows = _parse_csv(res.text)
            assert rows[0][-1] == "system", rows[0]
            assert all(r[20] == lead_sys for r in rows[1:]), rows

            # case-insensitive: the same slice
            res2 = await client.get("/processes/chains/history.csv", headers=h,
                                    params={"system": lead_sys.upper()})
            assert res2.headers["x-py8n-ride-count"] == "2", res2.headers

            # compose: system + leg - one leg, two rides
            res = await client.get("/processes/chains/history.csv", headers=h,
                                   params={"system": lead_sys,
                                           "leg": "Lead pipeline|won"})
            assert res.headers["x-py8n-leg-count"] == "1", res.headers
            assert res.headers["x-py8n-ride-count"] == "2", res.headers

            # an unknown system: the HONEST EMPTY file - header only
            res = await client.get("/processes/chains/history.csv", headers=h,
                                   params={"system": "Ghost system"})
            assert res.status_code == 200, res.text
            assert res.headers["x-py8n-leg-count"] == "0", res.headers
            assert len(_parse_csv(res.text)) == 1, res.text

            # the pivot arithmetic, straight off the service: the rides
            # break down BY system - leg 1's rides land in the lead
            # machine's bucket, leg 2's ride in the onboarding machine's
            from app.db import AsyncSessionLocal
            from app.services import business_processes as bp_svc

            async with AsyncSessionLocal() as session:
                out = await bp_svc.chain_history_csv(session, user["id"],
                                                     history_limit=50)
            assert out["by_system"].get(lead_sys) == 2, out["by_system"]
            onboard_sys = installed["node_systems"]["Customer onboarding"][0]
            assert out["by_system"].get(onboard_sys) == 1, out["by_system"]
            assert out["system_filter"] == "", out

            # and the scoped call carries its own echo
            async with AsyncSessionLocal() as session:
                out = await bp_svc.chain_history_csv(
                    session, user["id"], history_limit=50, system=lead_sys)
            assert out["system_filter"] == lead_sys, out
            assert out["ride_count"] == 2, out

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the schedule's own system scope - the report covers one system's slice
# ---------------------------------------------------------------------------

def test_v100_report_system_scope():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "scope")
            h = _auth(user["token"])
            installed = await _install_revenue_chain(client, h)
            lead_pid = installed["procs"]["Lead pipeline"]
            lead_sys = installed["node_systems"]["Lead pipeline"][0]
            await _ride_leg_one(client, h, lead_pid, "+15551000301",
                                "Scope Deal")

            # the schedule scoped to the lead's system - echoed back
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test",
                                         "cadence_seconds": 3600,
                                         "system": lead_sys})
            assert res.status_code == 200, res.text
            sched = res.json()["schedule"]
            assert sched["system"] == lead_sys, sched

            # the dispatch (no endpoint bound - the honest skip) counts
            # the SCOPED slice: the legs the lead machine fires, one ride
            res = await client.post("/processes/chain-report/send-now",
                                    headers=h)
            assert res.status_code == 200, res.text
            result = res.json()["result"]
            assert result["delivery"] == "skipped", result
            assert "no email endpoint bound" in result["detail"], result
            assert result["legs"] == 1 and result["rides"] == 1, result

            # an unknown system scope: an empty window - the skip names it
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test",
                                         "cadence_seconds": 3600,
                                         "system": "Ghost system"})
            assert res.status_code == 200, res.text
            res = await client.post("/processes/chain-report/send-now",
                                    headers=h)
            result = res.json()["result"]
            assert result["delivery"] == "skipped", result
            assert "nothing to summarize" in result["detail"], result
            assert result["rides"] == 0, result

            # the subject names the scope (the scan line grows the suffix)
            from app.services.business_processes import chain_report_subject
            assert chain_report_subject(2, 3) == \
                "[py8n] Chain history - 3 ride(s) across 2 leg(s)"
            assert chain_report_subject(2, 3, system=lead_sys) == \
                (f"[py8n] Chain history - 3 ride(s) across 2 leg(s)"
                 f" - System: {lead_sys}")

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5. the envelope's recipient list - the wire's own parse, and the pin
# ---------------------------------------------------------------------------

def test_v100_smtp_envelope_multi_rcpt():
    from app.services.channel_adapters import email_build_outbound
    from app.services.channel_adapters import email_rcpt_list

    # one name (every pre-v100 caller) stays exactly one name
    assert email_rcpt_list("ops@py8n.test") == ["ops@py8n.test"]
    # commas, semicolons, whitespace - an address never contains any
    assert email_rcpt_list("a@x.test, b@y.test; c@z.test\n d@w.test") == \
        ["a@x.test", "b@y.test", "c@z.test", "d@w.test"]
    assert email_rcpt_list("") == []
    assert email_rcpt_list(None) == []
    # dedupe is the save-time job, not the wire's - order survives as given
    assert email_rcpt_list("a@x.test, a@x.test") == ["a@x.test", "a@x.test"]

    # the built message: the To header names BOTH (RFC 5322 list form)
    req = email_build_outbound(
        {"smtp_host": "h", "from_address": "p@x.test"},
        "a@y.test, b@y.test", "the body", subject="[py8n] Chain history")
    tos = [a.addr_spec for a in email.message_from_string(
        req["message"], policy=email.policy.default)["To"].addresses]
    assert tos == ["a@y.test", "b@y.test"], (tos, req["to"])
    assert req["to"] == "a@y.test, b@y.test", req

    # and the pin
    assert settings.version == "1.105.0", settings.version
