"""V98 live smoke: boot the real server and walk the round on the real
clock - the machine board's receipt and the chain history leaving the
building as a file.

1. THE PER-LEG CHAIN-HISTORY CSV, END TO END: Sales + Operations +
   Finance install; TWO fresh leads WIN (leg 1 rides twice) and one
   onboarding case walks to hand_off (leg 2 rides once); GET
   /processes/chains/history.csv serves the SAME map the operator-detail
   chain draws as rows - Content-Type text/csv, Content-Disposition
   attachment, X-Py8n-Leg-Count / X-Py8n-Ride-Count naming the shape,
   the ride rows carrying the ref that threads the chain, the leg SLA,
   due_at, is_stuck. The clamp honors the window over the wire:
   history_limit=1 keeps one ride per leg and the truncated flag says
   so - the deepest window is one query param away.
2. THE MACHINE BOARD'S RECEIPT (the v98 parity wire): a knock machine
   goes past its SLA on the real clock; the door is driven by the REAL
   scheduler endpoint; the handler acks WITH a reschedule through the
   SAME POST .../escalations/ack the board's form posts - the receipt
   carries reschedule_at, both clocks together refuse 400, and the
   instance context's own book carries acked.reschedule_at: the exact
   field the board's new loan chip reads.
3. THE SIDE LEGS ARE HONEST ON THE REAL SERVER: a Hub machine firing
   two journeys walks its FIRST out-leg into a chain; the second leg -
   real history, real rides - ships under the "side legs" group in the
   map and the CSV names its chain on every row. Nothing vanishes.

Usage: /home/z/.venv/bin/python scripts/smoke_v98_live.py
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
SMOKE_SERVER = "/home/z/my-project/py8n/scripts/smoke_v74_server.py"
PY = ("/home/z/.venv/bin/python"
      if os.path.exists("/home/z/.venv/bin/python") else "python3")


def _free_port() -> int:
    """A fresh port per run - a zombie server from an earlier smoke must
    never answer for THIS one."""
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
    sandbox; the deadline has to outlive the imports."""
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


HEADER = ["chain", "leg_from", "leg_on_state", "leg_to", "leg_sla_seconds",
          "leg_opened", "leg_open_now", "leg_stuck", "history_truncated",
          "ref", "title", "state", "opened_at", "due_at", "is_stuck",
          "overdue_seconds", "acked_by", "snooze_remaining_seconds",
          "instance_id", "process_id"]


