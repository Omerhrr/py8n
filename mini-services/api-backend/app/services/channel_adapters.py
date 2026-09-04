"""Real provider adapters (v69+v70) - py8n IS the webhook receiver.

v68 made channels interchangeable METADATA with a universal ingress that
expected already-normalized messages. v69 closed the last mile for the
chat channels; v70 adds voice (Telnyx Call Control - SIP and PSTN both
ride the same call-control webhooks) and WhatsApp interactive buttons.
Everything here is PURE (parse/verify/build); the HTTP plumbing lives in
services/channel_endpoints.py, so every rule below is unit-testable
without network.

The webhook-native adapters:

* **meta_cloud_api** (WhatsApp) - Meta's Cloud API. Inbound webhooks POST
  ``{object, entry: [{changes: [{value: {messages, contacts, statuses}}]}]}``;
  a webhook can carry MANY messages, delivery statuses (not messages -
  honest skips) and INTERACTIVE replies (button/list taps, form
  submissions - v70: the tap IS the message). Verification is two-fold:
  the GET handshake (hub.mode=subscribe + hub.verify_token -> echo
  hub.challenge) and the ``X-Hub-Signature-256`` HMAC-SHA256 of the RAW
  body against the app secret. Outbound: text via Graph API messages, or
  INTERACTIVE reply buttons (``meta_build_interactive`` - Meta's limits
  enforced exactly: 1..3 buttons, title <= 20 chars).
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
* **telnyx_call_control** (v70) - SIP/PSTN calls through Telnyx Call
  Control. Events arrive as ``{data: {event_type, payload}}`` with the
  ``call_control_id`` identifying the call (sip: URIs in from/to when
  the trunk is SIP). Verification: HTTP Message Signatures (RFC 9421) -
  Ed25519 over the signature base derived from the request's covered
  components (``Signature-Input``/``Signature`` headers). Outbound: Call
  Control ACTIONS - ``POST {api}/v2/calls/{id}/actions/{answer,speak,
  hangup,...}``. The media itself rides the v70 websocket transport.

Every adapter reports the same shape: ``parse(...) -> ParseResult``
(normalized messages/events + honest skips), ``verify(...) -> bool``,
and ``build_outbound(...)/build_command(...) -> {method, url, headers,
json}`` - the request py8n would make to DELIVER a reply, credentials
included only at delivery time.
"""

from __future__ import annotations

import base64
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
                extra: dict = {"phone_number_id": str((value.get("metadata") or {}).get("phone_number_id") or "")}
                if mtype == "text":
                    text = str((msg.get("text") or {}).get("body") or "")
                elif mtype in ("image", "video", "document", "audio", "sticker"):
                    text = str((msg.get(mtype) or {}).get("caption") or "")
                elif mtype == "interactive":
                    # v70: the caller TAPPED a button / picked a list row /
                    # submitted a form - the reply IS the message.
                    inter = msg.get("interactive") or {}
                    itype = str(inter.get("type") or "")
                    if itype == "button_reply":
                        br = inter.get("button_reply") or {}
                        text = str(br.get("title") or "")
                        extra["interactive_type"] = "button_reply"
                        extra["interactive_reply_id"] = str(br.get("id") or "")
                    elif itype == "list_reply":
                        lr = inter.get("list_reply") or {}
                        text = str(lr.get("title") or "")
                        extra["interactive_type"] = "list_reply"
                        extra["interactive_reply_id"] = str(lr.get("id") or "")
                    elif itype == "nfm_reply":
                        nr = inter.get("nfm_reply") or {}
                        text = str(nr.get("response_json") or "")
                        extra["interactive_type"] = "nfm_reply"
                        extra["form_name"] = str(nr.get("name") or "")
                elif mtype == "button":
                    # template quick-reply buttons
                    btn = msg.get("button") or {}
                    text = str(btn.get("text") or "")
                    extra["button_payload"] = str(btn.get("payload") or "")
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
                    extra=extra,
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


# Meta's documented interactive-message limits (fail loud, never truncate
# a business message silently).
META_BUTTON_MAX = 3
META_BUTTON_TITLE_MAX = 20
META_BUTTON_ID_MAX = 256
META_INTERACTIVE_BODY_MAX = 1024


