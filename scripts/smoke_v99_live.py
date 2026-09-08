"""V99 live smoke: boot the real server and walk the round on the real
clock and the real SMTP wire - the chain-history FILE rides the mail.

1. THE CHAIN REPORT, END TO END OVER THE REAL WIRE: a REAL email
   endpoint (email_inbound) is bound to the dev SMTP sink; Sales +
   Operations + Finance install; a fresh lead WINS (leg 1 rides); the
   schedule saves (cadence 3600s - the window holds); the door's tick
   runs and the chain_report list stays EMPTY (the window is honest on
   the real clock); Send-now dispatches NOW - the file crosses the REAL
   SMTP wire as a REAL MIME attachment: multipart/mixed, the body
   naming the shape, the attachment named
   py8n-chain-history-YYYYMMDD.csv, text/csv, and the CSV content
   parsed back out of the wire naming the ride (the ref that threaded
   the chain). The schedule row stamps delivered + the window advances.
2. THE WINDOW ON THE REAL DOOR: a second tick finds the schedule NOT
   due (the send-now consumed nothing - but the first dispatch moved
   next_due an hour out) and the chain_report list stays empty; the
   manual door still works (a real send is a real send).
3. THE PLOT'S FILTERS OVER THE WIRE: chain=Revenue chain serves 2 legs
   with the filter echoed in the headers and the chain's slug in the
   filename; the leg filter serves ONE leg; an unknown chain is an
   honest empty file - header only, never a 404.

Usage: /home/z/.venv/bin/python scripts/smoke_v99_live.py
"""

from __future__ import annotations

import csv
import email
import email.policy
import io
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


def _parse(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


# ---------------------------------------------------------------------------
# 1+2) the file rides the REAL wire; the window holds on the real door
# ---------------------------------------------------------------------------

def chain_report_check(c: httpx.Client, sink: SmtpDevSink) -> dict:
    r = c.post("/channels/endpoints", json={
        "name": "Smoke v99 inbox", "provider": "email_inbound",
        "config": {"secret": "smoke-secret",
                   "smtp_host": "127.0.0.1", "smtp_port": str(sink.port),
                   "smtp_user": "smoke", "smtp_pass": "smoke",
                   "from_address": "py8n@py8n.test"}})
    assert r.status_code == 201, r.text

    for slug in ("operations-operator", "sales-operator", "finance-operator"):
        r = c.post(f"/operators/{slug}/install", json={"note": "smoke v99"})
        assert r.status_code == 200, r.text
    procs = {p["name"]: p["id"] for p in c.get("/processes").json()["processes"]}
    lead_pid = procs["Lead pipeline"]
    ref = "+15559997001"
    r = c.post(f"/processes/{lead_pid}/instances",
               json={"ref": ref, "title": "v99 Report Deal"})
    iid = r.json()["id"]
    for move in ("reach_out", "qualify", "book_demo", "run_demo",
                 "send_proposal", "negotiate", "win"):
        r = c.post(f"/processes/{lead_pid}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke"})
        assert r.status_code == 200, r.text

    # the schedule saves; the window anchors an hour out
    r = c.put("/processes/chain-report", json={
        "enabled": True, "cadence_seconds": 3600,
        "to": "ops@py8n.test", "history_limit": 5, "chain": ""})
    assert r.status_code == 200, r.text
    next_due = r.json()["schedule"]["next_due"]
    assert next_due, r.text

    # the door's tick runs - the schedule is NOT due: the file stays home
    r = c.post("/scheduler/escalations/tick", json={})
    assert r.status_code == 200, r.text
    assert r.json()["chain_report"] == [], r.json()
    assert sink.count == 0, sink.messages

    # the manual door: one dispatch NOW - over the REAL SMTP wire
    r = c.post("/processes/chain-report/send-now")
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["delivery"] == "delivered", result
    assert result["legs"] == 2 and result["rides"] == 1, result
    assert result["filename"].startswith("py8n-chain-history-"), result
    assert sink.count == 1, sink.messages

    # the wire's message: multipart/mixed, the CSV as a REAL MIME part
    raw = sink.last()["data"]
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert msg.is_multipart(), msg.get_content_type()
    assert "[py8n] Chain history - 1 ride(s) across 2 leg(s)" in str(msg["Subject"]), msg["Subject"]
    attachment = None
    for part in msg.iter_attachments():
        if part.get_filename() == result["filename"]:
            attachment = part
            break
    assert attachment is not None, [p.get_filename() for p in msg.iter_attachments()]
    assert attachment.get_content_type() == "text/csv", attachment.get_content_type()
    csv_text = attachment.get_content()
    rows = _parse(csv_text)
    assert rows[0][0] == "chain" and rows[0][1] == "leg_from", rows[0]
    won = next(x for x in rows[1:] if x[1] == "Lead pipeline")
    assert won[2] == "won" and won[9] == ref, won  # the ride named on the wire

    # the schedule row stamps the delivery; the window advanced
    sched = c.get("/processes/chain-report").json()["schedule"]
    assert sched["last_result"]["delivery"] == "delivered", sched
    assert sched["last_result"]["filename"] == result["filename"], sched
    assert sched["next_due"] > next_due, (sched["next_due"], next_due)

    # a second tick: the window STILL holds on the real door
    r = c.post("/scheduler/escalations/tick", json={})
    assert r.json()["chain_report"] == [], r.json()
    assert sink.count == 1, sink.messages

    # the dispatch is on the event door
    evs = c.get("/events", params={"type": "business.chain_report_dispatched"}).json()
    mine = [e for e in evs["events"] if e["payload"]["filename"] == result["filename"]]
    assert mine and mine[0]["payload"]["delivery"] == "delivered", evs
    return {"filename": result["filename"], "rides": result["rides"],
            "legs": result["legs"]}


