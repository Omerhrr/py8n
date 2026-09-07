"""V94 live smoke: boot the real server and walk the two new surfaces
through the real API on the real clock.

1. ACK/SNOOZE SURFACED ON THE CHAIN NODES: two real operator installs
   (sales + operations), both machines bound to one floor system; a lead
   walks to won and the leg opens the onboarding case itself; a second
   case with a 1s SLA goes past it, the real door sweeps it through the
   machine's own escalate self-loop, and the human takes it FROM THE
   NODE'S ROW - the same ack endpoint the attention rows post to, with a
   4h snooze loan. GET /systems/{id} then shows the node wearing the
   book: acked 1, snoozed 1, and the overdue instance named with the
   same receipt the attention feed serves.
2. THE ESCALATION-HISTORY SPARKLINE, FED BY A REAL KNOCK: a machine with
   an email policy bound to the real SMTP sink; a 1s SLA instance goes
   past it; the real door records the breach and the knock crosses the
   actual wire (the sink holds exactly one message); the ack lands; the
   machine's analytics then carry escalation_history - 14 named day
   buckets with TODAY's escalations and acknowledgements counted.
3. THE HONEST QUIET + THE LOAN REPLACED: a machine nobody ever knocked
   shows 14 all-zero days (a quiet stretch reads as data, not absence);
   re-acking the chain node's instance WITHOUT a snooze replaces the
   loan with ownership - snoozed drops to 0 while acked stays 1 and the
   snooze_until stamp is gone.

Usage: /home/z/.venv/bin/python scripts/smoke_v94_live.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dev_smtp_sink import SmtpDevSink  # noqa: E402

import httpx  # noqa: E402

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
# 1) ack/snooze surfaced on the chain nodes
# ---------------------------------------------------------------------------

def chain_node_ack_check(c: httpx.Client) -> dict:
    built = {}
    for slug in ("sales-operator", "operations-operator"):
        r = c.post(f"/operators/{slug}/install", json={"note": "smoke v94"})
        assert r.status_code == 200, r.text
        built[slug] = r.json()

    procs = {p["name"]: p["id"]
             for p in c.get("/processes").json()["processes"]}
    lead_pid, onb_pid = (procs["Lead pipeline"],
                         procs["Customer onboarding"])

    r = c.post("/systems", json={"name": "the v94 floor"})
    assert r.status_code == 201, r.text
    floor_id = r.json()["id"]
    for comp_pid in (lead_pid, onb_pid):
        r = c.post(f"/systems/{floor_id}/components",
                   json={"kind": "process", "ref_id": comp_pid})
        assert r.status_code == 201, r.text

    # a lead walks the whole way to won - the leg opens the case itself
    r = c.post(f"/processes/{lead_pid}/instances",
               json={"ref": "+15559990001", "title": "Floor Deal"})
    iid = r.json()["id"]
    for move in ("reach_out", "qualify", "book_demo", "run_demo",
                 "send_proposal", "negotiate", "win"):
        r = c.post(f"/processes/{lead_pid}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke"})
        assert r.status_code == 200, r.text

    # a second onboarding case with a 1s SLA - late within seconds
    r = c.post(f"/processes/{onb_pid}/instances",
               json={"ref": "OB-S1", "title": "the node's own breach",
                     "due_in_seconds": 1})
    assert r.status_code == 201, r.text
    ob_iid = r.json()["id"]
    time.sleep(1.2)

    # the real door: the machine's own escalate self-loop takes it
    report = c.post("/scheduler/escalations/tick", json={}).json()
    assert any(e["ref"] == "OB-S1" for e in report["escalated"]), report

    # the human takes it FROM THE NODE'S ROW - the same endpoint the
    # attention rows and the machine view post to, with a 4h loan
    r = c.post(f"/processes/{onb_pid}/instances/{ob_iid}/escalations/ack",
               json={"by": "ops", "note": "taking it from the drawing",
                     "snooze_hours": 4})
    assert r.status_code == 200, r.text

    r = c.get(f"/systems/{floor_id}").json()
    chains = r["chains"]
    assert [x["slug"] for x in chains] == ["revenue"], chains
    nodes = {n["process"]: n for n in chains[0]["nodes"]}
    ob_node = nodes["Customer onboarding"]
    assert ob_node["bound"] is True, ob_node
    # the pack seeds its own onboarding cases at install - OB-S1 joins them
    assert ob_node["open"] >= 2 and ob_node["overdue"] == 1, ob_node
    assert ob_node["acked"] == 1 and ob_node["snoozed"] == 1, ob_node
    listing = ob_node["overdue_instances"]
    assert len(listing) == 1, listing
    row = listing[0]
    assert row["process_id"] == onb_pid and row["instance_id"] == ob_iid, row
    assert row["ref"] == "OB-S1", row
    assert row["overdue_seconds"] >= 0, row
    assert row["escalation"]["count"] == 1, row
    assert row["escalation"]["acked_by"] == "ops", row
    assert row["escalation"]["snooze_until"], row
    assert row["escalation"]["snooze_active"] is True, row
    # the leg's counts carry the ack/snooze sub-counts too
    leg0 = chains[0]["legs"][0]
    assert leg0["counts"]["acked"] == 0 and leg0["counts"]["snoozed"] == 0, leg0
    assert "acked" in leg0["counts"] and "snoozed" in leg0["counts"], leg0
    return {"ctx": (onb_pid, ob_iid, floor_id),
            "node": {"overdue": ob_node["overdue"], "acked": ob_node["acked"],
                     "snoozed": ob_node["snoozed"]},
            "ack_row": row["escalation"]["acked_by"]}


# ---------------------------------------------------------------------------
# 2) the escalation-history sparkline, fed by a real knock
# ---------------------------------------------------------------------------

def sparkline_check(c: httpx.Client, sink: SmtpDevSink) -> dict:
    r = c.post("/channels/endpoints", json={
        "name": "Smoke v94 inbox", "provider": "email_inbound",
        "config": {"secret": "smoke-secret",
                   "smtp_host": "127.0.0.1", "smtp_port": str(sink.port),
                   "smtp_user": "smoke", "smtp_pass": "smoke",
                   "from_address": "py8n@py8n.test"}})
    assert r.status_code == 201, r.text

    r = c.post("/processes", json={
        "name": "Smoke sparkline machine",
        "definition": {"states": ["a", "done"], "initial": "a",
                       "transitions": [
                           {"name": "finish", "from": "a", "to": "done"}],
                       "escalation_policy": {
                           "channel": "email", "to": "ops@py8n.test",
                           "repeat_every_seconds": 3600,
                           "max_repeats": 5}}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = c.post(f"/processes/{pid}/instances",
               json={"ref": "SPK-1", "title": "the counted breach",
                     "due_in_seconds": 1})
    assert r.status_code == 201, r.text
    spk_iid = r.json()["id"]
    time.sleep(1.2)

    before = sink.count
    report = c.post("/scheduler/escalations/tick", json={}).json()
    assert any(e["ref"] == "SPK-1" for e in report["recorded"]), report
    assert sink.count == before + 1, (before, sink.count)
    assert "[py8n] Smoke sparkline machine" in sink.messages[-1]["subject"], \
        sink.messages[-1]["subject"]

    # the receipt joins the same day's ledger
    r = c.post(f"/processes/{pid}/instances/{spk_iid}/escalations/ack",
               json={"by": "ops", "note": "seen"})
    assert r.status_code == 200, r.text

    a = c.get(f"/processes/{pid}/analytics").json()
    hist = a["escalation_history"]
    assert hist["window_days"] == 14, hist
    assert len(hist["days"]) == 14, hist
    today = hist["days"][-1]
    assert today["escalations"] >= 1, today
    assert today["acknowledgements"] >= 1, today
    assert today["total"] == (today["escalations"]
                              + today["acknowledgements"] + today["digests"])
    assert all(d["total"] == 0 for d in hist["days"][:-1]), hist
    return {"today": {"escalations": today["escalations"],
                      "acknowledgements": today["acknowledgements"]},
            "window_days": hist["window_days"],
            "sink_messages": sink.count}


# ---------------------------------------------------------------------------
# 3) the honest quiet + the loan replaced
# ---------------------------------------------------------------------------

def quiet_and_loan_check(c: httpx.Client, ctx: dict) -> dict:
    # a machine nobody ever knocked: 14 all-zero days, honestly named
    r = c.post("/processes", json={
        "name": "Smoke quiet machine",
        "definition": {"states": ["a", "done"], "initial": "a",
                       "transitions": [
                           {"name": "finish", "from": "a", "to": "done"}]}})
    assert r.status_code == 201, r.text
    a = c.get(f"/processes/{r.json()['id']}/analytics").json()
    hist = a["escalation_history"]
    assert hist["window_days"] == 14 and len(hist["days"]) == 14
    assert all(d["total"] == 0 for d in hist["days"]), hist

    # re-ack the chain node's instance WITHOUT a snooze - the loan is
    # replaced by ownership: snoozed drops, the ack stays
    onb_pid, ob_iid, floor_id = ctx
    r = c.post(f"/processes/{onb_pid}/instances/{ob_iid}/escalations/ack",
               json={"by": "ops-lead", "note": "mine now"})
    assert r.status_code == 200, r.text
    r = c.get(f"/systems/{floor_id}").json()
    ob_node = {n["process"]: n
               for n in r["chains"][0]["nodes"]}["Customer onboarding"]
    assert ob_node["acked"] == 1 and ob_node["snoozed"] == 0, ob_node
    row = ob_node["overdue_instances"][0]
    assert row["escalation"]["acked_by"] == "ops-lead", row
    assert row["escalation"]["snooze_until"] == "", row
    assert row["escalation"]["snooze_active"] is False, row
    return {"quiet_days": len(hist["days"]),
            "after_reack": {"acked": ob_node["acked"],
                            "snoozed": ob_node["snoozed"]}}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v94_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert version == "1.94.0", version

            ctx1 = chain_node_ack_check(c)
            print("[1] CHAIN NODE ACK OK - two real operator installs on one "
                  "floor; the lead walked to won and the leg opened the case "
                  "itself; a 1s-SLA case went past it and the REAL door swept "
                  "it through the machine's own escalate self-loop; the human "
                  "took it from the node's row (the same ack endpoint, 4h "
                  "loan) - the drawn node now wears the book: overdue 1, "
                  f"acked 1, snoozed 1, the instance named with ops's receipt")
            print(f"    node book: {ctx1['node']} - acked_by "
                  f"{ctx1['ack_row']}")

            spark = sparkline_check(c, sink)
            print("[2] SPARKLINE OK - the knock crossed the actual SMTP wire "
                  "(the sink holds it, subject scan-line named) and the ack "
                  "joined the same day's ledger; the machine's analytics "
                  "carry escalation_history: 14 named day buckets, TODAY "
                  "counting the door's knock and the human's receipt, every "
                  "other day honestly zero")
            print(f"    today: {spark['today']} - window "
                  f"{spark['window_days']}d, sink messages {spark['sink_messages']}")

            quiet = quiet_and_loan_check(c, ctx1["ctx"])
            print("[3] QUIET + LOAN OK - a machine nobody knocked shows 14 "
                  "all-zero days (a quiet stretch reads as data, not "
                  "absence); re-acking without a snooze replaced the loan "
                  "with ownership - snoozed dropped to 0, the ack stayed, "
                  "the snooze_until stamp is gone")
            print(f"    after re-ack: {quiet['after_reack']} - "
                  f"{quiet['quiet_days']} quiet days on the fresh machine")

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
