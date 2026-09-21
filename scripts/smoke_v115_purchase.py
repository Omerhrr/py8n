"""V115 live smoke: the purchase side of the books - the ERP on the real
server.

1. THE COMPANY INSTALLS: ERP Core + Finance + Procurement + Logistics
   install - the whole supply chain takes the floor (the replenishment
   walk needs all four departments).
2. THE BUY SIDE POSTS ITSELF: the ERP system boots with
   activate_workflows; the low SKU (SKU-1003) places its reorder and the
   journey hands its facts forward - the PO opens carrying
   sku/unit_cost/qty, the delivery rides them again, and the vendor bill
   lands on Finance's machine STILL carrying its own cost*qty. The bill
   walks match -> approve -> schedule -> pay: the match posts the
   BALANCED pair (Inventory debit 153.0 + Accounts payable credit), the
   payment posts its own (Accounts payable debit + Cash credit), and the
   restock lands +30 on the Stock movements ledger. GET /erp/trial-balance
   proves the books balance and speak the OWED side: Inventory 153.0
   (asset), Accounts payable settled to 0.0 (liability), net income 0.0
   (purchases are the balance sheet, not the income statement).

Usage: /home/z/.venv/bin/python scripts/smoke_v115_purchase.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import httpx  # noqa: E402

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v74_server.py"
PY = ("/home/z/.venv/bin/python"
      if os.path.exists("/home/z/.venv/bin/python") else "python3")


def _free_port() -> int:
    """A fresh port per run - a zombie server from an earlier smoke must
    never answer for THIS one."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


SERVER_PORT = _free_port()
API = f"http://127.0.0.1:{SERVER_PORT}/api/v1"


def wait_health(client: httpx.Client, deadline: float = 240.0) -> None:
    """The boot imports pandas/sklearn/numexpr - minutes on a cold
    sandbox; the deadline has to outlive the imports."""
    end = time.time() + deadline
    while time.time() < end:
        try:
            res = client.get(f"{API}/health")
            if res.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.4)
    raise SystemExit("server never became healthy")


def install(c: httpx.Client, slug: str) -> dict:
    r = c.post(f"/operators/{slug}/install", json={"note": "smoke v115"})
    assert r.status_code == 200, r.text
    return r.json()


def drain(c: httpx.Client, seconds: float = 3.0) -> None:
    """The inline executor runs workflows on background tasks - give the
    reactive path a real beat before reading the ledgers."""
    time.sleep(seconds)


def advance(c: httpx.Client, pid: str, iid: str, move: str) -> dict:
    r = c.post(f"/processes/{pid}/instances/{iid}/advance",
               json={"transition": move, "actor": "smoke-v115"})
    assert r.status_code == 200, r.text
    return r.json()


def company_install_check(c: httpx.Client) -> dict:
    for slug in ("finance-operator", "procurement-operator",
                 "logistics-operator", "erp-operator"):
        built = install(c, slug)
        assert built["system"]["lifecycle"] == "running", slug
    procs = {p["name"] for p in c.get("/processes").json()["processes"]}
    for name in ("Sales order lifecycle", "Inventory replenishment",
                 "Purchase lifecycle", "Delivery pipeline",
                 "Invoice lifecycle", "Payroll lifecycle"):
        assert name in procs, (name, procs)
    return {"processes": len(procs)}


