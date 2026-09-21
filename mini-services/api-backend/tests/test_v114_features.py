"""v114 tests - the books speak + the people get paid.

The ERP core (v113) shipped the backbone but its books were a trail of
single-sided lines; v114 turns them into a REAL ledger and adds the
module every full ERP owes its people:

* DOUBLE-ENTRY POSTERS: the order ledger poster pairs every debit with
  a credit - shipped posts Accounts receivable debit AND Revenue
  credit, paid posts Cash debit AND the receivable's clearing credit,
  every other move lands a zero-value trail line. The payroll poster
  pairs salary expense with cash. The books BALANCE.
* THE TRIAL BALANCE DOOR: GET /erp/trial-balance over the GL entries
  dataset - one row per account (kind, debits, credits, balance by the
  kind's normal side), grand totals with the balanced flag, and the
  income summary (revenue vs expenses = net). Unknown or foreign
  datasets are a loud 404.
* THE PAYROLL MODULE: Employees roster + Payroll runs datasets (the
  calendar arrives empty - the period is the bookkeeper's to run) and
  the Payroll lifecycle machine (draft -> calculated -> approved ->
  paid, a recalculate hatch back to draft, escalate self-loops, a 12h
  door). Paying a run posts the balanced pair to the books off the
  wire's own context (gross), and the door's policy ships on the shelf.
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
        "email": f"v114-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v114 {tag}",
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
                   transition: str, actor: str = "v114-test") -> dict:
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
# 1. the payroll module ships on the shelf - roster, calendar, machine, door
# ---------------------------------------------------------------------------

def test_v114_payroll_module_ships():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "payroll-shelf")
            h = _auth(user["token"])

            built = await _install(client, h, "erp-operator")
            assert built["system"]["components"]["dataset"] == 7
            ds_specs = built["datasets"]  # spec order: Products, Sales
            # orders, Stock movements, GL entries, Employees, Payroll
            # runs, ERP policy - only the NAMES suffix, never the order
            assert {_base_name(d["name"]) for d in ds_specs} == {
                "Products", "Sales orders", "Stock movements", "GL entries",
                "Employees", "Payroll runs", "ERP policy"}

            # the roster arrives populated; the calendar arrives empty
            roster = await _dataset_rows(client, h, ds_specs[4]["id"])
            assert len(roster) == 5
            assert {r["emp"] for r in roster} == {"E-01", "E-02", "E-03",
                                                  "E-04", "E-05"}
            calendar = await _dataset_rows(client, h, ds_specs[5]["id"])
            assert calendar == []

            # the payroll machine + its door policy
            procs = await _procs(client, h)
            pl = procs["Payroll lifecycle"]
            assert pl["definition"]["initial"] == "draft"
            names = {(t["name"], t["from"], t["to"])
                     for t in pl["definition"]["transitions"]}
            assert ("calculate", "draft", "calculated") in names
            assert ("approve", "calculated", "approved") in names
            assert ("pay", "approved", "paid") in names
            assert ("recalculate", "calculated", "draft") in names
            # the door's own escalation policy rode the shelf
            assert pl["definition"]["escalation_policy"]["channel"] == "email"
            assert pl["definition"]["escalation_policy"]["repeat_every_seconds"] == 43200

            # the clerk knows the payroll answer now
            res = await client.get("/operators/erp-operator", headers=h)
            assert res.status_code == 200, res.text
            plan = res.json()
            assert any("Payroll poster" == w["name"]
                       for w in plan["installs"]["workflows"])
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 2. the books speak - double-entry posters + the trial balance door
# ---------------------------------------------------------------------------

def test_v114_the_books_balance_and_speak():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "books")
            h = _auth(user["token"])

            built = await _install(client, h, "erp-operator")
            gl_spec = built["datasets"][3]     # GL entries (spec order)
            gl_id = gl_spec["id"]
            await _boot(client, h, built)

            procs = await _procs(client, h)
            pid = procs["Sales order lifecycle"]["id"]
            so = await _instances(client, h, pid)
            iid = next(i["id"] for i in so if i["ref"] == "SO-1042")

            for move in ("confirm", "pick", "ship", "invoice", "pay"):
                await _advance(client, h, pid, iid, move)
                await _drain_background()

            # the unknown dataset is a loud 404 - no silent empty report
            res = await client.get("/erp/trial-balance",
                                   params={"dataset_id": "no-such-dataset"},
                                   headers=h)
            assert res.status_code == 404, res.text

            # the trial balance over the real books
            res = await client.get("/erp/trial-balance",
                                   params={"dataset_id": gl_id}, headers=h)
            assert res.status_code == 200, res.text
            tb = res.json()
            assert tb["totals"]["balanced"] is True
            accounts = {r["account"]: r for r in tb["rows"]}
            # the ship's pair + the payment's pair + the trail lines
            assert accounts["Accounts receivable"]["debits"] == 720.0
            assert accounts["Accounts receivable"]["credits"] == 720.0
            assert accounts["Accounts receivable"]["balance"] == 0.0  # settled
            assert accounts["Accounts receivable"]["kind"] == "asset"
            assert accounts["Revenue"]["credits"] == 720.0
            assert accounts["Revenue"]["balance"] == 720.0  # credit-normal
            assert accounts["Revenue"]["kind"] == "revenue"
            assert accounts["Cash"]["debits"] == 720.0
            assert accounts["Cash"]["balance"] == 720.0
            # the zero-value trail lines landed as memo rows
            assert accounts["Order desk"]["kind"] == "memo"
            assert accounts["Order desk"]["balance"] == 0.0
            # 5 moves = 2 financial pairs (4 lines) + 3 zero-value trails
            assert tb["line_count"] == 7
            # the income speaks
            assert tb["income"]["revenue"] == 720.0
            assert tb["income"]["expenses"] == 0.0
            assert tb["income"]["net"] == 720.0

            # resolving by NAME works too (case-insensitive) - the real
            # name may carry an install suffix; case never matters
            res = await client.get("/erp/trial-balance",
                                   params={"dataset_id": gl_spec["name"].upper()},
                                   headers=h)
            assert res.status_code == 200, res.text
            assert res.json()["totals"]["balanced"] is True
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------
# 3. the people get paid - a payroll run walks the machine and the books see it
# ---------------------------------------------------------------------------

def test_v114_payroll_runs_and_posts():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "payroll")
            h = _auth(user["token"])

            built = await _install(client, h, "erp-operator")
            ds_specs = built["datasets"]  # spec order stable
            runs_id = ds_specs[5]["id"]   # Payroll runs
            gl_id = ds_specs[3]["id"]     # GL entries
            await _boot(client, h, built)

            procs = await _procs(client, h)
            pid = procs["Payroll lifecycle"]["id"]

            # run payroll the way the People tab does: the row AND the
            # tracked instance (the onboarding loop's on_duplicate=skip
            # keeps the two doors from fighting)
            res = await client.post(f"/datasets/{runs_id}/rows", headers=h,
                                    json={"rows": [{
                                        "ref": "PR-2026-08",
                                        "period": "2026-08",
                                        "gross": "21400.00",
                                        "headcount": "4",
                                        "status": "draft"}]})
            assert res.status_code in (200, 201), res.text
            res = await client.post(f"/processes/{pid}/instances", headers=h,
                                    json={"ref": "PR-2026-08",
                                          "title": "Payroll PR-2026-08 - 2026-08",
                                          "context": {"ref": "PR-2026-08",
                                                      "period": "2026-08",
                                                      "gross": "21400.00",
                                                      "headcount": "4",
                                                      "status": "draft"}})
            assert res.status_code == 201, res.text
            iid = res.json()["id"]

            for move in ("calculate", "approve", "pay"):
                await _advance(client, h, pid, iid, move)
                await _drain_background()

            run = next(i for i in await _instances(client, h, pid)
                       if i["ref"] == "PR-2026-08")
            assert run["state"] == "paid" and run["is_terminal"] is True

            # the books saw the pay: the balanced pair off the wire's gross
            gl = await _dataset_rows(client, h, gl_id)
            pairs = [r for r in gl if r["memo"] == "payroll PR-2026-08 paid"]
            assert len(pairs) == 2, gl
            by_acct = {r["account"]: r for r in pairs}
            assert by_acct["Salary expense"]["debit"] == "21400.00"
            assert by_acct["Salary expense"]["credit"] == ""
            assert by_acct["Cash"]["credit"] == "21400.00"
            assert by_acct["Cash"]["debit"] == ""

            # the trial balance moves with it: expenses up, net down, BALANCED
            res = await client.get("/erp/trial-balance",
                                   params={"dataset_id": gl_id}, headers=h)
            tb = res.json()
            assert tb["totals"]["balanced"] is True
            assert tb["income"]["expenses"] == 21400.0
            assert tb["income"]["net"] == -21400.0
            accounts = {r["account"]: r for r in tb["rows"]}
            assert accounts["Salary expense"]["kind"] == "expense"
            assert accounts["Salary expense"]["balance"] == 21400.0
            assert accounts["Cash"]["balance"] == -21400.0  # paid out
    _sync(_wrap(_go()))


# ---------------------------------------------------------------------------

def test_v114_version_pin():
    assert settings.version == "1.121.0"
