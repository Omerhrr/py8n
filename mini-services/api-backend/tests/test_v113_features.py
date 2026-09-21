"""v113 tests - the ERP core: the backbone that composes the departments.

The shelf hired departments since v83 (sales, finance, procurement,
logistics); v113 adds the tenth operator - the ERP core - that ships the
company backbone the departments were missing:

* THE INSTALL: five datasets (Products, Sales orders, Stock movements,
  GL entries, ERP policy), three machines (Sales order lifecycle draft ->
  paid, Inventory replenishment one tracked entity per SKU, Month-end
  close), two hand-written reactive workflows (the order ledger poster +
  the stock pick ledger) plus the two generated onboarding loops, the
  clerk, the review room, the order desk queue, the board - bound into a
  RUNNING system. No dialer: collections is Finance's campaign.
* THE WIRE CARRIES THE MEMORY: business.state_changed now names the
  entity's context - the order's total and the sku/qty ride the event,
  so reactive workflows read facts off the wire instead of guessing
  from titles.
* THE HONEST STANDALONE: the ERP installs alone; shipping an order
  skips the invoice leg LOUDLY (business.journey_skipped naming the
  missing machine) - install Finance and the same ship OPENS the
  receivable with the order's own ref and the five-day SLA.
* THE REPLENISHMENT WALK: a low SKU reordered opens the Procurement
  operator's purchase order; the estate chain map draws the
  Replenishment chain with the un-installed tail as an honest pending
  leg naming Delivery pipeline.
* THE BOOKS POST THEMSELVES: boot the system with activate_workflows
  and the reactive path is live - every order move lands journal lines
  on GL entries (double-entry since v114: the ship debits receivable
  AND credits revenue, the payment debits cash AND clears the
  receivable, other moves land zero-value trail lines, the total from
  the wire) and every pick lands a stock movement (delta = -qty). The
  ledger is a workflow, not a report.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"


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


async def _wrap(coro):
    try:
        return await coro
    finally:
        await _drain_background()


def _sync(coro):
    return asyncio.run(coro)


def _base_name(name: str) -> str:
    """Second-and-later installs of an operator suffix their dataset names
    ('GL entries 2') - the spec order never moves; strip the suffix so the
    assertion holds no matter which install of the session ran first."""
    return re.sub(r"\s\d+$", "", name)


async def _mk_user(client: httpx.AsyncClient, tag: str) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v113-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v113 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _install(client: httpx.AsyncClient, h: dict, slug: str) -> dict:
    res = await client.post(f"/operators/{slug}/install", json={}, headers=h)
    assert res.status_code == 200, res.text
    return res.json()


async def _procs(client: httpx.AsyncClient, h: dict) -> dict:
    res = await client.get("/processes", headers=h)
    assert res.status_code == 200, res.text
    return {p["name"]: p for p in res.json()["processes"]}


async def _instances(client: httpx.AsyncClient, h: dict, pid: str) -> list[dict]:
    res = await client.get(f"/processes/{pid}/instances", headers=h)
    assert res.status_code == 200, res.text
    return res.json()["instances"]


async def _advance(client: httpx.AsyncClient, h: dict, pid: str, iid: str,
                   transition: str, actor: str = "v113-test") -> dict:
    res = await client.post(f"/processes/{pid}/instances/{iid}/advance",
                            json={"transition": transition, "actor": actor},
                            headers=h)
    assert res.status_code == 200, res.text
    return res.json()


async def _dataset_rows(client: httpx.AsyncClient, h: dict,
                        dataset_id: str) -> list[dict]:
    res = await client.get(f"/datasets/{dataset_id}/rows", headers=h)
    assert res.status_code == 200, res.text
    return res.json()["rows"]


# ---------------------------------------------------------------------------
# 1. the install - the backbone composes into a RUNNING system
# ---------------------------------------------------------------------------

def test_v113_erp_installs_the_backbone():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "install")
            h = _auth(user["token"])

            built = await _install(client, h, "erp-operator")
            assert built["system"]["lifecycle"] == "running"
            assert built["system"]["components"]["dataset"] == 7
            assert built["system"]["components"]["process"] == 4
            assert built["system"]["components"]["workflow"] == 8  # 4 reactive + 3 onboarding loops + the clerk's handler
            assert built["system"]["components"]["voice_agent"] == 1
            assert built["system"]["components"]["meeting"] == 1
            assert built["system"]["components"]["queue"] == 1
            assert built["system"]["components"]["dashboard"] == 1
            assert built["campaign"] is None  # collections is Finance's dialer

            ds_names = {_base_name(d["name"]) for d in built["datasets"]}
            assert {"Products", "Sales orders", "Stock movements", "GL entries",
                    "Employees", "Payroll runs", "ERP policy"} == ds_names

            procs = {p["name"]: p for p in built["processes"]}
            assert set(procs) == {"Sales order lifecycle",
                                  "Inventory replenishment", "Month-end close",
                                  "Payroll lifecycle"}
            # the order desk imports at its own stages (5 seed rows)
            assert procs["Sales order lifecycle"]["seeded_instances"] == 5
            # one tracked entity per SKU, the low ones start at 'low'
            assert procs["Inventory replenishment"]["seeded_instances"] == 5
            # the close calendar AND the payroll calendar arrive empty on purpose
            assert procs["Month-end close"]["seeded_instances"] == 0
            assert procs["Payroll lifecycle"]["seeded_instances"] == 0

            # every workflow installs INACTIVE - the boot door opens them
            assert all(w["active"] is False for w in built["workflows"])
            wf_names = {w["name"] for w in built["workflows"]}
            assert {"Order ledger poster", "Stock pick ledger", "Payroll poster",
                    "Purchase ledger poster",
                    "Sales order lifecycle onboarding",
                    "Inventory replenishment onboarding",
                    "Payroll lifecycle onboarding"} <= wf_names
            # the reactive trio rides the move event; the onboarding
            # loops ride dataset versions (the install list shows the
            # raw event_type - empty for dataset triggers)
            reactive = {w["name"]: w["trigger"] for w in built["workflows"]}
            assert reactive["Order ledger poster"] == "business.state_changed"
            assert reactive["Stock pick ledger"] == "business.state_changed"
            assert reactive["Payroll poster"] == "business.state_changed"
            assert reactive["Purchase ledger poster"] == "business.state_changed"
            assert reactive["Sales order lifecycle onboarding"] == ""
            assert reactive["Inventory replenishment onboarding"] == ""
            assert reactive["Payroll lifecycle onboarding"] == ""

            # the seeded instances sit at their own stages
            procs_live = await _procs(client, h)
            so = await _instances(client, h, procs_live["Sales order lifecycle"]["id"])
            by_ref = {i["ref"]: i["state"] for i in so}
            assert by_ref["SO-1042"] == "draft"
            assert by_ref["SO-1039"] == "confirmed"
            assert by_ref["SO-1036"] == "picked"
            assert by_ref["SO-1031"] == "invoiced"
            assert by_ref["SO-1028"] == "paid"
            # the seeded order carries its row as memory (the books read it)
            so1042 = next(i for i in so if i["ref"] == "SO-1042")
            assert so1042["context"]["total"] == "720.00"
            assert so1042["context"]["sku"] == "SKU-1001"
            inv = await _instances(client, h,
                                   procs_live["Inventory replenishment"]["id"])
            inv_states = {i["ref"]: i["state"] for i in inv}
            assert inv_states["SKU-1003"] == "low"
            assert inv_states["SKU-1005"] == "low"
            assert inv_states["SKU-1001"] == "healthy"

            # the detail page resolves the two chains, ERP at the head
            res = await client.get("/operators/erp-operator", headers=h)
            assert res.status_code == 200, res.text
            chains = {c["slug"]: c for c in res.json()["chains"]}
            assert set(chains) == {"order-to-cash", "replenishment"}
            assert chains["order-to-cash"]["position"] == 0
            repl_ops = [o["slug"] for o in chains["replenishment"]["operators"]]
            assert repl_ops == ["erp-operator", "procurement-operator",
                                "logistics-operator", "finance-operator"]
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the honest standalone - a ship names the missing invoice machine
# ---------------------------------------------------------------------------

def test_v113_erp_standalone_ships_honestly():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "standalone")
            h = _auth(user["token"])

            await _install(client, h, "erp-operator")
            procs = await _procs(client, h)
            assert "Invoice lifecycle" not in procs  # finance not installed

            pid = procs["Sales order lifecycle"]["id"]
            so = await _instances(client, h, pid)
            iid = next(i["id"] for i in so if i["ref"] == "SO-1042")

            for move in ("confirm", "pick", "ship"):
                out = await _advance(client, h, pid, iid, move)
            assert out["state"] == "shipped"
            # the leg skipped LOUDLY - the response names the missing machine
            legs = out.get("journeys_opened") or []
            assert len(legs) == 1 and legs[0]["opened"] is False
            assert "Invoice lifecycle" in legs[0]["reason"]

            # the skip is on the record: business.journey_skipped fired
            res = await client.get("/events",
                                   params={"type": "business.journey_skipped"},
                                   headers=h)
            assert res.status_code == 200, res.text
            ev = next(e for e in res.json()["events"]
                      if e["payload"]["ref"] == "SO-1042")
            assert ev["payload"]["target_process"] == "Invoice lifecycle"
            assert "not found" in ev["payload"]["reason"]

            # and no receivable appeared - nothing silently half-fired
            procs = await _procs(client, h)
            assert "Invoice lifecycle" not in procs
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. order-to-cash composes the company - the ship opens the receivable
# ---------------------------------------------------------------------------

def test_v113_order_to_cash_composes():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "o2c")
            h = _auth(user["token"])

            await _install(client, h, "finance-operator")
            await _install(client, h, "erp-operator")
            procs = await _procs(client, h)
            assert {"Sales order lifecycle", "Invoice lifecycle"} <= set(procs)

            pid = procs["Sales order lifecycle"]["id"]
            so = await _instances(client, h, pid)
            iid = next(i["id"] for i in so if i["ref"] == "SO-1039")

            await _advance(client, h, pid, iid, "pick")
            out = await _advance(client, h, pid, iid, "ship")
            legs = out.get("journeys_opened") or []
            assert len(legs) == 1 and legs[0]["opened"] is True
            target = legs[0]["target"]
            assert target["process_name"] == "Invoice lifecycle"
            assert target["ref"] == "SO-1039"  # the order's ref rides
            assert target["due_at"]  # the leg carries its own SLA

            # the opened payable remembers who opened it
            inv_rows = await _instances(client, h, procs["Invoice lifecycle"]["id"])
            inv = next(i for i in inv_rows if i["ref"] == "SO-1039")
            assert inv["state"] == "received"
            assert inv["context"]["via"] == "shipped order journey"
            assert inv["context"]["source_operator"] == "erp"
            assert inv["context"]["journey"]["from_process"] == "Sales order lifecycle"
            assert inv["context"]["journey"]["from_state"] == "shipped"

            # the wire carries the memory: the move's event names the total
            res = await client.get("/events",
                                   params={"type": "business.state_changed"},
                                   headers=h)
            assert res.status_code == 200, res.text
            ev = next(e for e in res.json()["events"]
                      if e["payload"]["ref"] == "SO-1039"
                      and e["payload"]["to"] == "shipped")
            assert ev["payload"]["context"]["total"] == "960.00"
            assert ev["payload"]["context"]["sku"] == "SKU-1002"

            # the order walks to paid; the receivable walks its own machine
            await _advance(client, h, pid, iid, "invoice")
            await _advance(client, h, pid, iid, "pay")
            order = await _instances(client, h, pid)
            assert next(i for i in order if i["ref"] == "SO-1039")["state"] == "paid"

            fin_pid = procs["Invoice lifecycle"]["id"]
            for move in ("match", "approve", "schedule", "pay"):
                await _advance(client, h, fin_pid, inv["id"], move)
            inv_rows = await _instances(client, h, fin_pid, )
            done = next(i for i in inv_rows if i["ref"] == "SO-1039")
            assert done["state"] == "paid" and done["is_terminal"] is True
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 4. the replenishment walk - a reorder opens the PO; the map draws it
# ---------------------------------------------------------------------------

def test_v113_replenishment_walks_the_supply_chain():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "repl")
            h = _auth(user["token"])

            await _install(client, h, "procurement-operator")
            await _install(client, h, "erp-operator")
            procs = await _procs(client, h)
            inv_pid = procs["Inventory replenishment"]["id"]
            sku = next(i for i in await _instances(client, h, inv_pid)
                       if i["ref"] == "SKU-1003")
            assert sku["state"] == "low"

            out = await _advance(client, h, inv_pid, sku["id"], "reorder")
            legs = out.get("journeys_opened") or []
            assert len(legs) == 1 and legs[0]["opened"] is True
            target = legs[0]["target"]
            assert target["process_name"] == "Purchase lifecycle"
            assert target["ref"] == "SKU-1003"
            assert target["due_at"]  # two days to dispatch

            po = next(i for i in await _instances(
                client, h, procs["Purchase lifecycle"]["id"])
                if i["ref"] == "SKU-1003")
            assert po["context"]["journey"]["from_process"] == \
                "Inventory replenishment"
            assert po["context"]["via"] == "reorder journey"

            # the estate map draws BOTH walks - the Replenishment chain
            # with its un-installed tail as an honest pending leg (the
            # walk stops where the department is missing, naming it)
            res = await client.get("/processes/chains", headers=h)
            assert res.status_code == 200, res.text
            chains = {c["name"]: c for c in res.json()["chains"]}
            assert "Replenishment chain" in chains
            repl = chains["Replenishment chain"]
            walk = [repl["head_name"]] + [l["to_name"] for l in repl["legs"]]
            assert walk == ["Inventory replenishment", "Purchase lifecycle",
                            "Delivery pipeline"]
            leg1, leg2 = repl["legs"]
            assert leg1["resolved"] is True and leg1["opened"] >= 1
            # logistics never installed: the walk stays honest about it
            assert leg2["resolved"] is False
            assert leg2["to_name"] == "Delivery pipeline"
            assert "Order to Cash chain" in chains
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 5. the books post themselves - the reactive path on the real engine
# ---------------------------------------------------------------------------

def test_v113_the_books_post_themselves():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "books")
            h = _auth(user["token"])

            built = await _install(client, h, "erp-operator")
            sys_id = built["system"]["id"]
            # the books land on THIS install's datasets - the spec order
            # is stable (Products, Sales orders, Stock movements, GL
            # entries, ERP policy); names suffix when the session store
            # already holds an earlier install's, so the ids are truth
            ds_specs = built["datasets"]
            mv_id = ds_specs[2]["id"]   # Stock movements
            gl_id = ds_specs[3]["id"]   # GL entries

            # the boot door: stop -> start(activate_workflows) opens the
            # reactive path (the install lands the system running but
            # its workflows inactive - the human picks the moment)
            res = await client.post(f"/systems/{sys_id}/stop", json={}, headers=h)
            assert res.status_code == 200, res.text
            res = await client.post(f"/systems/{sys_id}/start",
                                    json={"activate_workflows": True}, headers=h)
            assert res.status_code == 200, res.text

            procs = await _procs(client, h)
            pid = procs["Sales order lifecycle"]["id"]
            so = await _instances(client, h, pid)
            iid = next(i["id"] for i in so if i["ref"] == "SO-1042")

            for move in ("confirm", "pick", "ship", "invoice", "pay"):
                await _advance(client, h, pid, iid, move)
                await _drain_background()

            gl = await _dataset_rows(client, h, gl_id)
            by_memo = {r["memo"]: r for r in gl}
            assert "SO-1042 moved to confirmed" in by_memo
            ship_line = by_memo["SO-1042 moved to shipped"]
            assert ship_line["account"] == "Accounts receivable"
            assert ship_line["debit"] == "720.00"  # the total rode the wire
            assert ship_line["credit"] == ""
            # v114: the ship's OTHER side - revenue recognized
            rev_line = by_memo["revenue recognized on SO-1042"]
            assert rev_line["account"] == "Revenue"
            assert rev_line["debit"] == "" and rev_line["credit"] == "720.00"
            # invoiced is a document move now - a zero-value trail line
            inv_line = by_memo["SO-1042 moved to invoiced"]
            assert inv_line["debit"] == "" and inv_line["credit"] == ""
            # paid posts the balanced pair: cash IN, receivable cleared
            pay_line = by_memo["payment received on SO-1042"]
            assert pay_line["account"] == "Cash"
            assert pay_line["debit"] == "720.00" and pay_line["credit"] == ""
            settled = by_memo["SO-1042 settled"]
            assert settled["account"] == "Accounts receivable"
            assert settled["debit"] == "" and settled["credit"] == "720.00"
            # moves of OTHER machines never reach the books
            assert not any("Invoice lifecycle" in (r.get("memo") or "")
                           for r in gl)

            moves = await _dataset_rows(client, h, mv_id)
            assert len(moves) == 1
            mv = moves[0]
            assert mv["sku"] == "SKU-1001"
            assert mv["delta"] == "-6"  # the pick's qty, negated
            assert mv["reason"] == "picked for SO-1042"
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------

def test_v113_version_pin():
    assert settings.version == "1.118.0"