def the_buy_side_posts_check(c: httpx.Client) -> dict:
    procs = {p["name"]: p["id"] for p in c.get("/processes").json()["processes"]}
    erp_sys = next(s for s in c.get("/systems").json()
                   if s["name"] == "ERP Core")
    # the boot door: stop -> start(activate_workflows) opens the reactive path
    r = c.post(f"/systems/{erp_sys['id']}/stop", json={})
    assert r.status_code == 200, r.text
    r = c.post(f"/systems/{erp_sys['id']}/start",
               json={"activate_workflows": True})
    assert r.status_code == 200, r.text

    repl_pid = procs["Inventory replenishment"]
    insts = c.get(f"/processes/{repl_pid}/instances").json()["instances"]
    sku = next(i for i in insts if i["ref"] == "SKU-1003")
    assert sku["state"] == "low", sku["state"]

    # the reorder opens the PO - and the PO CARRIES THE FACTS
    out = advance(c, repl_pid, sku["id"], "reorder")
    legs = out.get("journeys_opened") or []
    assert len(legs) == 1 and legs[0]["opened"] is True, legs
    po_pid = procs["Purchase lifecycle"]
    po = next(i for i in c.get(f"/processes/{po_pid}/instances").json()["instances"]
              if i["ref"] == "SKU-1003")
    assert po["context"]["unit_cost"] == "5.10", po["context"]
    assert po["context"]["qty"] == "30", po["context"]
    for move in ("quote", "approve", "order"):
        advance(c, po_pid, po["id"], move)

    # the delivery rides the facts again and lands the bill
    del_pid = procs["Delivery pipeline"]
    delivery = next(i for i in c.get(f"/processes/{del_pid}/instances").json()["instances"]
                    if i["ref"] == "SKU-1003")
    assert delivery["context"]["unit_cost"] == "5.10", delivery["context"]
    for move in ("pick", "dispatch", "depart", "deliver"):
        advance(c, del_pid, delivery["id"], move)

    inv_pid = procs["Invoice lifecycle"]
    bill = next(i for i in c.get(f"/processes/{inv_pid}/instances").json()["instances"]
                if i["ref"] == "SKU-1003")
    assert bill["state"] == "received", bill["state"]
    assert bill["context"]["via"] == "delivered journey", bill["context"]
    assert bill["context"]["unit_cost"] == "5.10"
    assert bill["context"]["qty"] == "30"

    # the MATCH posts the goods: Inventory debit / Accounts payable credit
    advance(c, inv_pid, bill["id"], "match")
    drain(c)
    ds = {d["name"]: d["id"] for d in c.get("/datasets").json()}
    gl = c.get(f"/datasets/{ds['GL entries']}/rows").json()["rows"]
    got = [r for r in gl if r["memo"] == "goods received + matched for SKU-1003"]
    owed = [r for r in gl if r["memo"] == "bill owed on SKU-1003"]
    assert len(got) == 1 and got[0]["account"] == "Inventory" \
        and got[0]["debit"] == "153.0", got
    assert len(owed) == 1 and owed[0]["account"] == "Accounts payable" \
        and owed[0]["credit"] == "153.0", owed

    # the PAYMENT clears the liability: AP debit / Cash credit
    for move in ("approve", "schedule", "pay"):
        advance(c, inv_pid, bill["id"], move)
    drain(c)

    # the goods are on the shelf too: the restock lands +30
    advance(c, repl_pid, sku["id"], "restock")
    drain(c)
    mv = c.get(f"/datasets/{ds['Stock movements']}/rows").json()["rows"]
    restock = [m for m in mv if m["reason"] == "replenished SKU-1003"]
    assert len(restock) == 1 and restock[0]["delta"] == "30", restock

    # THE TRIAL BALANCE - the books balance and speak the OWED side
    r = c.get("/erp/trial-balance", params={"dataset_id": ds["GL entries"]})
    assert r.status_code == 200, r.text
    tb = r.json()
    assert tb["totals"]["balanced"] is True, tb
    accounts = {a["account"]: a for a in tb["rows"]}
    assert accounts["Inventory"]["kind"] == "asset", accounts
    assert accounts["Inventory"]["balance"] == 153.0, accounts
    assert accounts["Accounts payable"]["kind"] == "liability", accounts
    assert accounts["Accounts payable"]["balance"] == 0.0, accounts
    assert accounts["Cash"]["balance"] == -153.0, accounts
    assert tb["income"] == {"revenue": 0.0, "expenses": 0.0, "net": 0.0}, tb
    return {"gl_lines": len(gl), "inventory": accounts["Inventory"]["balance"],
            "ap": accounts["Accounts payable"]["balance"],
            "balanced": tb["totals"]["balanced"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v115_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v115_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.118.0", version

            inst = company_install_check(c)
            print(f"[1] THE COMPANY INSTALLS OK - ERP Core + Finance + "
                  f"Procurement + Logistics landed RUNNING "
                  f"({inst['processes']} machines on the floor)")

            books = the_buy_side_posts_check(c)
            print(f"[2] THE BUY SIDE POSTS ITSELF OK - SKU-1003 walked "
                  f"reorder -> PO -> delivery -> vendor bill with its own "
                  f"cost*qty riding the journey memory; the match posted "
                  f"Inventory/AP 153.0, the payment cleared the payable, "
                  f"the restock landed +30 ({books['gl_lines']} GL lines) "
                  f"and the trial balance BALANCED: Inventory "
                  f"{books['inventory']} (asset), AP {books['ap']} "
                  f"(settled liability), net 0.0")

            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        for f in (f"{BACKEND}/data",):
            try:
                os.remove(db_path)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