def meta_build_interactive(config: dict, to: str, body: str, buttons: list[dict],
                           *, header: str = "", footer: str = "") -> dict:
    """The Graph API request that sends WhatsApp INTERACTIVE buttons.

    Buttons are reply buttons (``type: reply``): the tap comes back as an
    ``interactive.button_reply`` webhook with the button's id - the round
    trip that turns a chat into a menu-driven flow. Meta's limits are
    enforced exactly: 1..3 buttons, title <= 20 chars, id <= 256 chars,
    body <= 1024 chars; violations raise ValueError with the exact reason.
    """
    phone_number_id = str(config.get("phone_number_id") or "")
    token = str(config.get("access_token") or config.get("bot_token") or "")
    body = str(body or "")
    if not body.strip():
        raise ValueError("interactive message requires a non-empty body")
    if len(body) > META_INTERACTIVE_BODY_MAX:
        raise ValueError(f"interactive body exceeds Meta's {META_INTERACTIVE_BODY_MAX}-char limit "
                         f"(got {len(body)})")
    if not isinstance(buttons, list) or not (1 <= len(buttons) <= META_BUTTON_MAX):
        raise ValueError(f"interactive messages carry 1..{META_BUTTON_MAX} buttons, got "
                         f"{len(buttons) if isinstance(buttons, list) else type(buttons).__name__}")
    clean: list[dict] = []
    seen_ids: set[str] = set()
    for i, btn in enumerate(buttons, start=1):
        bid = str((btn or {}).get("id") or "").strip()
        title = str((btn or {}).get("title") or "").strip()
        if not bid or not title:
            raise ValueError(f"button {i} requires both 'id' and 'title'")
        if len(bid) > META_BUTTON_ID_MAX:
            raise ValueError(f"button {i} id exceeds Meta's {META_BUTTON_ID_MAX}-char limit")
        if len(title) > META_BUTTON_TITLE_MAX:
            raise ValueError(f"button {i} title exceeds Meta's {META_BUTTON_TITLE_MAX}-char "
                             f"limit (got {len(title)}: {title!r})")
        if bid in seen_ids:
            raise ValueError(f"button id {bid!r} is duplicated - ids must be unique")
        seen_ids.add(bid)
        clean.append({"type": "reply", "reply": {"id": bid, "title": title}})
    interactive: dict = {"type": "button", "body": {"text": body}}
    if header:
        interactive["header"] = {"type": "text", "text": str(header)[:60]}
    if footer:
        interactive["footer"] = {"text": str(footer)[:60]}
    interactive["action"] = {"buttons": clean}
    return {
        "method": "POST",
        "url": f"https://graph.facebook.com/{META_GRAPH_VERSION}/{phone_number_id}/messages",
        "headers": {"Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"},
        "json": {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
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
# telnyx_call_control - SIP/PSTN calls through Telnyx Call Control
# ---------------------------------------------------------------------------

TELNYX_API_BASE = "https://api.telnyx.com/v2"

# The call-control commands py8n builds (the agent's side of the call).
TELNYX_COMMANDS = ("answer", "hangup", "speak", "gather_using_audio", "transfer", "reject")

# Telnyx hangup causes -> py8n end kinds (the honest ending of a call).
TELNYX_HANGUP_CAUSES: dict[str, str] = {
    "NORMAL_CLEARING": "hangup",
    "NO_ANSWER": "no_answer",
    "USER_BUSY": "busy",
    "CALL_REJECTED": "failed",
    "ORIGINATOR_CANCEL": "failed",
    "NETWORK_OUT_OF_ORDER": "failed",
    "DESTINATION_OUT_OF_ORDER": "failed",
    "SERVICE_UNAVAILABLE": "failed",
}


@dataclass
class TelnyxEvent:
    """One call-control webhook event, mapped onto py8n voice semantics.

    ``kind`` is the v69 voice event kind the state machine (or the
    receiver's dedicated handling) applies; ``end_kind`` names the honest
    ending when the call hung up; ``digits``/``speak_text`` carry the
    gather/speak payloads the voice layer acts on.
    """

    event_type: str
    call_control_id: str = ""
    call_session_id: str = ""
    direction: str = ""          # incoming | outgoing
    from_ref: str = ""           # E.164 or sip: URI (SIP trunking rides through)
    to_ref: str = ""
    kind: str | None = None      # call.ringing | call.answered | hangup-family | dtmf | ...
    end_kind: str | None = None  # hangup | no_answer | busy | failed
    digits: str = ""
    hangup_cause: str = ""
    client_state: str = ""

    def out(self) -> dict:
        return {"event_type": self.event_type, "call_control_id": self.call_control_id,
                "call_session_id": self.call_session_id, "direction": self.direction,
                "from": self.from_ref, "to": self.to_ref, "kind": self.kind,
                "end_kind": self.end_kind, "digits": self.digits,
                "hangup_cause": self.hangup_cause}


@dataclass
class TelnyxParseResult:
    events: list[TelnyxEvent] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.events)


def telnyx_parse_webhook(payload: dict) -> TelnyxParseResult:
    """Translate a Telnyx Call Control webhook into voice events.

    Telnyx nests everything under ``data: {event_type, payload}``; the
    ``payload`` carries the call_control_id that IDENTIFIES the call
    (py8n finds-or-creates its session by it) plus the semantics. SIP
    trunking rides the same events - ``from``/``to`` may be sip: URIs
    and are kept verbatim. Honest skips: recording events, fork
    startables (the media websocket is the transport's job), unknown
    event types.
    """
    result = TelnyxParseResult()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        result.skipped.append({"reason": "unsupported_payload",
                               "detail": "telnyx webhooks carry {data: {...}}"})
        return result
    data = payload["data"]
    event_type = str(data.get("event_type") or "")
    if not event_type:
        result.skipped.append({"reason": "no_event_type", "detail": "data.event_type is required"})
        return result
    p = data.get("payload") or {}
    if not isinstance(p, dict):
        p = {}
    ev = TelnyxEvent(
        event_type=event_type,
        call_control_id=str(p.get("call_control_id") or ""),
        call_session_id=str(p.get("call_session_id") or ""),
        direction=str(p.get("direction") or ""),
        from_ref=str(p.get("from") or ""),
        to_ref=str(p.get("to") or ""),
        client_state=str(p.get("client_state") or ""),
    )
    if event_type == "call.initiated":
        ev.kind = "call.ringing"   # inbound: the ring; outbound: the dial
    elif event_type == "call.answered":
        ev.kind = "call.answered"
    elif event_type == "call.hangup":
        cause = str(p.get("hangup_cause") or "").upper()
        ev.hangup_cause = cause
        ev.end_kind = TELNYX_HANGUP_CAUSES.get(cause, "failed")
        ev.kind = ev.end_kind
    elif event_type == "call.gather.ended":
        ev.kind = "dtmf"
        ev.digits = str(p.get("digits") or "")
        if not ev.digits:
            result.skipped.append({"reason": "empty_gather",
                                   "detail": "gather.ended carried no digits"})
            return result
    elif event_type in ("call.speak.started", "call.speak.ended"):
        ev.kind = "tts.started" if event_type == "call.speak.started" else "tts.ended"
    elif event_type == "call.machine.detection.ended":
        result_amd = str(p.get("result") or "").lower()
        if "machine" in result_amd or "greeting" in result_amd:
            ev.kind = "voicemail_detected"
        else:
            result.skipped.append({"reason": "amd_human",
                                   "detail": f"answering-machine detection says {result_amd or 'unknown'} "
                                             "- no session event"})
            return result
    elif event_type == "call.fork.started":
        result.skipped.append({"reason": "fork_started",
                               "detail": "audio forking started - point the fork websocket at "
                                         "/api/v1/voice/sessions/{id}/media for the transport"})
        return result
    else:
        result.skipped.append({"reason": "unhandled_event_type",
                               "detail": f"event_type {event_type!r} is not applied"})
        return result
    if not ev.call_control_id and event_type != "call.hangup":
        result.skipped.append({"reason": "no_call_control_id",
                               "detail": f"{event_type} without call_control_id"})
        return result
    result.events.append(ev)
    return result


def telnyx_verify_signature(public_key_pem: str, headers: dict, raw_body: bytes,
                            *, method: str = "POST", target: str = "") -> tuple[bool, str | None]:
    """Telnyx signs webhooks with HTTP Message Signatures (RFC 9421).

    ``Signature-Input: sig1=("@method" "@target" ...);created=...;keyid=...``
    and ``Signature: sig1=:base64(Ed25519 over the signature base):``. The
    base is each covered component as ``"name": value`` lines (derived from
    the REQUEST - @method, @target, or a named header), then the
    ``@signature-params`` line verbatim. Missing keys/headers fail loud.
    """
    sig_input = str(headers.get("signature-input") or "")
    sig = str(headers.get("signature") or "")
    if not public_key_pem:
        return False, "endpoint has no public_key configured"
    if not sig_input or not sig:
        return False, "missing Signature-Input or Signature headers"
    # signature-input: take the first label's covered components + params
    label_part, _, rest = sig_input.partition("=")
    label = label_part.strip()
    if not label or not rest.strip().startswith("("):
        return False, "Signature-Input is not RFC 9421 shaped (label=(...);params)"
    close = rest.find(")")
    if close < 0:
        return False, "Signature-Input covered components are unterminated"
    components_str = rest[1:close]
    params_str = rest[close + 1:]
    covered = [c.strip().strip('"').split(";")[0] for c in components_str.split() if c.strip()]
    if not covered:
        return False, "Signature-Input lists no covered components"
    # signature: sig1=:b64:
    value = None
    for chunk in sig.split(","):
        lab, _, val = chunk.partition("=")
        if lab.strip() == label and val.startswith(":") and val.endswith(":"):
            value = val[1:-1]
            break
    if value is None:
        return False, f"Signature header carries no '{label}=:...:' value"
    # the signature base - derived from the ACTUAL request, not echoed input
    lines: list[str] = []
    for comp in covered:
        if comp == "@method":
            lines.append(f'"@method": {method.upper()}')
        elif comp == "@target":
            lines.append(f'"@target": {target}')
        elif comp == "@path":
            lines.append(f'"@path": {target.split("?")[0]}')
        elif comp == "@authority":
            lines.append(f'"@authority": {str(headers.get("host") or "")}')
        elif comp.startswith("@"):
            return False, f"covered component {comp!r} is not supported (use @method/@target/@path/@authority)"
        else:
            v = headers.get(comp.lower())
            if v is None:
                return False, f"covered header {comp!r} is absent from the request"
            lines.append(f'"{comp.lower()}": {v}')
    lines.append(f'"@signature-params": ({components_str}){params_str}')
    base = "\n".join(lines)
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives import serialization
        pub = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        if not isinstance(pub, Ed25519PublicKey):
            return False, "the configured public_key is not an Ed25519 key"
        pub.verify(base64.b64decode(value), base.encode("utf-8"))
    except (InvalidSignature, ValueError, TypeError):
        return False, "signature verification failed"
    except ImportError:  # pragma: no cover - cryptography is a hard dep via Fernet
        return False, "cryptography backend unavailable for Ed25519 verification"
    # A covered content-digest BINDS the body: the signature alone only
    # proves the headers - py8n additionally checks the digest against the
    # raw body, so a tampered payload fails even with a valid signature.
    if "content-digest" in covered:
        provided = str(headers.get("content-digest") or "").strip()
        if not provided:
            return False, "content-digest is covered but absent from the request"
        if provided.lower().startswith("sha-256=:") and provided.endswith(":"):
            actual = base64.b64encode(hashlib.sha256(raw_body).digest()).decode()
            if not _hmac.compare_digest(provided[9:-1], actual):
                return False, "content-digest does not match the request body"
        else:
            return False, "content-digest must be 'sha-256=:<base64>:' to bind the body"
    return True, None


def telnyx_build_command(config: dict, call_control_id: str, command: str,
                         params: dict | None = None) -> dict:
    """The Call Control request that makes the agent DO something.

    ``POST {api}/v2/calls/{call_control_id}/actions/{command}`` - answer
    picks up, speak says text (TTS through Telnyx), gather_using_audio
    collects DTMF, hangup ends. The api_key rides the Authorization
    header, credentials included only at delivery time.
    """
    command = str(command or "").strip()
    if command not in TELNYX_COMMANDS:
        raise ValueError(f"unknown telnyx command {command!r} - known: {', '.join(TELNYX_COMMANDS)}")
    if not str(call_control_id or "").strip():
        raise ValueError("a call_control_id is required")
    api_key = str(config.get("api_key") or "")
    json_body: dict = {}
    if command == "speak":
        text = str((params or {}).get("payload") or (params or {}).get("text") or "")
        if not text.strip():
            raise ValueError("speak requires 'payload' (the text to say)")
        json_body = {"payload": text,
                     "voice": str((params or {}).get("voice") or "female"),
                     "language": str((params or {}).get("language") or "en-US")}
    elif command == "hangup":
        json_body = {"command_id": str((params or {}).get("command_id") or "")} if (params or {}).get("command_id") else {}
    elif command == "gather_using_audio":
        audio_url = str((params or {}).get("audio_url") or "")
        if not audio_url:
            raise ValueError("gather_using_audio requires 'audio_url' (the prompt to play)")
        json_body = {"audio_url": audio_url,
                     "minimum_digits": int((params or {}).get("minimum_digits") or 1),
                     "maximum_digits": int((params or {}).get("maximum_digits") or 8),
                     "terminating_digit": str((params or {}).get("terminating_digit") or "#")}
    elif command == "transfer":
        target = str((params or {}).get("to") or "")
        if not target:
            raise ValueError("transfer requires 'to'")
        json_body = {"to": target}
    return {
        "method": "POST",
        "url": f"{TELNYX_API_BASE}/calls/{call_control_id}/actions/{command}",
        "headers": {"Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"},
        "json": json_body,
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
    "telnyx_call_control": {"channel": "voice", "secret": ["public_key"],
                            "credential": ["api_key"],
                            "description": "Telnyx Call Control (SIP + PSTN voice)"},
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
                   headers: dict, query_params: dict | None = None,
                   method: str = "POST", target: str = "") -> tuple[bool, str | None]:
    """Pure verification: bytes + headers in, verdict out (no Request object).

    ``method``/``target`` matter to RFC 9421 signers (telnyx) - the
    signature base is derived from the request's method and target path.
    """
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
    if provider == "telnyx_call_control":
        return telnyx_verify_signature(str(endpoint_config.get("public_key") or ""),
                                       headers, raw_body, method=method, target=target)
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
                     "access_token", "public_key", "api_key"):
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
    "telnyx_call_control": "telnyx",
}
