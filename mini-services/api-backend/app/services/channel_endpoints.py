"""Channel endpoints (v69+v70) - CRUD and delivery for the REAL adapter surface.

A ChannelEndpoint turns a provider's native webhook into interaction-layer
ingests. This module owns the storage and the HTTP plumbing; the dialect
rules live in channel_adapters.py (pure, unit-testable).

The chat receive path (one call, shared by whatsapp/telegram/discord):

  verify (provider-native) -> parse (native dialect -> normalized
  messages + honest skips) -> for each message: interactions.ingest()
  (find-or-create conversation, run the bound handler, record both sides)
  -> deliver the reply through the provider's send API.

The voice receive path (v70, telnyx_call_control):

  verify (RFC 9421) -> parse call-control events -> find-or-create the
  VoiceSession by call_control_id -> run each event through the SAME
  voice state machine -> act: answer on initiated, run a voice turn on
  gather digits (the handler replies), complete the utterance on
  speak.ended -> build (and honestly attempt) the matching call-control
  command. The provider identifies the call; py8n never needs a session
  id in the webhook URL.

Delivery is honest about the last mile: with a credential configured the
built request is actually sent (httpx, short timeout) and the provider's
status recorded; without one (or with the endpoint disabled mid-flight)
the reply is recorded in the transcript but delivery is reported
``skipped`` with the exact missing key - py8n never pretends a message
was delivered when it wasn't.
"""

from __future__ import annotations

import asyncio
import json as _json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChannelEndpoint, Workflow
from . import channel_adapters as adapters
from . import interactions as inter_svc


class ChannelEndpointError(ValueError):
    """Honest 4xx-grade endpoint failures."""


