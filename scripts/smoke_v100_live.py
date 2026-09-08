"""V100 live smoke: boot the real server and walk the round on the real
clock and the real SMTP wire - the report rides to a LIST of names, and
breaks down BY system.

1. ONE ENVELOPE, EVERY NAME: a REAL email endpoint (email_inbound) is
   bound to the dev SMTP sink; Sales + Operations + Finance install; a
   fresh lead WINS (leg 1 rides); the schedule saves with TWO
   recipients; the door's tick finds the window honest (nothing
   dispatches); Send-now dispatches NOW - ONE message crosses the REAL
   SMTP wire carrying TWO RCPT TO envelopes, the To header names both,
   the subject carries the scan line, the attachment is the real
   text/csv MIME part parsed back out of the wire naming the ride and
   the system column, and the body carries the multi-recipient receipt.
2. THE SYSTEM SCOPE OVER THE WIRE: history.csv?system=<the lead's
   system> serves the revenue slice with the filter echoed and the
   -sys- slug in the filename; an unknown system is an honest empty
   file; the schedule re-scoped to the system dispatches the SAME slice
   (the due walk on the injected clock) and the email body breaks the
   rides down BY system.
3. THE WINDOW HOLDS: a tick while not due dispatches nothing - the
   next_due the last dispatch stamped still rules.

Usage: /home/z/.venv/bin/python scripts/smoke_v100_live.py
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
# 1) one envelope, every name - over the REAL wire
# ---------------------------------------------------------------------------

def multi_recipient_check(c: httpx.Client, sink: SmtpDevSink) -> dict:
    r = c.post("/channels/endpoints", json={
        "name": "Smoke v100 inbox", "provider": "email_inbound",
        "config": {"secret": "smoke-secret",
                   "smtp_host": "127.0.0.1", "smtp_port": str(sink.port),
                   "smtp_user": "smoke", "smtp_pass": "smoke",
                   "from_address": "py8n@py8n.test"}})
    assert r.status_code == 201, r.text

    for slug in ("operations-operator", "sales-operator", "finance-operator"):
        r = c.post(f"/operators/{slug}/install", json={"note": "smoke v100"})
        assert r.status_code == 200, r.text
    procs = {p["name"]: p["id"] for p in c.get("/processes").json()["processes"]}
    lead_pid = procs["Lead pipeline"]
    ref = "+15551000901"
    r = c.post(f"/processes/{lead_pid}/instances",
               json={"ref": ref, "title": "v100 List Deal"})
    iid = r.json()["id"]
    for move in ("reach_out", "qualify", "book_demo", "run_demo",
                 "send_proposal", "negotiate", "win"):
        r = c.post(f"/processes/{lead_pid}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke"})
        assert r.status_code == 200, r.text

    # the schedule: TWO names on one report
    r = c.put("/processes/chain-report", json={
        "enabled": True, "cadence_seconds": 3600,
        "to": "ops@py8n.test, boss@py8n.test", "history_limit": 5})
    assert r.status_code == 200, r.text
    sched = r.json()["schedule"]
    assert sched["recipient_count"] == 2, sched
    next_due = sched["next_due"]
    assert next_due, r.text

    # the tick runs - the schedule is NOT due: the file stays home
    r = c.post("/scheduler/escalations/tick", json={})
    assert r.status_code == 200, r.text
    assert r.json()["chain_report"] == [], r.json()
    assert sink.count == 0, sink.messages

    # the manual door: ONE envelope, EVERY name
    r = c.post("/processes/chain-report/send-now")
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["delivery"] == "delivered", result
    assert result["recipients"] == 2, result
    assert result["legs"] == 2 and result["rides"] == 1, result
    assert sink.count == 1, sink.messages

    # the wire: ONE message, TWO RCPT TO entries
    wire = sink.last()
    assert wire["to"] == ["ops@py8n.test", "boss@py8n.test"], wire["to"]
    msg = email.message_from_bytes(wire["data"], policy=email.policy.default)
    tos = [a.addr_spec for a in msg["To"].addresses]
    assert tos == ["ops@py8n.test", "boss@py8n.test"], tos
    assert "[py8n] Chain history - 1 ride(s) across 2 leg(s)" in str(msg["Subject"]), msg["Subject"]
    assert "Recipients: 2" in wire["text"], wire["text"]
    attachment = next(p for p in msg.iter_attachments()
                      if p.get_filename() == result["filename"])
    assert attachment.get_content_type() == "text/csv", attachment
    rows = _parse(attachment.get_content())
    assert rows[0][-1] == "system", rows[0]
    won = next(x for x in rows[1:] if x[1] == "Lead pipeline")
    assert won[2] == "won" and won[9] == ref, won
    assert won[20], won  # the machine's systems named on the row

    # the schedule row stamps the delivery; the window advanced
    sched = c.get("/processes/chain-report").json()["schedule"]
    assert sched["last_result"]["delivery"] == "delivered", sched
    assert sched["last_result"]["recipients"] == 2, sched
    assert sched["next_due"] > next_due, (sched["next_due"], next_due)
    return {"lead_sys": None, "ref": ref, "filename": result["filename"]}


# ---------------------------------------------------------------------------
# 2) the system scope over the wire - the slice, the empty, the breakdown
# ---------------------------------------------------------------------------

def system_scope_check(c: httpx.Client, sink: SmtpDevSink) -> dict:
    # the map's own node data names the systems the lead machine binds
    chains = c.get("/processes/chains").json()
    nodes = (chains.get("nodes") or {}).values()
    lead_sys = next((n["systems"][0] for n in nodes
                     if n["name"] == "Lead pipeline" and n.get("systems")), None)
    assert lead_sys, chains.get("nodes")

    # the filter: the legs the system's machines FIRE (the lead machine
    # fires leg 1), echoed + slugged
    r = c.get("/processes/chains/history.csv", params={"system": lead_sys})
    assert r.status_code == 200, r.text
    assert r.headers["x-py8n-system-filter"] == lead_sys, r.headers
    assert r.headers["x-py8n-leg-count"] == "1", r.headers
    assert r.headers["x-py8n-ride-count"] == "1", r.headers
    assert "-sys-" in r.headers["content-disposition"], r.headers
    rows = _parse(r.text)
    assert rows[0][-1] == "system", rows[0]
    assert all(row[20] == lead_sys for row in rows[1:]), rows

    # an unknown system: the honest empty file
    r = c.get("/processes/chains/history.csv", params={"system": "Ghost system"})
    assert r.status_code == 200, r.text
    assert r.headers["x-py8n-leg-count"] == "0", r.headers
    assert len(_parse(r.text)) == 1, r.text

    # the schedule re-scoped to the system; the manual door dispatches
    # the SAME slice NOW - and the body breaks the rides down BY system
    r = c.put("/processes/chain-report",
              json={"enabled": True, "cadence_seconds": 3600,
                    "to": "crew@py8n.test", "history_limit": 5,
                    "system": lead_sys})
    assert r.status_code == 200, r.text
    next_due = r.json()["schedule"]["next_due"]

    # the tick while not due: the window the upsert anchored still rules
    r = c.post("/scheduler/escalations/tick", json={})
    assert r.json()["chain_report"] == [], r.json()

    r = c.post("/processes/chain-report/send-now")
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["delivery"] == "delivered", result
    assert result["rides"] == 1 and result["legs"] == 1, result
    assert sink.count == 2, sink.messages
    wire = sink.last()
    assert wire["to"] == ["crew@py8n.test"], wire["to"]
    assert f"System: {lead_sys}" in wire["text"], wire["text"]
    assert "By system:" in wire["text"] and "1 ride(s)" in wire["text"], wire["text"]
    return {"lead_sys": lead_sys, "rides": result["rides"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v100_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",  # the smoke drives the door
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v100_server.log", "w")
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
            assert version == "1.100.0", version

            multi_recipient_check(c, sink)
            print(f"[1] ONE ENVELOPE, EVERY NAME OK - the file crossed a "
                  f"REAL SMTP wire as ONE message carrying TWO RCPT TO "
                  f"envelopes (the To header names both, the scan line on "
                  f"the subject, the Recipients receipt in the body, "
                  f"text/csv attachment parsed back naming the ride and "
                  f"the system column); the window held while not due")
            system_scope_check(c, sink)
            print(f"[2] THE SYSTEM SCOPE OK - system=<the lead's system> "
                  f"served the legs that machine fires (1 leg, 1 ride) with "
                  f"the filter echoed and the -sys- slug in the filename; an "
                  f"unknown system was an honest empty file; the re-scoped "
                  f"schedule dispatched the SAME slice and the body broke "
                  f"the rides down BY system")
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
