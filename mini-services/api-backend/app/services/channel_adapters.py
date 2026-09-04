"""Real provider adapters (v69) - py8n IS the webhook receiver.

v68 made channels interchangeable METADATA with a universal ingress that
expected already-normalized messages. v69 closes the last mile: these
adapters speak each provider's NATIVE webhook dialect and produce the
normalized interaction-layer shape, plus the exact outbound request each
provider's send API expects. Everything here is PURE (parse/verify/build);
the HTTP plumbing lives in services/channel_endpoints.py, so every rule
below is unit-testable without network.

The three webhook-native adapters:

* **meta_cloud_api** (WhatsApp) - Meta's Cloud API. Inbound webhooks POST
  ``{object, entry: [{changes: [{value: {messages, contacts, statuses}}]}]}``;
  a webhook can carry MANY messages and delivery statuses (which are not
  messages - honest skips). Verification is two-fold: the GET handshake
  (hub.mode=subscribe + hub.verify_token -> echo hub.challenge) and the
  ``X-Hub-Signature-256`` HMAC-SHA256 of the RAW body against the app
  secret. Outbound goes to the Graph API:
  ``POST graph.facebook.com/v21.0/{phone_number_id}/messages``.
* **telegram_bot_api** - the Bot API webhook POSTs full update objects
  (``{update_id, message: {from, chat, text}}``). Edits and non-text
  messages are honestly skipped. Verification: the
  ``X-Telegram-Bot-Api-Secret-Token`` header equals the secret set in
  setWebhook (timing-safe). Outbound:
  ``POST api.telegram.org/bot{token}/sendMessage {chat_id, text}``.
* **discord_bot** - Discord interactions arrive as signed payloads;
  ``type: 1`` is the URL-verification PING (answer ``{"type": 1}``),
  application commands carry text in their options. Verification:
  Ed25519 over ``{timestamp}{body}`` against the app's public key with
  the ``X-Signature-Ed25519`` / ``X-Signature-Timestamp`` headers.
  Outbound: Discord execute-webhook ``POST {webhook_url} {content}``.

Every adapter reports the same shape: ``parse(...) -> ParseResult``
(normalized messages + honest skips), ``verify(...) -> bool``, and
``build_outbound(...) -> {method, url, headers, json}`` - the request
py8n would make to DELIVER a reply, credentials included only at
delivery time.
"""

from __future__ import annotations

import hashlib
import hmac
import hmac as _hmac
import json
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# The normalized shape every adapter produces
# ---------------------------------------------------------------------------


@dataclass
class NormalizedInbound:
    """One user message, provider dialect stripped away."""

    channel: str
    sender_id: str
    sender_name: str
    text: str
    event_id: str = ""            # provider message id (dedupe reference)
    extra: dict = field(default_factory=dict)  # provider extras for the transcript payload


@dataclass
class ParseResult:
    """What a provider webhook contained: real messages + honest skips."""

    messages: list[NormalizedInbound] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # [{reason, detail}]

    @property
    def count(self) -> int:
        return len(self.messages)


# ---------------------------------------------------------------------------
# meta_cloud_api - WhatsApp through Meta's Cloud API
# ---------------------------------------------------------------------------

META_GRAPH_VERSION = "v21.0"


def meta_verify_handshake(params: dict, verify_token: str) -> tuple[bool, str | None]:
    """Meta's GET verification handshake.

    Meta calls the webhook URL with hub.mode=subscribe, hub.verify_token
    (the value YOU typed into the app dashboard) and hub.challenge; a
    matching endpoint must answer 200 with the raw challenge text. Returns
    (ok, challenge_or_none).
    """
    mode = str(params.get("hub.mode") or params.get("mode") or "")
    token = str(params.get("hub.verify_token") or params.get("verify_token") or "")
    challenge = str(params.get("hub.challenge") or params.get("challenge") or "")
    if mode != "subscribe" or not verify_token or not _hmac.compare_digest(token, verify_token):
        return False, None
    return True, challenge or None


