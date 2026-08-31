"""HTTP Request action node."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field

from ..context import ExecutionContext
from .base import BaseNode, Handle, NodeExecutionError, NodeResult


class HttpRequestNode(BaseNode):
    type = "http_request"
    name = "HTTP Request"
    description = "Calls any REST/HTTP API and returns status, headers and parsed body."
    category = "actions"
    icon = "globe"
    color = "#06b6d4"

    class ParamsModel(BaseModel):
        method: str = Field(default="GET", json_schema_extra={"widget": "select", "options": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]})
        url: str = Field(default="https://", description="Request URL - supports {{ expressions }}")
        headers: dict = Field(default_factory=dict, json_schema_extra={"widget": "code", "rows": 4})
        query_params: dict = Field(default_factory=dict, json_schema_extra={"widget": "code", "rows": 4})
        body_type: str = Field(default="none", json_schema_extra={"widget": "select", "options": ["none", "json", "raw"]})
        body: Any = Field(default=None, json_schema_extra={"widget": "code", "rows": 6})
        timeout_seconds: float = Field(default=30, ge=1, le=120)
        fail_on_error: bool = Field(default=True, description="Mark the node failed on non-2xx responses")
        credential_id: str | None = Field(default=None, description="Optional auth credential (header_auth or basic_auth)")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: HttpRequestNode.ParamsModel
        headers = dict(p.headers or {})
        auth: tuple[str, str] | None = None
        if p.credential_id:
            from ...services.crypto import decrypt_credential  # local import avoids cycle

            cred = await decrypt_credential(context, p.credential_id)
            if cred.get("type") == "header_auth":
                name = cred.get("header_name") or "Authorization"
                headers[name] = cred.get("value", "")
            elif cred.get("type") == "basic_auth":
                username = cred.get("username") or ""
                password = cred.get("password") or ""
                if not username:
                    raise NodeExecutionError("basic_auth credential is missing 'username'")
                auth = (username, password)

        kwargs: dict[str, Any] = {
            "method": p.method.upper(),
            "url": p.url,
            "headers": headers or None,
            "params": p.query_params or None,
            "timeout": p.timeout_seconds,
            "follow_redirects": True,
        }
        if auth is not None:
            kwargs["auth"] = auth
        if p.body_type == "json" and p.body is not None:
            kwargs["json"] = p.body
        elif p.body_type == "raw" and p.body is not None:
            kwargs["content"] = str(p.body).encode()

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.request(**kwargs)
        except httpx.HTTPError as exc:
            raise NodeExecutionError(f"HTTP request failed: {exc}") from exc

        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text[: context.trigger_payload.get("_max_capture", 20000)]

        payload = {
            "status_code": resp.status_code,
            "headers": dict(list(resp.headers.items())[:30]),
            "body": body,
            "url": str(resp.request.url),
        }
        if p.fail_on_error and resp.status_code >= 400:
            raise NodeExecutionError(f"HTTP {resp.status_code} from {p.url}: {str(body)[:300]}")
        return self._single(payload)
