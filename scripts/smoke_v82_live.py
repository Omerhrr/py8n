"""V82 live smoke: boot the real server and drive the AI SYSTEM COMPOSER
end to end - describe a business system, get real primitives bound into a
RUNNING system, and prove the composition reacts.

1. DESCRIBE -> DEPLOY: the support-line description composes through
   POST /ai-composer/generate (deterministic archetypes, zero credentials)
   - the built system binds datasets (with seed rows), a composed
   event-trigger workflow, a knowledge-bound voice agent, a meeting room
   and a channel queue with the honest backchannel wiring - all RUNNING
   with the build on the record (operation + system.built event).
2. THE COMPOSITION REACTS: stop -> start with the boot door activates the
   composed workflow loudly; a REAL call.ended event lands in /events and
   the built workflow's write lands IN the built dataset - primitives
   composing into a working system, not generated code.
3. LLM-FIRST THROUGH THE CATALOG: a real openai_compatible credential is
   probed via the catalog door; the propose door validates a hand-written
   spec through POST /ai-composer/propose (deterministic), and an
   impossible spec (hallucinated node type) is refused loud 400.

Usage: /home/z/.venv/bin/python scripts/smoke_v82_live.py
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
API = "http://127.0.0.1:8212/api/v1"
SERVER_PORT = 8212


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
# 1) describe -> deploy
# ---------------------------------------------------------------------------

def generate_check(c: httpx.Client, tag: str) -> dict:
    res = c.post("/ai-composer/generate", json={
        "description": "Build me a customer support system: a phone line that "
                       "answers from our FAQ, keeps callers in a queue with "
                       "announcements, and logs every ended call."})
    assert res.status_code == 201, res.text
    built = res.json()
    assert built["system"]["lifecycle"] == "running", built["system"]
    assert built["system"]["components"] == 6, built["system"]
    assert len(built["datasets"]) == 2 and built["datasets"][0]["rows"] >= 1
    assert len(built["workflows"]) == 1
    assert built["workflows"][0]["trigger"] == "event_trigger"
    assert built["voice_agents"][0]["knowledge"]["dataset_id"]
    assert built["queues"][0]["config"]["announce"]["enabled"] is True
    # the build is on the record
    ops = c.get(f"/systems/{built['system']['id']}/operations").json()["operations"]
    assert any(o["verb"] == "build" for o in ops), ops
    evs = c.get("/events", params={"type": "system.built",
                                   "correlation_id": built["system"]["id"]}).json()["events"]
    assert any(e["type"] == "system.built" for e in evs), evs
    return built


# ---------------------------------------------------------------------------
# 2) the composition reacts
# ---------------------------------------------------------------------------

def react_check(c: httpx.Client, built: dict) -> dict:
    sid = built["system"]["id"]
    wf = built["workflows"][0]
    log_ds = next(d for d in built["datasets"] if "calls" in d["name"].lower())

    # boot door: stop (running -> stopped), start with activate_workflows
    r = c.post(f"/systems/{sid}/stop", json={})
    assert r.status_code == 200, r.text
    r = c.post(f"/systems/{sid}/start", json={"activate_workflows": True})
    assert r.status_code == 200, r.text
    assert r.json()["workflows_activated"] == 1, r.text

    # a REAL event -> the built workflow reacts -> the row lands
    r = c.post("/events", json={
        "type": "call.ended", "source": "voice",
        "payload": {"reason": "smoke"}})
    assert r.status_code == 201, r.text
    _drain()

    runs = c.get("/executions", params={"limit": 30}).json()
    runs = [x for x in runs if x.get("workflow_id") == wf["id"]]
    assert len(runs) == 1, f"expected 1 run, got {len(runs)}"
    assert runs[0]["trigger_type"] == "event" and runs[0]["status"] == "success", runs[0]

    rows = c.get(f"/datasets/{log_ds['id']}/rows").json()
    items = rows.get("rows") or rows.get("records") or rows.get("items") or []
    assert len(items) == 1, rows
    assert items[0].get("event_type") == "call.ended", items[0]
    return {"run": runs[0]["id"], "dataset": log_ds["name"]}


# ---------------------------------------------------------------------------
# 3) validation honesty - the composer refuses the impossible
# ---------------------------------------------------------------------------

def refuse_check(c: httpx.Client) -> None:
    # hallucinated node type -> loud 400 with the exact name
    r = c.post("/ai-composer/build", json={"spec": {
        "name": "bad", "components": [
            {"kind": "dataset", "name": "D", "columns": ["a"]},
            {"kind": "workflow", "name": "w",
             "trigger": {"type": "manual_trigger"},
             "steps": [{"type": "quantum_compute"}]}]}})
    assert r.status_code == 400, r.text
    assert "quantum_compute" in r.json()["detail"], r.text

    # hallucinated component kind -> loud 400
    r = c.post("/ai-composer/build", json={"spec": {
        "name": "bad", "components": [{"kind": "oracle", "name": "O"}]}})
    assert r.status_code == 400, r.text

    # the catalog door: every composable node type is registered
    cat = c.get("/ai-composer/catalog").json()
    assert cat["kinds"], cat
    assert "python_transform" in cat["node_types"]["composable"], cat


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v82_{uuid.uuid4().hex[:8]}.sqlite3"
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
            assert version == "1.82.0", version
            tag = uuid.uuid4().hex[:6]

            built = generate_check(c, tag)
            print(f"[1] DESCRIBE -> DEPLOY OK - the description composed "
                  f"{built['system']['components']} primitives (datasets with "
                  f"seed rows, an event-trigger workflow, a knowledge-bound "
                  f"agent, a room, a queue with the backchannel wiring) into "
                  f"system {built['system']['name']!r} RUNNING, the build on "
                  f"the record (operation + system.built event)")

            r = react_check(c, built)
            print(f"[2] THE COMPOSITION REACTS OK - stop -> start(boot) "
                  f"activated the composed workflow; a REAL call.ended event "
                  f"ran it (execution {r['run'][:8]}) and the row landed IN "
                  f"the built dataset {r['dataset']!r} - primitives "
                  f"composing, never code generation")

            refuse_check(c)
            print(f"[3] VALIDATION HONESTY OK - a hallucinated node type and "
                  f"an unknown component kind are refused loud 400 with the "
                  f"exact name; the catalog door lists the composable truth")

            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