def meta_verify_signature(app_secret: str, raw_body: bytes, header: str) -> bool:
    """``X-Hub-Signature-256: sha256=<hex hmac-sha256(app_secret, raw_body)>``."""
    if not app_secret or not header:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = header.strip()
    if provided.lower().startswith("sha256="):
        provided = provided[7:]
    return _hmac.compare_digest(provided, expected)


def meta_parse_webhook(payload: dict) -> ParseResult:
    """Flatten Meta's entry->changes->messages nesting into messages.

    Skips: delivery/read statuses, non-text message types without a
    caption (image/video/audio/sticker/location/contact), unsupported
    objects and changes without a messages value.
    """
    result = ParseResult()
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        result.skipped.append({"reason": "unsupported_payload",
                               "detail": "object must be 'whatsapp_business_account'"})
        return result
    entries = payload.get("entry") or []
    if not isinstance(entries, list):
        result.skipped.append({"reason": "unsupported_payload", "detail": "entry must be a list"})
        return result
    for entry in entries:
        for change in (entry or {}).get("changes") or []:
            value = (change or {}).get("value") or {}
            contacts = {str(c.get("wa_id")): ((c.get("profile") or {}).get("name") or "")
                        for c in value.get("contacts") or [] if isinstance(c, dict)}
            statuses = value.get("statuses")
            if statuses and not value.get("messages"):
                result.skipped.append({"reason": "status_update",
                                       "detail": f"{len(statuses)} delivery status event(s) - not messages"})
                continue
            for msg in value.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                mtype = str(msg.get("type") or "")
                text = ""
                if mtype == "text":
                    text = str((msg.get("text") or {}).get("body") or "")
                elif mtype in ("image", "video", "document", "audio", "sticker"):
                    text = str((msg.get(mtype) or {}).get("caption") or "")
                if not text:
                    result.skipped.append({
                        "reason": "non_text_message",
                        "detail": f"message type {mtype!r} has no caption to read"})
                    continue
                sender = str(msg.get("from") or "")
                result.messages.append(NormalizedInbound(
                    channel="whatsapp",
                    sender_id=sender,
                    sender_name=contacts.get(sender, ""),
                    text=text,
                    event_id=str(msg.get("id") or ""),
                    extra={"phone_number_id": str((value.get("metadata") or {}).get("phone_number_id") or "")},
                ))
    return result


def meta_build_outbound(config: dict, to: str, text: str) -> dict:
    """The Graph API request that sends a WhatsApp text message."""
    phone_number_id = str(config.get("phone_number_id") or "")
    token = str(config.get("access_token") or config.get("bot_token") or "")
    return {
        "method": "POST",
        "url": f"https://graph.facebook.com/{META_GRAPH_VERSION}/{phone_number_id}/messages",
        "headers": {"Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"},
        "json": {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        },
    }


# ---------------------------------------------------------------------------
# telegram_bot_api - the Bot API webhook
# ---------------------------------------------------------------------------


def telegram_verify_secret(header_value: str, secret_token: str) -> bool:
    """``X-Telegram-Bot-Api-Secret-Token`` matches setWebhook's secret."""
    if not secret_token or not header_value:
        return False
    return _hmac.compare_digest(header_value.strip(), secret_token)


def telegram_parse_update(payload: dict) -> ParseResult:
    """Translate a Bot API update into messages; skip what is not chat.

    Handled: private/group ``message`` updates with text or caption.
    Skipped: edited_message (an edit, not a new message), channel_post,
    non-text content without caption, messages without a sender.
    """
    result = ParseResult()
    if not isinstance(payload, dict):
        result.skipped.append({"reason": "unsupported_payload", "detail": "update must be an object"})
        return result
    update_id = payload.get("update_id")
    body = payload.get("message")
    if body is None:
        for kind in ("edited_message", "channel_post", "edited_channel_post",
                     "callback_query", "my_chat_member", "chat_member"):
            if kind in payload:
                result.skipped.append({"reason": kind,
                                       "detail": f"{kind} updates are not chat messages"})
                return result
        result.skipped.append({"reason": "no_message", "detail": "update carries no message"})
        return result
    sender = body.get("from") or {}
    sender_id = str(sender.get("id") or "")
    if not sender_id:
        result.skipped.append({"reason": "no_sender", "detail": "message has no from.id"})
        return result
    text = str(body.get("text") or body.get("caption") or "")
    if not text:
        result.skipped.append({"reason": "non_text_message",
                               "detail": "message has no text or caption"})
        return result
    name = " ".join(str(sender.get(k) or "") for k in ("first_name", "last_name")).strip() \
        or str(sender.get("username") or "")
    result.messages.append(NormalizedInbound(
        channel="telegram",
        sender_id=sender_id,
        sender_name=name,
        text=text,
        event_id=f"{update_id}:{body.get('message_id')}" if update_id is not None else str(body.get("message_id") or ""),
        extra={"chat_id": str((body.get("chat") or {}).get("id") or ""),
               "chat_type": str((body.get("chat") or {}).get("type") or "")},
    ))
    return result


