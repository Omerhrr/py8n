"""V83 live smoke: boot the real server and drive the MARKETPLACE OPERATORS
end to end - install a business operator and watch it RUN as a business.

1. THE SHELF + THE INSTALL: GET /operators lists the three curated business
   operators; POST /operators/meeting-operator/install composes the whole
   business into real primitives - the Meeting notes dataset with seed rows,
   the event-reactive scribe workflow (installed INACTIVE - honest), the
   meeting concierge agent with its scaffolded handler, the VIDEO-FIRST
   meeting room, the waiting-room queue with the announce/SMS/callback
   wiring, and the staff dashboard generated over the built dataset - all
   bound into a RUNNING Py8nSystem with the install on the record.
2. THE BUSINESS REACTS: stop -> start with the boot door activates the
   scribe loudly; the operator's REAL meeting room ENDS (hung-up legs +
   meeting.ended event through the v80 event system) and the scribe's write
   lands IN the built Meeting notes dataset - the room logged itself.
3. THE SALES OPERATOR + HONESTY: install composes the CRM (seed rows), the
   SDR grounded in the built Sales FAQ (knowledge search answers from it),
   an EMPTY campaign with the retry/AMD defaults filled (create_campaign
   refuses empty target lists BY DESIGN); a REAL call.ended event scores
   the lead into Lead events; and the operator system refuses the
   solution-pack upgrade with the honest OPERATOR message.

Usage: /home/z/.venv/bin/python scripts/smoke_v83_live.py
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
API = "http://127.0.0.1:8213/api/v1"
SERVER_PORT = 8213


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
    time.sleep(1.2)


# ---------------------------------------------------------------------------
# 1) the shelf + the install
# ---------------------------------------------------------------------------

def install_check(c: httpx.Client) -> dict:
    shelf = c.get("/operators").json()["operators"]
    assert [o["slug"] for o in shelf] == ["meeting-operator", "sales-operator",
                                          "clinic-operator"], shelf
    meeting = shelf[0]
    assert meeting["topology"] == {"datasets": 1, "workflows": 1, "agents": 1,
                                   "rooms": 1, "queues": 1, "campaign": 0,
                                   "dashboard": 1}, meeting

    r = c.post("/operators/meeting-operator/install", json={})
    assert r.status_code == 200, r.text
    built = r.json()
    assert built["system"]["lifecycle"] == "running", built["system"]
    assert built["system"]["components"] == {
        "dataset": 1, "workflow": 2, "voice_agent": 1, "meeting": 1,
        "queue": 1, "dashboard": 1}, built["system"]
    assert built["datasets"][0]["rows"] == 2, built["datasets"]
    assert built["workflows"][0]["active"] is False, built["workflows"]
    assert built["rooms"][0]["modality"] == "video", built["rooms"]
    assert built["queues"][0]["config"]["announce"]["enabled"] is True
    assert built["dashboard"]["components"] > 0, built["dashboard"]

    # the video room is video-first for the media runtime
    room = c.get(f"/voice/meetings/{built['rooms'][0]['id']}").json()
    assert (room.get("context") or {}).get("modality") == "audio+video", room

    # the install is on the record
    ops = c.get(f"/systems/{built['system']['id']}/operations").json()["operations"]
    assert any(o["verb"] == "installed" for o in ops), ops
    return built


# ---------------------------------------------------------------------------
# 2) the business reacts - the room logs itself
# ---------------------------------------------------------------------------

def react_check(c: httpx.Client, built: dict) -> dict:
    sid = built["system"]["id"]
    scribe = built["workflows"][0]
    room_id = built["rooms"][0]["id"]
    notes_id = built["datasets"][0]["id"]

    # boot door: stop (running -> stopped), start with activate_workflows
    r = c.post(f"/systems/{sid}/stop", json={})
    assert r.status_code == 200, r.text
    r = c.post(f"/systems/{sid}/start", json={"activate_workflows": True})
    assert r.status_code == 200, r.text
    assert r.json()["workflows_activated"] == 2, r.text  # scribe + the agent's handler

    # the REAL room ends -> meeting.ended fires through the event system
    r = c.post(f"/voice/meetings/{room_id}/end", json={})
    assert r.status_code == 200, r.text
    _drain()

    runs = c.get("/executions", params={"limit": 30}).json()
    runs = [x for x in runs if x.get("workflow_id") == scribe["id"]]
    assert len(runs) == 1, f"expected 1 run, got {len(runs)}"
    assert runs[0]["trigger_type"] == "event" and runs[0]["status"] == "success", runs[0]

    rows = c.get(f"/datasets/{notes_id}/rows").json()
    items = rows.get("rows") or rows.get("records") or rows.get("items") or []
    assert len(items) == 3, rows  # 2 seeds + the ended meeting
    logged = items[-1]
    assert logged.get("title") == "Operator meeting room", logged
    assert logged.get("meeting_id") == room_id, logged
    return {"run": runs[0]["id"], "dataset": built["datasets"][0]["name"]}


# ---------------------------------------------------------------------------
# 3) the sales operator + the honest refusals
# ---------------------------------------------------------------------------

def sales_check(c: httpx.Client) -> dict:
    r = c.post("/operators/sales-operator/install", json={})
    assert r.status_code == 200, r.text
    built = r.json()

    # the campaign composed EMPTY with the retry/AMD defaults filled
    camp = built["campaign"]
    assert camp and camp["targets"] == 0, camp
    assert camp["config"], camp

    # the CRM seeds came along and the SDR is grounded in the built FAQ
    crm = next(d for d in built["datasets"] if d["name"].startswith("CRM leads"))
    rows = c.get(f"/datasets/{crm['id']}/rows").json()
    items = rows.get("rows") or rows.get("records") or []
    assert len(items) == 3, rows
    agent_id = built["agents"][0]["id"]
    r = c.post(f"/voice/agents/{agent_id}/knowledge/search",
               json={"query": "What does it cost"})
    assert r.status_code == 200, r.text
    hits = r.json().get("matches") or r.json().get("results") or []
    assert hits and "twenty dollars" in str(hits[0]), r.text

    # boot, then a REAL call.ended event: the lead scores itself
    sid = built["system"]["id"]
    scorer = built["workflows"][0]
    events_ds = next(d for d in built["datasets"] if d["name"].startswith("Lead events"))
    r = c.post(f"/systems/{sid}/stop", json={})
    assert r.status_code == 200, r.text
    r = c.post(f"/systems/{sid}/start", json={"activate_workflows": True})
    assert r.status_code == 200, r.text
    r = c.post("/events", json={
        "type": "call.ended", "source": "voice",
        "payload": {"end_reason": "completed", "state": "ended"}})
    assert r.status_code == 201, r.text
    _drain()

    runs = c.get("/executions", params={"limit": 30}).json()
    runs = [x for x in runs if x.get("workflow_id") == scorer["id"]]
    assert len(runs) == 1 and runs[0]["status"] == "success", runs
    rows = c.get(f"/datasets/{events_ds['id']}/rows").json()
    items = rows.get("rows") or rows.get("records") or []
    assert len(items) == 1 and int(items[0].get("score") or 0) == 60, items

    # the operator system refuses the solution-pack upgrade honestly
    r = c.post(f"/systems/{sid}/upgrade", json={})
    assert r.status_code == 400 and "OPERATOR" in r.json()["detail"], r.text
    return {"score": items[0].get("score"), "campaign": camp["name"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v83_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert version == "1.83.0", version

            built = install_check(c)
            print(f"[1] SHELF + INSTALL OK - the operators shelf lists 3 "
                  f"businesses; the meeting operator composed {sum(built['system']['components'].values())} "
                  f"primitives (notes dataset with seeds, the INACTIVE scribe, "
                  f"the concierge agent + handler, a VIDEO-FIRST room, the "
                  f"waiting-room queue with the backchannel wiring, the staff "
                  f"board) into system {built['system']['name']!r} RUNNING, "
                  f"the install on the record")

            r = react_check(c, built)
            print(f"[2] THE BUSINESS REACTS OK - stop -> start(boot) opened "
                  f"the reactive path; the operator's REAL meeting room ended "
                  f"(meeting.ended through the event system) and the scribe "
                  f"logged the room into {r['dataset']!r} (execution {r['run'][:8]}) "
                  f"- the room logged itself")

            s = sales_check(c)
            print(f"[3] SALES OPERATOR + HONESTY OK - the CRM seeded, the SDR "
                  f"answered from the built FAQ, the campaign composed EMPTY "
                  f"with defaults ({s['campaign']!r}), a REAL call.ended scored "
                  f"the lead {s['score']}/100 into Lead events, and the "
                  f"operator system refused the pack upgrade loudly")

            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
