"""The held-leg experience (v77) - waiting is not silence.

v76 put live calls in a queue and derived their positions honestly. But
a caller parked in on_hold heard EXACTLY what the provider's media plane
plays - py8n queued the conversation and stayed silent about the line.
This module closes that gap with two primitives over the SAME queue rows:

* **queue-position announcements** - TTS on the held leg. The caller's
  own leg is spoken to: the position text is synthesized through the
  registered TTS engines (the piper bridge - real audio, offline) and
  delivered over whatever carries the call: web legs receive the audio
  on their media websocket (voice_push), carrier legs (Telnyx) receive
  the provider speak command. Every announcement is recorded on the
  session timeline (tts.started/tts.ended with source=queue_announcement
  + the queue.announced delivery record) - the queue always told the
  caller WHERE they stood, and the record shows exactly what they heard.

* **the SMS backchannel** - a held phone can't scroll a web page. The
  queue can bind an SMS channel (telnyx_sms or the any-gateway
  generic_sms contract) and push position updates to the caller's real
  address: "You are #2 of 5 in the support line - waited 43s." Sends go
  through the SAME deliver_outbound the channels use (honest skipped
  records without credentials) and land in the entry's meta as the
  backchannel's operational log.

Both run as PASSES (announce_pass / sms_update_pass) driven by queue
events: on join, when the line moves (seat/leave), and whenever an
operator (or a scheduled workflow) POSTs the announce endpoint. Nothing
here holds state of its own - positions and waits derive from the queue
rows at pass time; the only meta written is the pass's own operational
history (what was announced/sent, when).

Templates: ``{position} {depth} {waited_seconds} {queue_name}`` -
validated at CONFIG WRITE time (an unknown placeholder refuses loudly,
never mid-announcement).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChannelEndpoint, ChannelQueue, ChannelQueueEntry, VoiceSession
from . import voice as voice_svc

DEFAULT_ANNOUNCE = ("You are number {position} of {depth} in line for {queue_name}. "
                    "You have been waiting {waited_seconds} seconds.")
DEFAULT_SMS = ("{queue_name}: you are #{position} of {depth} in line "
               "(waited {waited_seconds}s). We'll text if anything changes.")

TEMPLATE_KEYS = ("position", "depth", "waited_seconds", "queue_name")
MAX_SMS_HISTORY = 20


class WaitingError(ValueError):
    """Honest 4xx-grade waiting-experience failures."""


def _now():
    return datetime.now(timezone.utc)


class _StrictDict(dict):
    """format_map that refuses unknown placeholders instead of inventing text."""

    def __missing__(self, key):
        raise KeyError(key)


def render_template(template: str, **values) -> str:
    try:
        return template.format_map(_StrictDict(**values))
    except KeyError as exc:
        raise WaitingError(
            f"template uses unknown placeholder {exc.args[0]!r} - known: "
            f"{', '.join(TEMPLATE_KEYS)}") from exc


def waiting_config(raw: dict | None) -> dict:
    """The waiting-experience block of a queue's config, validated.

    ``announce``: {enabled (default true), interval_seconds (10..3600,
    default 60), template} - a queue that never tells you your place is
    a black hole, so announcements are on by default; a queue with no
    usable TTS engine skips honestly per pass (the hold itself never
    breaks). ``sms``: {enabled (default false), channel_id, template} -
    opt-in, needs a bound SMS channel to do anything.
    """
    raw = dict(raw or {})
    announce = dict(raw.get("announce") or {})
    interval_raw = announce.get("interval_seconds")
    try:
        interval = int(interval_raw) if interval_raw is not None else 60
    except (TypeError, ValueError):
        interval = 60
    cfg = {
        "enabled": bool(announce.get("enabled", True)),
        "interval_seconds": max(10, min(interval, 3600)),
        "template": str(announce.get("template") or "").strip()[:400] or "",
    }
    render_template(cfg["template"] or DEFAULT_ANNOUNCE,
                    position=2, depth=5, waited_seconds=43, queue_name="q")
    sms = dict(raw.get("sms") or {})
    sms_cfg = {
        "enabled": bool(sms.get("enabled", False)),
        "channel_id": str(sms.get("channel_id") or "").strip()[:36] or "",
        "template": str(sms.get("template") or "").strip()[:400] or "",
    }
    render_template(sms_cfg["template"] or DEFAULT_SMS,
                    position=2, depth=5, waited_seconds=43, queue_name="q")
    return {"announce": cfg, "sms": sms_cfg}


# ---------------------------------------------------------------------------
# the queue's waiting line, derived the same way queue_out derives it
# ---------------------------------------------------------------------------

async def _live_waiting(db: AsyncSession, queue: ChannelQueue) -> list[dict]:
    """The live waiting entries in FIFO order with derived position/wait."""
    q = (select(ChannelQueueEntry)
         .where(ChannelQueueEntry.queue_id == queue.id,
                ChannelQueueEntry.status == "waiting")
         .order_by(ChannelQueueEntry.joined_at.asc(), ChannelQueueEntry.id.asc()))
    entries = list((await db.execute(q)).scalars().all())
    sessions: dict[str, VoiceSession] = {}
    sids = [e.session_id for e in entries if e.session_id]
    if sids:
        sq = select(VoiceSession).where(VoiceSession.id.in_(sids))
        sessions = {s.id: s for s in (await db.execute(sq)).scalars().all()}
    live: list[dict] = []
    for e in entries:
        session = sessions.get(e.session_id or "")
        if session is None or session.state == "ended":
            continue  # abandoned while waiting - holds no position
        joined = e.joined_at
        if joined is not None and joined.tzinfo is None:
            joined = joined.replace(tzinfo=timezone.utc)
        waited = round(max(0.0, (_now() - (joined or _now())).total_seconds()), 3)
        live.append({"entry": e, "session": session, "waited": waited})
    for i, item in enumerate(live):
        item["position"] = i + 1
        item["depth"] = len(live)
    return live


def _selected(items: list[dict], entry_ids: list[str] | None) -> list[dict]:
    if not entry_ids:
        return items
    wanted = set(entry_ids)
    return [it for it in items if it["entry"].id in wanted]


# ---------------------------------------------------------------------------
# queue-position announcements - TTS on the held leg
# ---------------------------------------------------------------------------

def _resolve_announce_tts(session: VoiceSession) -> tuple[str | None, str]:
    """The announcement's (engine, voice): the leg's resolved TTS provider
    when it is registered in THIS process, else the local bridge, else
    (None, voice) - honest: announcements synthesize, they never pretend.

    A fallback to the local bridge carries the ENGINE's default voice, not
    the leg's hosted-voice name (a local piper cannot speak 'alloy', and
    failing loud on it would silence every announcement for legs that
    never configured a voice at all)."""
    from . import speech_engines as engines
    from .voice_agents import resolve_turn_tts

    provider, voice, _fmt = resolve_turn_tts(session)
    registered = engines.registered_tts_engines()
    if provider in registered:
        return provider, voice
    if engines.LOCAL_TTS_NAME in registered:
        return engines.LOCAL_TTS_NAME, ""
    return None, voice


async def _deliver_carrier_speak(db: AsyncSession, session: VoiceSession,
                                 text: str, voice: str) -> dict:
    """Carrier legs: the provider speak command on the held call."""
    if (session.provider or "") != "telnyx" or not (session.call_ref or "").strip():
        return {"delivery": "skipped",
                "detail": f"no carrier speak path for provider "
                          f"{session.provider!r} - the announcement is recorded "
                          "on the session timeline"}
    from ..models import ChannelEndpoint

    q = (select(ChannelEndpoint)
         .where(ChannelEndpoint.provider.in_(("telnyx", "telnyx_call_control")))
         .order_by(ChannelEndpoint.created_at.desc()))
    rows = list((await db.execute(q)).scalars().all())
    endpoint = next((r for r in rows
                     if r.owner_id is None or session.owner_id is None
                     or r.owner_id == session.owner_id), None)
    if endpoint is None:
        return {"delivery": "skipped",
                "detail": "no telnyx channel endpoint - the speak command was "
                          "not built"}
    from . import channel_adapters as adapters

    try:
        cmd = adapters.telnyx_build_command(endpoint.config or {}, session.call_ref,
                                            "speak", {"payload": text,
                                                      "voice": voice or "female"})
    except ValueError as exc:
        return {"delivery": "failed", "detail": f"speak command refused: {exc}"}
    from . import channel_endpoints

    return await channel_endpoints._attempt_command(endpoint.config or {}, cmd)


async def announce_pass(db: AsyncSession, queue: ChannelQueue, *,
                        entry_ids: list[str] | None = None,
                        force: bool = False) -> dict:
    """Announce queue positions to the held legs - the pass runs on join,
    when the line moves, and whenever driven explicitly.

    An entry is DUE when forced, when its position changed since the last
    announcement, or when interval_seconds passed since the last one. The
    text renders from the queue's template (or the default), is
    synthesized through the registered TTS engine, and delivered on the
    leg's live path (media websocket for web legs, provider speak for
    carrier legs). Results are honest per entry; a queue with no usable
    TTS engine announces NOTHING but records WHY."""
    from . import speech_engines as engines
    from . import voice_push

    cfg = waiting_config(queue.config)
    if not cfg["announce"]["enabled"]:
        raise WaitingError("the queue's announcements are disabled (config.announce.enabled)")
    items = _selected(await _live_waiting(db, queue), entry_ids)

    announced: list[dict] = []
    for item in items:
        entry: ChannelQueueEntry = item["entry"]
        session: VoiceSession = item["session"]
        meta = dict(entry.meta or {})
        due = bool(force
                   or meta.get("last_announced_position") != item["position"]
                   or _not_since(meta.get("last_announce_at"),
                                 cfg["announce"]["interval_seconds"]))
        if not due:
            announced.append({"entry_id": entry.id, "session_id": session.id,
                              "position": item["position"], "announced": False,
                              "reason": "not_due"})
            continue
        values = {"position": item["position"], "depth": item["depth"],
                  "waited_seconds": int(item["waited"]),
                  "queue_name": queue.name}
        text = render_template(cfg["announce"]["template"] or DEFAULT_ANNOUNCE,
                               **values)
        engine, voice = _resolve_announce_tts(session)
        delivery: dict = {}
        tts_block: dict = {"engine": engine}
        if engine is None:
            delivery = {"delivery": "skipped",
                        "detail": "no TTS engine is registered in this process - "
                                  "the position was computed but not spoken"}
        else:
            try:
                tts = engines.synthesize(engine, text, voice=voice or "", fmt="wav")
                tts_block.update({"voice": voice, "duration_ms":
                                  tts.get("duration_estimate_ms")})
                # web legs hear the announcement NOW (position frame + audio)
                n = await voice_push.push(session.id, {
                    "event": "queue_position", "queue_id": queue.id,
                    "queue_name": queue.name, "position": item["position"],
                    "depth": item["depth"], "waited_seconds": int(item["waited"]),
                    "text": text})
                audio_delivery = "web_media_socket"
                if n:
                    n2 = await voice_push.push(session.id, {
                        "event": "audio", "audio_b64": tts["audio_b64"],
                        "format": "wav", "source": "queue_announcement",
                        "duration_ms": tts.get("duration_estimate_ms")})
                    delivery = {"delivery": "delivered", "path": audio_delivery,
                                "frames": n + n2}
                else:
                    delivery = await _deliver_carrier_speak(db, session, text, voice or "")
            except voice_svc.VoiceError as exc:
                delivery = {"delivery": "failed", "detail": str(exc)}
        # the session timeline keeps the whole story
        if tts_block.get("engine"):
            ev = await voice_svc._add_event(db, session, "tts.started",
                                            {"text": text[:500],
                                             "provider": tts_block["engine"],
                                             "voice": tts_block.get("voice") or "",
                                             "barge_in_ok": False,
                                             "source": "queue_announcement"})
            await voice_svc._add_event(db, session, "tts.ended",
                                       {"tts_id": ev.id, "cancelled": False})
        await voice_svc._add_event(db, session, "queue.announced",
                                   {"queue_id": queue.id, "queue_name": queue.name,
                                    "position": item["position"],
                                    "depth": item["depth"],
                                    "waited_seconds": int(item["waited"]),
                                    "text": text[:500], "source": "queue_announcement",
                                    "delivery": {"delivery": delivery.get("delivery"),
                                                 "detail": str(delivery.get("detail") or "")[:300],
                                                 "path": delivery.get("path"),
                                                 "frames": delivery.get("frames")}})
        meta.update({"last_announced_position": item["position"],
                     "last_announce_at": _now().isoformat(),
                     "announcements": int(meta.get("announcements") or 0) + 1})
        entry.meta = meta
        db.add(entry)
        announced.append({"entry_id": entry.id, "session_id": session.id,
                          "position": item["position"], "depth": item["depth"],
                          "waited_seconds": int(item["waited"]), "text": text,
                          "announced": delivery.get("delivery") == "delivered",
                          "delivery": {"delivery": delivery.get("delivery"),
                                       "detail": str(delivery.get("detail") or "")[:300],
                                       "path": delivery.get("path"),
                                       "frames": delivery.get("frames")}})
    await db.flush()
    return {"queue_id": queue.id, "announced": announced,
            "considered": len(items),
            "note": ("announcements fire on join, when the line moves, and on "
                     "this pass - due = forced | position changed | interval "
                     f"({cfg['announce']['interval_seconds']}s) elapsed")}


def _not_since(iso_at: str | None, interval_seconds: int) -> bool:
    if not iso_at:
        return True
    try:
        at = datetime.fromisoformat(str(iso_at))
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (_now() - at).total_seconds() >= interval_seconds


# ---------------------------------------------------------------------------
# the SMS backchannel - the caller's phone gets the line, not just their ear
# ---------------------------------------------------------------------------

SMS_PROVIDERS = ("telnyx_sms", "generic_sms")


async def sms_update_pass(db: AsyncSession, queue: ChannelQueue, *,
                          entry_ids: list[str] | None = None,
                          event: str = "position") -> dict:
    """Push an SMS position update to waiting callers over the queue's
    bound SMS channel. Runs on join (event=joined) and when the line
    moves (event=position); every send - delivered, skipped, failed -
    lands in the entry's meta as the backchannel's log."""
    if event not in ("position", "joined", "seated", "expired"):
        raise WaitingError(f"unknown sms update event {event!r} - known: "
                           "position|joined|seated|expired")
    cfg = waiting_config(queue.config)
    if not cfg["sms"]["enabled"]:
        raise WaitingError("the queue's SMS backchannel is not enabled (config.sms.enabled)")
    if not cfg["sms"]["channel_id"]:
        raise WaitingError("the SMS backchannel needs config.sms.channel_id "
                           "(a telnyx_sms or generic_sms channel)")
    endpoint = await db.get(ChannelEndpoint, cfg["sms"]["channel_id"])
    if endpoint is None or (queue.owner_id is not None
                            and endpoint.owner_id is not None
                            and endpoint.owner_id != queue.owner_id):
        raise WaitingError(f"SMS channel {cfg['sms']['channel_id']!r} not found")
    if endpoint.provider not in SMS_PROVIDERS:
        raise WaitingError(f"channel provider {endpoint.provider!r} does not carry "
                           f"SMS - bind one of: {', '.join(SMS_PROVIDERS)}")
    from . import channel_endpoints

    items = _selected(await _live_waiting(db, queue), entry_ids)
    sent: list[dict] = []
    for item in items:
        entry: ChannelQueueEntry = item["entry"]
        session: VoiceSession = item["session"]
        if not (entry.address or "").strip():
            record = {"delivery": "skipped",
                      "detail": "the entry has no address to text"}
        else:
            values = {"position": item["position"], "depth": item["depth"],
                      "waited_seconds": int(item["waited"]),
                      "queue_name": queue.name}
            text = render_template(cfg["sms"]["template"] or DEFAULT_SMS, **values)
            record = await channel_endpoints.deliver_outbound(
                endpoint, entry.address, text)
            record = {**record, "text": text}
            await voice_svc._add_event(db, session, "queue.sms",
                                       {"queue_id": queue.id, "event": event,
                                        "to": entry.address, "text": text[:300],
                                        "delivery": record.get("delivery"),
                                        "detail": str(record.get("detail") or "")[:300]})
        meta = dict(entry.meta or {})
        history = list(meta.get("sms") or [])
        history.append({"at": _now().isoformat(), "event": event,
                        "delivery": record.get("delivery"),
                        "detail": str(record.get("detail") or "")[:200],
                        "text": str(record.get("text") or "")[:200]})
        meta["sms"] = history[-MAX_SMS_HISTORY:]
        entry.meta = meta
        db.add(entry)
        sent.append({"entry_id": entry.id, "session_id": session.id,
                     "position": item["position"], "to": entry.address or None,
                     "delivery": record.get("delivery"),
                     "detail": str(record.get("detail") or "")[:200],
                     "text": str(record.get("text") or "")})
    await db.flush()
    return {"queue_id": queue.id, "event": event, "sent": sent,
            "considered": len(items),
            "channel": {"id": endpoint.id, "name": endpoint.name,
                        "provider": endpoint.provider}}