def telegram_build_outbound(config: dict, to: str, text: str) -> dict:
    """The Bot API request that sends a Telegram message to a chat."""
    token = str(config.get("bot_token") or "")
    return {
        "method": "POST",
        "url": f"https://api.telegram.org/bot{token}/sendMessage",
        "headers": {"Content-Type": "application/json"},
        "json": {"chat_id": to, "text": text, "parse_mode": "HTML"},
    }


# ---------------------------------------------------------------------------
# discord_bot - interactions with Ed25519 signatures
# ---------------------------------------------------------------------------

def discord_verify_signature(public_key_hex: str, timestamp: str, raw_body: bytes,
                             signature_hex: str) -> tuple[bool, str | None]:
    """Discord's Ed25519 scheme: sign(timestamp + body), hex-encoded.

    Returns (ok, error_detail). Missing/invalid keys fail honest instead
    of pretending to verify.
    """
    if not public_key_hex:
        return False, "endpoint has no public_key configured"
    if not signature_hex or not timestamp:
        return False, "missing X-Signature-Ed25519 or X-Signature-Timestamp headers"
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:  # pragma: no cover - cryptography is a hard dep via Fernet
        return False, "cryptography backend unavailable for Ed25519 verification"
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        pub.verify(bytes.fromhex(signature_hex), timestamp.encode("utf-8") + raw_body)
    except (InvalidSignature, ValueError):
        return False, "signature verification failed"
    return True, None


def discord_parse_interaction(payload: dict) -> tuple[str | None, ParseResult]:
    """Translate a Discord interaction.

    Returns (response_body_or_None, parse_result): PING (type 1) returns
    the ``{"type": 1}`` pong and no messages; application commands (type
    2) become one message built from the command name + string options;
    everything else is skipped honestly (no response body).
    """
    result = ParseResult()
    if not isinstance(payload, dict):
        result.skipped.append({"reason": "unsupported_payload", "detail": "interaction must be an object"})
        return None, result
    itype = payload.get("type")
    if itype == 1:
        return {"type": 1}, result
    if itype not in (2, 3):
        result.skipped.append({"reason": "unsupported_interaction",
                               "detail": f"interaction type {itype!r} is not a command"})
        return None, result
    data = payload.get("data") or {}
    member = payload.get("member") or {}
    user = member.get("user") or payload.get("user") or {}
    sender_id = str(user.get("id") or "")
    if not sender_id:
        result.skipped.append({"reason": "no_sender", "detail": "interaction has no user id"})
        return None, result
    parts = [str(data.get("name") or "command")]
    for opt in data.get("options") or []:
        if isinstance(opt, dict) and opt.get("value") is not None:
            parts.append(str(opt["value"]))
    text = " ".join(p for p in parts if p)
    if not text:
        result.skipped.append({"reason": "no_text", "detail": "command carries no text"})
        return None, result
    result.messages.append(NormalizedInbound(
        channel="discord",
        sender_id=sender_id,
        sender_name=str(user.get("global_name") or user.get("username") or ""),
        text=text,
        event_id=str(payload.get("id") or ""),
        extra={"channel_id": str(payload.get("channel_id") or ""),
               "guild_id": str(payload.get("guild_id") or "")},
    ))
    return None, result