def endpoint_out(row: ChannelEndpoint, handler_name: str | None = None) -> dict:
    provider_meta = adapters.REQUIRED_CONFIG.get(row.provider, {})
    return {
        "id": row.id,
        "name": row.name,
        "provider": row.provider,
        "channel": row.channel,
        "enabled": bool(row.enabled),
        "handler_workflow_id": row.handler_workflow_id,
        "handler_workflow_name": handler_name,
        "config": adapters.mask_config(row.config or {}),
        "required_config": {k: v for k, v in provider_meta.items() if k != "description"},
        "webhook_url": f"/api/v1/channels/{adapters.PROVIDER_PATHS.get(row.provider, row.provider)}/{row.id}/webhook",
        "events_received": row.events_received,
        "last_event_at": row.last_event_at.isoformat() if row.last_event_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _handler_name(db: AsyncSession, workflow_id: str | None) -> str | None:
    if not workflow_id:
        return None
    wf = await db.get(Workflow, workflow_id)
    return wf.name if wf else None


def _validate_config(provider: str, config: dict, *, partial: bool = False) -> None:
    meta = adapters.REQUIRED_CONFIG.get(provider)
    if meta is None:
        raise ChannelEndpointError(
            f"unknown provider {provider!r} - known providers: "
            f"{', '.join(sorted(adapters.REQUIRED_CONFIG))}")
    if partial:
        return
    missing = [k for k in meta.get("secret", []) if not str(config.get(k) or "").strip()]
    if missing:
        raise ChannelEndpointError(
            f"provider {provider} requires {', '.join(missing)} in config "
            "(the keys that verify the provider's webhook)")


def _extract_sender_to(msg: adapters.NormalizedInbound) -> str:
    """Where the outbound reply goes, per provider."""
    if msg.channel == "telegram":
        return msg.extra.get("chat_id") or msg.sender_id
    if msg.channel == "whatsapp":
        return msg.sender_id
    if msg.channel == "discord":
        return ""  # the endpoint's configured webhook_url IS the destination
    if msg.channel == "sms":
        return msg.sender_id
    if msg.channel == "email":
        return msg.sender_id  # SMTP replies go to the sender's address
    return msg.sender_id


def _email_subject_from(msg: adapters.NormalizedInbound) -> str:
    """Reply subject for an inbound email: Re: <original>, once."""
    subject = str((msg.extra or {}).get("subject") or "").strip()
    if not subject:
        return ""
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


async def deliver_outbound(endpoint: ChannelEndpoint, to: str, text: str,
                           buttons: list[dict] | None = None,
                           *, subject: str = "") -> dict:
    """Build the provider request and (when possible) actually send it.

    ``buttons`` (v70) switches the request to the provider's INTERACTIVE
    shape - WhatsApp reply buttons only; other providers refuse loudly
    instead of silently degrading to plain text. ``subject`` (v71) rides
    the email SMTP request; other providers ignore it.

    Returns a delivery record: {delivery, detail, request} where request
    carries the url + json body (auth headers masked) for traceability.
    """
    config = endpoint.config or {}
    try:
        if buttons:
            if endpoint.provider != "meta_cloud_api":
                raise ChannelEndpointError(
                    f"provider {endpoint.provider} does not support interactive buttons - "
                    "only meta_cloud_api (WhatsApp) carries reply buttons")
            request = adapters.meta_build_interactive(config, to, text, buttons)
        elif endpoint.provider == "email_inbound":
            request = adapters.email_build_outbound(config, to, text, subject=subject)
        else:
            request = adapters.build_outbound(endpoint.provider, config, to, text)
    except ChannelEndpointError:
        raise
    except ValueError as exc:  # Meta's interactive limits refuse loudly -> 400
        raise ChannelEndpointError(str(exc)) from exc

    # --- the SMTP transport (v71): email does not ride httpx ---
    if request.get("transport") == "smtp":
        masked_request = {"transport": "smtp", "host": request.get("host"),
                          "port": request.get("port"), "to": request.get("to"),
                          "subject": request.get("subject"),
                          "message_bytes": len(request.get("message") or "")}
        cred_keys = adapters.REQUIRED_CONFIG.get(endpoint.provider, {}).get("credential", [])
        missing = [k for k in cred_keys if not str(config.get(k) or "").strip()
                   and k not in ("smtp_port",)]
        if missing:
            return {"delivery": "skipped",
                    "detail": f"no outbound credential configured (missing {', '.join(missing)}) - "
                              "the reply is recorded in the transcript but NOT delivered",
                    "request": masked_request}
        try:
            request["password"] = str(config.get("smtp_pass") or "")
            result = await asyncio.to_thread(adapters.email_send_smtp, request)
            result["request"] = masked_request
            return result
        except Exception as exc:  # noqa: BLE001 - SMTP failures are honest records
            return {"delivery": "failed", "detail": f"smtp send failed: {exc}",
                    "request": masked_request}

    masked_request = {"method": request["method"], "url": request["url"],
                      "json": request.get("json")}
    cred_keys = adapters.REQUIRED_CONFIG.get(endpoint.provider, {}).get("credential", [])
    missing = [k for k in cred_keys if not str(config.get(k) or "").strip()]
    if missing:
        return {"delivery": "skipped",
                "detail": f"no outbound credential configured (missing {', '.join(missing)}) - "
                          "the reply is recorded in the transcript but NOT delivered",
                "request": masked_request}
    try:
        import httpx
        headers = {k: v for k, v in request.get("headers", {}).items()}
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            resp = await client.request(request["method"], request["url"],
                                        headers=headers, json=request.get("json"))
        ok = 200 <= resp.status_code < 300
        detail = f"provider answered {resp.status_code}"
        if not ok:
            try:
                body = resp.json()
                detail += f": {_json.dumps(body)[:200]}"
            except Exception:  # noqa: BLE001 - provider error bodies vary
                pass
        return {"delivery": "delivered" if ok else "failed", "detail": detail,
                "status_code": resp.status_code, "request": masked_request}
    except Exception as exc:  # noqa: BLE001 - the sandbox may have no egress at all
        return {"delivery": "failed", "detail": f"outbound request failed: {exc}",
                "request": masked_request}


async def receive_webhook(db: AsyncSession, endpoint: ChannelEndpoint, *,
                          raw_body: bytes, headers: dict,
                          query_params: dict | None = None,
                          form_fields: dict | None = None,
                          method: str = "POST", target: str = "") -> dict:
    """The shared receiver: verify -> parse -> ingest each -> deliver replies.

    Raises ChannelEndpointError on verification failure (the API layer
    maps that to 401/403). Provider status events (delivery receipts,
    edits) are counted and reported as skips - they are not messages.
    ``form_fields`` (v71) carries the multipart form an email inbound
    parse posts (SendGrid-style: ``email`` = the raw MIME) - the API
    layer parses the form, this layer normalizes it into the generic
    JSON shape before the email adapter sees it. ``method``/``target``
    matter to RFC 9421 signers (telnyx_sms): the signature base is
    derived from the request's actual method and target path.
    """
    if not endpoint.enabled:
        raise ChannelEndpointError("this endpoint is disabled")
    provider = endpoint.provider

    # 1. verify - provider-native, always, before anything else runs
    ok, detail = adapters.verify_request(provider, endpoint.config or {},
                                         raw_body=raw_body, headers={
                                             k.lower(): v for k, v in headers.items()},
                                         method=method, target=target)
    if not ok:
        raise ChannelEndpointError(detail or "webhook verification failed")
    try:
        payload = _json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if provider == "email_inbound" and form_fields:
        # the raw-MIME multipart shape: normalize into the generic JSON
        # shape (raw MIME parsed with the stdlib parser), then reuse the
        # exact same parse path as the signed-JSON contract
        raw_mime = str(form_fields.get("email") or form_fields.get("body-mime") or "")
        if raw_mime:
            parsed_mime = adapters.email_parse_mime(raw_mime)
            payload = {**parsed_mime, "via_mime": True}
        else:  # fields-only form (from/to/subject/text as separate fields)
            payload = {k: v for k, v in form_fields.items()
                       if isinstance(v, str) and k not in ("headers",)}

    # 2. parse the provider dialect
    result = adapters.parse_inbound(provider, payload if isinstance(payload, dict) else {})
    handled: list[dict] = []

    # 3. ingest each message through the interaction layer (owner = endpoint owner)
    for msg in result.messages:
        ingest = await inter_svc.ingest(
            db, owner_id=endpoint.owner_id, channel=msg.channel,
            sender_id=msg.sender_id, sender_name=msg.sender_name, text=msg.text,
            handler_workflow_id=endpoint.handler_workflow_id,
            metadata={"provider": provider, "endpoint_id": endpoint.id,
                      "event_id": msg.event_id, **(msg.extra or {})})
        # 4. deliver the reply through the provider's send API (email
        # threads its reply subject; other providers ignore it)
        delivery = await deliver_outbound(
            endpoint, _extract_sender_to(msg), ingest.get("reply") or "",
            subject=_email_subject_from(msg) if provider == "email_inbound" else "")
        handled.append({"sender_id": msg.sender_id, "text": msg.text[:200],
                        "conversation_id": ingest["conversation_id"],
                        "reply": ingest.get("reply"), **delivery})

    # 5. event counters on the endpoint (write-side derived bookkeeping)
    endpoint.events_received = (endpoint.events_received or 0) + 1
    endpoint.last_event_at = datetime.now(timezone.utc)
    db.add(endpoint)

    return {"ok": True, "endpoint": endpoint.id, "provider": provider,
            "received": result.count, "skipped": result.skipped,
            "handled": handled}


# ---------------------------------------------------------------------------
# v70: the voice receive path (telnyx_call_control)
# ---------------------------------------------------------------------------


async def _find_or_create_voice_session(db: AsyncSession, endpoint: ChannelEndpoint,
                                        ev: adapters.TelnyxEvent) -> tuple["object", bool]:
    """The call_control_id IS the identity: find the session or open one.

    Returns (VoiceSession row, created). The session binds the endpoint's
    handler workflow, so gather-turns answer through the SAME workflow
    every other channel uses; when the endpoint's config names a
    ``agent_id`` (v71), the created session inherits that VoiceAgent -
    its greeting, ASR engine and TTS voice ride the call.
    """
    from ..models import VoiceSession
    q = (select(VoiceSession).where(VoiceSession.call_ref == ev.call_control_id)
         .order_by(VoiceSession.started_at.desc()))
    row = (await db.execute(q)).scalars().first()
    if row is not None and (endpoint.owner_id is None or row.owner_id is None
                            or row.owner_id == endpoint.owner_id):
        return row, False
    from .voice import create_session as _voice_create
    direction = "inbound" if ev.direction != "outgoing" else "outbound"
    agent_id = str((endpoint.config or {}).get("agent_id") or "") or None
    await _voice_create(
        db, owner_id=endpoint.owner_id, direction=direction, provider="telnyx",
        call_ref=ev.call_control_id, from_ref=ev.from_ref, to_ref=ev.to_ref,
        handler_workflow_id=endpoint.handler_workflow_id, agent_id=agent_id)
    await db.flush()
    row = (await db.execute(q)).scalars().first()
    return row, True


async def _attempt_command(config: dict, request: dict) -> dict:
    """Build-and-honestly-attempt one call-control command.

    Without an api_key the command is built and reported ``skipped`` -
    py8n never pretends it answered a call it could not answer.
    """
    masked = {"method": request["method"], "url": request["url"],
              "json": request.get("json")}
    if not str(config.get("api_key") or "").strip():
        return {"delivery": "skipped",
                "detail": "no api_key configured - the command was built but NOT sent",
                "request": masked}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            resp = await client.request(request["method"], request["url"],
                                        headers=request.get("headers", {}),
                                        json=request.get("json"))
        ok = 200 <= resp.status_code < 300
        detail = f"telnyx answered {resp.status_code}"
        if not ok:
            try:
                detail += f": {_json.dumps(resp.json())[:200]}"
            except Exception:  # noqa: BLE001
                pass
        return {"delivery": "delivered" if ok else "failed", "detail": detail,
                "status_code": resp.status_code, "request": masked}
    except Exception as exc:  # noqa: BLE001 - the sandbox may have no egress
        return {"delivery": "failed", "detail": f"command request failed: {exc}",
                "request": masked}


async def receive_voice_webhook(db: AsyncSession, endpoint: ChannelEndpoint, *,
                                raw_body: bytes, headers: dict,
                                method: str = "POST", target: str = "") -> dict:
    """The telnyx receive path: verify -> parse -> sessions -> state machine.

    Each mapped event runs through the SAME v69 voice primitives the HTTP
    API exposes: ringing/answered/ending events go through apply_event,
    gather digits become a voice TURN (the handler's reply is spoken back
    through a speak command), speak.started/ended drive the active-utterance
    bookkeeping, machine detection lands as voicemail_detected. Commands
    the agent would execute (answer/speak) are built and honestly
    attempted - skipped loudly when no api_key is configured.
    """
    from ..models import VoiceSession  # noqa: F401  (session table touchpoint)
    from .voice import VoiceError, _add_event, apply_event, complete_tts, hangup as _voice_hangup, voice_turn

    if not endpoint.enabled:
        raise ChannelEndpointError("this endpoint is disabled")
    config = endpoint.config or {}
    ok, detail = adapters.verify_request(
        endpoint.provider, config, raw_body=raw_body,
        headers={k.lower(): v for k, v in headers.items()},
        method=method, target=target)
    if not ok:
        raise ChannelEndpointError(detail or "webhook verification failed")
    try:
        payload = _json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}

    parsed = adapters.telnyx_parse_webhook(payload if isinstance(payload, dict) else {})
    handled: list[dict] = []

    for ev in parsed.events:
        session, created = await _find_or_create_voice_session(db, endpoint, ev)
        actions: list[str] = []
        delivery: dict | None = None
        try:
            if ev.kind in ("call.ringing", "call.answered"):
                # v74: the call may belong to a MEETING leg or a CAMPAIGN
                # target - client_state (or the call_control_id) binds it
                # back; the binding opens/binds the agent on the session
                # BEFORE the state machine answers, so greetings and turns
                # come from the meeting's/campaign's agent. Best-effort:
                # a plain inbound call binds nothing.
                try:
                    from . import voice_meetings as meetings_svc
                    from . import voice_campaigns as campaigns_svc

                    bound = await meetings_svc.link_call(
                        db, call_control_id=ev.call_control_id,
                        client_state=ev.client_state, session=session)
                    if bound:
                        await db.refresh(session)
                        actions.append(f"meeting_bound:{bound['meeting_id'][:8]}")
                except Exception:  # noqa: BLE001 - the room linkage must not break the call
                    pass
                if ev.kind == "call.answered":
                    try:
                        from . import voice_campaigns as campaigns_svc

                        clink = await campaigns_svc.on_call_event(
                            db, call_control_id=ev.call_control_id,
                            client_state=ev.client_state,
                            event_kind="call.answered", session=session)
                        if clink:
                            await db.refresh(session)
                            actions.append(f"campaign_bound:{clink['campaign_id'][:8]}")
                    except Exception:  # noqa: BLE001 - the campaign linkage must not break the call
                        pass
            if ev.kind == "call.ringing":
                await apply_event(db, session, "call.ringing",
                                  {"direction": ev.direction, "call_session_id": ev.call_session_id})
                cmd = adapters.telnyx_build_command(config, ev.call_control_id, "answer")
                delivery = await _attempt_command(config, cmd)
                actions.append("answer_built")
            elif ev.kind == "call.answered":
                await apply_event(db, session, "call.answered", {})
                # v71: the session's VoiceAgent greets the caller - an
                # interruptible tts.started from the agent's own TTS
                # config, spoken through the provider's speak command
                # (honestly attempted like every other command)
                from .voice_agents import on_answered as _on_agent_answered
                greeting = await _on_agent_answered(db, session)
                if greeting:
                    await db.commit()
                    await db.refresh(session)
                    cmd = adapters.telnyx_build_command(
                        config, ev.call_control_id, "speak",
                        {"payload": greeting["text"], "voice": greeting["voice"],
                         "language": (session.context or {}).get("voice_agent", {}).get("language", "en-US")})
                    delivery = await _attempt_command(config, cmd)
                    actions.append("greeting_speak_built")
            elif ev.kind == "dtmf":
                # gather.ended - the caller 'said' digits; run the SAME turn
                await apply_event(db, session, "dtmf", {"digits": ev.digits})
                turn = await voice_turn(db, session, transcript=ev.digits, confidence=1.0)
                actions.append("turn_run")
                if turn.get("reply"):
                    cmd = adapters.telnyx_build_command(config, ev.call_control_id, "speak",
                                                        {"payload": turn["reply"]})
                    delivery = await _attempt_command(config, cmd)
                    actions.append("speak_built")
            elif ev.kind == "tts.started":
                event = await _add_event(db, session, "tts.started",
                                         {"provider": "telnyx", "source": "speak.started"})
                ctx = dict(session.context or {})
                ctx["active_tts"] = event.id
                session.context = ctx
                db.add(session)
            elif ev.kind == "tts.ended":
                if (session.context or {}).get("active_tts"):
                    await complete_tts(db, session)
                    actions.append("utterance_closed")
            elif ev.kind == "voicemail_detected":
                await apply_event(db, session, "voicemail_detected",
                                  {"source": "telnyx_amd"})
                # v75: the carrier's answering-machine verdict on a
                # CAMPAIGN call - the campaign's on_machine policy decides
                # whether the agent stops spending time on the machine
                # (hang up through the provider, honestly attempted) or
                # keeps talking. Non-campaign calls just record the state.
                try:
                    from . import voice_campaigns as campaigns_svc

                    clink = await campaigns_svc.on_call_event(
                        db, call_control_id=ev.call_control_id,
                        client_state=ev.client_state,
                        event_kind="voicemail_detected", session=session)
                except Exception:  # noqa: BLE001 - the campaign linkage must not break the call
                    clink = None
                if clink and (clink.get("amd") or {}).get("hangup"):
                    # the session already sits in the voicemail state after
                    # apply_event - ended is legal from there (the machine
                    # gets the hangup, the agent stops spending time)
                    if session.state != "ended":
                        await _voice_hangup(db, session, reason="answering_machine")
                    cmd = adapters.telnyx_build_command(config, ev.call_control_id, "hangup")
                    delivery = await _attempt_command(config, cmd)
                    actions.append("amd_hangup_built")
                elif clink:
                    actions.append("amd_continue")
            elif ev.kind == "greeting_end":
                # v76: the machine's greeting FINISHED (greeting_end AMD
                # mode) - the voicemail-drop trigger. The campaign's policy
                # decides: hangup on the beep, keep going, or DROP the
                # configured message (speak command first, then hangup -
                # the wire order a real drop needs).
                await apply_event(db, session, "greeting_end",
                                  {"source": "telnyx_amd"})
                try:
                    from . import voice_campaigns as campaigns_svc

                    clink = await campaigns_svc.on_call_event(
                        db, call_control_id=ev.call_control_id,
                        client_state=ev.client_state,
                        event_kind="greeting_end", session=session)
                except Exception:  # noqa: BLE001 - the campaign linkage must not break the call
                    clink = None
                decision = (clink or {}).get("amd") or {}
                if decision.get("drop"):
                    from ..models import VoiceCampaign, VoiceCampaignTarget
                    from . import voice_campaigns as campaigns_svc

                    campaign = await db.get(VoiceCampaign, clink["campaign_id"])
                    target = await db.get(VoiceCampaignTarget, clink["target_id"])
                    drop = await campaigns_svc.record_voicemail_drop(
                        db, campaign, target, session, hangup_session=False)
                    cmd = adapters.telnyx_build_command(
                        config, ev.call_control_id, "speak",
                        {"payload": drop["message"],
                         "voice": drop["tts"]["voice"],
                         "language": (session.context or {}).get("voice_agent", {}).get("language", "en-US")})
                    delivery = await _attempt_command(config, cmd)
                    actions.append("vm_drop_speak_built")
                    if session.state != "ended":
                        await _voice_hangup(db, session, reason="voicemail_drop")
                    cmd = adapters.telnyx_build_command(config, ev.call_control_id, "hangup")
                    delivery = await _attempt_command(config, cmd)
                    actions.append("vm_drop_hangup_built")
                elif decision.get("hangup"):
                    if session.state != "ended":
                        await _voice_hangup(db, session, reason="answering_machine")
                    cmd = adapters.telnyx_build_command(config, ev.call_control_id, "hangup")
                    delivery = await _attempt_command(config, cmd)
                    actions.append("amd_hangup_built")
                elif clink:
                    actions.append("amd_continue")
            elif ev.end_kind:
                await apply_event(db, session, ev.kind, {"hangup_cause": ev.hangup_cause})
                # v75: an ending carrier event closes the CAMPAIGN target
                # honestly too (no_answer/busy/failed schedule the retry per
                # the campaign's plan; a post-answer hangup completes it)
                try:
                    from . import voice_campaigns as campaigns_svc

                    clink = await campaigns_svc.on_call_event(
                        db, call_control_id=ev.call_control_id,
                        client_state=ev.client_state,
                        event_kind=ev.kind, session=session)
                except Exception:  # noqa: BLE001 - the campaign linkage must not break the call
                    clink = None
                if clink:
                    actions.append(f"campaign_{clink['status']}")
        except VoiceError as exc:
            handled.append({"event": ev.out(), "session_id": session.id,
                            "created": created, "error": str(exc),
                            "actions": actions, "delivery": delivery})
            continue
        handled.append({"event": ev.out(), "session_id": session.id,
                        "created": created, "state": session.state,
                        "end_reason": session.end_reason or None,
                        "actions": actions, "delivery": delivery})

    endpoint.events_received = (endpoint.events_received or 0) + 1
    endpoint.last_event_at = datetime.now(timezone.utc)
    db.add(endpoint)

    return {"ok": True, "endpoint": endpoint.id, "provider": endpoint.provider,
            "received": parsed.count, "skipped": parsed.skipped, "handled": handled}


