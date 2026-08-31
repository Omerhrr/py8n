"""Live credential probes - one per credential type.

``probe_credential`` performs a cheap, real-world check (connect / auth /
HTTP call) and returns an honest result instead of guessing. Used by
``POST /credentials/{id}/test``; never logs or returns secret material.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

DEFAULT_TEST_URL = "https://example.com"


class ProbeError(Exception):
    """Raised for structurally unusable credentials (missing fields)."""


def _clean(detail: dict[str, Any]) -> dict[str, Any]:
    """Strip secret-looking values from probe detail output."""
    secret_keys = {"password", "api_key", "token", "value", "webhook_url"}
    return {k: ("••••" if k in secret_keys else v) for k, v in detail.items()}


# ----------------------------------------------------------------------
# per-type probes (sync bodies, run via asyncio.to_thread where blocking)
# ----------------------------------------------------------------------
def _probe_header_auth(data: dict, test_url: str) -> dict[str, Any]:
    header_name = data.get("header_name") or "Authorization"
    value = data.get("value") or ""
    if not value:
        raise ProbeError("header_auth credential is missing 'value'")
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        resp = client.get(test_url, headers={header_name: value})
    return {
        "ok": resp.status_code < 400,
        "message": f"HTTP {resp.status_code} from {test_url} with header '{header_name}'",
        "status_code": resp.status_code,
    }


def _probe_basic_auth(data: dict, test_url: str) -> dict[str, Any]:
    username = data.get("username") or ""
    password = data.get("password") or ""
    if not username:
        raise ProbeError("basic_auth credential is missing 'username'")
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        resp = client.get(test_url, auth=(username, password))
    return {
        "ok": resp.status_code < 400,
        "message": f"HTTP {resp.status_code} from {test_url} with basic auth as '{username}'",
        "status_code": resp.status_code,
    }


def _probe_smtp(data: dict) -> dict[str, Any]:  # noqa: C901
    import smtplib

    host = data.get("host")
    if not host:
        raise ProbeError("SMTP credential is missing 'host'")
    port = int(data.get("port") or 587)
    username = data.get("username")
    password = data.get("password")
    use_tls = bool(data.get("use_tls", True))

    t0 = time.monotonic()
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.ehlo()
        if use_tls:
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPException:
                pass  # server may not offer STARTTLS - continue unencrypted
        if username:
            if not password:
                raise ProbeError("SMTP credential has a username but no password")
            smtp.login(username, password)
    ms = int((time.monotonic() - t0) * 1000)
    auth = "login ok" if username else "no auth configured"
    return {"ok": True, "message": f"Connected to {host}:{port} ({auth})", "latency_ms": ms}


def _probe_slack(data: dict) -> dict[str, Any]:
    webhook_url = data.get("webhook_url")
    token = data.get("token")
    if webhook_url:
        with httpx.Client(timeout=15) as client:
            resp = client.post(webhook_url, json={"text": "py8n credential test"})
        return {
            "ok": resp.status_code < 400,
            "message": f"Webhook responded HTTP {resp.status_code}",
            "status_code": resp.status_code,
        }
    if token:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {token}"},
            )
        body = resp.json() if resp.status_code < 500 else {}
        ok = resp.status_code < 400 and body.get("ok") is True
        return {
            "ok": ok,
            "message": f"auth.test → HTTP {resp.status_code}" + ("" if ok else f" ({body.get('error', 'not ok')})"),
            "status_code": resp.status_code,
        }
    raise ProbeError("slack credential needs a 'webhook_url' or a bot 'token'")


def _probe_openai_compatible(data: dict) -> dict[str, Any]:
    base_url = (data.get("base_url") or "").rstrip("/")
    if not base_url:
        raise ProbeError("openai_compatible credential is missing 'base_url'")
    api_key = data.get("api_key")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = f"{base_url}/models"
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
    ok = resp.status_code < 400
    return {
        "ok": ok,
        "message": f"GET {url} → HTTP {resp.status_code}",
        "status_code": resp.status_code,
    }


def _probe_generic(data: dict, test_url: str) -> dict[str, Any]:
    """Best-effort: webhook_url → POST ping; token → Bearer GET; else honest no-op."""
    if data.get("webhook_url"):
        return _probe_slack({"webhook_url": data["webhook_url"]})
    if data.get("token"):
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.get(test_url, headers={"Authorization": f"Bearer {data['token']}"})
        return {
            "ok": resp.status_code < 400,
            "message": f"HTTP {resp.status_code} from {test_url} with Bearer token",
            "status_code": resp.status_code,
        }
    return {
        "ok": False,
        "message": "No live probe for this generic credential (add webhook_url or token, or dry-run it from a node)",
    }


PROBES = {
    "header_auth": lambda data, test_url: _probe_header_auth(data, test_url),
    "basic_auth": lambda data, test_url: _probe_basic_auth(data, test_url),
    "smtp": lambda data, test_url: _probe_smtp(data),
    "slack": lambda data, test_url: _probe_slack(data),
    "openai_compatible": lambda data, test_url: _probe_openai_compatible(data),
    "generic": lambda data, test_url: _probe_generic(data, test_url),
}


async def probe_credential(
    cred_type: str, data: dict, test_url: str | None = None
) -> dict[str, Any]:
    """Run the probe for ``cred_type``. Returns {ok, message, ...detail}.

    Raises ``ValueError`` for unknown credential types and ``ProbeError``
    for structurally incomplete ones - both mapped to HTTP 400 by the API.
    Network/auth failures are reported as ``ok: false`` results, not errors.
    """
    fn = PROBES.get(cred_type)
    if fn is None:
        raise ValueError(f"No live probe for credential type {cred_type!r}")

    url = test_url or DEFAULT_TEST_URL
    t0 = time.monotonic()
    try:
        result = await asyncio.to_thread(fn, data, url)
        result.setdefault("ok", False)
        result["latency_ms"] = int((time.monotonic() - t0) * 1000)
        result["detail"] = _clean({"test_url": url})
        return result
    except ProbeError as exc:
        return {"ok": False, "message": str(exc), "latency_ms": int((time.monotonic() - t0) * 1000)}
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "message": f"Network error: {exc}",
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }
    except Exception as exc:  # noqa: BLE001 - smtplib.SMTPAuthenticationError etc.
        return {
            "ok": False,
            "message": f"{type(exc).__name__}: {exc}",
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }


__all__ = ["probe_credential", "ProbeError", "DEFAULT_TEST_URL"]