def discord_build_outbound(config: dict, to: str, text: str) -> dict:
    """The execute-webhook request that posts into a Discord channel.

    The endpoint's configured webhook_url IS the destination (Discord
    webhooks address a channel themselves); ``to`` is only a fallback.
    """
    webhook_url = str(config.get("webhook_url") or to or "")
    return {
        "method": "POST",
        "url": webhook_url,
        "headers": {"Content-Type": "application/json"},
        "json": {"content": text[:2000], "allowed_mentions": {"parse": []}},
    }


# ---------------------------------------------------------------------------
# The adapter registry - provider id -> contract
# ---------------------------------------------------------------------------

REQUIRED_CONFIG: dict[str, dict[str, list[str]]] = {
    # secret: used to VERIFY the webhook (providers prove themselves)
    # credential: used to DELIVER outbound (py8n proves itself to the provider)
    "meta_cloud_api": {"channel": "whatsapp", "secret": ["verify_token", "app_secret"],
                       "credential": ["access_token", "phone_number_id"],
                       "description": "WhatsApp Business via Meta Cloud API"},
    "telegram_bot_api": {"channel": "telegram", "secret": ["secret_token"],
                         "credential": ["bot_token", "chat_prefix"],
                         "description": "Telegram bots via the Bot API webhook"},
    "discord_bot": {"channel": "discord", "secret": ["public_key"],
                    "credential": ["webhook_url"],
                    "description": "Discord via signed interactions"},
}


def parse_inbound(provider: str, payload: dict) -> ParseResult:
    """One entry point for parsing: provider id -> messages + skips."""
    if provider == "meta_cloud_api":
        return meta_parse_webhook(payload)
    if provider == "telegram_bot_api":
        return telegram_parse_update(payload)
    if provider == "discord_bot":
        _resp, result = discord_parse_interaction(payload)
        return result
    raise ValueError(f"unknown provider {provider!r}")


def verify_request(provider: str, endpoint_config: dict, *, raw_body: bytes,
                   headers: dict, query_params: dict | None = None) -> tuple[bool, str | None]:
    """Pure verification: bytes + headers in, verdict out (no Request object)."""
    if provider == "meta_cloud_api":
        ok = meta_verify_signature(str(endpoint_config.get("app_secret") or ""),
                                   raw_body, headers.get("x-hub-signature-256", ""))
        return (True, None) if ok else (False, "X-Hub-Signature-256 verification failed")
    if provider == "telegram_bot_api":
        ok = telegram_verify_secret(headers.get("x-telegram-bot-api-secret-token", ""),
                                    str(endpoint_config.get("secret_token") or ""))
        return (True, None) if ok else (False, "X-Telegram-Bot-Api-Secret-Token mismatch")
    if provider == "discord_bot":
        return discord_verify_signature(str(endpoint_config.get("public_key") or ""),
                                        headers.get("x-signature-timestamp", ""),
                                        raw_body, headers.get("x-signature-ed25519", ""))
    raise ValueError(f"unknown provider {provider!r}")


def build_outbound(provider: str, config: dict, to: str, text: str) -> dict:
    """The exact HTTP request each provider's send API expects."""
    if provider == "meta_cloud_api":
        return meta_build_outbound(config, to, text)
    if provider == "telegram_bot_api":
        return telegram_build_outbound(config, to, text)
    if provider == "discord_bot":
        return discord_build_outbound(config, to, text)
    raise ValueError(f"unknown provider {provider!r}")


def mask_config(config: dict) -> dict:
    """API output form of an endpoint config: secrets show shape, never value."""
    out: dict = {}
    for key, value in (config or {}).items():
        s = str(value)
        if not s:
            out[key] = ""
        elif key in ("verify_token", "app_secret", "bot_token", "secret_token",
                     "access_token", "public_key"):
            out[key] = f"{s[:4]}...({len(s)} chars)"
        else:
            out[key] = s
    return out


def endpoint_webhook_path(provider: str, endpoint_id: str) -> str:
    return f"/api/v1/channels/{PROVIDER_PATHS[provider]}/{endpoint_id}/webhook"


PROVIDER_PATHS: dict[str, str] = {
    "meta_cloud_api": "whatsapp",
    "telegram_bot_api": "telegram",
    "discord_bot": "discord",
}
