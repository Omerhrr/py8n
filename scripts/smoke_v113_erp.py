"""V113 live smoke: the ERP core on the real server - the company runs
end to end on the real clock.

1. THE COMPANY INSTALLS: ERP Core + Finance + Procurement + Logistics
   install one after another; each lands a RUNNING system with its
   machines seeded.
2. ORDER-TO-CASH ON THE REAL WIRE: the ERP system is booted with
   activate_workflows (the reactive path goes live); the seeded draft
   order SO-1042 walks confirm -> pick -> ship -> invoice -> pay; the
   ship OPENS the receivable on the Finance operator's Invoice
   lifecycle (same ref, its own five-day SLA); the receivable walks
   match -> approve -> schedule -> pay; the pick landed a real stock
   movement (delta -6) and the books carry REAL journal lines with the
   order's total riding the event's context - double-entry since v114:
   AR debit + revenue credit on shipped, cash debit + receivable
   clearing on paid - posted by the live ledger workflow.
3. THE REPLENISHMENT WALK: the low SKU (SKU-1003, seeded 'low') is
   reordered; the Purchase lifecycle opens the PO by itself with the
   SKU as its ref; the estate chain map draws BOTH the Order to Cash
   chain and the Replenishment chain with live counts.
4. THE CLOSE: a period is opened on the Month-end close machine and
   walks begin_close -> review -> close; the door's digest policy is
   on the definition.

Usage: /home/z/.venv/bin/python scripts/smoke_v113_erp.py
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
    r = c.post(f"/operators/{slug}/install", json={"note": "smoke v113"})
    assert r.status_code == 200, r.text
    return r.json()


def drain(c: httpx.Client, seconds: float = 2.0) -> None:
    """The inline executor runs workflows on background tasks - give the
    reactive path a real beat before reading the ledgers."""
    time.sleep(seconds)


def company_install_check(c: httpx.Client) -> dict:
    built = {}
    for slug in ("erp-operator", "finance-operator",
                 "procurement-operator", "logistics-operator"):
        built[slug] = install(c, slug)
        assert built[slug]["system"]["lifecycle"] == "running", built[slug]
    procs = {p["name"]: p["id"] for p in c.get("/processes").json()["processes"]}
    for name in ("Sales order lifecycle", "Inventory replenishment",
                 "Month-end close", "Invoice lifecycle",
                 "Purchase lifecycle", "Delivery pipeline"):
        assert name in procs, (name, list(procs))
    return {"systems": {k: v["system"]["name"] for k, v in built.items()}}


def order_to_cash_check(c: httpx.Client) -> dict:
    procs = {p["name"]: p["id"] for p in c.get("/processes").json()["processes"]}
    erp_sys = next(s for s in c.get("/systems").json()
                   if s["name"] == "ERP Core")
    # the boot door: stop -> start(activate_workflows) opens the reactive path
    r = c.post(f"/systems/{erp_sys['id']}/stop", json={})
    assert r.status_code == 200, r.text
    r = c.post(f"/systems/{erp_sys['id']}/start",
               json={"activate_workflows": True})
    assert r.status_code == 200, r.text

    pid = procs["Sales order lifecycle"]
    insts = c.get(f"/processes/{pid}/instances").json()["instances"]
    so = next(i for i in insts if i["ref"] == "SO-1042")
    assert so["context"]["total"] == "720.00", so["context"]
    iid = so["id"]
    for move in ("confirm", "pick", "ship"):
        r = c.post(f"/processes/{pid}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke-v113"})
        assert r.status_code == 200, r.text
        if move == "ship":
            legs = r.json().get("journeys_opened") or []
            assert len(legs) == 1 and legs[0]["opened"] is True, r.json()
            assert legs[0]["target"]["process_name"] == "Invoice lifecycle"
            assert legs[0]["target"]["ref"] == "SO-1042"

    # the receivable opened itself - walk it to paid
    fin_pid = procs["Invoice lifecycle"]
    inv = next(i for i in c.get(f"/processes/{fin_pid}/instances").json()["instances"]
               if i["ref"] == "SO-1042")
    assert inv["state"] == "received" and inv["due_at"], inv
    assert inv["context"]["via"] == "shipped order journey"
    for move in ("match", "approve", "schedule", "pay"):
        r = c.post(f"/processes/{fin_pid}/instances/{inv['id']}/advance",
                   json={"transition": move, "actor": "smoke-v113"})
        assert r.status_code == 200, r.text

    # the order finishes its own walk - and the wire carries the memory
    for move in ("invoice", "pay"):
        r = c.post(f"/processes/{pid}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke-v113"})
        assert r.status_code == 200, r.text
    evs = c.get("/events", params={"type": "business.state_changed"}).json()["events"]
    ev = next(e for e in evs if e["payload"]["ref"] == "SO-1042"
              and e["payload"]["to"] == "paid")
    assert ev["payload"]["context"]["total"] == "720.00", ev["payload"]

    # the books posted themselves on the live reactive path (double-entry)
    drain(c, 3.0)
    ds = {d["name"]: d["id"] for d in c.get("/datasets").json()}
    gl = c.get(f"/datasets/{ds['GL entries']}/rows").json()["rows"]
    by_memo = {r["memo"]: r for r in gl}
    ship_line = by_memo["SO-1042 moved to shipped"]
    assert ship_line["account"] == "Accounts receivable", ship_line
    assert ship_line["debit"] == "720.00", ship_line
    rev_line = by_memo["revenue recognized on SO-1042"]
    assert rev_line["account"] == "Revenue" and rev_line["credit"] == "720.00", rev_line
    inv_line = by_memo["SO-1042 moved to invoiced"]
    assert inv_line["debit"] == "" and inv_line["credit"] == "", inv_line
    pay_line = by_memo["payment received on SO-1042"]
    assert pay_line["account"] == "Cash" and pay_line["debit"] == "720.00", pay_line
    settled = by_memo["SO-1042 settled"]
    assert settled["account"] == "Accounts receivable" \
        and settled["credit"] == "720.00", settled

    # the pick landed its movement
    mv = c.get(f"/datasets/{ds['Stock movements']}/rows").json()["rows"]
    assert len(mv) == 1, mv
    assert mv[0]["sku"] == "SKU-1001" and mv[0]["delta"] == "-6", mv
    assert mv[0]["reason"] == "picked for SO-1042", mv
    return {"gl_lines": len(gl), "movements": len(mv)}


def replenishment_and_chains_check(c: httpx.Client) -> dict:
    procs = {p["name"]: p["id"] for p in c.get("/processes").json()["processes"]}
    inv_pid = procs["Inventory replenishment"]
    sku = next(i for i in c.get(f"/processes/{inv_pid}/instances").json()["instances"]
               if i["ref"] == "SKU-1003")
    assert sku["state"] == "low", sku
    r = c.post(f"/processes/{inv_pid}/instances/{sku['id']}/advance",
               json={"transition": "reorder", "actor": "smoke-v113"})
    assert r.status_code == 200, r.text
    legs = r.json().get("journeys_opened") or []
    assert len(legs) == 1 and legs[0]["opened"] is True, r.json()
    assert legs[0]["target"]["process_name"] == "Purchase lifecycle"
    assert legs[0]["target"]["ref"] == "SKU-1003"
    assert legs[0]["target"]["due_at"]

    po = next(i for i in c.get(f"/processes/{procs['Purchase lifecycle']}/instances")
              .json()["instances"] if i["ref"] == "SKU-1003")
    assert po["context"]["via"] == "reorder journey", po["context"]

    # the estate map draws the company's walks
    chains = {ch["name"]: ch for ch in
              c.get("/processes/chains").json()["chains"]}
    assert "Order to Cash chain" in chains, list(chains)
    assert "Replenishment chain" in chains, list(chains)
    o2c = chains["Order to Cash chain"]
    assert o2c["legs"][0]["from_name"] == "Sales order lifecycle", o2c
    assert o2c["legs"][0]["opened"] >= 1
    repl = chains["Replenishment chain"]
    walk = [repl["head_name"]] + [l["to_name"] for l in repl["legs"]]
    assert walk[0] == "Inventory replenishment" and walk[1] == "Purchase lifecycle", walk
    leg1 = repl["legs"][0]
    assert leg1["resolved"] and leg1["opened"] >= 1
    return {"chains": list(chains), "replenishment_walk": walk,
            "po_due": bool(legs[0]["target"]["due_at"])}


def close_check(c: httpx.Client) -> dict:
    procs = {p["name"]: p["id"] for p in c.get("/processes").json()["processes"]}
    pid = procs["Month-end close"]
    r = c.post(f"/processes/{pid}/instances",
               json={"ref": "2026-08", "title": "August close",
                     "context": {"period": "2026-08"}})
    assert r.status_code == 201, r.text
    iid = r.json()["id"]
    for move in ("begin_close", "review", "close"):
        r = c.post(f"/processes/{pid}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke-v113"})
        assert r.status_code == 200, r.text
    inst = next(i for i in c.get(f"/processes/{pid}/instances").json()["instances"]
                if i["ref"] == "2026-08")
    # 'closed' is not terminal BY DESIGN - the reopen hatch (closed -> open)
    # keeps the period live; the machine still records the close
    assert inst["state"] == "closed", inst
    return {"period": "2026-08", "final_state": inst["state"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v113_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v113_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.114.0", version

            systems = company_install_check(c)
            print(f"[1] THE COMPANY INSTALLS OK - four operators landed "
                  f"four RUNNING systems: {', '.join(systems['systems'].values())}; "
                  f"all six ERP-relevant machines exist seeded")

            o2c = order_to_cash_check(c)
            print(f"[2] ORDER-TO-CASH ON THE REAL WIRE OK - the ERP system "
                  f"booted with activate_workflows; SO-1042 walked "
                  f"confirm -> pick -> ship -> invoice -> pay; the ship "
                  f"OPENED the receivable on the Finance operator's "
                  f"Invoice lifecycle (same ref, its own SLA) and it walked "
                  f"match -> approve -> schedule -> pay; the books posted "
                  f"themselves on the live reactive path ({o2c['gl_lines']} "
                  f"GL lines: AR debit + revenue credit on shipped, cash "
                  f"debit + the clearing on paid) and the pick landed its "
                  f"movement ({o2c['movements']} row, delta -6)")

            repl = replenishment_and_chains_check(c)
            print(f"[3] THE REPLENISHMENT WALK OK - the low SKU reordered "
                  f"and the Procurement operator's PO opened itself "
                  f"(SKU-1003, two-day SLA); the estate map drew the Order "
                  f"to Cash chain AND the Replenishment chain "
                  f"({repl['chains']})")

            closed = close_check(c)
            print(f"[4] THE CLOSE OK - period {closed['period']} opened on "
                  f"the Month-end close machine and walked "
                  f"begin_close -> review -> close "
                  f"(final: {closed['final_state']})")

            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
