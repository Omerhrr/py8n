"""LLM Chat node - OpenAI-compatible or the built-in sandbox bridge."""

from __future__ import annotations

from typing import ClassVar

import httpx
from pydantic import BaseModel, Field

from ..context import ExecutionContext
from .base import BaseNode, Handle, NodeExecutionError, NodeResult


class LlmChatNode(BaseNode):
    type = "llm_chat"
    name = "LLM Chat"
    description = "Sends a chat prompt to an LLM (free sandbox bridge or your own OpenAI-compatible API)."
    category = "ai"
    icon = "brain"
    color = "#fb7185"

    class ParamsModel(BaseModel):
        provider: str = Field(
            default="sandbox_bridge",
            description="sandbox_bridge = free built-in model; openai_compatible = your own endpoint",
            json_schema_extra={"widget": "select", "options": ["sandbox_bridge", "openai_compatible"]},
        )
        model: str = Field(default="", description="Model name (optional; bridge picks a default)")
        system_prompt: str = Field(default="You are a helpful automation assistant.", json_schema_extra={"widget": "textarea", "rows": 3})
        user_prompt: str = Field(
            default="Summarize: {{ input }}",
            description="User message - supports {{ expressions }}",
            json_schema_extra={"widget": "textarea", "rows": 5},
        )
        temperature: float = Field(default=0.7, ge=0, le=2)
        max_tokens: int = Field(default=1024, ge=1, le=8192)
        credential_id: str | None = Field(default=None, description="OpenAI-compatible credential (base_url + api_key)")

    async def execute(self, context: ExecutionContext) -> NodeResult:
        p = self.params  # type: LlmChatNode.ParamsModel
        messages = [
            {"role": "system", "content": p.system_prompt},
            {"role": "user", "content": str(p.user_prompt)},
        ]

        if p.provider == "sandbox_bridge":
            from ...config import settings

            url = f"{settings.llm_bridge_url.rstrip('/')}/v1/chat/completions"
            headers = {}
            payload = {"messages": messages, "temperature": p.temperature, "max_tokens": p.max_tokens}
            if p.model:
                payload["model"] = p.model
        else:
            if not p.credential_id:
                raise NodeExecutionError("openai_compatible provider requires a credential")
            from ...services.crypto import decrypt_credential

            cred = await decrypt_credential(context, p.credential_id)
            if cred.get("type") != "openai_compatible":
                raise NodeExecutionError("Selected credential is not of type openai_compatible")
            base = (cred.get("base_url") or "").rstrip("/")
            if not base:
                raise NodeExecutionError("Credential is missing base_url")
            url = f"{base}/chat/completions"
            headers = {"Authorization": f"Bearer {cred.get('api_key', '')}"}
            payload = {
                "model": p.model or "gpt-4o-mini",
                "messages": messages,
                "temperature": p.temperature,
                "max_tokens": p.max_tokens,
            }

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise NodeExecutionError(f"LLM request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise NodeExecutionError(f"LLM API returned HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        # OpenAI-compatible shape
        content = ""
        usage: dict = {}
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError):
            content = data.get("content") or str(data)[:500]
        usage = data.get("usage") or {}

        return self._single({"text": content, "model": data.get("model", p.model or "bridge"), "usage": usage})
