"""v120: the platform grows up - signed webhooks, two-factor, portability,
the queue.

Four hardening moves, each tested on the real wire:

* WEBHOOK HMAC (Stripe/GitHub style) - the webhook trigger's ``hmac``
  auth mode verifies an HMAC-SHA256 hex digest over the RAW body
  (timing-safe, ``sha256=`` prefix accepted) before the flow runs;
* TOTP TWO-FACTOR (RFC 6238, stdlib) - setup -> enable with a live code;
  the login turns into a CHALLENGE (a short-lived mfa-scoped token that
  decode_token refuses as a bearer), verify mints the real session;
* SYSTEM BUNDLES - a whole system exports as ONE document (meta + pack of
  workflow graphs + dataset schemas and rows) and imports as a copy with
  fresh ids and inactive-honest workflows;
* THE QUEUE - ``PY8N_EXECUTION_MODE=queue`` lands runs as ``queued`` rows
  the house tick claims and runs one at a time through the SAME executor.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import totp_code
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


def _sync(coro):
    return asyncio.run(coro)


def _node(nid: str, ntype: str, params: dict | None = None) -> dict:
    return {"id": nid, "type": ntype, "name": nid,
            "position": {"x": 0, "y": 0}, "parameters": params or {}}


async def _mk_user(client: httpx.AsyncClient, tag: str) -> dict:
    res = await client.post("/auth/register", json={
        "email": f"v120-{tag}@py8n.test",
        "password": "correct-horse-battery",
        "name": f"v120 {tag}",
    })
    assert res.status_code == 201, res.text
    return {"token": res.json()["token"], "id": res.json()["user"]["id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _mk_hook_workflow(client: httpx.AsyncClient, name: str,
                            params: dict) -> str:
    graph = {
        "nodes": [
            _node("h", "webhook_trigger",
                  {"response_mode": "immediately", **params}),
            _node("s", "set_variable",
                  {"assignments": {"ok": "1"}, "keep_input": False}),
        ],
        "edges": [{"id": "e1", "source": "h", "target": "s",
                   "sourceHandle": "main", "targetHandle": "main"}],
    }
    res = await client.post("/workflows", json={"name": name, "graph": graph})
    assert res.status_code == 201, res.text
    wf_id = res.json()["id"]
    res = await client.post(f"/workflows/{wf_id}/activate")
    assert res.status_code == 200, res.text
    return wf_id


def test_v120_webhook_hmac_signatures():
    tag = uuid.uuid4().hex[:8]
    secret = "whsec_" + tag

    async def _go():
        async with _client() as client:
            wf_id = await _mk_hook_workflow(client, f"tmp v120 hmac {tag}", {
                "auth_mode": "hmac",
                "signature_header": "X-Signature",
                "signature_secret": secret,
            })
            url = f"/webhooks/{wf_id}"
            body = {"ping": 1}

            # unsigned -> 401; tampered -> 401
            res = await client.post(url, json=body)
            assert res.status_code == 401, res.text
            assert "signature" in res.json()["detail"].lower()
            wrong = hmac_mod.new(b"other", b"other", hashlib.sha256).hexdigest()
            res = await client.post(url, json=body, headers={"X-Signature": wrong})
            assert res.status_code == 401, res.text

            # the honest signature passes (202 immediately mode) - sign the
            # EXACT bytes on the wire (pre-serialized, so no re-serialization
            # drift between what we signed and what we sent)
            raw = json.dumps(body).encode()
            good = hmac_mod.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            res = await client.post(url, content=raw,
                                    headers={"Content-Type": "application/json",
                                             "X-Signature": good})
            assert res.status_code == 202, res.text

            # the sha256= prefix (GitHub style) passes too
            res = await client.post(url, content=raw,
                                    headers={"Content-Type": "application/json",
                                             "X-Signature": f"sha256={good}"})
            assert res.status_code == 202, res.text

            # the node def carries the new params so the form can set them
            res = await client.get("/node-definitions")
            defs = res.json()["definitions"]
            if isinstance(defs, list):
                defs = {d.get("type"): d for d in defs}
            props = defs["webhook_trigger"]["parameters_schema"]["properties"]
            assert "signature_header" in props and "signature_secret" in props
            assert "hmac" in props["auth_mode"].get("enum", []) or \
                "hmac" in str(props["auth_mode"])

            await client.delete(f"/workflows/{wf_id}")

    _sync(_go())


def test_v120_totp_two_factor():
    email = "v120-totp@py8n.test"

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "totp")
            h = _auth(user["token"])

            # wrong code refuses to enroll
            res = await client.post("/auth/2fa/setup", headers=h)
            assert res.status_code == 200, res.text
            secret = res.json()["secret"]
            assert res.json()["otpauth_uri"].startswith("otpauth://totp/Py8n:")
            res = await client.post("/auth/2fa/enable", headers=h,
                                    json={"code": "000000"})
            assert res.status_code == 400, res.text

            # the live code enrolls
            res = await client.post("/auth/2fa/enable", headers=h,
                                    json={"code": totp_code(secret)})
            assert res.status_code == 200, res.text
            assert res.json()["enabled"] is True
            # double-enable is a loud 409
            res = await client.post("/auth/2fa/setup", headers=h)
            assert res.status_code == 409, res.text

            # THE CHALLENGE: login now answers mfa_required with a
            # short-lived challenge token
            res = await client.post("/auth/login", json={
                "email": email,
                "password": "correct-horse-battery"})
            assert res.status_code == 200, res.text
            challenge = res.json()
            assert challenge["mfa_required"] is True
            mfa_token = challenge["mfa_token"]

            # a challenge token is NEVER a bearer
            res = await client.get("/auth/me",
                                   headers=_auth(mfa_token))
            assert res.status_code == 401, res.text

            # a wrong code never mints a session
            res = await client.post("/auth/2fa/verify", json={
                "mfa_token": mfa_token, "code": "123456"})
            assert res.status_code == 401, res.text

            # the right code mints the REAL session
            res = await client.post("/auth/2fa/verify", json={
                "mfa_token": mfa_token, "code": totp_code(secret)})
            assert res.status_code == 200, res.text
            real = res.json()["token"]
            res = await client.get("/auth/me", headers=_auth(real))
            assert res.status_code == 200, res.text

            # disabling requires the code too
            res = await client.post("/auth/2fa/disable", headers=h,
                                    json={"code": "000000"})
            assert res.status_code == 400, res.text
            res = await client.post("/auth/2fa/disable", headers=h,
                                    json={"code": totp_code(secret)})
            assert res.status_code == 200, res.text
            # ...and login is plain again
            res = await client.post("/auth/login", json={
                "email": email,
                "password": "correct-horse-battery"})
            assert res.json().get("mfa_required") is None

    _sync(_go())


def test_v120_system_export_import():
    tag = uuid.uuid4().hex[:8]

    async def _go():
        async with _client() as client:
            user = await _mk_user(client, "bundle")
            h = _auth(user["token"])

            # a REAL system built through its own doors: one workflow,
            # one dataset with rows, both bound
            graph = {
                "nodes": [
                    _node("m", "manual_trigger", {}),
                    _node("s", "set_variable",
                          {"assignments": {"ok": "1"}, "keep_input": False}),
                ],
                "edges": [{"id": "e1", "source": "m", "target": "s",
                           "sourceHandle": "main", "targetHandle": "main"}],
            }
            res = await client.post("/workflows",
                                    json={"name": f"v120 bundle flow {tag}",
                                          "graph": graph})
            assert res.status_code == 201, res.text
            wf_id = res.json()["id"]

            res = await client.post("/datasets", headers=h, json={
                "name": f"v120 bundle ds {tag}",
                "rows": [{"sku": "SKU-1", "qty": "3"},
                         {"sku": "SKU-2", "qty": "7"}]})
            assert res.status_code == 201, res.text
            ds_id = res.json()["id"]

            res = await client.post("/systems", headers=h,
                                    json={"name": "v120 bundle system"})
            assert res.status_code in (200, 201), res.text
            system_id = res.json()["id"]
            for kind, ref in (("workflow", wf_id), ("dataset", ds_id)):
                res = await client.post(f"/systems/{system_id}/components",
                                        headers=h,
                                        json={"kind": kind, "ref_id": ref})
                assert res.status_code in (200, 201), res.text

            # EXPORT: the whole system as one document
            res = await client.get(f"/systems/{system_id}/export", headers=h)
            assert res.status_code == 200, res.text
            bundle = res.json()
            assert bundle["format"] == "py8n-system"
            assert bundle["manifest"]["workflow_count"] == 1
            assert bundle["manifest"]["dataset_count"] == 1
            assert bundle["pack"]["datasets"][0]["rows"], "rows ride along"
            assert bundle["pack"]["workflows"][0]["graph"]["nodes"]

            # IMPORT: a copy lands - fresh system, fresh ids, inactive flows
            res = await client.post("/systems/import", headers=h,
                                    json={"name": "The bundle copy",
                                          "pack": bundle})
            assert res.status_code == 201, res.text
            imp = res.json()
            assert imp["system"]["name"] == "The bundle copy"
            assert imp["system"]["components"] == 2
            assert not imp.get("skipped"), imp.get("skipped")

            # the copy is bound and real: its detail door answers
            new_id = imp["system"]["id"]
            assert new_id != system_id
            res = await client.get(f"/systems/{new_id}", headers=h)
            assert res.status_code == 200, res.text

            # the workflow it bound is inactive-honest (pack semantics)
            for item in imp["installed"]:
                if item["kind"] == "workflow":
                    res = await client.get(f"/workflows/{item['ref_id']}",
                                           headers=h)
                    assert res.status_code == 200, res.text
                    assert res.json()["is_active"] is False

            # a malformed bundle is a loud 400
            res = await client.post("/systems/import", headers=h,
                                    json={"pack": {"workflows": "nope"}})
            assert res.status_code == 400, res.text

            await client.delete(f"/workflows/{wf_id}")

    _sync(_go())


def test_v120_queue_mode_runs_deferred():
    tag = uuid.uuid4().hex[:8]
    old_mode = settings.execution_mode
    settings.execution_mode = "queue"
    try:
        async def _go():
            async with _client() as client:
                graph = {
                    "nodes": [
                        _node("m", "manual_trigger", {}),
                        _node("s", "set_variable",
                              {"assignments": {"ok": "1"},
                               "keep_input": False}),
                    ],
                    "edges": [{"id": "e1", "source": "m", "target": "s",
                               "sourceHandle": "main", "targetHandle": "main"}],
                }
                res = await client.post("/workflows",
                                        json={"name": f"tmp v120 queue {tag}",
                                              "graph": graph})
                assert res.status_code == 201, res.text
                wf_id = res.json()["id"]

                # the dispatch lands as QUEUED - the id resolves, nothing ran
                res = await client.post(f"/workflows/{wf_id}/run",
                                        json={"payload": {}})
                assert res.status_code in (200, 202), res.text
                exec_id = res.json()["execution_id"]
                res = await client.get(f"/executions/{exec_id}")
                assert res.status_code == 200, res.text
                assert res.json()["status"] == "queued", res.json()

                # THE TICK claims and runs it through the REAL executor
                from app.services.executor import run_due_queue
                ran = await run_due_queue()
                assert exec_id in ran, (exec_id, ran)
                res = await client.get(f"/executions/{exec_id}")
                assert res.json()["status"] == "success", res.json()

                await client.delete(f"/workflows/{wf_id}")
        _sync(_go())
    finally:
        settings.execution_mode = old_mode


def test_v120_version_pin():
    assert settings.version == "1.120.0"