async def list_endpoints(db: AsyncSession, owner_id: str | None) -> list[dict]:
    q = select(ChannelEndpoint).order_by(ChannelEndpoint.created_at.desc())
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        rows = [r for r in rows if r.owner_id is None or r.owner_id == owner_id]
    return [endpoint_out(r, await _handler_name(db, r.handler_workflow_id)) for r in rows]


async def get_endpoint(db: AsyncSession, endpoint_id: str, owner_id: str | None) -> dict | None:
    row = await db.get(ChannelEndpoint, endpoint_id)
    if row is None:
        return None
    if owner_id is not None and row.owner_id is not None and row.owner_id != owner_id:
        return None
    return endpoint_out(row, await _handler_name(db, row.handler_workflow_id))


async def create_endpoint(db: AsyncSession, *, owner_id: str | None, name: str,
                          provider: str, handler_workflow_id: str | None,
                          config: dict) -> dict:
    if not name or not name.strip():
        raise ChannelEndpointError("an endpoint name is required")
    _validate_config(provider, config or {})
    if handler_workflow_id:
        wf = await db.get(Workflow, handler_workflow_id)
        if wf is None or (owner_id is not None and wf.owner_id is not None
                          and wf.owner_id != owner_id):
            raise ChannelEndpointError(f"handler workflow {handler_workflow_id!r} not found")
    row = ChannelEndpoint(
        name=name.strip()[:140],
        provider=provider,
        channel=adapters.REQUIRED_CONFIG[provider]["channel"],
        handler_workflow_id=handler_workflow_id,
        config=config or {},
    )
    row.owner_id = owner_id
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return endpoint_out(row, await _handler_name(db, row.handler_workflow_id))


