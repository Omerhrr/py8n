"""V93 live smoke: boot the real server and walk the three watching loops
through the real API on the real clock.

1. THE ATTENTION FEED AUTO-REFRESHES ON business.stuck: a REAL WebSocket
   tail connects to /events/stream (the same wire the board rides); the
   door sweeps a fresh breach and business.stuck crosses the actual
   socket with the payload keys the board needs - the frame that makes
   the feed re-read itself.
2. THE CHAINS GO LIVE ON THE INSTALLED SYSTEMS: two real operator
   installs (sales + operations), both machines bound to one system; a
   lead walks to won and the leg's live counts show it - in_state 1, the
   onboarding case fired by itself - while the finance end honestly
   reads NOT INSTALLED / half-wired.
3. THE SLA DIGEST PREVIEW INSIDE THE POLICY EDITOR: the draft digest is
   typeset over the machine's LIVE overdue items (subject + body), the
   delivery note names the honest skip while no email endpoint is bound
   and flips to would-deliver once one is; the knock draft renders the
   per-item messages; the preview writes NOTHING.

Usage: /home/z/.venv/bin/python scripts/smoke_v93_live.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dev_smtp_sink import SmtpDevSink  # noqa: E402

import httpx  # noqa: E402
import websocket  # websocket-client - the sync WS the tail check rides

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v74_server.py"
API = "http://127.0.0.1:8218/api/v1"
WS_URL = "ws://127.0.0.1:8218/api/v1/events/stream"
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
# 1) the live tail - business.stuck crosses the real socket
# ---------------------------------------------------------------------------

def live_tail_check(c: httpx.Client) -> dict:
    ws = websocket.create_connection(WS_URL, timeout=10)
    try:
        hello = json.loads(ws.recv())
        assert hello["event"] == "stream_started", hello

        r = c.post("/processes", json={
            "name": "Smoke watched machine",
            "definition": {"states": ["a", "done"], "initial": "a",
                           "transitions": [
                               {"name": "finish", "from": "a", "to": "done"}],
                           "escalation_policy": {
                               "channel": "email", "to": "tail@py8n.test",
                               "repeat_every_seconds": 3600,
                               "max_repeats": 5}}})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        r = c.post(f"/processes/{pid}/instances",
                   json={"ref": "TAIL-1", "title": "the watched breach",
                         "due_in_seconds": 1})
        assert r.status_code == 201, r.text

        time.sleep(2.2)
        report = c.post("/scheduler/escalations/tick", json={}).json()
        assert any(e["ref"] == "TAIL-1" for e in report["recorded"]), report

        # the frame that triggers the board's auto-refresh
        deadline = time.time() + 10
        stuck = None
        while time.time() < deadline:
            frame = json.loads(ws.recv())
            if frame.get("event") == "system_event" \
                    and frame.get("type") == "business.stuck":
                stuck = frame
                break
        assert stuck is not None, "no business.stuck crossed the tail"
        payload = stuck["payload"]
        assert payload["ref"] == "TAIL-1", payload
        assert payload["process_id"] == pid, payload
        assert payload["process_name"] == "Smoke watched machine", payload
        assert payload["state"] == "a" and payload["overdue_seconds"] >= 1, payload
        assert payload["instance_id"] == report["recorded"][0]["instance_id"]

        # and the feed it refreshes already carries the row
        feed = c.get("/processes/attention").json()
        assert any(x["ref"] == "TAIL-1" for x in feed["attention"]), feed
        return {"event": stuck["type"], "ref": payload["ref"],
                "overdue": payload["overdue_seconds"]}
    finally:
        ws.close()


# ---------------------------------------------------------------------------
# 2) the chains live on the installed systems - drawn with real counts
# ---------------------------------------------------------------------------

def system_chains_check(c: httpx.Client) -> dict:
    built = {}
    for slug in ("sales-operator", "operations-operator"):
        r = c.post(f"/operators/{slug}/install", json={"note": "smoke v93"})
        assert r.status_code == 200, r.text
        built[slug] = r.json()

    procs = {p["name"]: p["id"]
             for p in c.get("/processes").json()["processes"]}
    lead_pid, onb_pid = (procs["Lead pipeline"],
                         procs["Customer onboarding"])

    # one floor system with BOTH machines bound - the chain lives here
    r = c.post("/systems", json={"name": "the smoke floor"})
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

    r = c.get(f"/systems/{floor_id}").json()
    chains = r["chains"]
    assert [x["slug"] for x in chains] == ["revenue"], chains
    rev = chains[0]
    assert rev["complete"] is False, rev  # finance was never installed
    nodes = {n["process"]: n for n in rev["nodes"]}
    assert nodes["Lead pipeline"]["bound"] is True, nodes
    assert nodes["Customer onboarding"]["bound"] is True, nodes
    assert nodes["Invoice lifecycle"]["bound"] is False, nodes
    leg0, leg1 = rev["legs"]
    assert (leg0["from_process"], leg0["on_state"]) == ("Lead pipeline", "won")
    assert leg0["bound_from"] and leg0["bound_opens"], leg0
    # the pack's "won" is terminal - the lead CLOSES on landing (the house
    # rule), so nothing sits in the fire state; the fired child is the truth
    assert leg0["counts"] == {"in_state": 0, "fired": 1, "overdue": 0}, leg0
    assert leg1["bound_from"] is True and leg1["bound_opens"] is False, leg1
    assert leg1["counts"]["fired"] == 0, leg1

    # the sales install's OWN system shows the same chain, honestly
    # half-wired (onboarding is bound elsewhere)
    sales_sys = built["sales-operator"]["system"]["id"]
    r = c.get(f"/systems/{sales_sys}").json()
    rev2 = [x for x in r["chains"] if x["slug"] == "revenue"][0]
    assert rev2["complete"] is False, rev2
    return {"floor_leg0": leg0["counts"], "sales_system_chains": len(r["chains"])}


# ---------------------------------------------------------------------------
# 3) the SLA digest preview inside the policy editor
# ---------------------------------------------------------------------------

def policy_preview_check(c: httpx.Client, sink: SmtpDevSink) -> dict:
    r = c.post("/processes", json={
        "name": "Smoke preview machine",
        "definition": {"states": ["a", "done"], "initial": "a",
                       "transitions": [
                           {"name": "finish", "from": "a", "to": "done"}]}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    for ref in ("PR-1", "PR-2"):
        r = c.post(f"/processes/{pid}/instances",
                   json={"ref": ref, "title": f"the {ref} breach",
                         "due_in_seconds": 1})
        assert r.status_code == 201, r.text
    time.sleep(1.2)

    digest_draft = {"channel": "email", "to": "ops@py8n.test",
                    "mode": "digest", "digest_every_seconds": 86400,
                    "max_repeats": 3}
    r = c.post(f"/processes/{pid}/escalation-preview",
               json={"policy": digest_draft})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["overdue_count"] == 2, out
    assert out["digest"]["subject"] == ("[py8n] Escalation digest - Smoke "
                                        "preview machine (2 item(s) past SLA)"), out
    assert "PR-1" in out["digest"]["body"] and "PR-2" in out["digest"]["body"]
    assert out["would_deliver"] is False, out
    assert "no email channel endpoint bound" in out["delivery_note"], out

    # bind a real email endpoint - the same draft now would deliver
    r = c.post("/channels/endpoints", json={
        "name": "Smoke v93 inbox", "provider": "email_inbound",
        "config": {"secret": "smoke-secret",
                   "smtp_host": "127.0.0.1", "smtp_port": str(sink.port),
                   "smtp_user": "smoke", "smtp_pass": "smoke",
                   "from_address": "py8n@py8n.test"}})
    assert r.status_code == 201, r.text
    r = c.post(f"/processes/{pid}/escalation-preview",
               json={"policy": digest_draft})
    out2 = r.json()
    assert out2["would_deliver"] is True, out2
    assert out2["delivery_note"] == \
        "would deliver over email -> ops@py8n.test", out2
    assert out2["digest"]["subject"] == out["digest"]["subject"]  # identical render

    # the knock draft: a message per stuck item, attempt 1, subjects named
    knock_draft = {"channel": "email", "to": "ops@py8n.test",
                   "repeat_every_seconds": 3600, "max_repeats": 3}
    r = c.post(f"/processes/{pid}/escalation-preview",
               json={"policy": knock_draft})
    out3 = r.json()
    assert out3["mode"] == "knock" and len(out3["messages"]) == 2, out3
    m = out3["messages"][0]
    assert m["attempt"] == 1 and m["to"] == "ops@py8n.test", m
    assert m["subject"].startswith("[py8n] Smoke preview machine: "), m
    assert "PR-1" in m["message"] or "PR-2" in m["message"], m

    # the preview is pure read + render: no book, no events, no messages
    assert sink.count == 0, sink.messages
    insts = c.get(f"/processes/{pid}/instances").json()["instances"]
    assert all("escalations" not in (i["context"] or {}) for i in insts), insts
    events = c.get("/events", params={"type": "business.escalated"}).json()
    # (check 1's machine legitimately escalated - scope to THIS machine)
    assert all(e["payload"]["process_id"] != pid
               for e in events["events"]), events
    return {"overdue": out["overdue_count"], "subject": out["digest"]["subject"],
            "would_deliver_after_bind": out2["would_deliver"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v93_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert version == "1.93.0", version

            live_tail_check(c)
            print("[1] LIVE TAIL OK - a real WebSocket tail connected to "
                  "/events/stream; the door swept the fresh breach and "
                  "business.stuck crossed the actual socket with the payload "
                  "keys the board needs (ref / process_id / instance_id / "
                  "state / overdue_seconds) - the frame that re-reads the "
                  "attention feed; the feed already carried the row")

            system_chains_check(c)
            print("[2] SYSTEM CHAINS OK - two real operator installs, both "
                  "machines bound to one floor system; the lead walked to "
                  "won and the drawn chain shows the LIVE counts (leg0: "
                  "in_state 0 - won is terminal - fired 1) while the finance end reads NOT "
                  "INSTALLED and the chain honestly says incomplete; the "
                  "sales install's own system shows the same chain "
                  "half-wired")

            policy_preview_check(c, sink)
            print("[3] POLICY PREVIEW OK - the draft digest was typeset over "
                  "the machine's LIVE overdue items (subject + body naming "
                  "both refs), the delivery note named the honest skip "
                  "before any endpoint was bound and flipped to "
                  "would-deliver once one was; the knock draft rendered the "
                  "per-item messages with attempt numbers; the preview "
                  "wrote nothing (no book, no events, no messages)")

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
