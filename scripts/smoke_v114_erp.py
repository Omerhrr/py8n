"""V114 live smoke: the books speak + the people get paid - the ERP on
the real server.

1. THE COMPANY INSTALLS: ERP Core + Finance install; the payroll module
   rides the shelf (Employees roster populated, Payroll runs calendar
   empty on purpose, the Payroll lifecycle machine wired with its door).
2. THE BOOKS SPEAK (DOUBLE-ENTRY): the ERP system is booted with
   activate_workflows; the seeded draft order SO-1042 walks confirm ->
   pick -> ship -> invoice -> pay; the ship posts its BALANCED pair
   (Accounts receivable debit + Revenue credit), the payment posts its
   own (Cash debit + the receivable's clearing credit); the receivable
   opened itself on Finance's Invoice lifecycle. GET /erp/trial-balance
   over the live GL dataset proves the books balance and reads the
   income: revenue 720.00, net 720.00, cash 720.00, receivable settled.
3. THE PEOPLE GET PAID: a payroll run (PR-2026-08, gross 21400.00)
   lands as a row AND a tracked instance (the People tab's own move);
   it walks calculate -> approve -> pay and paying posts the balanced
   pair (Salary expense debit + Cash credit) off the wire's own gross;
   the trial balance moves with it - expenses 21400.00, net -20680.00,
   STILL balanced.

Usage: /home/z/.venv/bin/python scripts/smoke_v114_erp.py
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
    r = c.post(f"/operators/{slug}/install", json={"note": "smoke v114"})
    assert r.status_code == 200, r.text
    return r.json()


def drain(c: httpx.Client, seconds: float = 3.0) -> None:
    """The inline executor runs workflows on background tasks - give the
    reactive path a real beat before reading the ledgers."""
    time.sleep(seconds)


def company_install_check(c: httpx.Client) -> dict:
    erp = install(c, "erp-operator")
    fin = install(c, "finance-operator")
    assert erp["system"]["lifecycle"] == "running", erp
    assert fin["system"]["lifecycle"] == "running", fin
    ds_names = {d["name"] for d in erp["datasets"]}
    assert {"Employees", "Payroll runs"} <= ds_names, ds_names
    procs = {p["name"]: p for p in c.get("/processes").json()["processes"]}
    for name in ("Sales order lifecycle", "Inventory replenishment",
                 "Month-end close", "Payroll lifecycle", "Invoice lifecycle"):
        assert name in procs, (name, list(procs))
    pl = next(p for p in erp["processes"] if p["name"] == "Payroll lifecycle")
    assert pl["seeded_instances"] == 0  # the calendar is the bookkeeper's
    return {"datasets": sorted(ds_names)}


def the_books_speak_check(c: httpx.Client) -> dict:
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
    iid = so["id"]
    for move in ("confirm", "pick", "ship"):
        r = c.post(f"/processes/{pid}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke-v114"})
        assert r.status_code == 200, r.text
    # the ship opened the receivable on Finance's machine
    fin_pid = procs["Invoice lifecycle"]
    inv = next(i for i in c.get(f"/processes/{fin_pid}/instances").json()["instances"]
               if i["ref"] == "SO-1042")
    assert inv["state"] == "received", inv
    for move in ("invoice", "pay"):
        r = c.post(f"/processes/{pid}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke-v114"})
        assert r.status_code == 200, r.text

    drain(c)
    ds = {d["name"]: d["id"] for d in c.get("/datasets").json()}
    gl = c.get(f"/datasets/{ds['GL entries']}/rows").json()["rows"]
    by_memo = {r["memo"]: r for r in gl}
    ship = by_memo["SO-1042 moved to shipped"]
    assert ship["account"] == "Accounts receivable" \
        and ship["debit"] == "720.00", ship
    rev = by_memo["revenue recognized on SO-1042"]
    assert rev["account"] == "Revenue" and rev["credit"] == "720.00", rev
    pay = by_memo["payment received on SO-1042"]
    assert pay["account"] == "Cash" and pay["debit"] == "720.00", pay
    settled = by_memo["SO-1042 settled"]
    assert settled["account"] == "Accounts receivable" \
        and settled["credit"] == "720.00", settled

    # THE TRIAL BALANCE - the books balance and the income speaks
    r = c.get("/erp/trial-balance", params={"dataset_id": ds["GL entries"]})
    assert r.status_code == 200, r.text
    tb = r.json()
    assert tb["totals"]["balanced"] is True, tb
    accounts = {a["account"]: a for a in tb["rows"]}
    assert accounts["Accounts receivable"]["balance"] == 0.0, accounts
    assert accounts["Cash"]["balance"] == 720.0, accounts
    assert accounts["Revenue"]["balance"] == 720.0, accounts
    assert tb["income"] == {"revenue": 720.0, "expenses": 0.0, "net": 720.0}, tb
    return {"gl_lines": len(gl), "net": tb["income"]["net"]}


def the_people_get_paid_check(c: httpx.Client) -> dict:
    procs = {p["name"]: p["id"] for p in c.get("/processes").json()["processes"]}
    ds = {d["name"]: d["id"] for d in c.get("/datasets").json()}
    pid = procs["Payroll lifecycle"]

    # run payroll the way the People tab does: the row AND the instance
    r = c.post(f"/datasets/{ds['Payroll runs']}/rows",
               json={"rows": [{"ref": "PR-2026-08", "period": "2026-08",
                               "gross": "21400.00", "headcount": "4",
                               "status": "draft"}]})
    assert r.status_code in (200, 201), r.text
    r = c.post(f"/processes/{pid}/instances",
               json={"ref": "PR-2026-08",
                     "title": "Payroll PR-2026-08 - 2026-08",
                     "context": {"ref": "PR-2026-08", "period": "2026-08",
                                 "gross": "21400.00", "headcount": "4",
                                 "status": "draft"}})
    assert r.status_code == 201, r.text
    iid = r.json()["id"]
    for move in ("calculate", "approve", "pay"):
        r = c.post(f"/processes/{pid}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke-v114"})
        assert r.status_code == 200, r.text

    drain(c)
    gl = c.get(f"/datasets/{ds['GL entries']}/rows").json()["rows"]
    pairs = [r for r in gl if r["memo"] == "payroll PR-2026-08 paid"]
    assert len(pairs) == 2, gl
    by_acct = {r["account"]: r for r in pairs}
    assert by_acct["Salary expense"]["debit"] == "21400.00", by_acct
    assert by_acct["Cash"]["credit"] == "21400.00", by_acct

    # the trial balance moved with the pay - and STILL balances
    tb = c.get("/erp/trial-balance",
               params={"dataset_id": ds["GL entries"]}).json()
    assert tb["totals"]["balanced"] is True, tb
    assert tb["income"]["expenses"] == 21400.0, tb
    assert tb["income"]["net"] == -20680.0, tb  # 720 revenue - 21400 payroll
    return {"net": tb["income"]["net"],
            "balanced": tb["totals"]["balanced"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v114_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v114_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.120.0", version

            inst = company_install_check(c)
            print(f"[1] THE COMPANY INSTALLS OK - ERP Core + Finance landed "
                  f"RUNNING; the payroll module rides the shelf "
                  f"({', '.join(inst['datasets'])})")

            books = the_books_speak_check(c)
            print(f"[2] THE BOOKS SPEAK OK - SO-1042 walked the desk while "
                  f"the receivable opened itself on Finance; the posters "
                  f"kept their pairs ({books['gl_lines']} GL lines) and the "
                  f"trial balance BALANCED: net {books['net']}, cash up, "
                  f"receivable settled")

            pay = the_people_get_paid_check(c)
            print(f"[3] THE PEOPLE GET PAID OK - PR-2026-08 walked "
                  f"calculate -> approve -> pay, posted its balanced pair "
                  f"(salary expense / cash) and the books STILL balance "
                  f"(net {pay['net']})")

            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
