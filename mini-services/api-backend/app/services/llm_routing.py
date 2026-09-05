"""LLM routing (v74) - one credential layer, every model provider.

The v19-era ``openai_compatible`` support required the user to hand-type a
base_url and only spoke one wire format. The brain deserves better: this
module is the SINGLE routing layer every LLM call in py8n goes through -
the ``llm_chat`` node, the ``ai_agent`` tool loop, and the voice agent's
brain - with a curated provider catalog and TWO wire formats:

* ``openai``    - the OpenAI Chat Completions shape. This is what OpenAI
                  speaks, and what DeepSeek, Moonshot (Kimi), Qwen
                  (DashScope compatible mode), OpenRouter, Groq, Together,
                  Mistral, xAI, Ollama, LM Studio and vLLM all expose as
                  their compatibility surface. One wire, many homes.
* ``anthropic`` - Claude's native Messages API: a different URL
                  (``/v1/messages``), different auth (``x-api-key`` +
                  ``anthropic-version``), the system prompt as a
                  TOP-LEVEL parameter, ``max_tokens`` REQUIRED, and a
                  content-BLOCKS response. Wrapped here so the rest of
                  py8n never sees the difference.

A credential stores ``{provider, base_url, api_key, ...}`` where
``provider`` is a key of ``PROVIDERS`` (the preset the credential was
created from; also the honest default for the model name when the user
left it blank). The wire format comes from the preset's ``kind``, not
from URL sniffing.

Honesty rules (the platform's usual):

* the API key rides the request headers ONLY - it is never logged, never
  echoed in errors (HTTP error bodies are truncated previews of what the
  PROVIDER sent, never the request);
* a missing base_url / api_key / model fails LOUD with the exact
  remediation, before any network byte is spent;
* ``transport`` is injectable (httpx.MockTransport in tests); the
  default is a real client - the same code path both ways.
"""

from __future__ import annotations

from typing import Any

import httpx

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 2048


class LLMRoutingError(ValueError):
    """Honest 4xx-grade routing failures (bad preset, missing config, HTTP error)."""


# ---------------------------------------------------------------------------
# The provider catalog
# ---------------------------------------------------------------------------

OPENAI_KIND = "openai"
ANTHROPIC_KIND = "anthropic"

PROVIDERS: dict[str, dict] = {
    "openai": {
        "label": "OpenAI",
        "kind": OPENAI_KIND,
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "o3-mini"],
        "default_model": "gpt-4o-mini",
        "docs": "platform.openai.com",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "kind": ANTHROPIC_KIND,
        "base_url": "https://api.anthropic.com/v1",
        "models": ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1",
                   "claude-3-5-haiku-latest"],
        "default_model": "claude-sonnet-4-5",
        "docs": "docs.anthropic.com",
    },
    "deepseek": {
        "label": "DeepSeek",
        "kind": OPENAI_KIND,
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-chat",
        "docs": "api-docs.deepseek.com",
    },
    "kimi": {
        "label": "Moonshot Kimi",
        "kind": OPENAI_KIND,
        "base_url": "https://api.moonshot.ai/v1",
        "models": ["kimi-k2-0905-preview", "kimi-latest", "moonshot-v1-8k"],
        "default_model": "kimi-latest",
        "docs": "platform.moonshot.ai (cn endpoint: api.moonshot.cn/v1)",
    },
    "qwen": {
        "label": "Alibaba Qwen (DashScope)",
        "kind": OPENAI_KIND,
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-max", "qwen-plus", "qwen-turbo"],
        "default_model": "qwen-plus",
        "docs": "dashscope-intl.aliyuncs.com/compatible-mode (cn: dashscope.aliyuncs.com)",
    },
    "openrouter": {
        "label": "OpenRouter",
        "kind": OPENAI_KIND,
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4.5",
                   "deepseek/deepseek-chat", "qwen/qwen-2.5-72b-instruct"],
        "default_model": "openai/gpt-4o-mini",
        "docs": "openrouter.ai/docs (any model slug)",
    },
    "groq": {
        "label": "Groq",
        "kind": OPENAI_KIND,
        "base_url": "https://api.groq.com/openai/v1",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
        "default_model": "llama-3.3-70b-versatile",
        "docs": "console.groq.com/docs",
    },
    "together": {
        "label": "Together AI",
        "kind": OPENAI_KIND,
        "base_url": "https://api.together.xyz/v1",
        "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo",
                   "Qwen/Qwen2.5-72B-Instruct-Turbo"],
        "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "docs": "docs.together.ai",
    },
    "mistral": {
        "label": "Mistral AI",
        "kind": OPENAI_KIND,
        "base_url": "https://api.mistral.ai/v1",
        "models": ["mistral-large-latest", "mistral-small-latest"],
        "default_model": "mistral-small-latest",
        "docs": "docs.mistral.ai",
    },
    "xai": {
        "label": "xAI (Grok)",
        "kind": OPENAI_KIND,
        "base_url": "https://api.x.ai/v1",
        "models": ["grok-4", "grok-3-mini"],
        "default_model": "grok-3-mini",
        "docs": "docs.x.ai",
    },
    "ollama": {
        "label": "Ollama (local)",
        "kind": OPENAI_KIND,
        "base_url": "http://localhost:11434/v1",
        "models": ["llama3.2", "qwen2.5", "deepseek-r1"],
        "default_model": "llama3.2",
        "keyless": True,
        "docs": "github.com/ollama/ollama (OpenAI-compatible endpoint)",
    },
    "lm_studio": {
        "label": "LM Studio (local)",
        "kind": OPENAI_KIND,
        "base_url": "http://localhost:1234/v1",
        "models": ["(the model loaded in LM Studio)"],
        "default_model": "",
        "keyless": True,
        "docs": "lmstudio.ai (local server, OpenAI-compatible)",
    },
    "vllm": {
        "label": "vLLM (self-hosted)",
        "kind": OPENAI_KIND,
        "base_url": "http://localhost:8000/v1",
        "models": ["(the served model name)"],
        "default_model": "",
        "keyless": True,
        "docs": "docs.vllm.ai (OpenAI-compatible server)",
    },
}