async def update_endpoint(db: AsyncSession, row: ChannelEndpoint, *, name: str | None = None,
                          handler_workflow_id: str | None = None, config: dict | None = None,
                          enabled: bool | None = None) -> dict:
    if name is not None and name.strip():
        row.name = name.strip()[:140]
    if enabled is not None:
        row.enabled = bool(enabled)
    if config:
        merged = dict(row.config or {})
        merged.update(config)
        _validate_config(row.provider, merged)
        row.config = merged
    if handler_workflow_id is not None:
        if handler_workflow_id:
            wf = await db.get(Workflow, handler_workflow_id)
            if wf is None:
                raise ChannelEndpointError(f"handler workflow {handler_workflow_id!r} not found")
        row.handler_workflow_id = handler_workflow_id or None
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return endpoint_out(row, await _handler_name(db, row.handler_workflow_id))


async def delete_endpoint(db: AsyncSession, row: ChannelEndpoint) -> dict:
    payload = {"id": row.id, "name": row.name, "provider": row.provider,
               "note": "endpoint removed - conversations and transcripts survive"}
    await db.delete(row)
    await db.flush()
    return payload


async def preview_outbound(row: ChannelEndpoint, to: str, text: str,
                           buttons: list[dict] | None = None,
                           *, subject: str = "") -> dict:
    """Dry-run: the exact request py8n would send, secrets masked."""
    try:
        if buttons:
            if row.provider != "meta_cloud_api":
                raise ChannelEndpointError(
                    f"provider {row.provider} does not support interactive buttons - "
                    "only meta_cloud_api (WhatsApp) carries reply buttons")
            request = adapters.meta_build_interactive(row.config or {}, to, text, buttons)
        elif row.provider == "email_inbound":
            request = adapters.email_build_outbound(row.config or {}, to, text, subject=subject)
        else:
            request = adapters.build_outbound(row.provider, row.config or {}, to, text)
    except ChannelEndpointError:
        raise
    except ValueError as exc:  # Meta's interactive limits refuse loudly -> 400
        raise ChannelEndpointError(str(exc)) from exc
    cred_keys = adapters.REQUIRED_CONFIG.get(row.provider, {}).get("credential", [])
    missing = [k for k in cred_keys if not str((row.config or {}).get(k) or "").strip()
               and k != "smtp_port"]
    if request.get("transport") == "smtp":
        return {"transport": "smtp", "host": request.get("host"), "port": request.get("port"),
                "from": request.get("from"), "to": request.get("to"),
                "subject": request.get("subject"),
                "message_preview": (request.get("message") or "")[:400],
                "would_deliver": not missing,
                "missing_credentials": missing}
    headers = dict(request.get("headers") or {})
    for key in list(headers):
        if key.lower() in ("authorization", "x-telegram-bot-api-secret-token"):
            v = str(headers[key])
            headers[key] = f"{v[:8]}...({len(v)} chars)" if len(v) > 8 else v
    return {"method": request["method"], "url": request["url"],
            "headers": headers, "json": request.get("json"),
            "would_deliver": not missing,
            "missing_credentials": missing}
