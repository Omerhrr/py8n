"""v118: the month closes - the aging, the cash flow and THE CLOSE.

The ERP doors always spoke read-only; v118 gives the books their first
WRITE and the collection desk its first grip:

* the AGING - every open receivable ref and open payable ref with its
  age (from the journal lines' ``at`` stamps) in the fixed buckets
  current / 31-60 / 61-90 / 90+ / undated; settled refs say nothing;
* the CASH FLOW - the direct-method movement of the money: every ref
  that touches a cash account is sectioned (operating / investing /
  financing / other) by its counterpart rows under a stated heuristic,
  and the sections' net IS the cash balance's movement;
* THE CLOSE - revenue and expense sweep through Income summary into
  Retained earnings with REAL closing entries appended to the same
  book; the close is recorded and the book LOCKS. An unbalanced book
  is refused, an empty book is refused, a second close is a loud 409,
  and the balance sheet still ties out afterward (the retained earnings
  account lives INSIDE equity, so the equation reads assets ==
  liabilities + equity once it exists).

Every report reads the dataset through the SAME one-pass reader in
``app/services/erp_books.py``, so they can never disagree.
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


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


def _sync(coro):
    return asyncio.run(coro)


async def _mk_user(client: httpx.AsyncClient, tag: str) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v118-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v118 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_dataset(client: httpx.AsyncClient, h: dict,
                      name: str, rows: list[dict]) -> dict:
    res = await client.post("/datasets", json={"name": name, "rows": rows},
                            headers=h)
    assert res.status_code == 201, res.text
    return res.json()


def _at(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# the operating book - a sale, a payroll run, a vendor bill:
# 1353.0 on both sides, net 800.0 waiting to be closed into retained
_OPERATING_ROWS = [
    {"ref": "SO-2001", "account": "Cash", "debit": "1000.00",
     "credit": "0.00", "memo": "sale settled"},
    {"ref": "SO-2001", "account": "Revenue", "debit": "0.00",
     "credit": "1000.00", "memo": "sale settled"},
    {"ref": "PR-2026-09", "account": "Salary expense", "debit": "200.00",
     "credit": "0.00", "memo": "payroll run"},
    {"ref": "PR-2026-09", "account": "Cash", "debit": "0.00",
     "credit": "200.00", "memo": "payroll run"},
    {"ref": "PO-3001", "account": "Inventory", "debit": "153.00",
     "credit": "0.00", "memo": "vendor bill matched"},
    {"ref": "PO-3001", "account": "Accounts payable", "debit": "0.00",
     "credit": "153.00", "memo": "vendor bill matched"},
]


def test_v118_the_close_posts_locks_and_ties():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "close")
            h = _auth(user["token"])

            # the unknown dataset is a loud 404 - same as every report
            res = await client.post("/erp/close",
                                    params={"dataset_id": "no-such-book"},
                                    headers=h)
            assert res.status_code == 404, res.text

            ds = await _mk_dataset(client, h, "Close books", _OPERATING_ROWS)

            # before: the door says the book is open
            res = await client.get("/erp/close",
                                   params={"dataset_id": ds["id"]}, headers=h)
            assert res.status_code == 200, res.text
            assert res.json()["closed"] is False
            assert res.json()["closes"] == []

            # THE CLOSE: net 800 sweeps into retained earnings
            res = await client.post("/erp/close",
                                    params={"dataset_id": ds["id"]},
                                    json={"period": "2026-08"}, headers=h)
            assert res.status_code == 200, res.text
            receipt = res.json()
            assert receipt["net"] == 800.0
            assert receipt["revenue"] == 1000.0
            assert receipt["expenses"] == 200.0
            assert receipt["retained_after"] == 800.0
            assert receipt["locked"] is True
            # every entry carries the CLOSE ref and lands on the stamped book
            assert receipt["entries"] == len(receipt["lines"]) > 0
            assert {line["ref"] for line in receipt["lines"]} == \
                {receipt["ref"]}
            accounts = {line["account"] for line in receipt["lines"]}
            assert {"Revenue", "Salary expense", "Income summary",
                    "Retained earnings"} <= accounts

            # the receipt board remembers
            res = await client.get("/erp/close",
                                   params={"dataset_id": ds["id"]}, headers=h)
            assert res.status_code == 200, res.text
            hist = res.json()
            assert hist["closed"] is True
            assert len(hist["closes"]) == 1
            assert hist["closes"][0]["net"] == 800.0
            assert hist["closes"][0]["period"] == "2026-08"

            # the LOCK: a second close is a loud 409
            res = await client.post("/erp/close",
                                    params={"dataset_id": ds["id"]},
                                    json={}, headers=h)
            assert res.status_code == 409, res.text
            assert "already closed" in res.json()["detail"]

            # the books AFTER: income accounts read zero, the earnings
            # live in retained earnings, and the trial balance still balances
            res = await client.get("/erp/trial-balance",
                                   params={"dataset_id": ds["id"]}, headers=h)
            assert res.status_code == 200, res.text
            tb = res.json()
            by_account = {r["account"]: r for r in tb["rows"]}
            assert by_account["Revenue"]["balance"] == 0.0
            assert by_account["Salary expense"]["balance"] == 0.0
            assert by_account["Retained earnings"]["balance"] == 800.0
            assert by_account["Income summary"]["balance"] == 0.0
            assert tb["totals"]["balanced"] is True
            assert tb["income"]["net"] == 0.0
            buckets = {b["kind"]: b for b in tb["summary"]}
            assert buckets["revenue"]["total"] == 0.0
            assert buckets["expense"]["total"] == 0.0
            assert buckets["equity"]["total"] == 800.0

            # the balance sheet STILL ties out: the retained account is
            # inside equity now, so the equation reads assets ==
            # liabilities + equity (953 = 153 + 800)
            res = await client.get("/erp/statements",
                                   params={"dataset_id": ds["id"]}, headers=h)
            assert res.status_code == 200, res.text
            st = res.json()
            assert st["balance"]["total_assets"] == 953.0
            assert st["balance"]["total_liabilities"] == 153.0
            assert st["balance"]["total_equity"] == 800.0
            assert st["balance"]["retained_earnings"] == 800.0
            assert st["balance"]["equation_side"] == 953.0
            assert st["balance"]["balanced"] is True

    _sync(_go())


def test_v118_the_close_refuses_the_honest_way():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "refuse")
            h = _auth(user["token"])

            # an UNBALANCED book - the close refuses to post
            bad = await _mk_dataset(client, h, "Close unbalanced", [
                {"ref": "SO-9001", "account": "Cash", "debit": "1000.00",
                 "credit": "0.00", "memo": "sale"},
                {"ref": "SO-9001", "account": "Revenue", "debit": "0.00",
                 "credit": "1000.00", "memo": "sale"},
                {"ref": "X-1", "account": "Goodwill", "debit": "10.00",
                 "credit": "0.00", "memo": "stray debit"},
            ])
            res = await client.post("/erp/close",
                                    params={"dataset_id": bad["id"]},
                                    json={}, headers=h)
            assert res.status_code == 409, res.text
            assert "do not balance" in res.json()["detail"]

            # an EMPTY (memo-only) book - nothing to close is a loud 409
            blank = await _mk_dataset(client, h, "Close blank", [
                {"ref": "M-1", "account": "Order desk", "debit": "0.00",
                 "credit": "0.00", "memo": "walk-in"},
            ])
            res = await client.post("/erp/close",
                                    params={"dataset_id": blank["id"]},
                                    json={}, headers=h)
            assert res.status_code == 409, res.text
            assert "nothing to close" in res.json()["detail"]
            # ...and the blank book stays unlocked (it was never closed)
            res = await client.get("/erp/close",
                                   params={"dataset_id": blank["id"]},
                                   headers=h)
            assert res.json()["closed"] is False

            # a LOSS-making book closes honestly against retained earnings
            loss = await _mk_dataset(client, h, "Close loss", [
                {"ref": "SO-9002", "account": "Revenue", "debit": "0.00",
                 "credit": "100.00", "memo": "sale"},
                {"ref": "PR-9002", "account": "Salary expense",
                 "debit": "400.00", "credit": "0.00", "memo": "payroll"},
                {"ref": "PR-9002", "account": "Cash", "debit": "0.00",
                 "credit": "400.00", "memo": "payroll"},
                {"ref": "SO-9002", "account": "Cash", "debit": "100.00",
                 "credit": "0.00", "memo": "sale"},
            ])
            res = await client.post("/erp/close",
                                    params={"dataset_id": loss["id"]},
                                    json={}, headers=h)
            assert res.status_code == 200, res.text
            receipt = res.json()
            assert receipt["net"] == -300.0
            assert receipt["retained_after"] == -300.0

    _sync(_go())


def test_v118_the_aging_buckets():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "aging")
            h = _auth(user["token"])

            ds = await _mk_dataset(client, h, "Aging books", [
                # INV-A: invoiced 5 days ago, part-paid 2 days ago -> 300 open, current
                {"ref": "INV-A", "account": "Accounts receivable",
                 "debit": "500.00", "credit": "0.00", "memo": "invoice",
                 "at": _at(5)},
                {"ref": "INV-A", "account": "Revenue", "debit": "0.00",
                 "credit": "500.00", "memo": "invoice", "at": _at(5)},
                {"ref": "INV-A", "account": "Cash", "debit": "200.00",
                 "credit": "0.00", "memo": "part payment", "at": _at(2)},
                {"ref": "INV-A", "account": "Accounts receivable",
                 "debit": "0.00", "credit": "200.00", "memo": "part payment",
                 "at": _at(2)},
                # INV-B: invoiced 100 days ago, untouched -> 700 open, 90+
                {"ref": "INV-B", "account": "Accounts receivable",
                 "debit": "700.00", "credit": "0.00", "memo": "invoice",
                 "at": _at(100)},
                {"ref": "INV-B", "account": "Revenue", "debit": "0.00",
                 "credit": "700.00", "memo": "invoice", "at": _at(100)},
                # PO-C: a bill matched 45 days ago -> 153 owed, 31-60
                {"ref": "PO-C", "account": "Inventory", "debit": "153.00",
                 "credit": "0.00", "memo": "bill", "at": _at(45)},
                {"ref": "PO-C", "account": "Accounts payable",
                 "debit": "0.00", "credit": "153.00", "memo": "bill",
                 "at": _at(45)},
                # PO-D: an undated bill -> 50 owed, undated
                {"ref": "PO-D", "account": "Salary expense",
                 "debit": "50.00", "credit": "0.00", "memo": "bill"},
                {"ref": "PO-D", "account": "Accounts payable",
                 "debit": "0.00", "credit": "50.00", "memo": "bill"},
            ])

            res = await client.get("/erp/aging",
                                   params={"dataset_id": ds["id"]}, headers=h)
            assert res.status_code == 200, res.text
            aging = res.json()

            # the receivables: two open refs, 1000.0 total
            ar = aging["receivables"]
            assert ar["open_refs"] == 2
            assert ar["total"] == 1000.0
            buckets = {b["bucket"]: b for b in ar["buckets"]}
            assert buckets["current"]["total"] == 300.0
            assert buckets["current"]["refs"] == 1
            assert buckets["d90_plus"]["total"] == 700.0
            assert buckets["d31_60"]["total"] == 0.0
            lines = {line["ref"]: line for line in ar["lines"]}
            assert lines["INV-A"]["open"] == 300.0
            assert lines["INV-A"]["bucket"] == "current"
            assert lines["INV-A"]["age_days"] is not None
            assert lines["INV-B"]["bucket"] == "d90_plus"
            assert lines["INV-B"]["age_days"] >= 99

            # the payables: two open refs, 203.0 total, one undated
            ap = aging["payables"]
            assert ap["open_refs"] == 2
            assert ap["total"] == 203.0
            buckets = {b["bucket"]: b for b in ap["buckets"]}
            assert buckets["d31_60"]["total"] == 153.0
            assert buckets["undated"]["total"] == 50.0
            assert buckets["current"]["total"] == 0.0

            # a settled ref says nothing: pay INV-A in full and it leaves
            res = await client.post(f"/datasets/{ds['id']}/rows", headers=h,
                                    json={"rows": [
                                        {"ref": "INV-A",
                                         "account": "Cash", "debit": "300.00",
                                         "credit": "0.00", "memo": "settled",
                                         "at": _at(0)},
                                        {"ref": "INV-A",
                                         "account": "Accounts receivable",
                                         "debit": "0.00", "credit": "300.00",
                                         "memo": "settled", "at": _at(0)},
                                    ]})
            assert res.status_code in (200, 201), res.text
            res = await client.get("/erp/aging",
                                   params={"dataset_id": ds["id"]}, headers=h)
            ar = res.json()["receivables"]
            assert ar["open_refs"] == 1
            assert ar["total"] == 700.0

    _sync(_go())


def test_v118_the_cash_flow_sections():
    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "cash")
            h = _auth(user["token"])

            ds = await _mk_dataset(client, h, "Cash flow books", [
                # the sale's cash lands: operating +1000
                {"ref": "SO-1", "account": "Cash", "debit": "1000.00",
                 "credit": "0.00", "memo": "sale settled"},
                {"ref": "SO-1", "account": "Revenue", "debit": "0.00",
                 "credit": "1000.00", "memo": "sale settled"},
                # payroll: operating -200
                {"ref": "PR-1", "account": "Salary expense",
                 "debit": "200.00", "credit": "0.00", "memo": "payroll"},
                {"ref": "PR-1", "account": "Cash", "debit": "0.00",
                 "credit": "200.00", "memo": "payroll"},
                # a vendor bill matched (no cash) then PAID: operating -153
                {"ref": "PO-1", "account": "Inventory", "debit": "153.00",
                 "credit": "0.00", "memo": "bill matched"},
                {"ref": "PO-1", "account": "Accounts payable",
                 "debit": "0.00", "credit": "153.00", "memo": "bill matched"},
                {"ref": "PO-1", "account": "Accounts payable",
                 "debit": "153.00", "credit": "0.00", "memo": "bill paid"},
                {"ref": "PO-1", "account": "Cash", "debit": "0.00",
                 "credit": "153.00", "memo": "bill paid"},
                # equipment bought: investing -400
                {"ref": "EQ-1", "account": "Equipment", "debit": "400.00",
                 "credit": "0.00", "memo": "the owner's machine"},
                {"ref": "EQ-1", "account": "Cash", "debit": "0.00",
                 "credit": "400.00", "memo": "the owner's machine"},
                # a loan drawn: financing +2000
                {"ref": "LOAN-1", "account": "Cash", "debit": "2000.00",
                 "credit": "0.00", "memo": "loan drawn"},
                {"ref": "LOAN-1", "account": "Loan payable",
                 "debit": "0.00", "credit": "2000.00", "memo": "loan drawn"},
            ])

            res = await client.get("/erp/cash-flow",
                                   params={"dataset_id": ds["id"]}, headers=h)
            assert res.status_code == 200, res.text
            cf = res.json()
            assert cf["cash_accounts"] == ["Cash"]
            sections = {s["name"]: s for s in cf["sections"]}
            # the treadmill (sales, payroll, paying the vendor): 647.0
            assert sections["operating"]["net"] == 647.0
            assert sections["operating"]["inflows"] == 1000.0
            assert sections["operating"]["outflows"] == 353.0
            # the owner's machine: investing -400
            assert sections["investing"]["net"] == -400.0
            # the loan: financing +2000
            assert sections["financing"]["net"] == 2000.0
            assert sections["other"]["net"] == 0.0
            # the sections' net IS the cash movement, to the cent
            assert cf["inflow_total"] == 3000.0
            assert cf["outflow_total"] == 753.0
            assert cf["net_cash_movement"] == 2247.0
            cash_rows = [1000.0 - 200.0 - 153.0 - 400.0 + 2000.0]
            assert cf["net_cash_movement"] == round(cash_rows[0], 2)

            # an unknown dataset is a loud 404 here too
            res = await client.get("/erp/cash-flow",
                                   params={"dataset_id": "no-such-book"},
                                   headers=h)
            assert res.status_code == 404, res.text

    _sync(_go())


def test_v118_version_pin():
    assert settings.version == "1.120.0"
