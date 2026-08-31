"""V10 feature tests: credentials vault completion (update, live test probe,
usage tracking, delete protection, http_request basic_auth).

Runs the FastAPI app in-process via httpx ASGITransport against the dev SQLite
DB. Probe targets are local loopback TCP servers - no external network needed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import smtplib
import uuid

import httpx

from app.main import app

API = "http://testserver/api/v1"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _drain_background() -> None:
    from app.services import executor as executor_mod

    tasks = [t for t in executor_mod._background_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ----------------------------------------------------------------------
# loopback HTTP server helper
# ----------------------------------------------------------------------
async def _loopback(handler):
    """Tiny raw-TCP HTTP server. handler(request_bytes) -> (status_line, body).

    Returns (server, port); caller must server.close() in finally.
    """

    async def _on_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await asyncio.wait_for(reader.read(65536), timeout=5)
            status, body = handler(data)
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            writer.write(
                b"HTTP/1.1 " + status.encode() + b"\r\n"
                + b"Content-Type: application/json\r\n"
                + b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
                + b"Connection: close\r\n\r\n" + payload
            )
            await writer.drain()
        except Exception:  # noqa: BLE001 - test sink
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(_on_conn, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _echo_auth(data: bytes):
    text = data.decode("latin-1", "replace")
    headers: dict[str, str] = {}
    for line in text.split("\r\n")[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return "200 OK", {"authorization": headers.get("authorization", ""), "x-api-key": headers.get("x-api-key", "")}


def _ok_resp(data: bytes):
    return "200 OK", {"ok": True}


def _unauthorized(data: bytes):
    return "401 Unauthorized", {"error": "bad credentials"}


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
async def _mk_cred(client: httpx.AsyncClient, name: str, type_: str, data: dict) -> dict:
    res = await client.post("/credentials", json={"name": name, "type": type_, "data": data})
    assert res.status_code == 201, res.text
    return res.json()


async def _cleanup(cred_ids: list[str], wf_ids: list[str]) -> None:
    async with _client() as client:
        for wid in wf_ids:
            try:
                await client.delete(f"/workflows/{wid}")
            except Exception:
                pass
        for cid in cred_ids:
            try:
                await client.delete(f"/credentials/{cid}", params={"force": "true"})
            except Exception:
                pass
    await _drain_background()


# ------------------------------------------------------------------ PATCH
def test_update_credential_rename_and_reencrypt():
    tag = uuid.uuid4().hex[:8]
    created_c: list[str] = []
    created_w: list[str] = []

    async def _go():
        async with _client() as client:
            cred = await _mk_cred(client, f"old-name-{tag}", "header_auth", {"header_name": "X-Key", "value": "secret-abcd"})
            created_c.append(cred["id"])
            original_hint = cred["masked_hint"]
            assert original_hint.endswith("abcd") or "••" in original_hint

            # rename only
            res = await client.patch(f"/credentials/{cred['id']}", json={"name": f"new-name-{tag}"})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["name"] == f"new-name-{tag}"
            assert body["masked_hint"] == original_hint  # data untouched

            # replace data → masked hint follows the new secret
            res = await client.patch(
                f"/credentials/{cred['id']}",
                json={"data": {"header_name": "X-Key", "value": "rotated-9876"}},
            )
            assert res.status_code == 200
            assert res.json()["masked_hint"].endswith("9876")

            # verify the rotation actually persisted (decrypt round-trip via re-patch read)
            listing = await client.get("/credentials")
            row = next(c for c in listing.json() if c["id"] == cred["id"])
            assert row["name"] == f"new-name-{tag}"

            # --- edit-time detail: non-secrets visible, secrets blanked
            res = await client.get(f"/credentials/{cred['id']}")
            assert res.status_code == 200
            det = res.json()
            assert det["data"]["header_name"] == "X-Key"  # non-secret visible
            assert det["data"]["value"] == ""  # secret blanked, never returned

            # --- __keep__ marker keeps the stored secret while renaming a field
            res = await client.patch(
                f"/credentials/{cred['id']}",
                json={"data": {"header_name": "Y-Key", "value": "__keep__"}},
            )
            assert res.status_code == 200
            assert res.json()["masked_hint"].endswith("9876")  # secret survived
            det = (await client.get(f"/credentials/{cred['id']}")).json()
            assert det["data"]["header_name"] == "Y-Key"
            assert det["data"]["value"] == ""

            # 404 unknown / 400 empty name
            assert (await client.patch("/credentials/nope", json={"name": "x"})).status_code == 404
            assert (await client.patch(f"/credentials/{cred['id']}", json={"name": "  "})).status_code == 400
            assert (await client.get("/credentials/nope")).status_code == 404
        await _drain_background()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(_client.__self__ if False else _c_ids(created_c), created_w))


def _c_ids(ids: list[str]) -> list[str]:
    return ids


# ------------------------------------------------------------------ test probe
def test_credential_test_probe_loopback_and_guards():
    tag = uuid.uuid4().hex[:8]
    created_c: list[str] = []
    created_w: list[str] = []

    async def _go():
        async with _client() as client:
            cred = await _mk_cred(client, f"probe-{tag}", "header_auth", {"header_name": "X-Key", "value": "k-1234"})
            created_c.append(cred["id"])

            # --- loopback 200 → ok=true
            server, port = await _loopback(_ok_resp)
            try:
                res = await client.post(
                    f"/credentials/{cred['id']}/test",
                    json={"test_url": f"http://127.0.0.1:{port}/"},
                )
                assert res.status_code == 200, res.text
                body = res.json()
                assert body["ok"] is True, body
                assert "200" in body["message"]
                assert body["latency_ms"] >= 0
                assert body["probed_at"]
            finally:
                server.close()

            # --- loopback 401 → ok=false (honest failure, not HTTP error)
            server, port = await _loopback(_unauthorized)
            try:
                res = await client.post(
                    f"/credentials/{cred['id']}/test",
                    json={"test_url": f"http://127.0.0.1:{port}/"},
                )
                body = res.json()
                assert body["ok"] is False
                assert "401" in body["message"]
            finally:
                server.close()

            # --- 404 unknown credential
            res = await client.post("/credentials/does-not-exist/test")
            assert res.status_code == 404

            # --- 400 unknown credential type
            bogus = await _mk_cred(client, f"bogus-{tag}", "carrier_pigeon", {"x": "y"})
            created_c.append(bogus["id"])
            res = await client.post(f"/credentials/{bogus['id']}/test")
            assert res.status_code == 400
            assert "carrier_pigeon" in res.json()["detail"]

            # --- 400 structurally incomplete (header_auth without value)
            broken = await _mk_cred(client, f"broken-{tag}", "header_auth", {"header_name": "X-Key"})
            created_c.append(broken["id"])
            res = await client.post(f"/credentials/{broken['id']}/test")
            assert res.status_code == 200
            assert res.json()["ok"] is False
            assert "value" in res.json()["message"]
        await _drain_background()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(_c_ids(created_c), created_w))


# ------------------------------------------------------------------ smtp probe (fake SMTP)
def test_credential_test_smtp_probe_with_fake_server():
    tag = uuid.uuid4().hex[:8]
    created_c: list[str] = []
    created_w: list[str] = []

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host, self.port = host, port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            return None

        def starttls(self):
            return None

        def login(self, user, password):
            if password == "wrong":
                raise smtplib.SMTPAuthenticationError(535, b"Bad credentials")

    async def _go_real():
        async with _client() as client:
            import app.services.credential_probe as probe_mod

            cred = await _mk_cred(
                client, f"smtp-{tag}", "smtp",
                {"host": "smtp.test.local", "port": 587, "username": "u", "password": "pw", "use_tls": True},
            )
            created_c.append(cred["id"])

            real_smtp = smtplib.SMTP
            smtplib.SMTP = _FakeSMTP
            try:
                res = await client.post(f"/credentials/{cred['id']}/test")
                assert res.status_code == 200
                body = res.json()
                assert body["ok"] is True, body
                assert "smtp.test.local:587" in body["message"]
                assert "login ok" in body["message"]
            finally:
                smtplib.SMTP = real_smtp

            # auth failure path → ok=false with honest message
            res = await client.patch(
                f"/credentials/{cred['id']}",
                json={"data": {"host": "smtp.test.local", "port": 587, "username": "u", "password": "wrong"}},
            )
            assert res.status_code == 200
            smtplib.SMTP = _FakeSMTP
            try:
                res = await client.post(f"/credentials/{cred['id']}/test")
                body = res.json()
                assert body["ok"] is False
                assert "535" in body["message"] or "SMTPAuthentication" in body["message"] or "Bad credentials" in body["message"]
            finally:
                smtplib.SMTP = real_smtp

    try:
        asyncio.run(_go_real())
    finally:
        asyncio.run(_cleanup(_c_ids(created_c), created_w))


# ------------------------------------------------------------------ usage + delete protection
def test_usage_tracking_and_delete_protection():
    tag = uuid.uuid4().hex[:8]
    created_c: list[str] = []
    created_w: list[str] = []

    async def _go():
        async with _client() as client:
            cred = await _mk_cred(client, f"used-{tag}", "header_auth", {"header_name": "Authorization", "value": "Bearer t"})
            created_c.append(cred["id"])

            # workflow referencing the credential from an http_request node
            wf_res = await client.post(
                "/workflows",
                json={
                    "name": f"v10-usage-{tag}",
                    "description": "temp",
                    "is_active": False,
                    "graph": {
                        "nodes": [
                            {"id": "t", "type": "manual_trigger", "parameters": {"payload": {}}},
                            {
                                "id": "h",
                                "type": "http_request",
                                "parameters": {
                                    "method": "GET",
                                    "url": "https://example.com",
                                    "credential_id": cred["id"],
                                },
                            },
                        ],
                        "edges": [{"id": "e1", "source": "t", "target": "h"}],
                    },
                },
            )
            assert wf_res.status_code in (200, 201)
            wf = wf_res.json()
            created_w.append(wf["id"])

            # usage endpoint finds it
            res = await client.get(f"/credentials/{cred['id']}/usage")
            assert res.status_code == 200
            usage = res.json()
            assert usage["workflow_count"] == 1
            assert usage["workflows"][0]["id"] == wf["id"]
            assert usage["workflows"][0]["name"] == f"v10-usage-{tag}"
            assert usage["workflows"][0]["nodes"] == ["h"]

            # delete → 409 with helpful detail
            res = await client.delete(f"/credentials/{cred['id']}")
            assert res.status_code == 409
            detail = res.json()["detail"]
            assert "1 workflow" in detail and "force" in detail

            # usage 404 for unknown credential
            assert (await client.get("/credentials/nope/usage")).status_code == 404

            # force delete works even while referenced
            res = await client.delete(f"/credentials/{cred['id']}", params={"force": "true"})
            assert res.status_code == 204
            listing = await client.get("/credentials")
            assert all(c["id"] != cred["id"] for c in listing.json())
            created_c.clear()
        await _drain_background()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(_c_ids(created_c), created_w))


# ------------------------------------------------------------------ http node basic_auth
def test_http_request_basic_auth_credential_end_to_end():
    tag = uuid.uuid4().hex[:8]
    created_c: list[str] = []
    created_w: list[str] = []

    async def _go():
        async with _client() as client:
            cred = await _mk_cred(client, f"basic-{tag}", "basic_auth", {"username": "alice", "password": "s3cret"})
            created_c.append(cred["id"])

            server, port = await _loopback(_echo_auth)
            try:
                wf_res = await client.post(
                    "/workflows",
                    json={
                        "name": f"v10-basic-{tag}",
                        "description": "temp",
                        "is_active": False,
                        "graph": {
                            "nodes": [
                                {"id": "t", "type": "manual_trigger", "parameters": {"payload": {}}},
                                {
                                    "id": "h",
                                    "type": "http_request",
                                    "parameters": {
                                        "method": "GET",
                                        "url": f"http://127.0.0.1:{port}/echo",
                                        "credential_id": cred["id"],
                                    },
                                },
                            ],
                            "edges": [{"id": "e1", "source": "t", "target": "h"}],
                        },
                    },
                )
                assert wf_res.status_code in (200, 201)
                wf = wf_res.json()
                created_w.append(wf["id"])

                run_res = await client.post(f"/workflows/{wf['id']}/run", json={"payload": {}})
                assert run_res.status_code == 200, run_res.text
                exec_id = run_res.json()["execution_id"]

                deadline = asyncio.get_event_loop().time() + 15
                detail = None
                while asyncio.get_event_loop().time() < deadline:
                    d = await client.get(f"/executions/{exec_id}")
                    if d.status_code == 200 and d.json()["status"] != "running":
                        detail = d.json()
                        break
                    await asyncio.sleep(0.15)
                assert detail and detail["status"] == "success", detail

                http_run = next(r for r in detail["node_runs"] if r["node_id"] == "h")
                sent = http_run["output"]["body"]
                expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
                assert sent["authorization"] == expected, sent
            finally:
                server.close()
        await _drain_background()

    try:
        asyncio.run(_go())
    finally:
        asyncio.run(_cleanup(_c_ids(created_c), created_w))
