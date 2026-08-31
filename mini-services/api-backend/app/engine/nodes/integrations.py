"""Integration nodes: Email (SMTP) and Slack.

Both nodes ship with a ``dry_run`` safety default (on): the message is
rendered and returned as a preview without touching the network, so graphs
can be tested offline. Flip ``dry_run`` off once a credential is attached.

Credentials come from the encrypted vault (``app/services/crypto.py``):
* ``email_send`` expects a credential of type ``smtp``
  (fields: host, port, username, password, use_tls)
* ``slack_message`` accepts either an incoming **webhook URL** parameter or a
  credential of type ``generic`` holding a bot token (``token: xoxb-…``)
"""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar

import httpx
from pydantic import BaseModel, Field

from ..context import ExecutionContext
from .base import BaseNode, NodeExecutionError, NodeResult


class EmailSendNode(BaseNode):
    """Sends an email via SMTP, or previews it when dry_run is on."""

    type = "email_send"
    name = "Send Email"
    description = "Sends an email over SMTP using a vault credential - dry-run preview by default."
    category = "actions"
    icon = "mail"
    color = "#f472b6"

    class ParamsModel(BaseModel):
        to: str = Field(default="", description="Recipient(s), comma-separated - supports {{ expressions }}")
        subject: str = Field(default="", description="Subject line - supports {{ expressions }}")
        body: str = Field(default="", description="Plain-text or HTML body", json_schema_extra={"widget": "textarea", "rows": 6})
        html: bool = Field(default=False, description="Send the body as HTML")
        dry_run: bool = Field(default=True, description="Render the message without sending (safe default)")
        credential_id: str | None = Field(
            default=None,
            description="SMTP credential (host, port, username, password, use_tls)",
            json_schema_extra={"widget": "credential"},
        )

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: EmailSendNode.ParamsModel
        to = [addr.strip() for addr in (p.to or "").split(",") if addr.strip()]
        if not to:
            raise NodeExecutionError("No recipient - fill the 'to' field")
        message = {"to": to, "subject": p.subject, "body": p.body, "html": p.html}

        if p.dry_run:
            return self._single({"delivered": False, "dry_run": True, "message": message})

        if not p.credential_id:
            raise NodeExecutionError("Sending requires an SMTP credential - attach one or keep dry_run enabled")

        from ...services.crypto import decrypt_credential

        cred = await decrypt_credential(context, p.credential_id)
        if cred.get("type") != "smtp":
            raise NodeExecutionError("Selected credential is not of type smtp")
        host = cred.get("host") or ""
        port = int(cred.get("port") or 587)
        username = cred.get("username") or ""
        password = cred.get("password") or ""
        use_tls = bool(cred.get("use_tls", True))
        if not host:
            raise NodeExecutionError("SMTP credential is missing 'host'")

        import smtplib
        from email.message import EmailMessage

        def _send() -> None:
            msg = EmailMessage()
            msg["From"] = username or "py8n@localhost"
            msg["To"] = ", ".join(to)
            msg["Subject"] = p.subject
            if p.html:
                msg.set_content("This message requires an HTML-capable mail client.")
                msg.add_alternative(p.body, subtype="html")
            else:
                msg.set_content(p.body)
            with smtplib.SMTP(host, port, timeout=30) as server:
                if use_tls:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                if username:
                    server.login(username, password)
                server.send_message(msg)

        try:
            await asyncio.to_thread(_send)
        except Exception as exc:  # noqa: BLE001
            raise NodeExecutionError(f"SMTP send failed: {exc}") from exc
        return self._single({"delivered": True, "dry_run": False, "message": message, "smtp": {"host": host, "port": port}})


class SlackMessageNode(BaseNode):
    """Posts a message to Slack via incoming webhook or bot-token credential."""

    type = "slack_message"
    name = "Slack Message"
    description = "Posts a message to Slack through an incoming webhook URL or a bot token credential."
    category = "actions"
    icon = "slack"
    color = "#e01e5a"

    class ParamsModel(BaseModel):
        webhook_url: str = Field(
            default="",
            description="Slack incoming webhook URL (https://hooks.slack.com/...) - supports {{ expressions }}",
        )
        text: str = Field(
            default="{{ input }}",
            description="Message text - supports {{ expressions }}",
            json_schema_extra={"widget": "textarea", "rows": 4},
        )
        channel: str = Field(default="", description="Channel / user ID (bot-token mode only, e.g. #alerts)")
        dry_run: bool = Field(default=True, description="Build the payload without posting (safe default)")
        credential_id: str | None = Field(
            default=None,
            description="Bot-token credential (generic type with a token field, xoxb-…)",
            json_schema_extra={"widget": "credential"},
        )

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: SlackMessageNode.ParamsModel
        text = p.text if isinstance(p.text, str) else json.dumps(p.text, default=str, ensure_ascii=False)

        if p.webhook_url:
            mode = "webhook"
            payload: dict = {"text": text}
            if p.channel:
                payload["channel"] = p.channel
        elif p.credential_id:
            mode = "bot"
            payload = {"channel": p.channel or "#general", "text": text}
        else:
            raise NodeExecutionError("Provide a Slack webhook_url or a bot-token credential")

        if p.dry_run:
            return self._single({"delivered": False, "dry_run": True, "mode": mode, "payload": payload})

        if mode == "webhook":
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(p.webhook_url, json=payload)
            except httpx.HTTPError as exc:
                raise NodeExecutionError(f"Slack webhook request failed: {exc}") from exc
            if resp.status_code >= 400:
                raise NodeExecutionError(f"Slack webhook returned HTTP {resp.status_code}: {resp.text[:200]}")
            return self._single(
                {"delivered": True, "mode": "webhook", "status_code": resp.status_code, "response": resp.text[:200]}
            )

        from ...services.crypto import decrypt_credential

        cred = await decrypt_credential(context, p.credential_id)
        token = cred.get("token") or cred.get("value") or cred.get("api_key") or ""
        if not token:
            raise NodeExecutionError("Credential has no token field (expected a Slack bot token, xoxb-…)")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://slack.com/api/chat.postMessage",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            raise NodeExecutionError(f"Slack API request failed: {exc}") from exc
        data = safe_json(resp)
        if not data.get("ok", False):
            raise NodeExecutionError(f"Slack API error: {data.get('error', resp.text[:200])}")
        return self._single({"delivered": True, "mode": "bot", "channel": data.get("channel"), "ts": data.get("ts")})


def safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
