"""v115 tests - the purchase side of the books.

The ERP's books were double-entry since v114 but they only spoke the
SALES side: receivable + revenue + cash in, salary out. The company's
SPENDING never hit the ledger - no Inventory value, no Accounts payable,
no vendor bills. v115 wires the buy side end to end:

* JOURNEY MEMORY RENDERS: a leg's memory string values render from the
  source instance's fields, so the replenishment walk hands its facts
  forward - {sku} {cost} {reorder_qty} ride the replenishment -> PO ->
  delivery -> vendor-bill journey, and the bill arrives CARRYING its own
  cost*qty. A template naming a key the source lacks renders empty
  (honest degradation).
* THE PURCHASE LEDGER POSTER: the vendor bill the Supply chain opened
  (via='delivered journey') posts its balanced pairs - matched debits
  Inventory AND credits Accounts payable; paid debits Accounts payable
  AND credits Cash. Receivables and bills the books cannot value skip
  honestly.
* THE STOCK LEDGER SEES THE GOODS TOO: a replenishment landing on
  'replenished' lands its +reorder_qty movement row - buys in, orders
  out, one movements ledger.
* THE TRIAL BALANCE SPEAKS THE OWED SIDE: Inventory is an asset,
  Accounts payable a liability (credit-normal) - the report shows what
  the company owes, not just what it owns and earned.
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


def _base_name(name: str) -> str:
    """Second-and-later installs of an operator suffix their dataset names
    ('GL entries 2') - the spec order never moves, so the suffix-stripped
    names and the spec's own indexes are the stable handles."""
    return re.sub(r"\s\d+$", "", name)


async def _wrap(coro):
    try:
        return await coro
    finally:
        await _drain_background()


def _sync(coro):
    return asyncio.run(coro)


async def _mk_user(client: httpx.AsyncClient, tag: str) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v115-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v115 {tag}",
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
                   transition: str, actor: str = "v115-test") -> dict:
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


async def _boot(client: httpx.AsyncClient, h: dict, built: dict) -> None:
    """stop -> start(activate_workflows): the human's moment, then the
    reactive path goes live."""
    sys_id = built["system"]["id"]
    res = await client.post(f"/systems/{sys_id}/stop", json={}, headers=h)
    assert res.status_code == 200, res.text
    res = await client.post(f"/systems/{sys_id}/start",
                            json={"activate_workflows": True}, headers=h)
    assert res.status_code == 200, res.text


# ---------------------------------------------------------------------------
# 1. the buy side ships on the shelf - cost data, the threading legs, the poster
# ---------------------------------------------------------------------------

def test_v115_the_buy_side_ships():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "buy-side-shelf")
            h = _auth(user["token"])

            built = await _install(client, h, "erp-operator")
            ds_specs = built["datasets"]

            # the catalog carries what the buy side posts from
            products = await _dataset_rows(client, h, ds_specs[0]["id"])
            gloves = next(r for r in products if r["sku"] == "SKU-1003")
            assert gloves["cost"] == "5.10"
            assert gloves["reorder_qty"] == "30"

            # the replenishment leg hands its facts to the PO
            procs = await _procs(client, h)
            repl = procs["Inventory replenishment"]
            legs = repl["definition"]["journeys"]
            assert len(legs) == 1
            memory = legs[0]["open"]["memory"]
            assert memory["sku"] == "{sku}"
            assert memory["unit_cost"] == "{cost}"
            assert memory["qty"] == "{reorder_qty}"

            # the purchase ledger poster rides the shelf (the install
            # response lists the raw event_type on every workflow)
            res = await client.get("/operators/erp-operator", headers=h)
            assert res.status_code == 200, res.text
            plan = res.json()
            wf = {w["name"] for w in plan["installs"]["workflows"]}
            assert "Purchase ledger poster" in wf
            reactive = {w["name"]: w["trigger"] for w in built["workflows"]}
            assert reactive["Purchase ledger poster"] == "business.state_changed"

            # the clerk knows the buy-side answer now
            policy = await _dataset_rows(client, h, ds_specs[6]["id"])
            assert any("purchases hit the books" in (r.get("question") or "")
                       for r in policy)
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the purchase side of the books - the supply walk posts its pairs
# ---------------------------------------------------------------------------

