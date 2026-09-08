"""v101 tests - the report's scope is a chain TAG LIST, and a weekly
digest rides the SAME envelope path to the report's own list.

v99 put the chain-history file on a cadence; v100 gave it a recipient
LIST and a by-system breakdown. v101 widens the scope and paces the
beat:

* MULTI-CHAIN / TAG-LIST REPORT SCOPE: the schedule's scope is no
  longer ONE optional chain - ``chains`` carries a TAG LIST (commas,
  semicolons and newlines all separate, whitespace stripped, duplicates
  collapsed case-insensitively first-wins, the ceiling of 8 loud - the
  same discipline the recipient list obeys). The file covers every
  chain the list names (composing with the system scope); the legacy
  single ``chain`` folds into the list and stays truthful for one-name
  scopes so the v99/v100 readers never drift. The map filter gained
  the same door (``chains`` on the CSV endpoint, the echo header, the
  slug in the filename, an unknown name an HONEST EMPTY file) and the
  rides break down BY CHAIN in the file's own pivot (by_chain).
* A WEEKLY DIGEST RIDING THE SAME ENVELOPE PATH TO THE REPORT'S LIST:
  the schedule speaks named rhythms now (hourly | daily | weekly -
  weekly = 604800s). The weekly digest is the report on its weekly
  beat, not a different message: the SAME bound email endpoint, the
  SAME envelope carrying EVERY name on the report's list (one SMTP
  conversation, one RCPT TO per name), the SAME real MIME attachment -
  only the scan line renames itself ("[py8n] Weekly chain digest - ...")
  and the body names its rhythm. The due walk rides the same wire on
  the injected clock: the weekly window holds, then the SAME envelope
  goes out.

The real-wire proof rides the dev SMTP sink (the same REAL SMTP server
the v90 digests and v100's list rode): one message, the whole list on
the envelope, the multi-chain file parsed back out of the wire.
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
        "email": f"v101-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v101 {tag}",
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
        "name": "v101 inbox",
        "provider": "email_inbound",
        "config": {"secret": "v101-secret",
                   "smtp_host": "127.0.0.1", "smtp_port": str(sink.port),
                   "smtp_user": "v101", "smtp_pass": "v101",
                   "from_address": "py8n@py8n.test"}})
    assert res.status_code == 201, res.text
    return res.json()


async def _install_two_chains(client: httpx.AsyncClient, h: dict) -> dict:
    """Revenue + Supply installed - the map draws TWO chains, both named
    by the canonical CHAIN_NAMES (Lead pipeline|won -> Revenue chain,
    Purchase lifecycle|ordered -> Supply chain)."""
    for slug in ("operations-operator", "sales-operator", "finance-operator",
                 "procurement-operator", "logistics-operator"):
        res = await client.post(f"/operators/{slug}/install", headers=h,
                                json={})
        assert res.status_code == 200, res.text
    procs = {p["name"]: p["id"] for p in
             (await client.get("/processes", headers=h)).json()["processes"]}
    chains = (await client.get("/processes/chains", headers=h)).json()
    names = sorted(c["name"] for c in chains.get("chains") or [])
    assert "Revenue chain" in names and "Supply chain" in names, names
    return {"procs": procs}


async def _ride_lead(client: httpx.AsyncClient, h: dict, lead_pid: str,
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


async def _ride_purchase(client: httpx.AsyncClient, h: dict, purch_pid: str,
                         ref: str, title: str) -> None:
    res = await client.post(f"/processes/{purch_pid}/instances", headers=h,
                            json={"ref": ref, "title": title})
    iid = res.json()["id"]
    for move in ("quote", "approve", "order"):
        res = await client.post(
            f"/processes/{purch_pid}/instances/{iid}/advance", headers=h,
            json={"transition": move})
        assert res.status_code == 200, res.text


# ---------------------------------------------------------------------------
# 1. the chain TAG LIST - parsed like the recipients, ceilinged loud,
#    the legacy single chain folded in
# ---------------------------------------------------------------------------

def test_v101_tag_list_scope_roundtrip():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "scope")
            h = _auth(user["token"])

            # commas, semicolons AND newlines separate; order survives;
            # the schedule stores the normalized comma-joined list and
            # serves it parsed with its count
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test",
                                         "cadence_seconds": 3600,
                                         "chains": "Revenue chain, supply chain;"
                                                   "Care chain\n Revenue CHAIN"})
            assert res.status_code == 200, res.text
            sched = res.json()["schedule"]
            assert sched["chains"] == ["Revenue chain", "supply chain",
                                       "Care chain"], sched
            assert sched["chain_count"] == 3, sched
            # a MULTI-name scope leaves the legacy single-chain column
            # empty (the list is the scope's home now)
            assert sched["chain"] == "", sched

            # the legacy single chain still works: it folds into the list
            # and the legacy column stays truthful (v99/v100 readers)
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test",
                                         "cadence_seconds": 3600,
                                         "chain": "Revenue chain"})
            assert res.status_code == 200, res.text
            sched = res.json()["schedule"]
            assert sched["chain"] == "Revenue chain", sched
            assert sched["chains"] == ["Revenue chain"], sched
            assert sched["chain_count"] == 1, sched

            # the named rhythms: weekly paces the window at 7 days and
            # echoes its name; hourly and daily map too
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test",
                                         "cadence": "weekly",
                                         "chain": "Revenue chain"})
            assert res.status_code == 200, res.text
            sched = res.json()["schedule"]
            assert sched["cadence_seconds"] == 7 * 86400, sched
            assert sched["cadence"] == "weekly", sched
            assert sched["next_due"], sched
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test",
                                         "cadence": "hourly"})
            assert res.json()["schedule"]["cadence_seconds"] == 3600, res.text
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test",
                                         "cadence": "daily"})
            assert res.json()["schedule"]["cadence_seconds"] == 86400, res.text

            # an unknown rhythm refuses loud, naming the honest set
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test",
                                         "cadence": "fortnightly"})
            assert res.status_code == 400, res.text
            assert "unknown cadence" in res.json()["detail"], res.json()
            assert "weekly" in res.json()["detail"], res.json()

            # the ceiling is loud: a report is a staff brief, not an atlas
            nine = ", ".join(f"Chain {i}" for i in range(9))
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test",
                                         "cadence_seconds": 3600,
                                         "chains": nine})
            assert res.status_code == 400, res.text
            assert "at most 8" in res.json()["detail"], res.json()
            eight = ", ".join(f"Chain {i}" for i in range(8))
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test",
                                         "cadence_seconds": 3600,
                                         "chains": eight})
            assert res.status_code == 200, res.text
            assert res.json()["schedule"]["chain_count"] == 8, res.json()

            # an empty scope reads as the whole estate map
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test",
                                         "cadence_seconds": 3600,
                                         "chains": " , ;"})
            assert res.status_code == 200, res.text
            sched = res.json()["schedule"]
            assert sched["chains"] == [] and sched["chain_count"] == 0, sched

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the multi-chain weekly digest - ONE envelope, the report's OWN list,
#    the file carrying BOTH chains - over the REAL wire
# ---------------------------------------------------------------------------

def test_v101_multi_chain_weekly_digest_on_the_wire(sink):
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "wire")
            h = _auth(user["token"])
            await _mk_email_endpoint(client, h, sink)
            installed = await _install_two_chains(client, h)
            await _ride_lead(client, h, installed["procs"]["Lead pipeline"],
                             "+15551010101", "Tag Deal")
            await _ride_purchase(
                client, h, installed["procs"]["Purchase lifecycle"],
                "PO-101", "Tag Order")

            # the schedule: a TWO-chain tag list, the WEEKLY rhythm, a
            # two-name list - one envelope will carry every name
            res = await client.put("/processes/chain-report", headers=h,
                                   json={"to": "ops@py8n.test, boss@py8n.test",
                                         "cadence": "weekly",
                                         "history_limit": 5,
                                         "chains": "Revenue chain, Supply chain"})
            assert res.status_code == 200, res.text
            sched = res.json()["schedule"]
            assert sched["cadence"] == "weekly", sched
            assert sched["chain_count"] == 2, sched
            next_due = sched["next_due"]

            # the weekly window holds on the real door: the tick
            # dispatches nothing while the week is young
            res = await client.post("/scheduler/escalations/tick", json={})
            assert res.status_code == 200, res.text
            assert res.json()["chain_report"] == [], res.json()
            assert sink.count == 0, sink.messages

            # the manual door: ONE envelope, EVERY name, ONE attachment
            # carrying BOTH chains
            res = await client.post("/processes/chain-report/send-now",
                                    headers=h)
            assert res.status_code == 200, res.text
            result = res.json()["result"]
            assert result["delivery"] == "delivered", result
            assert result["recipients"] == 2, result
            assert result["cadence"] == "weekly", result
            assert result["chains"] == ["Revenue chain",
                                        "Supply chain"], result
            assert result["legs"] == 4 and result["rides"] == 2, result
            assert "revenue-chain" in result["filename"], result
            assert sink.count == 1, sink.messages

            # the wire: one message, BOTH names on the envelope, the
            # weekly digest's own scan line on the subject
            wire = sink.last()
            assert wire["to"] == ["ops@py8n.test",
                                  "boss@py8n.test"], wire["to"]
            msg = email.message_from_bytes(wire["data"],
                                           policy=email.policy.default)
            subject = str(msg["Subject"])
            assert subject.startswith("[py8n] Weekly chain digest"), subject
            assert "2 ride(s) across 4 leg(s)" in subject, subject
            assert "Chains: Revenue chain, Supply chain" in subject, subject

            # the body names its rhythm (the SAME envelope path to the
            # report's OWN list) and breaks the rides down BY chain
            assert "Rhythm: weekly digest - the same envelope that carries " \
                "the report, to the report's own list" in wire["text"], \
                wire["text"]
            assert "Revenue chain 1 ride(s)" in wire["text"], wire["text"]
            assert "Supply chain 1 ride(s)" in wire["text"], wire["text"]
            assert "Recipients: 2" in wire["text"], wire["text"]

            # the attachment parses back out of the wire: rows from BOTH
            # chains, each ride under its own chain's name
            attachment = next(p for p in msg.iter_attachments()
                              if p.get_filename() == result["filename"])
            rows = _parse_csv(attachment.get_content())
            row_chains = {r[0] for r in rows[1:]}
            assert row_chains == {"Revenue chain", "Supply chain"}, rows
            assert any(r[9] == "+15551010101" for r in rows[1:]), rows
            assert any(r[9] == "PO-101" for r in rows[1:]), rows

            # the event door names the tag list and the rhythm
            evs = (await client.get("/events", params={
                "type": "business.chain_report_dispatched"})).json()
            mine = [e for e in evs["events"]
                    if e["payload"]["filename"] == result["filename"]]
            assert mine, evs
            assert mine[0]["payload"]["chains"] == ["Revenue chain",
                                                    "Supply chain"], evs
            assert mine[0]["payload"]["cadence"] == "weekly", evs

            # the due walk on the injected clock: the WEEKLY window holds
            # for 6 days and 23 hours, then the SAME envelope path rides
            # to the SAME list
            from app.db import AsyncSessionLocal
            from app.services import business_processes as bp_svc

            base = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as session:
                early = await bp_svc.dispatch_due_chain_reports(
                    session, user["id"], now=base + timedelta(days=6,
                                                              seconds=86400 - 1))
                await session.commit()
            assert early == [], early
            assert sink.count == 1, sink.messages
            async with AsyncSessionLocal() as session:
                due = await bp_svc.dispatch_due_chain_reports(
                    session, user["id"], now=base + timedelta(days=7,
                                                              seconds=1))
                await session.commit()
            assert len(due) == 1 and due[0]["recipients"] == 2, due
            assert due[0]["cadence"] == "weekly", due
            assert due[0]["chains"] == ["Revenue chain",
                                        "Supply chain"], due
            assert sink.count == 2, sink.messages
            assert sink.last()["to"] == ["ops@py8n.test",
                                         "boss@py8n.test"], sink.last()["to"]
            assert sink.last()["to"] == wire["to"], (sink.last(), wire)

            # and the schedule row stamped it, the window advanced weekly
            got = (await client.get("/processes/chain-report",
                                    headers=h)).json()["schedule"]
            assert got["last_result"]["delivery"] == "delivered", got
            assert got["next_due"] > next_due, (got["next_due"], next_due)

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the chains filter over the wire - the tag list, the honest empty,
#    the composition, the by-chain pivot
# ---------------------------------------------------------------------------

def test_v101_chains_filter_on_the_csv():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "csv")
            h = _auth(user["token"])
            installed = await _install_two_chains(client, h)
            await _ride_lead(client, h, installed["procs"]["Lead pipeline"],
                             "+15551010201", "Csv Deal")
            await _ride_purchase(
                client, h, installed["procs"]["Purchase lifecycle"],
                "PO-201", "Csv Order")

            # the tag list: BOTH chains ride the file - echoed in the
            # headers, the slug in the filename
            res = await client.get("/processes/chains/history.csv", headers=h,
                                   params={"chains": "Revenue chain, Supply chain"})
            assert res.status_code == 200, res.text
            assert res.headers["x-py8n-chains-filter"] == \
                "Revenue chain, Supply chain", res.headers
            assert res.headers["x-py8n-leg-count"] == "4", res.headers
            assert res.headers["x-py8n-ride-count"] == "2", res.headers
            assert "revenue-chain" in res.headers["content-disposition"], \
                res.headers
            rows = _parse_csv(res.text)
            assert {r[0] for r in rows[1:]} == {"Revenue chain",
                                                "Supply chain"}, rows

            # case-insensitive, order-insensitive: the same slice
            res2 = await client.get("/processes/chains/history.csv",
                                    headers=h,
                                    params={"chains": "supply CHAIN;revenue chain"})
            assert res2.headers["x-py8n-ride-count"] == "2", res2.headers

            # compose with the leg filter: the tag list narrowed to one leg
            res = await client.get("/processes/chains/history.csv", headers=h,
                                   params={"chains": "Revenue chain, Supply chain",
                                           "leg": "Lead pipeline|won"})
            assert res.headers["x-py8n-leg-count"] == "1", res.headers
            assert res.headers["x-py8n-ride-count"] == "1", res.headers

            # one name narrows to one chain (2 legs, 1 ride)
            res = await client.get("/processes/chains/history.csv", headers=h,
                                   params={"chains": "Revenue chain"})
            assert res.headers["x-py8n-leg-count"] == "2", res.headers
            assert res.headers["x-py8n-ride-count"] == "1", res.headers

            # an unknown name: the HONEST EMPTY file - header only
            res = await client.get("/processes/chains/history.csv", headers=h,
                                   params={"chains": "Ghost chain"})
            assert res.status_code == 200, res.text
            assert res.headers["x-py8n-leg-count"] == "0", res.headers
            assert len(_parse_csv(res.text)) == 1, res.text

            # the pivot arithmetic, straight off the service: the rides
            # break down BY chain (the digest body's own summary), and a
            # scoped call carries its own echo
            from app.db import AsyncSessionLocal
            from app.services import business_processes as bp_svc

            async with AsyncSessionLocal() as session:
                out = await bp_svc.chain_history_csv(session, user["id"],
                                                     history_limit=50)
            assert out["by_chain"].get("Revenue chain") == 1, out["by_chain"]
            assert out["by_chain"].get("Supply chain") == 1, out["by_chain"]
            assert out["chains_filter"] == "", out
            async with AsyncSessionLocal() as session:
                out = await bp_svc.chain_history_csv(
                    session, user["id"], history_limit=50,
                    chains=["Revenue chain"])
            assert out["chains_filter"] == "Revenue chain", out
            assert out["ride_count"] == 1, out
            assert out["by_chain"] == {"Revenue chain": 1}, out

    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the parser's own units, the subject's shapes, and the pin
# ---------------------------------------------------------------------------

def test_v101_scope_units_and_pin():
    from app.services.business_processes import (
        CHAIN_REPORT_CADENCES,
        CHAIN_REPORT_MAX_CHAINS,
        chain_report_subject,
        parse_report_chains,
    )

    # the same discipline the recipients obey: separators, strip, dedupe
    # case-insensitively (first spelling wins), order preserved
    assert parse_report_chains("Revenue, supply;Care\n revenue, Revenue") == \
        ["Revenue", "supply", "Care"]
    assert parse_report_chains("") == []
    assert parse_report_chains(None) == []
    assert parse_report_chains(" , ;") == []
    assert CHAIN_REPORT_MAX_CHAINS == 8

    # the ceiling is the caller's choice: the endpoint parses without it
    nine = ", ".join(f"Chain {i}" for i in range(9))
    assert len(parse_report_chains(nine, ceiling=None)) == 9
    with pytest.raises(Exception) as ei:
        parse_report_chains(nine)
    assert "at most 8" in str(ei.value)

    # the rhythms: weekly is the digest's own beat
    assert CHAIN_REPORT_CADENCES == {"hourly": 3600, "daily": 86400,
                                     "weekly": 604800}

    # the subject keeps every legacy shape...
    assert chain_report_subject(2, 3) == \
        "[py8n] Chain history - 3 ride(s) across 2 leg(s)"
    assert chain_report_subject(2, 3, "Revenue chain") == \
        "[py8n] Chain history - 3 ride(s) across 2 leg(s) - Revenue chain"
    assert chain_report_subject(2, 3, system="Ops") == \
        "[py8n] Chain history - 3 ride(s) across 2 leg(s) - System: Ops"
    # ...names the tag list when the scope is multi...
    assert chain_report_subject(4, 2, chains=["Revenue chain",
                                              "Supply chain"]) == \
        ("[py8n] Chain history - 2 ride(s) across 4 leg(s)"
         " - Chains: Revenue chain, Supply chain")
    # ...and the weekly digest renames the scan line
    assert chain_report_subject(4, 2, chains=["Revenue chain"],
                                cadence="weekly") == \
        ("[py8n] Weekly chain digest - 2 ride(s) across 4 leg(s)"
         " - Revenue chain")

    # and the pin
    assert settings.version == "1.102.0", settings.version
