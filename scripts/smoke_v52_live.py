"""V52 live smoke: boot the real server and verify the three feature areas E2E.

1. Registry exposes google_sheets_source + ftp_source with generated forms.
2. Storage migration: local -> real S3-compatible HTTP endpoint (moto_server)
   through POST /storage/migrate - copied, verified, idempotent re-run.
3. Report delivery: a run POSTs the envelope to a real local webhook sink
   (file base64-inline) and lands ok/skipped delivery events in the trail.

Usage: /home/z/.venv/bin/python scripts/smoke_v52_live.py
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
API = "http://127.0.0.1:8199/api/v1"
MOTO_PORT = 8200
SINK_PORT = 8201


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


class _Sink(BaseHTTPRequestHandler):
    captured: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        _Sink.captured.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"received": true}')

    def log_message(self, *args):  # silence
        pass


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v52_{uuid.uuid4().hex[:8]}.sqlite3"
    env = dict(os.environ)
    env.update({
        "PY8N_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "PY8N_EXECUTION_MODE": "inline",
        "PY8N_REQUIRE_AUTH": "false",
    })
    proc = subprocess.Popen(
        ["/home/z/.venv/bin/python", "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "8199", "--log-level", "warning"],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    moto = subprocess.Popen(
        ["/home/z/.venv/bin/python", "-m", "moto.server", "-p", str(MOTO_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sink = ThreadingHTTPServer(("127.0.0.1", SINK_PORT), _Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()

    checks = 0
    try:
        with httpx.Client(timeout=30.0) as client:
            wait_health(client)
            ver = client.get(f"{API}/health").json()["version"]
            assert ver == "1.52.0", ver
            checks += 1
            print(f"1. server healthy (v{ver})")

            # --- 1. connectors registered with generated form schemas -----------
            defs = client.get(f"{API}/node-definitions").json()["definitions"]
            by_type = {d["type"]: d for d in defs}
            assert "google_sheets_source" in by_type, sorted(by_type)[:5]
            assert "ftp_source" in by_type
            sheet_params = by_type["google_sheets_source"]["parameters_schema"]["properties"]
            ftp_params = by_type["ftp_source"]["parameters_schema"]["properties"]
            assert {"sheet", "mode", "credential_id"} <= set(sheet_params), list(sheet_params)
            assert {"host", "remote_path", "fmt", "secure"} <= set(ftp_params), list(ftp_params)
            checks += 1
            print("2. connectors registered: google_sheets_source + ftp_source (forms generated)")

            # --- 2. storage migration: local -> real S3 endpoint ----------------
            tag = uuid.uuid4().hex[:6]
            res = client.post(f"{API}/datasets", json={
                "name": f"smoke52 {tag} migration",
                "rows": [{"region": "eu", "ltv": 120.5}, {"region": "us", "ltv": 80.0}],
            })
            assert res.status_code == 201, res.text
            ds_id = res.json()["id"]
            # a second version so the snapshot moves too
            res = client.post(f"{API}/datasets/{ds_id}/rows", json={"rows": [{"region": "apac", "ltv": 55.0}]})
            assert res.status_code == 200, res.text

            # bucket on the moto server
            import boto3

            moto_s3 = boto3.client("s3", region_name="us-east-1",
                                   endpoint_url=f"http://127.0.0.1:{MOTO_PORT}",
                                   aws_access_key_id="test", aws_secret_access_key="test")
            moto_s3.create_bucket(Bucket="smoke52")

            res = client.post(f"{API}/storage/migrate", json={
                "target": {"kind": "minio", "bucket": "smoke52", "prefix": "estate",
                           "endpoint_url": f"http://127.0.0.1:{MOTO_PORT}",
                           "access_key_id": "test", "secret_access_key": "test"},
                "dataset_ids": [ds_id],
            })
            assert res.status_code == 200, res.text
            body = res.json()
            entry = next(d for d in body["datasets"] if d["dataset_id"] == ds_id)
            assert entry["copied"] >= 2 and entry["missing"] == 0, entry
            live = moto_s3.get_object(Bucket="smoke52", Key=f"estate/{ds_id}.parquet")["Body"].read()
            assert len(live) > 100, len(live)
            assert f"estate/versions/{ds_id}/v1.parquet" in [
                o["Key"] for o in moto_s3.list_objects_v2(Bucket="smoke52", Prefix="estate/").get("Contents", [])
            ]
            checks += 1
            print(f"3. migration copied {entry['copied']} blobs to S3 endpoint (live + version), bytes verified")

            res = client.post(f"{API}/storage/migrate", json={
                "target": {"kind": "minio", "bucket": "smoke52", "prefix": "estate",
                           "endpoint_url": f"http://127.0.0.1:{MOTO_PORT}",
                           "access_key_id": "test", "secret_access_key": "test"},
                "dataset_ids": [ds_id],
            })
            entry2 = next(d for d in res.json()["datasets"] if d["dataset_id"] == ds_id)
            assert entry2["copied"] == 0 and entry2["skipped"] >= 2, entry2
            checks += 1
            print(f"4. migration is idempotent: re-run skipped all {entry2['skipped']} blobs")

            # --- 3. report delivery: real webhook sink + skipped email ----------
            res = client.post(f"{API}/reports", json={
                "name": f"smoke52 weekly {tag}",
                "source_type": "dataset",
                "source_id": ds_id,
                "fmt": "csv",
                "cron": "0 6 * * *",
                "delivery": {
                    "channels": [
                        {"type": "webhook", "url": f"http://127.0.0.1:{SINK_PORT}/py8n-hook", "include_attachment": True},
                        {"type": "email", "to": "ops@example.com"},
                    ]
                },
            })
            assert res.status_code == 201, res.text
            report = res.json()
            res = client.post(f"{API}/reports/{report['id']}/run")
            assert res.status_code == 200, res.text
            run = res.json()["run"]
            assert run["ok"] is True, run
            delivered = run["delivery"]
            assert delivered[0]["status"] == "ok" and delivered[0]["attached"] is True, delivered
            assert delivered[1]["status"] == "skipped" and "PY8N_SMTP_HOST" in delivered[1]["detail"], delivered

            assert len(_Sink.captured) == 1, len(_Sink.captured)
            envelope = _Sink.captured[0]["body"]
            assert envelope["event"] == "py8n.report.completed"
            assert envelope["artifact"]["id"] == run["artifact_id"]
            inline = envelope["artifact"]["data_base64"]
            assert "region,ltv" in io.StringIO(__import__("base64").b64decode(inline).decode()).read()
            checks += 1
            print("5. webhook delivery: real HTTP sink received the envelope with the csv inline")

            trail = client.get(f"{API}/reports/{report['id']}/deliveries").json()
            statuses = [e["status"] for e in trail["events"]]
            assert statuses == ["skipped", "ok"], statuses  # newest first
            checks += 1
            print(f"6. delivery trail: {[ (e['channel'], e['status']) for e in trail['events'] ]} - visible without server logs")

            # --- 4. storage status still local (migration never switches) --------
            status = client.get(f"{API}/storage").json()
            assert status["kind"] == "local" and status["ping"] is True, status
            checks += 1
            print("7. active backend untouched (local, ping ok) - cutover stays a deploy decision")

            client.delete(f"{API}/reports/{report['id']}")
            client.delete(f"{API}/datasets/{ds_id}")

        print(f"\nSMOKE v52 GREEN: {checks} checks passed")
        return 0
    finally:
        sink.shutdown()
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        moto.terminate()
        try:
            moto.wait(timeout=5)
        except subprocess.TimeoutExpired:
            moto.kill()
        if os.path.exists(db_path):
            os.unlink(db_path)


if __name__ == "__main__":
    sys.exit(main())