def test_v115_the_purchase_side_of_the_books():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "buy-side-books")
            h = _auth(user["token"])

            # the whole supply chain takes the floor
            await _install(client, h, "finance-operator")
            await _install(client, h, "procurement-operator")
            await _install(client, h, "logistics-operator")
            built = await _install(client, h, "erp-operator")
            ds_specs = built["datasets"]
            gl_id = ds_specs[3]["id"]     # GL entries (spec order)
            mv_id = ds_specs[2]["id"]     # Stock movements
            await _boot(client, h, built)

            procs = await _procs(client, h)
            repl_pid = procs["Inventory replenishment"]["id"]
            po_pid = procs["Purchase lifecycle"]["id"]
            del_pid = procs["Delivery pipeline"]["id"]
            inv_pid = procs["Invoice lifecycle"]["id"]

            # a low SKU places its reorder - the PO opens carrying the facts
            sku = next(i for i in await _instances(client, h, repl_pid)
                       if i["ref"] == "SKU-1003")
            assert sku["state"] == "low"
            out = await _advance(client, h, repl_pid, sku["id"], "reorder")
            legs = out.get("journeys_opened") or []
            assert len(legs) == 1 and legs[0]["opened"] is True

            po = next(i for i in await _instances(client, h, po_pid)
                      if i["ref"] == "SKU-1003")
            assert po["context"]["sku"] == "SKU-1003"
            assert po["context"]["unit_cost"] == "5.10"
            assert po["context"]["qty"] == "30"

            # the PO orders itself into a delivery; the delivery rides the
            # facts again and lands the bill on Finance's machine
            for move in ("quote", "approve", "order"):
                await _advance(client, h, po_pid, po["id"], move)
            delivery = next(i for i in await _instances(client, h, del_pid)
                            if i["ref"] == "SKU-1003")
            assert delivery["context"]["unit_cost"] == "5.10"
            for move in ("pick", "dispatch", "depart", "deliver"):
                await _advance(client, h, del_pid, delivery["id"], move)

            bill = next(i for i in await _instances(client, h, inv_pid)
                        if i["ref"] == "SKU-1003")
            assert bill["state"] == "received"
            assert bill["context"]["via"] == "delivered journey"
            assert bill["context"]["unit_cost"] == "5.10"
            assert bill["context"]["qty"] == "30"

            # the MATCH posts the goods: Inventory debit / AP credit (153.00)
            await _advance(client, h, inv_pid, bill["id"], "match")
            await _drain_background()
            gl = await _dataset_rows(client, h, gl_id)
            got = [r for r in gl if r["memo"] == "goods received + matched for SKU-1003"]
            owed = [r for r in gl if r["memo"] == "bill owed on SKU-1003"]
            assert len(got) == 1 and len(owed) == 1, gl
            assert got[0]["account"] == "Inventory" and got[0]["debit"] == "153.0"
            assert owed[0]["account"] == "Accounts payable" \
                and owed[0]["credit"] == "153.0"

            # the PAYMENT clears the liability: AP debit / Cash credit
            for move in ("approve", "schedule", "pay"):
                await _advance(client, h, inv_pid, bill["id"], move)
            await _drain_background()
            gl = await _dataset_rows(client, h, gl_id)
            settled = [r for r in gl if r["memo"] == "bill paid on SKU-1003"]
            assert len(settled) == 2, gl
            by_acct = {r["account"]: r for r in settled}
            assert by_acct["Accounts payable"]["debit"] == "153.0"
            assert by_acct["Cash"]["credit"] == "153.0"

            # a receivable (or any non-supply bill) never posts here: the
            # finance seed INV-2041 advances with no 'via' - the GL is quiet
            # (finance tracks invoices by the vendor's phone, its ref_column)
            before = len(gl)
            seeded = next(i for i in await _instances(client, h, inv_pid)
                          if (i.get("context") or {}).get("invoice") == "INV-2041")
            await _advance(client, h, inv_pid, seeded["id"], "approve")
            await _drain_background()
            gl = await _dataset_rows(client, h, gl_id)
            assert len(gl) == before

            # the trial balance speaks the OWED side - and still balances
            res = await client.get("/erp/trial-balance",
                                   params={"dataset_id": gl_id}, headers=h)
            assert res.status_code == 200, res.text
            tb = res.json()
            assert tb["totals"]["balanced"] is True
            accounts = {r["account"]: r for r in tb["rows"]}
            assert accounts["Inventory"]["kind"] == "asset"
            assert accounts["Inventory"]["balance"] == 153.0
            assert accounts["Accounts payable"]["kind"] == "liability"
            assert accounts["Accounts payable"]["balance"] == 0.0  # settled
            assert accounts["Cash"]["balance"] == -153.0  # paid out
            # purchases are the balance sheet, not the income statement
            assert tb["income"] == {"revenue": 0.0, "expenses": 0.0, "net": 0.0}

            # the goods are on the shelf too: the restock lands +30
            out = await _advance(client, h, repl_pid, sku["id"], "restock")
            await _drain_background()
            assert out["state"] == "replenished"
            moves = await _dataset_rows(client, h, mv_id)
            restock = [m for m in moves if m["reason"] == "replenished SKU-1003"]
            assert len(restock) == 1
            assert restock[0]["sku"] == "SKU-1003"
            assert restock[0]["delta"] == "30"
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------

def test_v115_version_pin():
    assert settings.version == "1.115.0"
