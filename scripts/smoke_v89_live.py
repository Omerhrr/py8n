"""V89 live smoke: boot the real server and drive the snooze, the digest,
and the cross-operator handoff.

1. SNOOZE ON ACKS: the real door knocks, a named human takes the episode
   ON A LOAN (snooze_hours=1) - the receipt carries snooze_until, the
   door HOLDS with the loan visible (reason=acknowledged +
   snooze_remaining_seconds).
2. ESCALATION DIGEST: the policy rides mode="digest" with a 60s window -
   stuck items become CANDIDATES (one business.stuck fact each, no
   knocks), and when the window elapses ONE business.escalation_digest
   event + one summary covers them all (honestly skipped until an
   endpoint is bound - the skip IS the result).
3. CROSS-OPERATOR JOURNEY: the Operations + Sales operators install; a
   fresh lead walks the pipeline ... and WINS - the Customer onboarding
   case OPENS ITSELF at kickoff (ref = the lead's phone), linked in the
   opened context, business.journey_opened on the deal's thread.

Usage: /home/z/.venv/bin/python scripts/smoke_v89_live.py
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
API = "http://127.0.0.1:8218/api/v1"
SERVER_PORT = 8218

MACHINE = {
    "states": ["a", "b", "done", "dead"],
    "initial": "a",
    "transitions": [
        {"name": "go", "from": "a", "to": "b"},
        {"name": "finish", "from": "b", "to": "done"},
        {"name": "kill", "from": "a", "to": "dead"},
    ],
}


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
# 1) snooze on acks - the hold is a loan
# ---------------------------------------------------------------------------

def snooze_check(c: httpx.Client) -> dict:
    r = c.post("/processes", json={
        "name": "Loan machine",
        "definition": {**MACHINE, "escalation_policy": {
            "channel": "email", "to": "ops@py8n.test",
            "repeat_every_seconds": 60, "max_repeats": 5}}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = c.post(f"/processes/{pid}/instances",
               json={"ref": "SNZ-1", "title": "the loaned one",
                     "due_in_seconds": 1})
    iid = r.json()["id"]

    import time as _t
    _t.sleep(1.2)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    mine = [e for e in report["recorded"] if e["instance_id"] == iid]
    assert len(mine) == 1 and mine[0]["attempt"] == 1, report

    # the named take ON A LOAN - snooze 1 hour
    r = c.post(f"/processes/{pid}/instances/{iid}/escalations/ack",
               json={"by": "amara", "note": "on it", "snooze_hours": 1})
    assert r.status_code == 200, r.text
    ack = r.json()["ack"]
    assert ack["snooze_hours"] == 1.0 and ack["snooze_until"], ack
    assert any(j["transition"] == "escalation_acknowledged"
               for j in r.json()["instance"]["journey"]), ack

    # the door HOLDS - the loan is visible in its held list
    report2 = c.post("/scheduler/escalations/tick", json={}).json()
    held = [x for x in report2["held"] if x["instance_id"] == iid]
    assert held and held[0]["reason"] == "acknowledged", report2
    assert held[0]["snooze_until"] == ack["snooze_until"], report2
    assert held[0]["snooze_remaining_seconds"] > 0, report2
    return {"instance_id": iid, "snooze_until": ack["snooze_until"]}


# ---------------------------------------------------------------------------
# 2) the escalation digest - one summary per window
# ---------------------------------------------------------------------------

def digest_check(c: httpx.Client) -> dict:
    r = c.post("/processes", json={
        "name": "Digest machine",
        "definition": {**MACHINE, "escalation_policy": {
            "channel": "email", "to": "billing@py8n.test",
            "mode": "digest", "digest_every_seconds": 60,
            "max_repeats": 1}}})
    assert r.status_code == 201, r.text
    assert "digest over email" in r.json()["escalation_summary"], r.text
    pid = r.json()["id"]
    for ref in ("INV-1", "INV-2"):
        r = c.post(f"/processes/{pid}/instances",
                   json={"ref": ref, "title": f"invoice {ref}",
                         "due_in_seconds": 1})
        assert r.status_code == 201, r.text

    import time as _t
    _t.sleep(1.2)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    assert not report["escalated"] and not report["recorded"], report
    assert report["digest"]["sent"] == [] and \
        report["digest"]["pending"][0]["items"] == 2, report
    stuck = c.get("/events", params={"type": "business.stuck"}).json()["events"]
    assert len([e for e in stuck if e["payload"].get("mode") == "digest"]) == 2, stuck

    # the window elapses (60s) - ONE summary covers BOTH items
    print("    ... walking one digest window (60s) on the real clock")
    _t.sleep(61)
    report = c.post("/scheduler/escalations/tick", json={}).json()
    sent = report["digest"]["sent"]
    assert len(sent) == 1 and sent[0]["items"] == 2, report
    assert sent[0]["delivery"] == "skipped", report  # no endpoint bound - honest
    evs = c.get("/events", params={"type": "business.escalation_digest"}
                ).json()["events"]
    assert len(evs) == 1 and evs[0]["payload"]["item_count"] == 2, evs
    assert {it["ref"] for it in evs[0]["payload"]["items"]} == {"INV-1", "INV-2"}, evs
    return {"items": 2, "delivery": sent[0]["delivery"]}


# ---------------------------------------------------------------------------
# 3) the cross-operator journey - a won deal opening an onboarding case
# ---------------------------------------------------------------------------

def journey_check(c: httpx.Client) -> dict:
    r = c.post("/operators/operations-operator/install", json={"note": "smoke v89"})
    assert r.status_code == 200, r.text
    built_ops = r.json()
    onb = next(p for p in built_ops["processes"]
               if p["name"] == "Customer onboarding")
    assert onb["seeded_instances"] == 2, onb

    r = c.post("/operators/sales-operator/install", json={"note": "smoke v89"})
    assert r.status_code == 200, r.text
    lead = next(p for p in r.json()["processes"] if p["name"] == "Lead pipeline")
    full = c.get(f"/processes/{lead['id']}").json()
    assert full["journeys"] and full["journeys"][0]["open"]["process"] == \
        "Customer onboarding", full["journeys"]

    r = c.post(f"/processes/{lead['id']}/instances",
               json={"ref": "+15558880444", "title": "Smoke Deal",
                     "context": {"company": "Smoke Co"}})
    iid = r.json()["id"]
    for move in ("reach_out", "qualify", "book_demo", "run_demo",
                 "send_proposal", "negotiate", "win"):
        r = c.post(f"/processes/{lead['id']}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke"})
        assert r.status_code == 200, r.text
    opened = r.json().get("journeys_opened")
    assert opened and opened[0]["opened"] is True, r.text
    assert opened[0]["target"]["process_name"] == "Customer onboarding", opened

    rows = c.get(f"/processes/{onb['id']}/instances").json()["instances"]
    case = next(x for x in rows if x["ref"] == "+15558880444")
    assert case["state"] == "kickoff", case
    assert case["title"] == "Onboarding - Smoke Deal", case
    assert case["context"]["journey"]["from_process"] == "Lead pipeline", case
    evs = c.get("/events", params={"type": "business.journey_opened",
                                   "correlation_id": iid}).json()["events"]
    assert evs and evs[0]["payload"]["target"]["ref"] == "+15558880444", evs
    return {"case_state": case["state"], "case_id": case["id"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v89_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PORT": str(SERVER_PORT),
    })
    proc = subprocess.Popen(
        ["/home/z/.venv/bin/python", SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.89.0", version

            snooze_check(c)
            print(f"[1] SNOOZE OK - the real door knocked, a named human took "
                  f"the episode ON A LOAN (snooze_hours=1): the receipt "
                  f"carries snooze_until, and the door HOLDS with the loan "
                  f"visible (reason=acknowledged + snooze_remaining_seconds) - "
                  f"when the hour runs out the door re-knocks (the unit "
                  f"clock walks that path)")

            digest_check(c)
            print(f"[2] DIGEST OK - mode=digest with a 60s window on the REAL "
                  f"door: stuck items became candidates (one business.stuck "
                  f"fact each, ZERO knocks), and when the window elapsed ONE "
                  f"business.escalation_digest event + one summary covered "
                  f"both items (honestly skipped until an endpoint is bound) "
                  f"- one summary per window instead of N knocks")

            journey_check(c)
            print(f"[3] CROSS-OPERATOR JOURNEY OK - the Operations + Sales "
                  f"operators installed; a fresh lead walked the pipeline "
                  f"and WON, and the Customer onboarding case OPENED ITSELF "
                  f"at kickoff (ref = the lead's phone, title rendered, the "
                  f"journey link in the opened context, business."
                  f"journey_opened on the deal's correlation thread)")

            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
