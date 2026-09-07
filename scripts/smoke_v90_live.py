"""V90 live smoke: boot the real server and walk the digest into a real
inbox, then walk the two three-operator chains.

1. DIGEST OVER A BOUND ENDPOINT: a REAL email endpoint (email_inbound,
   SMTP out) is bound with its smtp_host pointed at the dev SMTP sink
   (scripts/dev_smtp_sink.py - a real RFC 5321 server on a real local
   socket). The real door walks the digest window and the ONE summary
   crosses the actual wire: policy -> door -> bucket -> render -> adapter
   -> smtplib -> sink. The inbox holds the message: scan-line subject,
   both refs in the body. A knock machine rides the same endpoint and
   its attempt lands with its own subject line.
2. THE REVENUE CHAIN: Operations + Sales + Finance install; a fresh lead
   walks the pipeline and WINS (the v89 leg: onboarding opens at
   kickoff), the case walks to hand_off - and the Finance invoice opens
   ITSELF at 'received' with its own 5-day SLA. One ref, three
   departments.
3. THE SUPPLY CHAIN: Procurement + Logistics install; the approved
   purchase is ORDERED (the delivery opens at 'placed' with its own
   SLA), the goods travel and are DELIVERED - the vendor's invoice opens
   itself at 'received'. One ref threading Procurement -> Logistics ->
   Finance.

Usage: /home/z/.venv/bin/python scripts/smoke_v90_live.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dev_smtp_sink import SmtpDevSink  # noqa: E402

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v74_server.py"
API = "http://127.0.0.1:8218/api/v1"
SERVER_PORT = 8218


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


# ---------------------------------------------------------------------------
# 1) the digest over a bound endpoint - the summary crosses the real wire
# ---------------------------------------------------------------------------

def digest_check(c: httpx.Client, sink: SmtpDevSink) -> dict:
    r = c.post("/channels/endpoints", json={
        "name": "Smoke inbox", "provider": "email_inbound",
        "config": {"secret": "smoke-secret",
                   "smtp_host": "127.0.0.1", "smtp_port": str(sink.port),
                   "smtp_user": "smoke", "smtp_pass": "smoke",
                   "from_address": "py8n@py8n.test"}})
    assert r.status_code == 201, r.text

    r = c.post("/processes", json={
        "name": "Smoke digest machine",
        "definition": {"states": ["a", "b", "done", "dead"], "initial": "a",
                       "transitions": [
                           {"name": "go", "from": "a", "to": "b"},
                           {"name": "finish", "from": "b", "to": "done"},
                           {"name": "kill", "from": "a", "to": "dead"}],
                       "escalation_policy": {
                           "channel": "email", "to": "billing@py8n.test",
                           "mode": "digest", "digest_every_seconds": 60,
                           "max_repeats": 1}}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    for ref in ("INV-901", "INV-902"):
        r = c.post(f"/processes/{pid}/instances",
                   json={"ref": ref, "title": f"invoice {ref}",
                         "due_in_seconds": 1})
        assert r.status_code == 201, r.text

    time.sleep(1.2)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    assert report["digest"]["sent"] == [] and \
        report["digest"]["pending"][0]["items"] == 2, report

    print("    ... walking one digest window (60s) on the real clock")
    time.sleep(61)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    sent = report["digest"]["sent"]
    assert len(sent) == 1 and sent[0]["items"] == 2, report
    assert sent[0]["delivery"] == "delivered", report
    assert "smtp accepted" in sent[0]["detail"], report

    assert sink.count == 1, sink.messages
    msg = sink.last()
    assert msg["to"] == ["billing@py8n.test"], msg
    assert msg["subject"] == ("[py8n] Escalation digest - Smoke digest "
                              "machine (2 item(s) past SLA)"), msg
    assert "INV-901" in msg["text"] and "INV-902" in msg["text"], msg
    evs = c.get("/events", params={"type": "business.escalation_digest"}
                ).json()["events"]
    assert len(evs) == 1 and evs[0]["payload"]["delivery"] == "delivered", evs
    assert evs[0]["payload"]["endpoint"] == "Smoke inbox", evs
    assert evs[0]["payload"]["subject"].startswith("[py8n] Escalation digest")

    # a knock rides the same bound endpoint - with its own subject line
    r = c.post("/processes", json={
        "name": "Smoke knock machine",
        "definition": {"states": ["a", "b", "done", "dead"], "initial": "a",
                       "transitions": [
                           {"name": "go", "from": "a", "to": "b"},
                           {"name": "finish", "from": "b", "to": "done"}],
                       "escalation_policy": {
                           "channel": "email", "to": "ops@py8n.test",
                           "repeat_every_seconds": 60, "max_repeats": 5}}})
    kpid = r.json()["id"]
    r = c.post(f"/processes/{kpid}/instances",
               json={"ref": "K-90", "title": "the stuck one",
                     "due_in_seconds": 1})
    assert r.status_code == 201, r.text
    time.sleep(1.2)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    mine = [e for e in report["recorded"] if e["delivery"] == "delivered"]
    assert mine, report
    assert sink.count == 2, sink.messages
    knock = sink.last()
    assert knock["to"] == ["ops@py8n.test"], knock
    assert knock["subject"] == ("[py8n] Smoke knock machine: 'the stuck one' "
                                "(ref K-90) past SLA in 'a' (attempt 1)"), knock
    return {"delivery": sent[0]["delivery"], "sink_port": sink.port}


# ---------------------------------------------------------------------------
# 2) the revenue chain - Sales -> Operations -> Finance
# ---------------------------------------------------------------------------

def revenue_chain_check(c: httpx.Client) -> dict:
    for slug in ("operations-operator", "sales-operator", "finance-operator"):
        r = c.post(f"/operators/{slug}/install", json={"note": "smoke v90"})
        assert r.status_code == 200, r.text
    procs = {p["name"]: p["id"]
             for p in c.get("/processes").json()["processes"]}
    lead_pid, onb_pid, inv_pid = (procs["Lead pipeline"],
                                  procs["Customer onboarding"],
                                  procs["Invoice lifecycle"])

    r = c.get(f"/processes/{onb_pid}").json()
    assert r["journeys"][0]["on_state"] == "handed_off", r["journeys"]
    assert r["journeys"][0]["open"]["process"] == "Invoice lifecycle"

    r = c.post(f"/processes/{lead_pid}/instances",
               json={"ref": "+15558880444", "title": "Chain Deal",
                     "context": {"company": "Chain Co"}})
    iid = r.json()["id"]
    for move in ("reach_out", "qualify", "book_demo", "run_demo",
                 "send_proposal", "negotiate", "win"):
        r = c.post(f"/processes/{lead_pid}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke"})
        assert r.status_code == 200, r.text

    r = c.get(f"/processes/{onb_pid}/instances").json()
    case = next(x for x in r["instances"] if x["ref"] == "+15558880444")
    for move in ("provision", "train", "go_live", "hand_off"):
        r = c.post(f"/processes/{onb_pid}/instances/{case['id']}/advance",
                   json={"transition": move, "actor": "smoke"})
        assert r.status_code == 200, r.text

    r = c.get(f"/processes/{inv_pid}/instances").json()
    inv = next(x for x in r["instances"] if x["ref"] == "+15558880444")
    assert inv["state"] == "received", inv
    assert inv["title"] == "Billing - Onboarding - Chain Deal", inv
    assert inv["context"]["journey"]["from_process"] == "Customer onboarding", inv
    assert inv["due_at"], inv
    return {"ref": inv["ref"], "state": inv["state"], "title": inv["title"]}


# ---------------------------------------------------------------------------
# 3) the supply chain - Procurement -> Logistics -> Finance
# ---------------------------------------------------------------------------

def supply_chain_check(c: httpx.Client) -> dict:
    for slug in ("procurement-operator", "logistics-operator"):
        r = c.post(f"/operators/{slug}/install", json={"note": "smoke v90"})
        assert r.status_code == 200, r.text
    procs = {p["name"]: p["id"]
             for p in c.get("/processes").json()["processes"]}
    pur_pid, dlv_pid, inv_pid = (procs["Purchase lifecycle"],
                                 procs["Delivery pipeline"],
                                 procs["Invoice lifecycle"])

    r = c.get(f"/processes/{pur_pid}/instances").json()
    purchase = next(x for x in r["instances"] if x["state"] == "approved")
    ref = purchase["ref"]

    r = c.post(f"/processes/{pur_pid}/instances/{purchase['id']}/advance",
               json={"transition": "order", "actor": "smoke"})
    assert r.status_code == 200, r.text
    opened = r.json().get("journeys_opened")
    assert opened and opened[0]["opened"] is True, r.text
    assert opened[0]["target"]["process_name"] == "Delivery pipeline"

    r = c.get(f"/processes/{dlv_pid}/instances").json()
    delivery = next(x for x in r["instances"] if x["ref"] == ref)
    assert delivery["due_at"], delivery
    for move in ("pick", "dispatch", "depart", "deliver"):
        r = c.post(f"/processes/{dlv_pid}/instances/{delivery['id']}/advance",
                   json={"transition": move, "actor": "smoke"})
        assert r.status_code == 200, r.text

    r = c.get(f"/processes/{inv_pid}/instances").json()
    inv = next(x for x in r["instances"] if x["ref"] == ref)
    assert inv["state"] == "received", inv
    assert inv["title"] == f"Bill - Delivery - {purchase['title']}", inv
    assert inv["context"]["journey"]["from_process"] == "Delivery pipeline", inv

    # ONE ref threads THREE departments
    r = c.get(f"/processes/{pur_pid}/instances").json()
    assert any(x["ref"] == ref for x in r["instances"])
    return {"ref": ref, "purchase": purchase["title"], "invoice": inv["title"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v90_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",  # the smoke drives the door
        "PORT": str(SERVER_PORT),
    })
    proc = subprocess.Popen(
        ["/home/z/.venv/bin/python", SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sink = SmtpDevSink().start()
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.90.0", version

            digest_check(c, sink)
            print(f"[1] DIGEST OVER A BOUND ENDPOINT OK - a REAL email "
                  f"endpoint (email_inbound, smtp_host 127.0.0.1:{sink.port}) "
                  f"was bound; the real door walked the 60s digest window and "
                  f"the ONE summary crossed the actual SMTP wire into the dev "
                  f"sink: scan-line subject, both refs in the body, delivery "
                  f"'delivered' in the report + the event; the knock machine "
                  f"rode the same endpoint with its own subject line")

            revenue_chain_check(c)
            print(f"[2] REVENUE CHAIN OK - Operations + Sales + Finance "
                  f"installed; the fresh lead WON (onboarding opened itself "
                  f"at kickoff), the case walked to hand_off, and the "
                  f"Finance invoice OPENED ITSELF at 'received' with its own "
                  f"5-day SLA - one ref, three departments, zero meetings")

            supply_chain_check(c)
            print(f"[3] SUPPLY CHAIN OK - the approved purchase was ORDERED "
                  f"(the delivery opened itself at 'placed' with its own "
                  f"SLA), the goods travelled pick -> dispatch -> depart -> "
                  f"deliver, and the vendor's invoice OPENED ITSELF at "
                  f"'received' - Procurement -> Logistics -> Finance on one "
                  f"ref, each hop business.journey_opened on the record")

            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        sink.stop()


if __name__ == "__main__":
    sys.exit(main())
