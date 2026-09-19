"""AUDIT E2E v107: build a whole company on py8n from an EMPTY server and
walk EVERY functional family over the real wire - the full-product audit.

The story: Acme arrives on a fresh py8n. Data lands, machines are built
(builder + AI composer), the three Revenue-chain operators are installed,
a deal rides Sales -> Operations -> Finance, the door knocks over a REAL
SMTP wire (knock + digest), the team organizes into a system with roles,
the system goes public on its custom domain (route sheet, TLS notes, ping
rhythm, branded front door, work surface), speaks through its own API key,
takes an upgrade through the pending -> accept lifecycle and rolls one
back, mails its chain report to a list, and the census proves every API
family answers. The events stream listens on the real WebSocket.

13 numbered phases, each printing one PASS line. Any failure aborts loud
with the phase number.

Usage: /home/z/.venv/bin/python scripts/audit_e2e_v107.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SCRIPTS = "/home/z/my-project/py8n/scripts"
SMOKE_SERVER = f"{SCRIPTS}/smoke_v74_server.py"
sys.path.insert(0, SCRIPTS)

from dev_smtp_sink import SmtpDevSink  # noqa: E402

PY = ("/home/z/.venv/bin/python"
      if os.path.exists("/home/z/.venv/bin/python") else "python3")

MACHINE = {
    "states": ["a", "b", "done", "dead"],
    "initial": "a",
    "transitions": [
        {"name": "go", "from": "a", "to": "b"},
        {"name": "finish", "from": "b", "to": "done"},
        {"name": "kill", "from": "a", "to": "dead"},
    ],
}

FRONT_DESK = {
    "states": ["a", "b", "done"],
    "initial": "a",
    "transitions": [
        {"name": "go", "from": "a", "to": "b"},
        {"name": "finish", "from": "b", "to": "done"},
    ],
    "escalation_policy": {
        "channel": "email", "to": "ops@py8n.test",
        "repeat_every_seconds": 3600, "max_repeats": 5},
}

BILLING = {
    "states": ["a", "b", "done"],
    "initial": "a",
    "transitions": [
        {"name": "go", "from": "a", "to": "b"},
        {"name": "finish", "from": "b", "to": "done"},
    ],
    "escalation_policy": {
        "channel": "email", "to": "billing@py8n.test",
        "mode": "digest", "digest_every_seconds": 60, "max_repeats": 1},
}


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


SERVER_PORT = _free_port()
API = f"http://127.0.0.1:{SERVER_PORT}/api/v1"


def wait_health(client: httpx.Client, deadline: float = 240.0) -> None:
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


def phase(n: int, title: str) -> None:
    print(f"[{n}] {title} ...", flush=True)


def ok(n: int, msg: str) -> None:
    print(f"[{n}] PHASE OK - {msg}", flush=True)


# ---------------------------------------------------------------------------
# 1) the company arrives - identity + auth
# ---------------------------------------------------------------------------
def phase_identity(c: httpx.Client) -> dict:
    phase(1, "THE COMPANY ARRIVES (auth + identity)")
    tag = uuid.uuid4().hex[:6]
    users = {}
    for who in ("owner", "editor", "viewer"):
        r = c.post("/auth/register", json={
            "email": f"{who}-{tag}@acme.test",
            "password": "correct-horse-battery", "name": who})
        assert r.status_code == 201, r.text
        users[who] = r.json()
    oh = {"Authorization": f"Bearer {users['owner']['token']}"}
    eh = {"Authorization": f"Bearer {users['editor']['token']}"}
    vh = {"Authorization": f"Bearer {users['viewer']['token']}"}

    r = c.get("/auth/me", headers=oh)
    assert r.status_code == 200 and r.json()["email"] == f"owner-{tag}@acme.test", r.text
    r = c.post("/auth/login", json={
        "email": f"owner-{tag}@acme.test", "password": "correct-horse-battery"})
    assert r.status_code == 200 and r.json().get("token"), r.text
    r = c.get("/auth/status")
    assert r.status_code == 200, r.text
    ok(1, f"owner/editor/viewer registered, login + me + status answer "
          f"(owner {users['owner'].get('email', '')})")
    return {"oh": oh, "eh": eh, "vh": vh, "users": users}


# ---------------------------------------------------------------------------
# 2) the data lands - datasets + dashboards + share gates
# ---------------------------------------------------------------------------
def phase_data(c: httpx.Client, h: dict) -> dict:
    phase(2, "THE DATA LANDS (dataset + dashboard + share)")
    oh = h["oh"]
    rows = [{"region": "eu", "amount": 120}, {"region": "us", "amount": 340},
            {"region": "eu", "amount": 90}]
    r = c.post("/datasets", headers=oh, json={
        "name": f"Acme sales {uuid.uuid4().hex[:5]}", "rows": rows})
    assert r.status_code == 201, r.text
    ds = r.json()
    r = c.get(f"/datasets/{ds['id']}/rows", headers=oh)
    assert r.status_code == 200, r.text
    r = c.post(f"/datasets/{ds['id']}/certify", headers=oh)
    assert r.status_code in (200, 201), r.text

    r = c.post("/dashboards", headers=oh, json={
        "name": f"Acme revenue board {uuid.uuid4().hex[:5]}",
        "dataset_ids": [ds["id"]]})
    assert r.status_code == 201, r.text
    board = r.json()
    r = c.post(f"/dashboards/{board['id']}/publish", headers=oh)
    assert r.status_code in (200, 201), r.text
    token = c.put(f"/dashboards/{board['id']}/share", headers=oh,
                  json={"enabled": True}).json()["share_token"]
    r_ok = c.get(f"/dashboards/{board['slug']}/runtime?t={token}")
    r_bad = c.get(f"/dashboards/{board['slug']}/runtime?t=nope")
    r_none = c.get(f"/dashboards/{board['slug']}/runtime")
    assert r_ok.status_code == 200 and r_bad.status_code == 403 \
        and r_none.status_code == 403, (r_ok.status_code, r_bad.status_code,
                                        r_none.status_code)
    ok(2, "dataset with rows certified, dashboard published, share token "
          "admits the runtime and a bad/absent token is refused (403)")
    return {"dataset": ds, "board": board}


# ---------------------------------------------------------------------------
# 3) the machines are built - workflow run + builder + AI composer
# ---------------------------------------------------------------------------
def phase_builders(c: httpx.Client, h: dict) -> dict:
    phase(3, "THE MACHINES ARE BUILT (workflow run + builder + ai-composer)")
    oh = h["oh"]
    r = c.post("/workflows", headers=oh, json={
        "name": f"Acme welcome flow {uuid.uuid4().hex[:5]}",
        "graph": {"nodes": [{"id": "t1", "type": "manual_trigger",
                             "name": "t1", "position": {"x": 0, "y": 0},
                             "parameters": {}}],
                  "edges": []}})
    assert r.status_code == 201, r.text
    wf = r.json()
    r = c.post(f"/workflows/{wf['id']}/run", headers=oh, json={"payload": {}})
    assert r.status_code in (200, 201), r.text
    run = r.json()
    ex = None
    for _ in range(100):
        ex = c.get(f"/executions/{run['execution_id']}", headers=oh).json()
        if ex.get("status") != "running":
            break
        time.sleep(0.1)
    assert ex and ex.get("status") in ("succeeded", "success", "completed"), ex
    r = c.get("/executions", headers=oh)
    assert r.status_code == 200, r.text

    r = c.post("/builder/systems", headers=oh, json={
        "description": "A customer support desk with a ticket queue and "
                       "an FAQ agent"})
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft.get("spec"), draft
    r = c.post(f"/builder/systems/{draft['id']}/build", headers=oh,
               json={"as_system": False})
    assert r.status_code in (200, 201), r.text
    built = r.json()

    r = c.get("/ai-composer/catalog", headers=oh)
    assert r.status_code == 200, r.text
    cat = r.json()
    assert cat.get("node_types") or cat.get("kinds"), cat
    r = c.post("/ai-composer/generate", headers=oh, json={
        "description": "An invoice chasing desk that watches overdue bills "
                       "and emails the team"})
    assert r.status_code == 201, r.text
    gen = r.json()
    ok(3, f"workflow ran to a settled execution; the v59 builder turned a "
          f"description into built primitives ({sorted(built.get('built', {}))[:3]}...); "
          f"the v82 AI composer catalog + deterministic describe->deploy "
          f"built a system too")
    return {"workflow": wf, "builder_draft": draft, "composer": gen}


# ---------------------------------------------------------------------------
# 4) the operators arrive - marketplace + installs
# ---------------------------------------------------------------------------
def phase_operators(c: httpx.Client, h: dict) -> dict:
    phase(4, "THE OPERATORS ARRIVE (marketplace catalog + installs)")
    oh = h["oh"]
    r = c.get("/operators", headers=oh)
    assert r.status_code == 200, r.text
    catalog = r.json()
    ops = catalog.get("operators") or catalog
    assert len(ops) >= 9, f"expected 9 operators, got {len(ops)}"

    r = c.get("/operators/sales-operator", headers=oh)
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail.get("chains"), "operator detail lost its chains"

    procs = {}
    for slug in ("sales-operator", "operations-operator", "finance-operator"):
        r = c.post(f"/operators/{slug}/install", headers=oh,
                   json={"note": "audit e2e"})
        assert r.status_code == 200, r.text
        built = r.json()
        assert built["system"]["lifecycle"] == "running", built["system"]
        for p in built.get("processes", []):
            procs[p["name"]] = p["id"]
    for name in ("Lead pipeline", "Customer onboarding", "Invoice lifecycle"):
        assert name in procs, f"{name} missing after install: {sorted(procs)}"
    ok(4, f"{len(ops)} operators on the shelf, sales detail wears its chains, "
          f"Revenue-chain trio installed as running systems "
          f"({len(procs)} machines)")
    return {"procs": procs}


# ---------------------------------------------------------------------------
# 5) the revenue chain runs - journeys + chain map + CSV
# ---------------------------------------------------------------------------
def phase_revenue_chain(c: httpx.Client, h: dict, procs: dict) -> dict:
    phase(5, "THE REVENUE CHAIN RUNS (journey fire + chain map + CSV)")
    oh = h["oh"]
    lead_pid = procs["Lead pipeline"]
    onb_pid = procs["Customer onboarding"]
    inv_pid = procs["Invoice lifecycle"]

    r = c.get(f"/processes/{onb_pid}", headers=oh)
    journeys = r.json()["journeys"]
    assert any(j["on_state"] == "handed_off" for j in journeys), journeys

    ref = f"+1555{uuid.uuid4().hex[:6]}"
    r = c.post(f"/processes/{lead_pid}/instances", headers=oh, json={
        "ref": ref, "title": "Audit Deal", "context": {"company": "Audit Co"}})
    assert r.status_code == 201, r.text
    iid = r.json()["id"]
    for move in ("reach_out", "qualify", "book_demo", "run_demo",
                 "send_proposal", "negotiate", "win"):
        r = c.post(f"/processes/{lead_pid}/instances/{iid}/advance",
                   headers=oh, json={"transition": move, "actor": "audit"})
        assert r.status_code == 200, f"{move}: {r.text}"

    r = c.get(f"/processes/{onb_pid}/instances", headers=oh)
    case = next(x for x in r.json()["instances"] if x["ref"] == ref)
    for move in ("provision", "train", "go_live", "hand_off"):
        r = c.post(f"/processes/{onb_pid}/instances/{case['id']}/advance",
                   headers=oh, json={"transition": move, "actor": "audit"})
        assert r.status_code == 200, f"{move}: {r.text}"

    r = c.get(f"/processes/{inv_pid}/instances", headers=oh)
    inv = next(x for x in r.json()["instances"] if x["ref"] == ref)
    assert inv["state"] == "received", inv
    assert inv["context"]["journey"]["from_process"] == "Customer onboarding", inv
    assert inv["due_at"], inv

    r = c.get("/processes/chains", headers=oh)
    assert r.status_code == 200, r.text
    chains = r.json().get("chains", [])
    rev = next((ch for ch in chains
                if "revenue" in str(ch.get("name", "")).lower()), None)
    assert rev, [ch.get("name") for ch in chains]
    legs = rev.get("legs", [])
    assert any(leg.get("opened", 0) >= 1 for leg in legs), legs

    r = c.get("/processes/chains/history.csv", headers=oh)
    assert r.status_code == 200, r.text
    assert "x-py8n-leg-count" in {k.lower() for k in r.headers}, r.headers
    csv_all = r.text
    assert csv_all.splitlines()[0].startswith("chain"), csv_all[:120]

    r = c.get("/processes/chains/history.csv", headers=oh,
              params={"chain": "revenue"})
    assert r.status_code == 200 and \
        "x-py8n-chain-filter" in {k.lower() for k in r.headers}, r.headers
    r = c.get("/processes/chains/history.csv", headers=oh,
              params={"chain": "zilch"})
    assert r.status_code == 200 and r.headers.get("x-py8n-leg-count") == "0", \
        r.headers.get("x-py8n-leg-count")
    ok(5, f"one ref rode Sales->Operations->Finance (invoice '{inv['title']}' "
          f"landed received with its journey receipt + own SLA), the estate "
          f"chain map wears the Revenue chain with real rides, the CSV "
          f"exports with filters (honest empty for a ghost chain)")
    return {"ref": ref, "invoice": inv}


# ---------------------------------------------------------------------------
# 6) the door knocks - escalations over the REAL SMTP wire
# ---------------------------------------------------------------------------
def phase_escalations(c: httpx.Client, h: dict, sink: SmtpDevSink) -> dict:
    phase(6, "THE DOOR KNOCKS (attention + knock + ack/snooze + digest "
             "+ heatmap + analytics)")
    oh = h["oh"]

    r = c.post("/channels/endpoints", headers=oh, json={
        "name": "Audit inbox", "provider": "email_inbound",
        "config": {"secret": "audit-secret",
                   "smtp_host": "127.0.0.1", "smtp_port": str(sink.port),
                   "smtp_user": "audit", "smtp_pass": "audit",
                   "from_address": "py8n@py8n.test"}})
    assert r.status_code == 201, r.text

    r = c.post("/processes", headers=oh,
               json={"name": "Audit front desk", "definition": FRONT_DESK})
    assert r.status_code == 201, r.text
    fd = r.json()

    # the late one - 1s SLA on the real clock (the v96 discipline)
    r = c.post(f"/processes/{fd['id']}/instances", headers=oh, json={
        "ref": "K-1", "title": "the late visitor", "due_in_seconds": 1})
    assert r.status_code == 201, r.text
    late_iid = r.json()["id"]
    r = c.post(f"/processes/{fd['id']}/instances", headers=oh, json={
        "ref": "K-2", "title": "the calm visitor", "due_in_seconds": 3600})
    assert r.status_code == 201, r.text
    calm_iid = r.json()["id"]

    time.sleep(2.0)
    r = c.get("/processes/attention", headers=oh)
    assert r.status_code == 200, r.text
    att = r.json()
    rows = att.get("attention") or att.get("rows") or []
    assert any((row.get("instance_id") or row.get("id")) == late_iid
               for row in rows), att

    r = c.post("/scheduler/escalations/tick", headers=oh, json={})
    assert r.status_code == 200, r.text
    time.sleep(0.5)
    assert sink.count >= 1, sink.messages
    knock = sink.last()
    assert knock["to"] == ["ops@py8n.test"], knock
    assert "past SLA" in knock["subject"], knock

    # the human takes the row - ack + snooze; the second tick holds
    r = c.post(f"/processes/{fd['id']}/instances/{late_iid}/escalations/ack",
               headers=oh, json={"by": "audit-owner", "note": "on it",
                                 "snooze_hours": 4})
    assert r.status_code == 200, r.text
    acks_before = sink.count
    c.post("/scheduler/escalations/tick", headers=oh, json={})
    time.sleep(0.5)
    assert sink.count == acks_before, "the ack did not hold the door"

    # NOW the digest machine - kept apart so the two asserts never interleave
    r = c.post("/processes", headers=oh,
               json={"name": "Audit billing", "definition": BILLING})
    assert r.status_code == 201, r.text
    bil = r.json()
    for ref_ in ("INV-1", "INV-2"):
        r = c.post(f"/processes/{bil['id']}/instances", headers=oh, json={
            "ref": ref_, "title": f"invoice {ref_}", "due_in_seconds": 1})
        assert r.status_code == 201, r.text

    # the digest window walks (the door clamps digest_every_seconds >= 60
    # and the clamp was CAUGHT LIVE by this audit - the bounded loop walks
    # the real window; the report shape is the proof)
    sent = []
    rep = {}
    deadline = time.time() + 95
    while time.time() < deadline and not sent:
        rep = c.post("/scheduler/escalations/tick", headers=oh, json={}).json()
        sent = rep.get("digest", {}).get("sent", [])
        if not sent:
            time.sleep(4)
    assert sent, f"digest never sent: {rep.get('digest')}"
    time.sleep(0.5)
    digest = sink.last()
    assert digest["to"] == ["billing@py8n.test"], digest
    assert "digest" in digest["subject"].lower(), digest
    body = digest["text"]
    assert "INV-1" in body and "INV-2" in body, body[:400]

    today = time.strftime("%Y-%m-%d")
    r = c.get("/processes/escalation-history", headers=oh, params={"days": 14})
    assert r.status_code == 200, r.text
    grid = r.json()
    assert grid.get("days") and grid.get("machines"), grid.keys()
    r = c.get(f"/processes/escalation-history/{fd['id']}/{today}", headers=oh)
    assert r.status_code == 200, r.text
    r = c.get(f"/processes/{fd['id']}/analytics", headers=oh)
    assert r.status_code == 200, r.text
    an = r.json()
    hist = an.get("escalation_history") or {}
    assert hist.get("window_days") == 14 and len(hist.get("days", [])) == 14, hist
    assert an.get("escalations", 0) >= 1 and an.get("acknowledgements", 0) >= 1, \
        (an.get("escalations"), an.get("acknowledgements"))
    ok(6, "the attention feed carried the late row, the knock crossed the "
          "REAL SMTP wire (ops@py8n.test), the ack+snooze held the second "
          "tick, the digest landed in billing's inbox with BOTH refs, and "
          "the 14-day heatmap + day drill + per-machine analytics all read "
          "the same door")
    return {"front_desk": fd, "billing": bil, "late_iid": late_iid,
            "calm_iid": calm_iid}


# ---------------------------------------------------------------------------
# 7) the teams organize - systems, bindings, member roles
# ---------------------------------------------------------------------------
def phase_teams(c: httpx.Client, h: dict, dataset: dict, fd: dict) -> dict:
    phase(7, "THE TEAMS ORGANIZE (system + bindings + member roles)")
    oh, eh, vh = h["oh"], h["eh"], h["vh"]
    r = c.post("/systems", headers=oh, json={
        "name": "Acme HQ", "color": "#38bdf8"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    r = c.post(f"/systems/{sid}/components", headers=oh,
               json={"kind": "dataset", "ref_id": dataset["id"]})
    assert r.status_code in (200, 201), r.text
    r = c.post(f"/systems/{sid}/components", headers=oh,
               json={"kind": "process", "ref_id": fd["id"]})
    assert r.status_code in (200, 201), r.text

    editor_email = h["users"]["editor"]["user"]["email"]
    viewer_email = h["users"]["viewer"]["user"]["email"]
    r = c.post(f"/systems/{sid}/members", headers=oh,
               json={"email": viewer_email, "role": "viewer"})
    assert r.status_code == 201, r.text
    r = c.get(f"/systems/{sid}", headers=vh)
    assert r.status_code == 200, r.text
    r = c.post(f"/systems/{sid}/components", headers=vh,
               json={"kind": "dataset", "ref_id": dataset["id"]})
    assert r.status_code == 403, r.text
    r = c.post(f"/systems/{sid}/members", headers=oh,
               json={"email": editor_email, "role": "viewer"})
    assert r.status_code == 201, r.text
    vid = h["users"]["editor"]["user"]["id"]
    r = c.put(f"/systems/{sid}/members/{vid}", headers=oh,
              json={"role": "editor"})
    assert r.status_code == 200, r.text
    r = c.post("/datasets", headers=oh, json={
        "name": f"Acme extra {uuid.uuid4().hex[:5]}", "rows": [{"k": 1}]})
    assert r.status_code == 201, r.text
    r = c.post(f"/systems/{sid}/components", headers=eh,
               json={"kind": "dataset", "ref_id": r.json()["id"]})
    assert r.status_code in (200, 201), r.text
    r = c.post(f"/systems/{sid}/members", headers=oh,
               json={"email": "ghost@acme.test", "role": "viewer"})
    assert r.status_code == 404, r.text
    ok(7, "Acme HQ holds its machines + data, the viewer read but could not "
          "bind (403), the promoted editor bound one, a ghost member is a "
          "loud 404")
    return {"sid": sid}


# ---------------------------------------------------------------------------
# 8) the estate goes public - deployment, route sheet, ping, front door
# ---------------------------------------------------------------------------
def phase_deploy(c: httpx.Client, h: dict, sid: str, fd_id: str) -> str:
    phase(8, "THE ESTATE GOES PUBLIC (deployment + routes.caddy + ping "
             "rhythm + front door + work surface)")
    oh = h["oh"]
    domain = f"ops{uuid.uuid4().hex[:6]}.acme.com"
    r = c.put(f"/systems/{sid}/deployment", headers=oh, json={
        "domain": domain, "environment": "production",
        "branding": {"accent": "#38bdf8", "tagline": "Acme runs on py8n",
                     "login_headline": "Welcome to Acme Operations"}})
    assert r.status_code == 200, r.text
    r = c.post(f"/systems/{sid}/deployment/deploy", headers=oh)
    assert r.status_code == 200, r.text
    assert r.json()["deployment"]["status"] == "live", r.text

    r = c.get("/systems/deployment/routes.caddy", headers=oh)
    assert r.status_code == 200, r.text
    sheet = r.text
    assert domain in sheet, sheet[:300]
    assert "automatic HTTPS" in sheet or "no probe evidence yet" in sheet, sheet[:400]

    r = c.post(f"/systems/{sid}/deployment/ping", headers=oh)
    assert r.status_code == 200, r.text
    ping = r.json().get("ping", r.json())
    assert ping.get("ok") is True, ping
    r = c.get(f"/systems/{sid}/deployment", headers=oh)
    dep = r.json()["deployment"]
    assert dep["ping_rhythm"]["every"] == "10m", dep["ping_rhythm"]
    assert dep["ping_rhythm"]["next_due_at"], dep["ping_rhythm"]
    assert dep["last_ping"]["ok"] is True, dep["last_ping"]

    r = c.get(f"/systems/by-domain/{domain}", headers=oh)
    assert r.status_code == 200, r.text
    door = r.json()
    assert door["branding"]["tagline"] == "Acme runs on py8n", door["branding"]
    assert door.get("my_role") in (None, "owner", "editor", "viewer"), door

    r = c.get(f"/systems/by-domain/{domain}/work", headers=oh)
    assert r.status_code == 200, r.text
    work = r.json()["work"]
    assert len(work["machines"]) >= 1, work
    assert any(m["process_id"] == fd_id for m in work["machines"]), work["machines"]
    assert r.json()["liveness"]["ping_rhythm"]["every"] == "10m", r.json()["liveness"]
    ok(8, f"{domain} deployed live, the route sheet carries its TLS note, "
          f"the real ping answered HTTP 200 and the rhythm says auto-probe "
          f"every 10m with a next check, the branded front door wears the "
          f"system's own tagline, and the work surface carries per-machine "
          f"views + liveness")
    return domain


# ---------------------------------------------------------------------------
# 9) the system speaks - API keys on the doors
# ---------------------------------------------------------------------------
def phase_keys(c: httpx.Client, h: dict, sid: str, fd: dict,
               calm_iid: str) -> None:
    phase(9, "THE SYSTEM SPEAKS (API key mints, operates, revokes)")
    oh = h["oh"]
    r = c.post(f"/systems/{sid}/keys", headers=oh,
               json={"name": "Audit ERP job"})
    assert r.status_code == 201, r.text
    key_body = r.json()
    key = key_body["key"]
    assert key.startswith("py8n_sys_"), key_body
    kh = {"X-API-Key": key}

    r = c.get(f"/systems/{sid}", headers=kh)
    assert r.status_code == 200 and r.json()["my_role"] == "editor", r.text
    r = c.post(f"/processes/{fd['id']}/instances/{calm_iid}/advance",
               headers=kh, json={"transition": "go", "actor": "erp"})
    assert r.status_code == 200, r.text
    r = c.get("/processes", headers=kh)
    assert r.status_code == 403, r.status_code
    r = c.post(f"/systems/{sid}/keys", headers=kh, json={"name": "self-mint"})
    assert r.status_code == 403, r.text
    keys = c.get(f"/systems/{sid}/keys", headers=oh).json()["keys"]
    row = next(k for k in keys if k["id"] == key_body["id"])
    assert row["prefix"] == key_body["prefix"] and "key" not in row, row
    r = c.delete(f"/systems/{sid}/keys/{key_body['id']}", headers=oh)
    assert r.status_code == 204, r.text
    keys = c.get(f"/systems/{sid}/keys", headers=oh).json()["keys"]
    assert next(k for k in keys if k["id"] == key_body["id"])["revoked"], keys
    ok(9, "the key spoke AS the system (editor) and advanced a bound "
          "machine's instance, the estate-wide view refused it (403), "
          "self-mint refused (403), the list masked the secret, and the "
          "revocation landed")


# ---------------------------------------------------------------------------
# 10) the update lifecycle - upgrade, pending chip, accept, rollback
# ---------------------------------------------------------------------------
def phase_updates(c: httpx.Client, h: dict, db_path: str,
                  quiet_sid: str) -> None:
    phase(10, "THE UPDATE LIFECYCLE (upgrade -> pending chip -> accept / "
              "rollback)")
    oh = h["oh"]
    tag = uuid.uuid4().hex[:6]
    r = c.post("/workflows", headers=oh, json={
        "name": f"Audit handler {tag}", "graph": {
            "nodes": [{"id": "t1", "type": "manual_trigger", "name": "t1",
                       "position": {"x": 0, "y": 0}, "parameters": {}}],
            "edges": []}})
    assert r.status_code == 201, r.text
    wf1 = r.json()
    r = c.post("/solutions", headers=oh, json={
        "name": f"Audit suite {tag}", "outcomes": ["one workflow"],
        "workflow_ids": [wf1["id"]]})
    assert r.status_code in (200, 201), r.text
    slug = r.json()["slug"]
    r = c.post(f"/solutions/{slug}/install", headers=oh,
               json={"as_system": True})
    assert r.status_code == 200, r.text
    sid = r.json()["system"]["id"]

    # grow the pack the authoring way - the smoke opens the SAME sqlite
    # database and patches the solution's pack_json (a fresh install has
    # nothing to add; growing is what makes the upgrade a real changeset)
    _grow_pack(db_path, slug, [f"Audit handler {tag}", f"Audit extra {tag}"])

    r = c.get("/systems/health/overview", headers=oh)
    by_id = {row["id"]: row for row in r.json()["systems"]}
    assert by_id[sid]["pending_update"] is None, by_id[sid]
    assert by_id[quiet_sid]["pending_update"] is None, by_id[quiet_sid]

    r = c.post(f"/systems/{sid}/upgrade", headers=oh)
    assert r.status_code == 200 and r.json()["added"]["workflow"] == 1, r.text
    r = c.get(f"/systems/{sid}/update/preview", headers=oh)
    pending = r.json().get("pending")
    assert pending and pending["operation_id"], r.text
    r = c.get("/systems/health/overview", headers=oh)
    by_id = {row["id"]: row for row in r.json()["systems"]}
    chip = by_id[sid]["pending_update"]
    assert chip and chip["added_total"] == 1, by_id[sid]
    assert by_id[quiet_sid]["pending_update"] is None, by_id[quiet_sid]

    r = c.post(f"/systems/{sid}/update/accept", headers=oh)
    assert r.status_code == 200, r.text
    r = c.get("/systems/health/overview", headers=oh)
    by_id = {row["id"]: row for row in r.json()["systems"]}
    assert by_id[sid]["pending_update"] is None, by_id[sid]
    r = c.post(f"/systems/{sid}/update/rollback", headers=oh)
    assert r.status_code in (400, 404, 409), r.status_code

    # second round: the rollback path itself - grow a genuinely NEW name
    tag2 = uuid.uuid4().hex[:6]
    _grow_pack(db_path, slug, [f"Audit handler {tag}", f"Audit extra {tag}",
                               f"Audit extra {tag2}"])
    r = c.post(f"/systems/{sid}/upgrade", headers=oh)
    assert r.status_code == 200 and r.json()["added"]["workflow"] == 1, r.text
    r = c.post(f"/systems/{sid}/update/rollback", headers=oh)
    assert r.status_code == 200, r.text
    r = c.get("/workflows", headers=oh)
    assert any(w["name"] == f"Audit handler {tag}" for w in r.json()), \
        "rollback must not delete the import"
    ok(10, "the settled system wore no chip; the upgrade put the changeset "
          "PENDING and the estate row wore the chip while the quiet system "
          "stayed clean; the accept settled it and the rollback then "
          "refused; the second upgrade rolled back cleanly and the import "
          "stayed in the estate")


def _grow_pack(db_path: str, slug: str, names: list[str]) -> None:
    """Patch the solution's pack_json the authoring way - the SAME move
    smoke_v107 uses (the authoring API packs FROM the estate; growing an
    already-packed solution needs the row itself)."""
    os.environ["PY8N_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["PY8N_EXECUTION_MODE"] = "inline"
    sys.path.insert(0, BACKEND)
    import asyncio

    from sqlalchemy import select

    from app.db import AsyncSessionLocal
    from app.models import Solution

    async def _grow():
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(Solution).where(Solution.slug == slug))).scalar_one()
            row.pack_json = {
                "format": "py8n-pack", "pack_version": 1,
                "workflows": [
                    {"name": name, "description": "",
                     "graph": {"nodes": [{"id": "t1", "type": "manual_trigger",
                                          "name": "t1",
                                          "position": {"x": 0, "y": 0},
                                          "parameters": {}}], "edges": []}}
                    for name in names],
                "datasets": []}
            await session.commit()
    asyncio.run(_grow())


# ---------------------------------------------------------------------------
# 11) the paperwork sails - the chain report on the real envelope path
# ---------------------------------------------------------------------------
def phase_report(c: httpx.Client, h: dict, sink: SmtpDevSink) -> None:
    phase(11, "THE PAPERWORK SAILS (chain report -> real MIME attachment)")
    oh = h["oh"]
    r = c.put("/processes/chain-report", headers=oh, json={
        "enabled": True, "cadence_seconds": 3600,
        "to": "ops@audit.test, boss@audit.test", "history_limit": 5})
    assert r.status_code == 200, r.text
    sched = r.json()["schedule"]
    assert sched["recipient_count"] == 2, sched
    assert sched.get("next_due"), sched

    r = c.post("/scheduler/escalations/tick", headers=oh, json={})
    assert r.status_code == 200, r.text
    assert r.json()["chain_report"] == [], "not-yet-due must stay home"

    before = sink.count
    r = c.post("/processes/chain-report/send-now", headers=oh)
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["delivery"] == "delivered", result
    assert result["recipients"] == 2, result
    assert result.get("rides", 0) >= 1, result

    time.sleep(0.5)
    assert sink.count == before + 1, sink.count
    wire = sink.last()
    assert wire["to"] == ["ops@audit.test", "boss@audit.test"], wire["to"]
    import email as email_mod
    import email.policy

    msg = email_mod.message_from_bytes(wire["data"],
                                       policy=email_mod.policy.default)
    atts = [p.get_filename() for p in msg.iter_parts() if p.get_filename()]
    assert any(a and a.endswith(".csv") for a in atts), atts
    r = c.get("/processes/chain-report", headers=oh)
    assert r.status_code == 200, r.text
    last = r.json().get("schedule", {}).get("last_result") or {}
    assert last.get("delivery") == "delivered", last
    ok(11, "the schedule holds two recipients, the not-due tick left it "
          "home, send-now delivered ONE envelope to BOTH names on the real "
          f"wire with the CSV attachment ({atts[0]}), and the schedule row "
          "wears the receipt")


# ---------------------------------------------------------------------------
# 12) the census - every API family answers
# ---------------------------------------------------------------------------
CENSUS = [
    "/agents", "/artifacts", "/builder/systems", "/catalog", "/deployments",
    "/events", "/executions", "/insights", "/keys", "/model-systems",
    "/models", "/notifications", "/operators", "/platform", "/processes",
    "/reports", "/solutions", "/storage", "/systems", "/tags", "/templates",
    "/datasets", "/dashboards", "/workflows", "/credentials", "/folders",
    "/env-vars", "/schedules", "/apps", "/channels/adapters",
    "/documents/engines", "/media/sessions", "/voice/agents",
    "/interactions/channels", "/ai-composer/catalog",
    "/observability/overview", "/ops/overview", "/executions/queue",
    "/voice/queues", "/settings/retention",
]


def phase_census(c: httpx.Client, h: dict) -> int:
    phase(12, f"THE CENSUS (every API family answers - {len(CENSUS)} roots)")
    oh = h["oh"]
    bad = []
    for path in CENSUS:
        try:
            r = c.get(path, headers=oh)
            if r.status_code != 200:
                bad.append((path, r.status_code))
        except Exception as exc:  # noqa: BLE001
            bad.append((path, repr(exc)))
    assert not bad, f"families not answering 200: {bad}"
    ok(12, f"all {len(CENSUS)} API family roots answered 200 on the live "
           f"server - nothing broken, nothing hiding")
    return len(CENSUS)


# ---------------------------------------------------------------------------
# 13) the wire listens - the live tail on the real WebSocket
# ---------------------------------------------------------------------------
def phase_wire(c: httpx.Client, h: dict, fd: dict, token: str) -> None:
    phase(13, "THE WIRE LISTENS (WS /events/stream - business.stuck arrives "
              "live)")
    from websockets.sync.client import connect as ws_connect

    ws_url = (f"ws://127.0.0.1:{SERVER_PORT}/api/v1/events/stream"
              f"?token={token}")
    frames = []
    with ws_connect(ws_url, close_timeout=2) as ws:
        # a fresh late row on the REAL clock, then the sweep - the tail
        # must hear the breach without anybody polling
        r = c.post(f"/processes/{fd['id']}/instances", headers=h["oh"], json={
            "ref": "K-3", "title": "the wire visitor", "due_in_seconds": 1})
        assert r.status_code == 201, r.text
        time.sleep(2.0)
        c.post("/scheduler/escalations/tick", headers=h["oh"], json={})
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                raw = ws.recv(timeout=3)
            except Exception:
                break
            frames.append(raw if isinstance(raw, str) else raw.decode())
            if "stuck" in frames[-1]:
                break
    assert frames, "the tail never spoke"
    assert any("stuck" in f for f in frames), frames[-3:]
    ok(13, f"the live tail delivered business.stuck over the real WebSocket "
           f"({len(frames)} frame(s) read) - the boards can watch the door "
           f"without polling")


# ---------------------------------------------------------------------------
def main() -> int:
    db_path = f"{BACKEND}/data/audit_e2e_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",  # the audit drives the clock
        "PY8N_DEPLOY_PING_OVERRIDE":
            f"http://127.0.0.1:{SERVER_PORT}/api/v1/health",
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/audit_e2e_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER], cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT)
    sink = SmtpDevSink(port=0)
    sink.start()
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.111.0", version
            print(f"=== AUDIT E2E v107 on {API} (server 1.111.0, sink :{sink.port}) ===")

            h = phase_identity(c)
            r = c.post("/systems", headers=h["oh"],
                       json={"name": "Audit quiet ops"})
            assert r.status_code == 201, r.text
            quiet_sid = r.json()["id"]

            data = phase_data(c, h)
            phase_builders(c, h)
            ops = phase_operators(c, h)
            phase_revenue_chain(c, h, ops["procs"])
            esc = phase_escalations(c, h, sink)
            team = phase_teams(c, h, data["dataset"], esc["front_desk"])
            domain = phase_deploy(c, h, team["sid"], esc["front_desk"]["id"])
            phase_keys(c, h, team["sid"], esc["front_desk"], esc["calm_iid"])
            phase_updates(c, h, db_path, quiet_sid)
            phase_report(c, h, sink)
            phase_census(c, h)
            phase_wire(c, h, esc["front_desk"], h["users"]["owner"]["token"])

            print("=== AUDIT E2E OK - 13 phases, the whole product walked "
                  "end to end on the real wire ===")
            return 0
    finally:
        sink.stop()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
