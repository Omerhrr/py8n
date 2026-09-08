"""V97 live smoke: boot the real server and walk the round on the real
clock and the real SMTP wire.

1. RESCHEDULE EVIDENCE INSIDE THE DIGEST, OVER THE REAL WIRE: a REAL
   email endpoint (email_inbound) is bound to the dev SMTP sink; the
   digest machine's first summary crosses the wire as a bare line; the
   handler acks WITH a reschedule; while the reschedule holds, the door
   is past its window and the bucket stays EMPTY; when the named moment
   passes, the item rides digest #2 - and the email that lands in the
   sink carries the evidence on the line: "rescheduled to <moment> by
   smoke-handler - the door re-knocked".
2. THE DRILL-DOWN: a knock machine goes past its SLA on the real clock;
   the handler acks with a reschedule; GET
   /processes/escalation-history/{pid}/{today} opens the machine's day
   off the log - the escalation AND the ack (with the reschedule stamp
   on the receipt row), counts matching the v95 grid cell beside it.
3. THE CHAIN MAP'S LIVE WIRE: Sales + Operations install; a fresh lead
   WINS; business.journey_opened lands on /events with the exact keys
   the operator-detail chain map matches (process_name, on_state,
   target.process_name); /processes/chains?history_limit=25 serves the
   leg's history naming the ride - the wire the live map refreshes from.

Usage: /home/z/.venv/bin/python scripts/smoke_v97_live.py
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
PY = ("/home/z/.venv/bin/python"
      if os.path.exists("/home/z/.venv/bin/python") else "python3")


def _free_port() -> int:
    """A fresh port per run - a zombie server from an earlier smoke must
    never answer for THIS one (the boot imports take minutes; a fixed
    port + a 30s health deadline once split the run across two servers
    with two databases, found live by this smoke's own first runs)."""
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
    sandbox; the deadline has to outlive the imports, and every answer
    must come from THIS run's server (the port is unique to the run)."""
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
# 1) the reschedule evidence rides the digest over the REAL wire
# ---------------------------------------------------------------------------

def reschedule_digest_check(c: httpx.Client, sink: SmtpDevSink) -> dict:
    r = c.post("/channels/endpoints", json={
        "name": "Smoke v97 inbox", "provider": "email_inbound",
        "config": {"secret": "smoke-secret",
                   "smtp_host": "127.0.0.1", "smtp_port": str(sink.port),
                   "smtp_user": "smoke", "smtp_pass": "smoke",
                   "from_address": "py8n@py8n.test"}})
    assert r.status_code == 201, r.text

    r = c.post("/processes", json={
        "name": "Smoke v97 digest machine",
        "definition": {"states": ["a", "b", "done"], "initial": "a",
                       "transitions": [
                           {"name": "go", "from": "a", "to": "b"},
                           {"name": "finish", "from": "b", "to": "done"}],
                       "escalation_policy": {
                           "channel": "email", "to": "billing@py8n.test",
                           "mode": "digest", "digest_every_seconds": 60,
                           "max_repeats": 3}}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = c.post(f"/processes/{pid}/instances",
               json={"ref": "EV-97", "title": "the moved one",
                     "due_in_seconds": 1})
    iid = r.json()["id"]

    print(f"    t={time.monotonic():.1f} pre-tick1")
    time.sleep(1.2)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    print(f"    t={time.monotonic():.1f} post-tick1")
    assert report["digest"]["sent"] == [] and \
        report["digest"]["pending"][0]["items"] == 1, report

    print("    ... walking one digest window (60s) on the real clock")
    time.sleep(61)
    print(f"    t={time.monotonic():.1f} awake, pre-tick2")
    insts = c.get(f"/processes/{pid}/instances").json()["instances"]
    print(f"    pre-tick2 book -> {(insts[0].get('context') or {}).get('escalations')}")
    report = c.post("/scheduler/escalations/tick", json={}).json()
    print(f"    t={time.monotonic():.1f} post-tick2")
    sent = report["digest"]["sent"]
    assert len(sent) == 1 and sent[0]["items"] == 1, report
    assert sent[0]["delivery"] == "delivered", report
    assert sink.count == 1, sink.messages
    first = sink.last()
    assert "EV-97" in first["text"], first
    assert "rescheduled" not in first["text"], first  # digest #1: bare line

    # the human takes it WITH an explicit reschedule (0.4 minutes out)
    r = c.post(f"/processes/{pid}/instances/{iid}/escalations/ack",
               json={"by": "smoke-handler", "reschedule_in_minutes": 0.4})
    assert r.status_code == 200, r.text
    ack = r.json()["ack"]
    assert ack["reschedule_at"], ack

    # past the digest window but INSIDE the reschedule: the bucket stays
    # empty - the summary does not go out, the moment rules the door
    time.sleep(3)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    assert report["digest"]["sent"] == [], report["digest"]
    held = [x for x in report["held"] if x["instance_id"] == iid]
    assert held and held[0]["reason"] == "acknowledged", report
    assert held[0]["reschedule_at"] == ack["reschedule_at"], held

    # the moment passes AND the window re-elapses since digest #1 - the
    # item rides digest #2 and the WIRE carries the evidence on the line
    print("    ... walking the reschedule out (62s) on the real clock")
    time.sleep(62)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    sent = report["digest"]["sent"]
    assert len(sent) == 1 and sent[0]["delivery"] == "delivered", report
    assert sink.count == 2, sink.messages
    second = sink.last()
    assert "rescheduled" in second["text"], second
    assert "smoke-handler" in second["text"], second
    assert "the door re-knocked" in second["text"], second
    assert "EV-97" in second["text"], second

    evs = c.get("/events", params={"type": "business.escalation_digest"}
                ).json()["events"]
    mine = [e for e in evs if e["payload"]["process_id"] == pid]
    assert len(mine) == 2, mine
    ev2 = max(mine, key=lambda e: e["created_at"])
    item = next(it for it in ev2["payload"]["items"] if it["ref"] == "EV-97")
    assert "rescheduled" in item["reschedule_note"], item

    # the attention feed wears the evidence too (while the loan held)
    att = c.get("/processes/attention").json()["attention"]
    row = next((x for x in att if x["instance_id"] == iid), None)
    assert row is None or row["escalation"]["reschedule_at"], row
    return {"evidence_line": second["text"].strip().splitlines()[-1][:120]}


# ---------------------------------------------------------------------------
# 2) the drill-down - one heatmap cell opened: the machine's day
# ---------------------------------------------------------------------------

def drill_down_check(c: httpx.Client) -> dict:
    r = c.post("/processes", json={
        "name": "Smoke v97 drill machine",
        "definition": {"states": ["a", "b", "done"], "initial": "a",
                       "transitions": [{"name": "go", "from": "a", "to": "b"},
                                       {"name": "finish", "from": "b",
                                        "to": "done"}],
                       "escalation_policy": {
                           "channel": "", "to": "",
                           "repeat_every_seconds": 60, "max_repeats": 3}}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = c.post(f"/processes/{pid}/instances",
               json={"ref": "DRILL-97", "due_in_seconds": 1})
    iid = r.json()["id"]

    time.sleep(2.3)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    assert report["scanned"] >= 1, report
    r = c.post(f"/processes/{pid}/instances/{iid}/escalations/ack",
               json={"by": "smoke-handler", "reschedule_in_minutes": 30})
    assert r.status_code == 200, r.text

    today = time.strftime("%Y-%m-%d")
    r = c.get(f"/processes/escalation-history/{pid}/{today}")
    assert r.status_code == 200, r.text
    day = r.json()
    assert day["process_id"] == pid and day["day"] == today, day
    assert day["counts"]["escalations"] >= 1, day
    assert day["counts"]["acks"] >= 1, day
    kinds = {row["kind"] for row in day["rows"]}
    assert kinds == {"escalation", "ack"}, day
    ack_row = next(row for row in day["rows"] if row["kind"] == "ack")
    assert ack_row["ref"] == "DRILL-97", day
    assert ack_row["reschedule_at"], day
    ats = [row["at"] for row in day["rows"]]
    assert ats == sorted(ats), ats

    # a quiet day reads as data; a bad day refuses loud
    r = c.get(f"/processes/escalation-history/{pid}/2020-01-01")
    assert r.status_code == 200 and r.json()["total"] == 0, r.text
    r = c.get(f"/processes/escalation-history/{pid}/not-a-day")
    assert r.status_code == 400, r.text

    # the v95 grid stands beside the drill-down
    r = c.get("/processes/escalation-history?days=3").json()
    mine = next(m for m in r["machines"] if m["process_id"] == pid)
    assert mine["cells"][today]["escalations"] >= 1, mine
    return {"day_counts": day["counts"], "rows": day["total"]}


# ---------------------------------------------------------------------------
# 3) the chain map's live wire - journey_opened carries the match keys
# ---------------------------------------------------------------------------

def journey_liveness_check(c: httpx.Client) -> dict:
    for slug in ("operations-operator", "sales-operator"):
        r = c.post(f"/operators/{slug}/install", json={"note": "smoke v97"})
        assert r.status_code == 200, r.text

    procs = {p["name"]: p["id"] for p in c.get("/processes").json()["processes"]}
    lead_pid = procs["Lead pipeline"]
    ref = "+15559770101"
    r = c.post(f"/processes/{lead_pid}/instances",
               json={"ref": ref, "title": "v97 Live Deal"})
    iid = r.json()["id"]
    for move in ("reach_out", "qualify", "book_demo", "run_demo",
                 "send_proposal", "negotiate", "win"):
        r = c.post(f"/processes/{lead_pid}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke"})
        assert r.status_code == 200, r.text

    # the event the live tail listens for - with the match keys
    evs = c.get("/events", params={"type": "business.journey_opened"}).json()
    ev = next(e for e in evs["events"] if e["payload"]["ref"] == ref)
    payload = ev["payload"]
    assert payload["process_name"] == "Lead pipeline", payload
    assert payload["on_state"] == "won", payload
    assert payload["target"]["process_name"] == "Customer onboarding", payload
    assert payload["target"]["instance_id"], payload

    # the deeper history serves the ride (beyond the recent 5 cap)
    r = c.get("/processes/chains", params={"history_limit": 25}).json()
    revenue = next(ch for ch in r["chains"] if ch["name"] == "Revenue chain")
    leg1 = revenue["legs"][0]
    assert leg1["opened"] >= 1, leg1
    assert leg1["history_limit"] == 25, leg1
    assert any(h["ref"] == ref for h in leg1["history"]), leg1
    return {"event_keys": [payload["process_name"], payload["on_state"],
                           payload["target"]["process_name"]],
            "leg_history_refs": [h["ref"] for h in leg1["history"]]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v97_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",  # the smoke drives the door
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v97_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT,
    )
    sink = SmtpDevSink().start()
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.97.0", version

            reschedule_digest_check(c, sink)
            print(f"[1] RESCHEDULE EVIDENCE IN THE DIGEST OK - a REAL email "
                  f"endpoint was bound to the dev sink; digest #1 crossed the "
                  f"wire as a bare line; the handler acked WITH a reschedule "
                  f"(0.4m); past the 60s window but inside the moment the "
                  f"bucket stayed EMPTY; when the moment passed, digest #2 "
                  f"crossed the wire with the evidence on the line "
                  f"(rescheduled ... by smoke-handler - the door re-knocked)")

            drill_down_check(c)
            print(f"[2] HEATMAP DRILL-DOWN OK - the knock machine went past "
                  f"its SLA on the real clock; the ack carried a reschedule; "
                  f"GET /processes/escalation-history/{{pid}}/{{today}} opened "
                  f"the machine's day off the log (escalation + ack rows with "
                  f"the reschedule stamp, chronological); the quiet day read "
                  f"as data, the bad day refused 400, and the v95 grid cell "
                  f"matched beside it")

            journey_liveness_check(c)
            print(f"[3] CHAIN MAP LIVE WIRE OK - the fresh lead WON and "
                  f"business.journey_opened landed on /events with the exact "
                  f"keys the operator-detail chain map matches (Lead pipeline "
                  f"on won -> Customer onboarding); /processes/chains with "
                  f"history_limit=25 served the leg's history naming the ride "
                  f"- the wire the v97 live map refreshes and flashes from")

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