def _parse(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


# ---------------------------------------------------------------------------
# 1) the CSV export - the chain history leaves the building
# ---------------------------------------------------------------------------

def csv_export_check(c: httpx.Client) -> dict:
    for slug in ("operations-operator", "sales-operator", "finance-operator"):
        r = c.post(f"/operators/{slug}/install", json={"note": "smoke v98"})
        assert r.status_code == 200, r.text

    procs = {p["name"]: p["id"] for p in c.get("/processes").json()["processes"]}
    lead_pid = procs["Lead pipeline"]
    onboard_pid = procs["Customer onboarding"]

    refs = ("+15559878001", "+15559878002")
    onboard_iid = None
    for ref in refs:
        r = c.post(f"/processes/{lead_pid}/instances",
                   json={"ref": ref, "title": f"v98 Deal {ref[-2:]}"})
        iid = r.json()["id"]
        for move in ("reach_out", "qualify", "book_demo", "run_demo",
                     "send_proposal", "negotiate", "win"):
            r = c.post(f"/processes/{lead_pid}/instances/{iid}/advance",
                       json={"transition": move, "actor": "smoke"})
            assert r.status_code == 200, r.text
        if onboard_iid is None:
            # the first win opened the case (the source's ref carries over);
            # find it on the ONBOARDING roster and walk it to the hand-off
            rows = c.get(f"/processes/{onboard_pid}/instances").json()["instances"]
            onboard_iid = next(i["id"] for i in rows if i["ref"] == ref)
            for move in ("provision", "train", "go_live", "hand_off"):
                r = c.post(
                    f"/processes/{onboard_pid}/instances/{onboard_iid}/advance",
                    json={"transition": move, "actor": "smoke"})
                assert r.status_code == 200, r.text

    r = c.get("/processes/chains/history.csv")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv"), r.headers
    assert "attachment" in r.headers.get("content-disposition", ""), r.headers
    assert r.headers["x-py8n-leg-count"] == "2", r.headers
    assert r.headers["x-py8n-ride-count"] == "3", r.headers
    rows = _parse(r.text)
    assert rows[0] == HEADER, rows[0]
    won = next(x for x in rows[1:] if x[1] == "Lead pipeline")
    assert won[0] == "Revenue chain" and won[2] == "won", won
    assert won[5] == "2", won                      # both leads rode this leg
    assert won[9] in refs, won                     # a ride's ref named on its row
    billed = next(x for x in rows[1:] if x[1] == "Customer onboarding")
    assert billed[2] == "handed_off" and billed[3] == "Invoice lifecycle", billed
    assert billed[4] == str(5 * 24 * 3600), billed  # the leg's own SLA seconds
    assert billed[10].startswith("Billing - "), billed
    assert billed[13] != "", billed                # the SLA promise: a real due_at

    # the clamp honors the window over the wire: depth 1 keeps ONE ride
    # per leg and the truncated flag names it
    r = c.get("/processes/chains/history.csv", params={"history_limit": 1})
    assert r.headers["x-py8n-ride-count"] == "2", r.headers
    rows1 = _parse(r.text)
    won1 = next(x for x in rows1[1:] if x[1] == "Lead pipeline")
    assert won1[8] == "yes", won1                  # history_truncated - honesty in the file
    return {"ride_rows": r.headers["x-py8n-ride-count"],
            "legs": r.headers["x-py8n-leg-count"],
            "threaded_ref": billed[9]}


# ---------------------------------------------------------------------------
# 2) the machine board's receipt - the v98 parity wire
# ---------------------------------------------------------------------------

def board_receipt_check(c: httpx.Client) -> dict:
    r = c.post("/processes", json={
        "name": "Smoke v98 board machine",
        "definition": {"states": ["a", "b", "done"], "initial": "a",
                       "transitions": [
                           {"name": "go", "from": "a", "to": "b"},
                           {"name": "finish", "from": "b", "to": "done"}]}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = c.post(f"/processes/{pid}/instances",
               json={"ref": "BOARD-98", "title": "the board's own",
                     "due_in_seconds": 1})
    iid = r.json()["id"]

    time.sleep(1.2)  # the real clock passes the 1s SLA
    report = c.post("/scheduler/escalations/tick", json={}).json()
    assert report["stuck"] >= 1, report

    # the machine board's form posts THE SAME endpoint, one clock per receipt
    r = c.post(f"/processes/{pid}/instances/{iid}/escalations/ack",
               json={"by": "smoke-board", "note": "on it",
                     "snooze_hours": 2, "reschedule_in_minutes": 30})
    assert r.status_code == 400, r.text  # the door refuses to guess

    r = c.post(f"/processes/{pid}/instances/{iid}/escalations/ack",
               json={"by": "smoke-board", "note": "on it",
                     "reschedule_in_minutes": 45})
    assert r.status_code == 200, r.text
    ack = r.json()["ack"]
    assert ack["by"] == "smoke-board" and ack["reschedule_at"], ack

    # the instance context's own book carries the receipt - the exact
    # field the board's new loan chip reads (loanRemaining on reschedule_at)
    inst = c.get(f"/processes/{pid}/instances").json()["instances"][0]
    book = (inst.get("context") or {}).get("escalations") or {}
    assert (book.get("acked") or {}).get("reschedule_at") == ack["reschedule_at"], book
    assert (book.get("acked") or {}).get("by") == "smoke-board", book
    return {"reschedule_at": ack["reschedule_at"]}


# ---------------------------------------------------------------------------
# 3) side legs - the second out-leg is history, never a drop
# ---------------------------------------------------------------------------

def side_legs_check(c: httpx.Client) -> dict:
    r = c.post("/processes", json={
        "name": "Smoke v98 Hub",
        "definition": {
            "states": ["start", "mid", "end", "closed"],
            "initial": "start",
            "transitions": [
                {"name": "go_mid", "from": "start", "to": "mid"},
                {"name": "go_end", "from": "mid", "to": "end"},
                {"name": "close", "from": "end", "to": "closed"}],
            "journeys": [
                {"on_state": "mid",
                 "open": {"process": "Smoke v98 Spoke A",
                          "title_template": "A - {title}"}},
                {"on_state": "end",
                 "open": {"process": "Smoke v98 Spoke B",
                          "title_template": "B - {title}"}}]}})
    assert r.status_code == 201, r.text
    for name in ("Smoke v98 Spoke A", "Smoke v98 Spoke B"):
        r = c.post("/processes", json={
            "name": name,
            "definition": {"states": ["a", "b", "done"], "initial": "a",
                           "transitions": [
                               {"name": "go", "from": "a", "to": "b"},
                               {"name": "finish", "from": "b", "to": "done"}]}})
        assert r.status_code == 201, r.text

    procs = {p["name"]: p["id"] for p in c.get("/processes").json()["processes"]}
    hub_pid = procs["Smoke v98 Hub"]
    r = c.post(f"/processes/{hub_pid}/instances",
               json={"ref": "HUB-98", "title": "the hub's ride"})
    iid = r.json()["id"]
    for move in ("go_mid", "go_end"):
        r = c.post(f"/processes/{hub_pid}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke"})
        assert r.status_code == 200, r.text

    r = c.get("/processes/chains").json()
    names = [ch["name"] for ch in r["chains"]]
    assert "side legs" in names, names
    side = next(ch for ch in r["chains"] if ch["name"] == "side legs")
    assert [(l["from_name"], l["on_state"], l["to_name"])
            for l in side["legs"]] == [
        ("Smoke v98 Hub", "end", "Smoke v98 Spoke B")], side
    assert side["legs"][0]["opened"] == 1, side

    r = c.get("/processes/chains/history.csv")
    rows = _parse(r.text)
    end_rows = [x for x in rows[1:] if x[2] == "end"]
    assert len(end_rows) == 1 and end_rows[0][0] == "side legs", end_rows
    assert end_rows[0][9] == "HUB-98", end_rows
    return {"side_leg_rides": side["legs"][0]["opened"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v98_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",  # the smoke drives the door
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v98_server.log", "w")
    proc = subprocess.Popen(
        [PY, SMOKE_SERVER],
        cwd=BACKEND, env=env,
        stdout=srv_log, stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=API, timeout=300) as c:
            wait_health(c)
            version = c.get("/health").json().get("version", "?")
            assert version == "1.98.0", version

            csv_export_check(c)
            print(f"[1] PER-LEG CHAIN-HISTORY CSV OK - both revenue legs "
                  f"served their rides as rows over the REAL server (3 rides, "
                  f"2 legs, the ref threading the chain named on every row, "
                  f"the leg's own SLA + due_at in their columns); depth 1 kept "
                  f"one ride per leg and the file says history_truncated")

            board_receipt_check(c)
            print(f"[2] MACHINE-BOARD RECEIPT OK - the knock machine went "
                  f"past its SLA on the real clock; the door was driven by "
                  f"the REAL scheduler endpoint; both clocks together "
                  f"refused 400; the ack-with-reschedule receipt landed and "
                  f"the instance context's own book carries "
                  f"acked.reschedule_at - the field the board's new chip reads")

            side_legs_check(c)
            print(f"[3] SIDE LEGS OK - the Hub's second out-leg shipped under "
                  f"the honest 'side legs' group with its ride in the map AND "
                  f"in the CSV, the chain named on the row - nothing vanishes")

            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
