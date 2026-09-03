"""V60 live smoke: boot the real server and verify the Solution Marketplace.

1. Shelf: the three curated roadmap solutions are seeded with outcome
   checklists; detail carries the embedded pack summary.
2. Install: Invoice Processing installs through the pack machinery
   (workflow inactive + starter datasets), installs counter increments.
3. Run: the installed 'Invoice Approval Flow' runs OFFLINE through the
   real engine - the sample invoice passes validation and lands in
   invoices_approved.

Usage: /home/z/.venv/bin/python scripts/smoke_v60_live.py
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid

import httpx

BACKEND = "/home/z/my-project/py8n/mini-services/api-backend"
API = "http://127.0.0.1:8199/api/v1"


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


def main() -> int:
    db_path = f"{BACKEND}/data/smoke_v60_{uuid.uuid4().hex[:8]}.sqlite3"
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
    try:
        with httpx.Client(base_url=API, timeout=60) as c:
            wait_health(c)
            checks = 0

            # --- 1) the shelf --------------------------------------------------
            res = c.get("/solutions")
            assert res.status_code == 200, res.text
            shelf = res.json()
            slugs = {s["slug"] for s in shelf["solutions"]}
            assert {"customer-support-automation", "invoice-processing", "api-monitoring"} <= slugs, slugs
            support = next(s for s in shelf["solutions"] if s["slug"] == "customer-support-automation")
            assert len(support["outcomes"]) >= 5
            checks += 1
            print(f"[1] shelf: {len(shelf['solutions'])} solutions, categories={shelf['categories']}")

            # --- 2) detail + install --------------------------------------------
            res = c.get("/solutions/invoice-processing")
            detail = res.json()
            assert "Approval gate" in detail["outcomes"]
            assert detail["pack"]["workflows"][0]["name"] == "Invoice Approval Flow"
            res = c.post("/solutions/invoice-processing/install", json={})
            assert res.status_code in (200, 201), res.text
            installed = res.json()
            assert installed["installs"] == 1
            wf = installed["created_workflows"][0]
            ds_names = {d["name"] for d in installed["created_datasets"]}
            assert {"invoices_approved", "invoice_exceptions"} <= ds_names
            checks += 1
            print(f"[2] installed: {wf['name']} + datasets {sorted(ds_names)}")

            # --- 3) run it offline through the real engine ----------------------
            res = c.post(f"/workflows/{wf['id']}/run", json={})
            assert res.status_code in (200, 202), res.text
            ex = res.json()["execution_id"]
            for _ in range(200):
                det = c.get(f"/executions/{ex}").json()
                if det["status"] not in ("running", "queued"):
                    break
                time.sleep(0.05)
            assert det["status"] == "success", det
            approved = next(d for d in installed["created_datasets"] if d["name"] == "invoices_approved")
            rows = c.get(f"/datasets/{approved['id']}/rows?limit=10").json()
            got = rows.get("rows", [])
            assert got and got[-1].get("invoice_id") == "INV-2044", got
            checks += 1
            print(f"[3] run: status={det['status']} approved_rows={len(got)} (exception lane idle)")

            print(f"SMOKE v60 GREEN - {checks}/3 checks passed")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        try:
            os.unlink(db_path)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
