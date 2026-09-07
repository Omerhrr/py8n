"""V92 live smoke: boot the real server and walk the three operator-chair
loops through the real API on the real clock.

1. THE DIFF ON POLICY SAVE: the knock fires attempt 1; the board PATCHes
   the cadence 3600s -> 60s and the response CARRIES THE DIFF
   (changed == ['repeat_every_seconds']); the door re-knocks on the real
   clock; the board switches knock -> digest and the diff names the
   rhythm AND both clocks; ONE digest crosses the actual SMTP wire.
2. ACK/SNOOZE STRAIGHT FROM THE ATTENTION ROW: the overdue row lists on
   GET /processes/attention; the receipt posts to the SAME ack endpoint
   addressed by the ROW's own ids; the row stays on the feed wearing the
   ack + the snooze; the door holds (reason=acknowledged).
3. THE CHAINS DRAWN ON OPERATOR DETAIL: GET /operators/sales-operator
   resolves the revenue chain with position 0; finance terminates all
   three chains; the catalog cards name their chains beside the v91 legs.

Usage: /home/z/.venv/bin/python scripts/smoke_v92_live.py
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
# 1) the diff on policy save - every re-tune names what moved
# ---------------------------------------------------------------------------

def policy_diff_check(c: httpx.Client, sink: SmtpDevSink) -> dict:
    r = c.post("/channels/endpoints", json={
        "name": "Smoke v92 inbox", "provider": "email_inbound",
        "config": {"secret": "smoke-secret",
                   "smtp_host": "127.0.0.1", "smtp_port": str(sink.port),
                   "smtp_user": "smoke", "smtp_pass": "smoke",
                   "from_address": "py8n@py8n.test"}})
    assert r.status_code == 201, r.text

    r = c.post("/processes", json={
        "name": "Smoke diff machine",
        "definition": {"states": ["a", "done"], "initial": "a",
                       "transitions": [
                           {"name": "finish", "from": "a", "to": "done"}],
                       "escalation_policy": {
                           "channel": "email", "to": "billing@py8n.test",
                           "repeat_every_seconds": 3600, "max_repeats": 5}}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = c.post(f"/processes/{pid}/instances",
               json={"ref": "DIFF-1", "title": "the stuck one",
                     "due_in_seconds": 1})
    assert r.status_code == 201, r.text

    time.sleep(1.2)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    mine = [e for e in report["recorded"] if e["ref"] == "DIFF-1"]
    assert mine and mine[0]["attempt"] == 1, report
    assert mine[0]["delivery"] == "delivered", report

    # the board re-tunes the cadence: the response carries the DIFF
    r = c.patch(f"/processes/{pid}/escalation-policy",
                json={"policy": {"channel": "email", "to": "billing@py8n.test",
                                 "repeat_every_seconds": 60,
                                 "max_repeats": 5},
                      "actor": "the board"})
    assert r.status_code == 200, r.text
    diff = r.json()["policy_diff"]
    assert diff["before"]["repeat_every_seconds"] == 3600, diff
    assert diff["after"]["repeat_every_seconds"] == 60, diff
    assert diff["changed"] == ["repeat_every_seconds"], diff["changed"]

    print("    ... walking the NEW 60s cadence on the real clock")
    time.sleep(61)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    mine = [e for e in report["recorded"] if e["ref"] == "DIFF-1"]
    assert mine and mine[0]["attempt"] == 2, report
    assert mine[0]["delivery"] == "delivered", report
    assert sink.count == 2, sink.messages

    # knock -> digest, mid-episode: the diff names the rhythm + both clocks
    # (the board form sends the cap too - a field it leaves alone must not
    # show up in the diff)
    r = c.patch(f"/processes/{pid}/escalation-policy",
                json={"policy": {"channel": "email", "to": "billing@py8n.test",
                                 "mode": "digest",
                                 "digest_every_seconds": 60,
                                 "max_repeats": 5},
                      "actor": "the board"})
    assert r.status_code == 200, r.text
    diff = r.json()["policy_diff"]
    assert diff["before"]["mode"] == "knock", diff
    assert diff["after"]["mode"] == "digest", diff
    assert diff["changed"] == ["digest_every_seconds", "mode",
                               "repeat_every_seconds"], diff["changed"]
    report = c.post("/scheduler/escalations/tick", json={}).json()
    assert report["digest"]["sent"] == [] and \
        len(report["digest"]["pending"]) == 1, report

    print("    ... walking the digest window (60s) on the real clock")
    time.sleep(61)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    sent = report["digest"]["sent"]
    assert len(sent) == 1 and sent[0]["items"] == 1, report
    assert sent[0]["delivery"] == "delivered", report
    msg = sink.last()
    assert "DIFF-1" in msg["text"], msg
    assert msg["subject"] == ("[py8n] Escalation digest - Smoke diff "
                              "machine (1 item(s) past SLA)"), msg
    return {"diffs": 2, "digest": sent[0]["delivery"]}


# ---------------------------------------------------------------------------
# 2) ack/snooze straight from the attention row
# ---------------------------------------------------------------------------

def attention_ack_check(c: httpx.Client) -> dict:
    r = c.post("/processes", json={
        "name": "Smoke row machine",
        "definition": {"states": ["a", "done"], "initial": "a",
                       "transitions": [
                           {"name": "finish", "from": "a", "to": "done"}],
                       "escalation_policy": {
                           "channel": "email", "to": "row@py8n.test",
                           "repeat_every_seconds": 3600, "max_repeats": 5}}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = c.post(f"/processes/{pid}/instances",
               json={"ref": "ROW-9", "title": "the row's own receipt",
                     "due_in_seconds": 1})
    assert r.status_code == 201, r.text

    time.sleep(1.2)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    assert any(e["ref"] == "ROW-9" for e in report["recorded"]), report

    feed = c.get("/processes/attention").json()
    row = next(x for x in feed["attention"] if x["ref"] == "ROW-9")
    assert row["process_id"] == pid
    assert row["escalation"]["acked_by"] == "", row["escalation"]

    # the receipt straight from the row - the row's own ids address it
    r = c.post(f"/processes/{row['process_id']}/instances/"
               f"{row['instance_id']}/escalations/ack",
               json={"by": "dana", "note": "on it from the feed",
                     "snooze_hours": 1})
    assert r.status_code == 200, r.text

    feed = c.get("/processes/attention").json()
    row2 = next(x for x in feed["attention"] if x["ref"] == "ROW-9")
    assert row2["escalation"]["acked_by"] == "dana", row2["escalation"]
    assert row2["escalation"]["snooze_until"], row2["escalation"]

    # the door holds while the loan runs
    report = c.post("/scheduler/escalations/tick", json={}).json()
    assert all(e["ref"] != "ROW-9" for e in report["recorded"]), report
    held = [x for x in report["held"] if x["ref"] == "ROW-9"]
    assert held and held[0]["reason"] == "acknowledged", report
    return {"acked_from_row": True, "held": held[0]["reason"]}


# ---------------------------------------------------------------------------
# 3) the chains drawn on operator detail
# ---------------------------------------------------------------------------

def chains_check(c: httpx.Client) -> dict:
    r = c.get("/operators/sales-operator")
    assert r.status_code == 200, r.text
    detail = r.json()
    chains = detail["chains"]
    assert [x["slug"] for x in chains] == ["revenue"], chains
    rev = chains[0]
    assert rev["position"] == 0, rev
    assert [o["slug"] for o in rev["operators"]] == [
        "sales-operator", "operations-operator", "finance-operator"], rev
    assert [(l["from_process"], l["on_state"], l["opens"]) for l in rev["legs"]] == [
        ("Lead pipeline", "won", "Customer onboarding"),
        ("Customer onboarding", "handed_off", "Invoice lifecycle")], rev
    assert rev["legs"][1]["due_in_seconds"] == 5 * 24 * 3600

    r = c.get("/operators/finance-operator")
    fin = {x["slug"]: x for x in r.json()["chains"]}
    assert set(fin) == {"revenue", "supply", "care"}, fin
    assert [fin["revenue"]["position"], fin["supply"]["position"],
            fin["care"]["position"]] == [2, 2, 1], fin

    r = c.get("/operators/meeting-operator")
    assert r.json()["chains"] == []

    ops = {o["slug"]: o for o in c.get("/operators").json()["operators"]}
    assert ops["finance-operator"]["chains"] == ["revenue", "supply", "care"]
    assert ops["sales-operator"]["chains"] == ["revenue"]
    total = sum(len(o["journeys"]) for o in ops.values())
    assert total == 10, total
    return {"chains_on_sales": 1, "chains_on_finance": 3, "shelf_legs": total}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v92_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert version == "1.92.0", version

            policy_diff_check(c, sink)
            print(f"[1] POLICY DIFF OK - attempt 1 delivered, the board "
                  f"re-tuned the cadence and the response carried the diff "
                  f"(changed == ['repeat_every_seconds']); the door "
                  f"re-knocked on the real clock (attempt 2 over the actual "
                  f"SMTP wire); the knock->digest switch named the rhythm "
                  f"AND both clocks, and ONE summary crossed the wire with "
                  f"the scan-line subject")

            attention_ack_check(c)
            print(f"[2] ROW RECEIPT OK - the overdue row listed on the "
                  f"attention feed, the ack+snooze posted straight from the "
                  f"row's own ids, the row stayed on the feed wearing the "
                  f"ack + the snooze, and the door held "
                  f"(reason=acknowledged) while the loan ran")

            chains_check(c)
            print(f"[3] CHAIN DATA OK - /operators/sales-operator resolves "
                  f"the revenue chain with position 0 and the leg SLAs, "
                  f"finance terminates all three chains (positions 2/2/1), "
                  f"meeting stands alone, and the shelf cards name their "
                  f"chains beside the 10 v91 legs")

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