# The credential types this router accepts (mirrors the vault's SECRET_FIELDS).
CREDENTIAL_KIND_TO_TYPE = {OPENAI_KIND: "openai_compatible",
                           ANTHROPIC_KIND: "anthropic"}
CREDENTIAL_TYPES = ("openai_compatible", "anthropic")


def providers_out() -> list[dict]:
    """The catalog for the credentials UI (derived, nothing stored)."""
    out = []
    for key, p in PROVIDERS.items():
        out.append({
            "provider": key,
            "label": p["label"],
            "kind": p["kind"],
            "credential_type": CREDENTIAL_KIND_TO_TYPE[p["kind"]],
            "base_url": p["base_url"],
            "models": list(p["models"]),
            "default_model": p.get("default_model") or "",
            "keyless": bool(p.get("keyless")),
            "docs": p.get("docs") or "",
        })
    return out


def preset_credential_data(provider: str, api_key: str = "", model: str = "") -> dict:
    """The credential payload for a preset - the preset fills base_url +
    the provider key; the caller adds the secret. Unknown preset fails loud."""
    p = PROVIDERS.get(str(provider or "").strip())
    if p is None:
        raise LLMRoutingError(
            f"unknown LLM provider {provider!r} - known: {', '.join(sorted(PROVIDERS))}")
    return {
        "provider": provider,
        "base_url": p["base_url"],
        "api_key": api_key,
        "suggested_model": model or p.get("default_model") or "",
    }


def resolve_credential(data: dict) -> dict:
    """Validate a decrypted credential payload for routing.

    Returns ``{provider, kind, base_url, api_key, default_model}``. The
    provider key may be explicit in the data; a legacy credential without
    one is honestly assumed openai-compatible (the only shape the vault
    knew before v74)."""
    data = data or {}
    base = str(data.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise LLMRoutingError(
            "the credential carries no base_url - edit it and set the provider's "
            "endpoint (use the presets from GET /credentials/providers)")
    provider = str(data.get("provider") or "").strip()
    if not provider:
        # legacy credential: base_url typed by hand, no preset recorded.
        # Anthropic's URL is recognized honestly rather than mis-probed.
        if base.endswith("anthropic.com/v1") or "anthropic.com" in base:
            provider = "anthropic"
        else:
            provider = "openai_compatible_custom"
    p = PROVIDERS.get(provider)
    kind = p["kind"] if p else (
        ANTHROPIC_KIND if provider == "anthropic" else OPENAI_KIND)
    key = str(data.get("api_key") or "")
    if not key and not (p and p.get("keyless")):
        raise LLMRoutingError(
            f"the credential has no api_key - {p['label'] if p else provider} requires "
            "one (local runtimes like Ollama / LM Studio / vLLM are the keyless "
            "exceptions; create their credential with any non-empty placeholder)")
    return {
        "provider": provider,
        "label": p["label"] if p else provider,
        "kind": kind,
        "base_url": base,
        "api_key": key,
        "default_model": (p.get("default_model") if p else "") or "",
    }


# ---------------------------------------------------------------------------
# Message shaping
# ---------------------------------------------------------------------------


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """Anthropic wants the system prompt TOP-LEVEL and the messages list
    free of system roles. All system content is joined; the rest passes
    through (role/content pairs)."""
    system_parts: list[str] = []
    rest: list[dict] = []
    for m in messages:
        role = str(m.get("role") or "user")
        content = m.get("content")
        if role == "system":
            system_parts.append(content if isinstance(content, str) else str(content))
        else:
            rest.append({"role": role, "content": content})
    return "\n\n".join(p for p in system_parts if p), rest


def _anthropic_payload(data: dict, messages: list[dict], *, model: str,
                       temperature: float, max_tokens: int) -> dict:
    system, msgs = _split_system(messages)
    if not msgs:
        raise LLMRoutingError("anthropic requires at least one user/assistant message")
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,  # REQUIRED by the Messages API - no default
        "messages": msgs,
        "temperature": temperature,
    }
    if system:
        payload["system"] = system
    return payload


