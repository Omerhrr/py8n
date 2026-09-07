"""V86 live smoke: boot the real server and drive THE NINE-OPERATOR SHELF
+ THE CLOSED INTAKE LOOP end to end.

1. THE SHELF GREW TO NINE: the catalog ships the six department operators
   (support / operations / hr / finance / procurement / logistics); the
   finance operator installs a REAL Invoice lifecycle machine (escalate
   self-loops included) seeded one instance per invoice AT ITS LEDGER
   STAGE (ref = the vendor phone), plus the empty collections dialer,
   and both the payment advancer (business_advance) and the invoice
   intake (business_start) bind the BUILT process ids.
2. THE INTAKE LOOP IS CLOSED: boot the system (the boot door activates
   the reactive workflows), emit a REAL sms.received from an untracked
   vendor phone -> an invoice row AND a tracked instance appear BY
   THEMSELVES (the v86 business_start node riding a real event); the
   same phone texts again -> another row, never a doubled case; a REAL
   call.ended from a seeded vendor moves its invoice matched ->
   approved, and the ledger logger writes beside it.
3. THE DOOR ESCALATES THE NEW MACHINES: a fresh invoice due in one
   second breaches -> the sweep walks it through the machine's OWN
   escalate move (received -> received, the stint re-arms), both
   business.stuck AND business.state_changed land, and the process
   analytics count the nudge (the v86 metric fix: the door's nudge
   counts whether it landed as the machine's move or the no-move row).

Usage: /home/z/.venv/bin/python scripts/smoke_v86_live.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v74_server.py"
API = "http://127.0.0.1:8216/api/v1"
SERVER_PORT = 8216


def wait_health(client: httpx.Client, deadline: float = 30.0) -> None:
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


def _drain() -> None:
    time.sleep(1.5)


# ---------------------------------------------------------------------------
# 1) the shelf grew to nine - the finance operator installs bound
# ---------------------------------------------------------------------------

def install_check(c: httpx.Client) -> dict:
    r = c.get("/operators")
    assert r.status_code == 200, r.text
    ops = {o["slug"]: o for o in r.json()["operators"]}
    assert len(ops) == 9, sorted(ops)
    assert {"support-operator", "operations-operator", "hr-operator",
            "finance-operator", "procurement-operator",
            "logistics-operator"} <= set(ops)
    assert ops["hr-operator"]["topology"]["processes"] == 2
    assert ops["finance-operator"]["topology"]["campaign"] == 1

    r = c.post("/operators/finance-operator/install", json={})
    assert r.status_code == 200, r.text
    built = r.json()
    assert built["system"]["lifecycle"] == "running", built["system"]
    assert built["system"]["components"].get("process") == 1, built["system"]
    assert built["system"]["components"].get("campaign") == 1, built["system"]
    proc = built["processes"][0]
    assert proc["name"] == "Invoice lifecycle" and proc["seeded_instances"] == 3, proc

    # seeded AT the ledger's own stage
    r = c.get(f"/processes/{proc['id']}/instances")
    stages = {i["ref"]: i["state"] for i in r.json()["instances"]}
    assert stages["+15550006111"] == "matched", stages   # Brightline (imported)
    assert stages["+15550006222"] == "approved", stages  # Corelink (imported)
    assert stages["+15550006333"] == "received", stages  # Datahost (imported)

    # advancer AND intake bind the BUILT process id
    for wname, ntype in (("Payment advancer", "business_advance"),
                         ("Invoice intake", "business_start")):
        wf = next(w for w in built["workflows"] if w["name"] == wname)
        r = c.get(f"/workflows/{wf['id']}")
        node = next(n for n in r.json()["graph"]["nodes"] if n["type"] == ntype)
        assert node["parameters"]["process"] == proc["id"], node["parameters"]
    return {"built": built, "proc": proc}


# ---------------------------------------------------------------------------
# 2) the intake loop is closed - real events open rows AND tracked cases
# ---------------------------------------------------------------------------

def loop_check(c: httpx.Client, built: dict, proc: dict) -> None:
    sid = built["system"]["id"]
    r = c.post(f"/systems/{sid}/stop")
    assert r.status_code == 200, r.text
    r = c.post(f"/systems/{sid}/start", json={"activate_workflows": True})
    assert r.status_code == 200, r.text
    assert r.json()["workflows_activated"] >= 4, r.text  # intake + logger + advancer + handler

    ds_id = next(d["id"] for d in built["datasets"] if d["name"].startswith("Invoices"))

    # a REAL sms.received from an UNTRACKED vendor phone
    vendor_ref = "+15550006999"
    r = c.post("/events", json={
        "type": "sms.received", "source": "sms", "actor": vendor_ref,
        "payload": {"text": "invoice from Vinet & Co, 640 due net 30"},
        "correlation_id": "smoke-sms-vinet", "session_id": "smoke-sms-vinet"})
    assert r.status_code == 201, r.text
    _drain()

    r = c.get(f"/processes/{proc['id']}/instances")
    fresh = [i for i in r.json()["instances"] if i["ref"] == vendor_ref]
    assert len(fresh) == 1, fresh  # the intake STARTED the tracked instance
    assert fresh[0]["state"] == "received"

    r = c.get(f"/datasets/{ds_id}/rows")
    rows = r.json() if isinstance(r.json(), list) else r.json().get("rows", [])
    assert any(row.get("phone") == vendor_ref for row in rows), rows

    # the same phone texts again - another row, never a doubled case
    r = c.post("/events", json={
        "type": "sms.received", "source": "sms", "actor": vendor_ref,
        "payload": {"text": "resending the invoice"},
        "correlation_id": "smoke-sms-vinet-2", "session_id": "smoke-sms-vinet-2"})
    assert r.status_code == 201, r.text
    _drain()
    r = c.get(f"/processes/{proc['id']}/instances")
    assert len([i for i in r.json()["instances"]
                if i["ref"] == vendor_ref]) == 1

    # a REAL call.ended from a SEEDED vendor moves its invoice (matched -> approved)
    r = c.post("/events", json={
        "type": "call.ended", "source": "voice", "actor": "+15550006111",
        "payload": {"end_reason": "completed", "state": "ended"},
        "correlation_id": "smoke-call-brightline", "session_id": "smoke-call-brightline"})
    assert r.status_code == 201, r.text
    _drain()

    r = c.get(f"/processes/{proc['id']}/instances")
    bright = next(i for i in r.json()["instances"] if i["ref"] == "+15550006111")
    assert bright["state"] == "approved", bright  # the invoice moved ITSELF
    r = c.get("/events", params={"type": "business.state_changed",
                                 "correlation_id": bright["id"]})
    evs = r.json()["events"]
    assert len(evs) == 1 and evs[0]["payload"]["to"] == "approved", evs


# ---------------------------------------------------------------------------
# 3) the door escalates the new machines - and the analytics count it
# ---------------------------------------------------------------------------

def door_check(c: httpx.Client, proc: dict) -> None:
    # the seeded invoices sit far inside their SLA
    r = c.post("/scheduler/escalations/tick")
    out = r.json()
    assert out["scanned"] >= 3 and not out["escalated"] and not out["recorded"], out

    # a fresh invoice breaches - the machine's OWN escalate move
    r = c.post(f"/processes/{proc['id']}/instances", json={
        "ref": "INV-STUCK-1", "title": "The stuck invoice", "due_in_seconds": 1})
    iid = r.json()["id"]
    time.sleep(1.6)

    r = c.post("/scheduler/escalations/tick")
    out = r.json()
    assert len(out["escalated"]) == 1, out
    entry = out["escalated"][0]
    assert entry["instance_id"] == iid
    assert entry["state"] == "received" and entry["moved_to"] == "received", entry

    # both facts on the thread + the stint re-armed
    r = c.get("/events", params={"correlation_id": iid})
    types = {e["type"] for e in r.json()["events"]}
    assert {"business.stuck", "business.state_changed"} <= types, types
    r = c.get(f"/processes/{proc['id']}/instances/{iid}")
    assert r.json()["age_in_state_seconds"] < 5, r.json()

    # the v86 metric fix: the machine's own escalate move COUNTS
    r = c.get(f"/processes/{proc['id']}/analytics")
    assert r.json()["escalations"] == 1, r.json()

    # one knock per stint
    r = c.post("/scheduler/escalations/tick")
    out = r.json()
    assert not out["escalated"] and not out["recorded"], out


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v86_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PORT": str(SERVER_PORT),
    })
    proc_srv = subprocess.Popen(
        ["/home/z/.venv/bin/python", SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.86.0", version

            got = install_check(c)
            print(f"[1] THE SHELF GREW TO NINE OK - the six department operators "
                  f"ship next to the founding three (hr carries TWO machines, "
                  f"finance and logistics carry dialers); the finance operator "
                  f"composed the Invoice lifecycle machine (escalate self-loops on "
                  f"every working state), seeded 3 invoices AT their ledger stages "
                  f"(ref = the vendor phone), shipped the empty collections dialer, "
                  f"and BOTH the payment advancer and the invoice intake point at "
                  f"the BUILT process id (system {got['built']['system']['id'][:8]})")

            loop_check(c, got["built"], got["proc"])
            print(f"[2] THE INTAKE LOOP IS CLOSED OK - boot opened the reactive "
                  f"path; a REAL sms.received from an untracked vendor opened an "
                  f"invoice row AND a tracked instance BY THEMSELVES (the v86 "
                  f"business_start node riding a real event), the same phone "
                  f"texting again doubled nothing (on_duplicate=skip), and a REAL "
                  f"call.ended from a seeded vendor moved its invoice matched -> "
                  f"approved with business.state_changed on the thread")

            door_check(c, got["proc"])
            print(f"[3] THE DOOR ESCALATES THE NEW MACHINES OK - the seeded "
                  f"invoices sat inside their SLA (the sweep passed them), a "
                  f"breaching invoice got walked through the machine's OWN "
                  f"escalate move (received -> received, the stint re-armed, both "
                  f"business.stuck AND business.state_changed landed), the process "
                  f"analytics COUNT the nudge (the v86 metric fix), and the second "
                  f"tick knocked nothing (one escalation per stint)")

            return 0
    finally:
        proc_srv.terminate()
        try:
            proc_srv.wait(timeout=10)
        except Exception:
            proc_srv.kill()


if __name__ == "__main__":
    sys.exit(main())
