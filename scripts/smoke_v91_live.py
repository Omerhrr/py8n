"""V91 live smoke: boot the real server and walk the three board-side
features through the real API on the real clock.

1. THE OVERDUE-ATTENTION VIEW: two overdue instances across two machines
   (one born from a journey hand-off) - GET /processes/attention lists
   both, most overdue first; advancing one to a terminal state drops it
   from the feed (a closed entity is not asking for attention).
2. POLICY EDITING FROM THE BOARD: an email endpoint is bound at the dev
   SMTP sink's port; the knock fires attempt 1; the board PATCHes the
   cadence 3600s -> 60s and the door RE-KNOCKS on the real clock (the
   old cadence would hold); the board switches knock -> digest mid-
   episode, the door anchors the summary window and ONE digest crosses
   the actual wire. The re-tune rules the NEXT tick - every time.
3. THE CHAINS ON THE SHELF: GET /operators carries the journey legs per
   operator card - sales hands off (out), finance is fed by three
   departments (in), ten legs across the shelf.

Usage: /home/z/.venv/bin/python scripts/smoke_v91_live.py
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
# 1) the attention view - every machine, one feed
# ---------------------------------------------------------------------------

def attention_check(c: httpx.Client) -> dict:
    r = c.post("/processes", json={
        "name": "Smoke attention A",
        "definition": {"states": ["a", "b", "done"], "initial": "a",
                       "transitions": [
                           {"name": "go", "from": "a", "to": "b"},
                           {"name": "finish", "from": "b", "to": "done"}]}})
    assert r.status_code == 201, r.text
    pa = r.json()["id"]
    r = c.post("/processes", json={
        "name": "Smoke attention B",
        "definition": {"states": ["new", "done"], "initial": "new",
                       "transitions": [
                           {"name": "close", "from": "new", "to": "done"}]}})
    assert r.status_code == 201, r.text
    pb = r.json()["id"]

    r = c.post(f"/processes/{pa}/instances",
               json={"ref": "ATT-1", "title": "the overdue one",
                     "due_in_seconds": 1})
    assert r.status_code == 201, r.text
    r = c.post(f"/processes/{pb}/instances",
               json={"ref": "ATT-2", "title": "the hand-off",
                     "due_in_seconds": 1,
                     "context": {"journey": {"from_process": "Lead pipeline",
                                             "from_state": "won"}}})
    assert r.status_code == 201, r.text

    time.sleep(1.2)
    feed = c.get("/processes/attention").json()
    assert feed["count"] == 2 and feed["machines"] == 2, feed
    assert [x["ref"] for x in feed["attention"]] == ["ATT-1", "ATT-2"], feed
    assert feed["attention"][1]["journey_leg"] is True, feed

    # a closed entity is not asking for attention
    insts = c.get(f"/processes/{pa}/instances").json()["instances"]
    iid = next(x["id"] for x in insts if x["ref"] == "ATT-1")
    r = c.post(f"/processes/{pa}/instances/{iid}/advance",
               json={"transition": "go"})
    assert r.status_code == 200, r.text
    r = c.post(f"/processes/{pa}/instances/{iid}/advance",
               json={"transition": "finish"})
    assert r.status_code == 200, r.text
    feed = c.get("/processes/attention").json()
    assert [x["ref"] for x in feed["attention"]] == ["ATT-2"], feed
    return {"refs_seen": 2, "after_close": 1}


# ---------------------------------------------------------------------------
# 2) the policy editor - the re-tune rules the next tick, on the real clock
# ---------------------------------------------------------------------------

def policy_edit_check(c: httpx.Client, sink: SmtpDevSink) -> dict:
    r = c.post("/channels/endpoints", json={
        "name": "Smoke v91 inbox", "provider": "email_inbound",
        "config": {"secret": "smoke-secret",
                   "smtp_host": "127.0.0.1", "smtp_port": str(sink.port),
                   "smtp_user": "smoke", "smtp_pass": "smoke",
                   "from_address": "py8n@py8n.test"}})
    assert r.status_code == 201, r.text

    r = c.post("/processes", json={
        "name": "Smoke re-tuned machine",
        "definition": {"states": ["a", "done"], "initial": "a",
                       "transitions": [
                           {"name": "finish", "from": "a", "to": "done"}],
                       "escalation_policy": {
                           "channel": "email", "to": "billing@py8n.test",
                           "repeat_every_seconds": 3600, "max_repeats": 5}}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = c.post(f"/processes/{pid}/instances",
               json={"ref": "POL-1", "title": "the stuck one",
                     "due_in_seconds": 1})
    assert r.status_code == 201, r.text

    time.sleep(1.2)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    mine = [e for e in report["recorded"] if e["ref"] == "POL-1"]
    assert mine and mine[0]["attempt"] == 1, report
    assert mine[0]["delivery"] == "delivered", report

    # the board re-tunes the cadence: 3600s -> 60s
    r = c.patch(f"/processes/{pid}/escalation-policy",
                json={"policy": {"channel": "email", "to": "billing@py8n.test",
                                 "repeat_every_seconds": 60,
                                 "max_repeats": 5},
                      "actor": "the board"})
    assert r.status_code == 200, r.text
    assert r.json()["escalation_policy"]["repeat_every_seconds"] == 60
    evs = c.get("/events", params={"type": "business.policy_updated"}).json()["events"]
    assert evs and evs[0]["payload"]["process_id"] == pid, evs

    print("    ... walking the NEW 60s cadence on the real clock")
    time.sleep(61)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    mine = [e for e in report["recorded"] if e["ref"] == "POL-1"]
    assert mine and mine[0]["attempt"] == 2, report
    assert mine[0]["delivery"] == "delivered", report
    assert sink.count == 2, sink.messages

    # knock -> digest, mid-episode: the window anchors, ONE summary crosses
    r = c.patch(f"/processes/{pid}/escalation-policy",
                json={"policy": {"channel": "email", "to": "billing@py8n.test",
                                 "mode": "digest",
                                 "digest_every_seconds": 60},
                      "actor": "the board"})
    assert r.status_code == 200, r.text
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
    assert "POL-1" in msg["text"], msg
    assert msg["subject"] == ("[py8n] Escalation digest - Smoke re-tuned "
                              "machine (1 item(s) past SLA)"), msg
    return {"attempts": 2, "digest": sent[0]["delivery"]}


# ---------------------------------------------------------------------------
# 3) the chains on the shelf - journey legs on the operator cards
# ---------------------------------------------------------------------------

def shelf_check(c: httpx.Client) -> dict:
    ops = {o["slug"]: o for o in c.get("/operators").json()["operators"]}
    sales = ops["sales-operator"]["journeys"]
    assert any(j["direction"] == "out" and j["from_process"] == "Lead pipeline"
               and j["opens"] == "Customer onboarding" for j in sales), sales
    fin = ops["finance-operator"]["journeys"]
    assert len(fin) == 3 and \
        all(j["direction"] == "in" for j in fin), fin
    total = sum(len(o["journeys"]) for o in ops.values())
    assert total == 10, total  # 5 chains, each visible out + in
    return {"legs": total}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v91_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert version == "1.91.0", version

            attention_check(c)
            print(f"[1] ATTENTION VIEW OK - two overdue instances across two "
                  f"machines listed most-overdue first (the hand-off flagged "
                  f"as a journey leg); closing one dropped it from the feed "
                  f"- a closed entity is not asking for attention")

            policy_edit_check(c, sink)
            print(f"[2] POLICY EDIT OK - attempt 1 delivered, the board "
                  f"re-tuned the cadence 3600s -> 60s and the door re-knocked "
                  f"on the real clock (attempt 2 over the actual SMTP wire), "
                  f"then knock -> digest mid-episode: the window anchored, "
                  f"and ONE summary crossed the wire with the scan-line "
                  f"subject. The re-tune ruled the NEXT tick - every time")

            shelf_check(c)
            print(f"[3] SHELF LEGS OK - the operator cards carry the chains: "
                  f"sales hands off (out), finance is fed by three "
                  f"departments (in), 10 legs across the shelf - the "
                  f"cross-department journeys are visible BEFORE the install")

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