def _openai_payload(data: dict, messages: list[dict], *, model: str,
                    temperature: float, max_tokens: int) -> dict:
    return {"model": model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens}


def _parse_openai_response(data: dict) -> tuple[str, dict]:
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        text = data.get("content") or ""
    usage_raw = data.get("usage") or {}
    usage = {"prompt_tokens": usage_raw.get("prompt_tokens"),
             "completion_tokens": usage_raw.get("completion_tokens"),
             "total_tokens": usage_raw.get("total_tokens")}
    return str(text), {k: v for k, v in usage.items() if v is not None}


def _parse_anthropic_response(data: dict) -> tuple[str, dict]:
    # content is a BLOCK list: [{"type": "text", "text": ...}, ...]
    parts: list[str] = []
    content = data.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
    elif isinstance(content, str):
        parts.append(content)
    usage_raw = data.get("usage") or {}
    usage = {"prompt_tokens": usage_raw.get("input_tokens"),
             "completion_tokens": usage_raw.get("output_tokens")}
    total = (usage["prompt_tokens"] or 0) + (usage["completion_tokens"] or 0)
    if total:
        usage["total_tokens"] = total
    return "".join(parts), {k: v for k, v in usage.items() if v}


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

# Test seam: when set, every routing call uses this transport instead of a
# fresh real client. Production never touches it.
_TRANSPORT_OVERRIDE: httpx.BaseTransport | None = None


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _TRANSPORT_OVERRIDE
    _TRANSPORT_OVERRIDE = transport


def get_transport() -> httpx.BaseTransport | None:
    return _TRANSPORT_OVERRIDE


async def chat_completion(cred_data: dict, *, model: str = "",
                          messages: list[dict], temperature: float = 0.4,
                          max_tokens: int = DEFAULT_MAX_TOKENS,
                          timeout: float = 180.0) -> dict:
    """Route ONE chat completion through the credential's provider.

    Returns ``{text, model, provider, kind, usage, stop_reason}``. Raises
    ``LLMRoutingError`` with the exact cause on any failure - the caller
    (node or phone brain) surfaces it, never silently degrades.
    """
    cred = resolve_credential(cred_data)
    chosen = str(model or "").strip() or cred["default_model"]
    if not chosen:
        raise LLMRoutingError(
            "no model given and the provider preset carries no default - pass the "
            "model explicitly (e.g. the preset's suggested model)")

    if cred["kind"] == ANTHROPIC_KIND:
        url = f"{cred['base_url']}/messages"
        headers = {"x-api-key": cred["api_key"],
                   "anthropic-version": ANTHROPIC_VERSION,
                   "content-type": "application/json"}
        payload = _anthropic_payload(cred, messages, model=chosen,
                                     temperature=temperature, max_tokens=max_tokens)
    else:
        url = f"{cred['base_url']}/chat/completions"
        headers = {"Authorization": f"Bearer {cred['api_key']}",
                   "content-type": "application/json"}
        payload = _openai_payload(cred, messages, model=chosen,
                                  temperature=temperature, max_tokens=max_tokens)

    transport = _TRANSPORT_OVERRIDE
    try:
        if transport is not None:
            client = httpx.AsyncClient(transport=transport, timeout=timeout)
        else:
            client = httpx.AsyncClient(timeout=timeout)
        async with client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise LLMRoutingError(f"LLM request to {cred['label']} failed: {exc}") from exc

    if resp.status_code >= 400:
        body = (resp.text or "")[:300]
        raise LLMRoutingError(
            f"{cred['label']} returned HTTP {resp.status_code} for {chosen!r}: {body}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise LLMRoutingError(
            f"{cred['label']} returned a non-JSON body (HTTP {resp.status_code})") from exc

    if cred["kind"] == ANTHROPIC_KIND:
        text, usage = _parse_anthropic_response(data)
    else:
        text, usage = _parse_openai_response(data)
    if not str(text).strip():
        raise LLMRoutingError(
            f"{cred['label']} returned an empty completion (HTTP {resp.status_code})")
    stop_reason = data.get("stop_reason")
    if not stop_reason:
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            stop_reason = choices[0].get("finish_reason")
    return {
        "text": text,
        "model": data.get("model") or chosen,
        "provider": cred["provider"],
        "label": cred["label"],
        "kind": cred["kind"],
        "usage": usage,
        "stop_reason": stop_reason,
    }


def credential_type_matches(cred_type: str) -> bool:
    """Which vault credential types the router accepts."""
    return cred_type in CREDENTIAL_TYPES
