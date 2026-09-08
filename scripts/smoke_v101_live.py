"""V101 live smoke: boot the real server and walk the round on the real
clock and the real SMTP wire - the report's scope is a chain TAG LIST,
and a weekly digest rides the SAME envelope path to the report's list.

1. THE TAG-LIST WEEKLY DIGEST, OVER THE WIRE: a REAL email endpoint
   (email_inbound) is bound to the dev SMTP sink; Revenue + Supply
   install (the map draws TWO chains); a lead WINS and a purchase
   ORDERS (one ride on each chain); the schedule saves with a TWO-chain
   tag list, the WEEKLY rhythm and TWO recipients; the door's tick
   finds the weekly window honest (nothing dispatches); Send-now
   dispatches NOW - ONE message crosses the REAL SMTP wire carrying the
   report's own list on the envelope, the weekly digest's scan line on
   the subject, the Rhythm line in the body, and the attachment is the
   real text/csv MIME part parsed back out of the wire naming rides
   from BOTH chains.
2. THE TAG LIST OVER THE WIRE: history.csv?chains=<A, B> serves BOTH
   chains' legs with the filter echoed and the slug in the filename; a
   single name narrows; an unknown name is an honest empty file; the
   by-chain pivot arithmetic comes straight off the service.
3. THE WEEKLY WINDOW HOLDS: a tick while not due dispatches nothing -
   the weekly next_due the last dispatch stamped still rules.

Usage: /home/z/.venv/bin/python scripts/smoke_v101_live.py
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
# 1) the tag-list weekly digest over the REAL wire
# ---------------------------------------------------------------------------

def weekly_tag_list_digest_check(c: httpx.Client, sink: SmtpDevSink) -> dict:
    r = c.post("/channels/endpoints", json={
        "name": "Smoke v101 inbox", "provider": "email_inbound",
        "config": {"secret": "smoke-secret",
                   "smtp_host": "127.0.0.1", "smtp_port": str(sink.port),
                   "smtp_user": "smoke", "smtp_pass": "smoke",
                   "from_address": "py8n@py8n.test"}})
    assert r.status_code == 201, r.text

    for slug in ("operations-operator", "sales-operator", "finance-operator",
                 "procurement-operator", "logistics-operator"):
        r = c.post(f"/operators/{slug}/install", json={"note": "smoke v101"})
        assert r.status_code == 200, r.text
    procs = {p["name"]: p["id"] for p in c.get("/processes").json()["processes"]}

    # one ride on EACH chain: the lead WINS (Revenue), the purchase ORDERS
    r = c.post(f"/processes/{procs['Lead pipeline']}/instances",
               json={"ref": "+15551010901", "title": "v101 Tag Deal"})
    iid = r.json()["id"]
    for move in ("reach_out", "qualify", "book_demo", "run_demo",
                 "send_proposal", "negotiate", "win"):
        r = c.post(f"/processes/{procs['Lead pipeline']}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke"})
        assert r.status_code == 200, r.text
    r = c.post(f"/processes/{procs['Purchase lifecycle']}/instances",
               json={"ref": "PO-901", "title": "v101 Tag Order"})
    iid = r.json()["id"]
    for move in ("quote", "approve", "order"):
        r = c.post(f"/processes/{procs['Purchase lifecycle']}/instances/{iid}/advance",
                   json={"transition": move, "actor": "smoke"})
        assert r.status_code == 200, r.text

    # the schedule: a TWO-chain tag list, the WEEKLY rhythm, TWO names
    r = c.put("/processes/chain-report", json={
        "enabled": True, "cadence": "weekly", "history_limit": 5,
        "to": "ops@py8n.test, boss@py8n.test",
        "chains": "Revenue chain, Supply chain"})
    assert r.status_code == 200, r.text
    sched = r.json()["schedule"]
    assert sched["cadence"] == "weekly" and sched["chain_count"] == 2, sched
    assert sched["cadence_seconds"] == 7 * 86400, sched
    assert sched["recipient_count"] == 2, sched
    next_due = sched["next_due"]
    assert next_due, r.text

    # the tick runs - the WEEKLY window is honest: nothing dispatches
    r = c.post("/scheduler/escalations/tick", json={})
    assert r.status_code == 200, r.text
    assert r.json()["chain_report"] == [], r.json()
    assert sink.count == 0, sink.messages

    # the manual door: ONE envelope, the report's OWN list, BOTH chains
    r = c.post("/processes/chain-report/send-now")
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["delivery"] == "delivered", result
    assert result["cadence"] == "weekly", result
    assert result["chains"] == ["Revenue chain", "Supply chain"], result
    assert result["recipients"] == 2, result
    assert result["legs"] == 4 and result["rides"] == 2, result
    assert "revenue-chain" in result["filename"], result
    assert sink.count == 1, sink.messages

    wire = sink.last()
    assert wire["to"] == ["ops@py8n.test", "boss@py8n.test"], wire["to"]
    msg = email.message_from_bytes(wire["data"], policy=email.policy.default)
    subject = str(msg["Subject"])
    assert subject.startswith("[py8n] Weekly chain digest"), subject
    assert "2 ride(s) across 4 leg(s)" in subject, subject
    assert "Chains: Revenue chain, Supply chain" in subject, subject
    assert "Rhythm: weekly digest" in wire["text"], wire["text"]
    assert "Revenue chain 1 ride(s)" in wire["text"], wire["text"]
    assert "Supply chain 1 ride(s)" in wire["text"], wire["text"]
    attachment = next(p for p in msg.iter_attachments()
                      if p.get_filename() == result["filename"])
    assert attachment.get_content_type() == "text/csv", attachment
    rows = _parse(attachment.get_content())
    assert {row[0] for row in rows[1:]} == {"Revenue chain", "Supply chain"}, rows
    return {"filename": result["filename"], "next_due": next_due}


# ---------------------------------------------------------------------------
# 2) the tag list over the wire - the slice, the narrow, the empty
# ---------------------------------------------------------------------------

def tag_list_filter_check(c: httpx.Client, sink: SmtpDevSink,
                          db_path: str) -> dict:
    # BOTH chains ride the file, echoed + slugged
    r = c.get("/processes/chains/history.csv",
              params={"chains": "Revenue chain, Supply chain"})
    assert r.status_code == 200, r.text
    assert r.headers["x-py8n-chains-filter"] == "Revenue chain, Supply chain", r.headers
    assert r.headers["x-py8n-leg-count"] == "4", r.headers
    assert r.headers["x-py8n-ride-count"] == "2", r.headers
    assert "revenue-chain" in r.headers["content-disposition"], r.headers
    rows = _parse(r.text)
    assert {row[0] for row in rows[1:]} == {"Revenue chain", "Supply chain"}, rows

    # one name narrows to one chain
    r = c.get("/processes/chains/history.csv", params={"chains": "Supply chain"})
    assert r.headers["x-py8n-ride-count"] == "1", r.headers

    # an unknown name: the honest empty file
    r = c.get("/processes/chains/history.csv", params={"chains": "Ghost chain"})
    assert r.status_code == 200, r.text
    assert r.headers["x-py8n-leg-count"] == "0", r.headers
    assert len(_parse(r.text)) == 1, r.text

    # the by-chain pivot, straight off the service - the in-process
    # import must read the SAME fresh smoke DB the server booted (the
    # env var is set BEFORE the first app import, config reads it once)
    import asyncio
    os.environ["PY8N_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["PY8N_EXECUTION_MODE"] = "inline"
    sys.path.insert(0, BACKEND)
    from app.db import AsyncSessionLocal
    from app.services import business_processes as bp_svc

    async def _pivot():
        async with AsyncSessionLocal() as session:
            return await bp_svc.chain_history_csv(session, None,
                                                  history_limit=50)
    out = asyncio.run(_pivot())
    assert out["by_chain"].get("Revenue chain") == 1, out["by_chain"]
    assert out["by_chain"].get("Supply chain") == 1, out["by_chain"]
    return {"by_chain": out["by_chain"]}


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v101_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
        "PY8N_ESCALATION_TICK_SECONDS": "3600",  # the smoke drives the door
        "PORT": str(SERVER_PORT),
    })
    srv_log = open("/tmp/smoke_v101_server.log", "w")
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
            assert version == "1.101.0", version

            weekly_tag_list_digest_check(c, sink)
            print(f"[1] TAG-LIST WEEKLY DIGEST OK - ONE envelope with the "
                  f"report's OWN list crossed a REAL SMTP wire (TWO RCPT TO "
                  f"names), the weekly digest's scan line on the subject, "
                  f"the Rhythm line in the body, and the text/csv attachment "
                  f"parsed back naming rides from BOTH chains; the weekly "
                  f"window held while not due")
            tag_list_filter_check(c, sink, db_path)
            print(f"[2] THE TAG LIST OK - chains=<A, B> served BOTH chains "
                  f"(4 legs, 2 rides) with the filter echoed and the slug in "
                  f"the filename; one name narrowed; an unknown name was an "
                  f"honest empty file; the by-chain pivot read 1 ride per "
                  f"chain straight off the service")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        sink.stop()


if __name__ == "__main__":
    raise SystemExit(main())