# ---------------------------------------------------------------------------
# 3) the plot's filters over the wire
# ---------------------------------------------------------------------------

def filter_check(c: httpx.Client) -> dict:
    # the chain filter: one chain, echoed in headers + filename
    r = c.get("/processes/chains/history.csv", params={"chain": "Revenue chain"})
    assert r.status_code == 200, r.text
    assert r.headers["x-py8n-chain-filter"] == "Revenue chain", r.headers
    assert r.headers["x-py8n-leg-count"] == "2", r.headers
    assert "revenue-chain" in r.headers["content-disposition"], r.headers

    # the leg filter: ONE leg
    r = c.get("/processes/chains/history.csv",
              params={"leg": "Lead pipeline|won"})
    assert r.headers["x-py8n-leg-filter"] == "Lead pipeline|won", r.headers
    assert r.headers["x-py8n-leg-count"] == "1", r.headers
    assert r.headers["x-py8n-ride-count"] == "1", r.headers

    # an unknown chain: the honest empty file, never a 404
    r = c.get("/processes/chains/history.csv", params={"chain": "Ghost chain"})
    assert r.status_code == 200, r.text
    assert r.headers["x-py8n-leg-count"] == "0", r.headers
    assert len(_parse(r.text)) == 1, r.text
    return {"chain_legs": 2, "leg_legs": 1, "ghost_legs": 0}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v99_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",  # the smoke drives the door
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v99_server.log", "w")
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
            assert version == "1.99.0", version

            chain_report_check(c, sink)
            print(f"[1] CHAIN REPORT OVER THE REAL WIRE OK - the file crossed "
                  f"a REAL SMTP wire as a REAL MIME attachment (multipart/mixed, "
                  f"the scan line on the subject, text/csv named part); the "
                  f"attachment parsed back out of the wire names the ride; the "
                  f"schedule row stamps delivered and the window advanced; the "
                  f"tick while not due dispatched NOTHING")
            print(f"[2] THE WINDOW ON THE REAL DOOR OK - the second tick found "
                  f"the schedule not due (chain_report empty, sink count "
                  f"unchanged) - next_due advanced by the first dispatch holds")
            filter_check(c)
            print(f"[3] THE PLOT'S FILTERS OK - chain=Revenue chain served 2 "
                  f"legs with the filter echoed in the headers and the slug in "
                  f"the filename; the leg filter served ONE leg; an unknown "
                  f"chain was an honest empty file (header only, 200)")
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
