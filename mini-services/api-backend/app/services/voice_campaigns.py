"""Outbound voice campaigns (v74) - dial a list through an agent.

A campaign dials a list of addresses through the meeting/campaign dial
primitive (``telnyx_build_dial``) with a client_state binding every
carrier event back to the target row. When a call is ANSWERED, the
webhook path (or the honest simulate endpoint) opens a REAL VoiceSession
bound to the campaign's agent - the answered conversation is a full
v69..v73 session: greeting, ASR engine, knowledge binding, brain.

Honesty:

* without provider credentials the dials are built and SKIPPED loudly -
  every target reports ``skipped`` with the exact reason, the campaign
  stays ``draft``; py8n never pretends it called anyone;
* the campaign + target rows are TRAFFIC STATE (calls that exist) - the
  same deliberate exception to derived-never-stored as the
  deployment-token hit rows; progress COUNTS are derived from the rows;
* the simulate endpoint is NAMED simulate - it exists so a demo or a
  test can walk the answered path without a carrier, and it says so in
  the session's context.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import VoiceCampaign, VoiceCampaignTarget, VoiceSession
from .interactions import _handler_name as _wf_name
from .voice_agents import VoiceAgentError, _load as _load_agent


class VoiceCampaignError(ValueError):
    """Honest 4xx-grade campaign failures."""


MAX_TARGETS = 500
TARGET_STATUSES = ("pending", "dialing", "answered", "completed",
                   "no_answer", "failed", "skipped")


def _now():
    return datetime.now(timezone.utc)


def client_state_for(campaign_id: str, target_id: str) -> str:
    raw = json.dumps({"cmp": campaign_id, "tgt": target_id}).encode()
    return base64.b64encode(raw).decode("ascii")


def decode_client_state(client_state: str) -> dict:
    try:
        data = json.loads(base64.b64decode(client_state or "").decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - a foreign client_state is not ours
        return {}


async def _load(db: AsyncSession, campaign_id: str, owner_id: str | None) -> VoiceCampaign:
    row = await db.get(VoiceCampaign, campaign_id)
    if row is None:
        raise VoiceCampaignError(f"campaign {campaign_id!r} not found")
    if owner_id is not None and row.owner_id is not None and row.owner_id != owner_id:
        raise VoiceCampaignError(f"campaign {campaign_id!r} not found")
    return row


async def _targets(db: AsyncSession, campaign_id: str,
                   limit: int | None = None) -> list[VoiceCampaignTarget]:
    q = (select(VoiceCampaignTarget)
         .where(VoiceCampaignTarget.campaign_id == campaign_id)
         .order_by(VoiceCampaignTarget.created_at.asc(), VoiceCampaignTarget.id.asc()))
    if limit:
        q = q.limit(int(limit))
    return list((await db.execute(q)).scalars().all())


def target_out(t: VoiceCampaignTarget) -> dict:
    return {
        "id": t.id, "address": t.address, "name": t.name, "status": t.status,
        "attempts": t.attempts, "session_id": t.session_id or None,
        "call_control_id": t.call_control_id or None,
        "last_error": t.last_error or None,
        "dialed_at": t.dialed_at.isoformat() if t.dialed_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


async def campaign_out(db: AsyncSession, row: VoiceCampaign, *,
                       include_targets: bool = True) -> dict:
    legs = await _targets(db, row.id)
    counts: dict[str, int] = {s: 0 for s in TARGET_STATUSES}
    for t in legs:
        counts[t.status] = counts.get(t.status, 0) + 1
    agent_name = None
    handler_id = None
    if row.agent_id:
        from ..models import VoiceAgent

        agent = await db.get(VoiceAgent, row.agent_id)
        if agent is not None:
            agent_name = agent.name
            handler_id = agent.handler_workflow_id
    endpoint_name = None
    if row.endpoint_id:
        from ..models import ChannelEndpoint

        ep = await db.get(ChannelEndpoint, row.endpoint_id)
        endpoint_name = ep.name if ep is not None else None
    out = {
        "id": row.id, "name": row.name, "status": row.status,
        "agent_id": row.agent_id, "agent_name": agent_name,
        "handler_workflow_id": handler_id,
        "endpoint_id": row.endpoint_id, "endpoint_name": endpoint_name,
        "config": row.config or {},
        "progress": {
            "total": len(legs),
            "counts": {k: v for k, v in counts.items() if v or k in ("pending", "answered")},
            "placed": counts["dialing"] + counts["answered"] + counts["completed"]
                      + counts["no_answer"] + counts["failed"],
        },
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
    }
    if include_targets:
        shown = legs[:200]
        out["targets"] = [target_out(t) for t in shown]
        if len(legs) > len(shown):
            out["targets_note"] = f"showing the first {len(shown)} of {len(legs)} targets"
    return out


async def create_campaign(db: AsyncSession, *, owner_id: str | None,
                          agent_id: str, name: str,
                          targets: list[dict], endpoint_id: str | None = None,
                          config: dict | None = None) -> dict:
    name = (name or "").strip()[:140]
    if not name:
        raise VoiceCampaignError("a campaign name is required")
    agent = await _load_agent(db, agent_id, owner_id)  # owner-scoped
    if not targets:
        raise VoiceCampaignError("a campaign needs at least one target")
    if len(targets) > MAX_TARGETS:
        raise VoiceCampaignError(f"a campaign carries at most {MAX_TARGETS} targets")
    clean: list[dict] = []
    for t in targets:
        if not isinstance(t, dict):
            raise VoiceCampaignError("each target must be {address, name?}")
        address = str(t.get("address") or "").strip()[:180]
        if not address:
            raise VoiceCampaignError("every target needs an address (E.164 or sip: URI)")
        clean.append({"address": address,
                      "name": str(t.get("name") or "").strip()[:140]})
    if endpoint_id:
        from ..models import ChannelEndpoint

        ep = await db.get(ChannelEndpoint, endpoint_id)
        if ep is None or (owner_id is not None and ep.owner_id is not None
                          and ep.owner_id != owner_id):
            raise VoiceCampaignError(f"channel endpoint {endpoint_id!r} not found")
        if (ep.provider or "") not in ("telnyx", "telnyx_call_control", "telnyx_sms"):
            raise VoiceCampaignError(
                f"endpoint {ep.name!r} is a {ep.provider!r} receiver - campaign dials "
                "need a telnyx voice endpoint")
    row = VoiceCampaign(owner_id=owner_id, agent_id=agent.id, name=name,
                        endpoint_id=endpoint_id or None, config=dict(config or {}))
    db.add(row)
    await db.flush()
    for t in clean:
        db.add(VoiceCampaignTarget(campaign_id=row.id, owner_id=owner_id,
                                   address=t["address"], name=t["name"]))
    await db.flush()
    await db.refresh(row)
    return await campaign_out(db, row)


async def get_campaign(db: AsyncSession, campaign_id: str, owner_id: str | None) -> dict:
    row = await _load(db, campaign_id, owner_id)
    return await campaign_out(db, row)


async def list_campaigns(db: AsyncSession, owner_id: str | None,
                         limit: int = 50) -> list[dict]:
    q = select(VoiceCampaign).order_by(VoiceCampaign.created_at.desc()).limit(max(1, min(limit, 200)))
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        rows = [r for r in rows if r.owner_id is None or r.owner_id == owner_id]
    return [await campaign_out(db, r, include_targets=False) for r in rows]


async def _deliver_dial(config: dict, request: dict, sender=None) -> dict:
    """Send one dial; ``sender(config, request) -> {status_code, json?}``
    injectable for tests. Missing credentials = honest skip, never sent."""
    masked = {"method": request["method"], "url": request["url"],
              "json": request.get("json")}
    if not str(config.get("api_key") or "").strip():
        return {"delivery": "skipped",
                "detail": "no api_key configured - the dial was built but NOT sent",
                "request": masked}
    if sender is None:
        async def _default_sender(cfg, req):
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(req["url"], json=req.get("json"),
                                         headers=req.get("headers", {}))
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                return {"status_code": resp.status_code, "json": body}
        sender = _default_sender
    try:
        result = await sender(config, request)
    except Exception as exc:  # noqa: BLE001 - the sandbox may have no egress
        return {"delivery": "failed", "detail": f"dial request failed: {exc}",
                "request": masked}
    ok = 200 <= int(result.get("status_code") or 0) < 300
    body = result.get("json") or {}
    return {"delivery": "delivered" if ok else "failed",
            "detail": f"telnyx answered {result.get('status_code')}",
            "call_control_id": str(((body.get("data") or {}).get("call_control_id")) or ""),
            "request": masked}


def _dial_request(db_row: VoiceCampaign, ep_config: dict, target: VoiceCampaignTarget) -> dict:
    from .channel_adapters import telnyx_build_dial

    webhook_url = str(ep_config.get("webhook_url") or "").strip()
    if not webhook_url:
        raise VoiceCampaignError(
            "the endpoint config carries no webhook_url - set the absolute URL the "
            "provider posts call events to (your /api/v1/channels/telnyx/<id>/webhook)")
    connection_id = str(ep_config.get("connection_id") or "").strip()
    if not connection_id:
        raise VoiceCampaignError(
            "the endpoint config carries no connection_id - set it (the Telnyx Call "
            "Control application id) so py8n can originate calls")
    return telnyx_build_dial(
        ep_config, to=target.address,
        from_ref=str(ep_config.get("from_number") or "").strip() or target.address,
        connection_id=connection_id, webhook_url=webhook_url,
        client_state=client_state_for(db_row.id, target.id))


async def start_campaign(db: AsyncSession, owner_id: str | None, campaign_id: str,
                         *, limit: int | None = None, sender=None) -> dict:
    """Dial the pending targets through the bound endpoint.

    Every dial is built with a client_state binding the carrier call to
    the target row; delivery is honest (skipped without credentials -
    the remaining pending targets are then marked skipped with the same
    reason rather than hammered one by one with a dial that cannot go).
    """
    row = await _load(db, campaign_id, owner_id)
    if row.status in ("stopped", "completed"):
        raise VoiceCampaignError(f"the campaign is {row.status} - no further dials")
    if not row.endpoint_id:
        # honest: without a provider endpoint NOTHING can dial
        pending = [t for t in await _targets(db, row.id) if t.status == "pending"]
        for t in pending:
            t.status = "skipped"
            t.last_error = ("no voice endpoint bound - bind endpoint_id (a telnyx "
                            "receiver with api_key + connection_id + webhook_url) "
                            "and re-start")
            t.updated_at = _now()
            db.add(t)
        await db.flush()
        out = await campaign_out(db, row)
        out["start_note"] = ("nothing was dialed - the campaign has no endpoint_id; "
                             "every pending target was marked skipped with the reason")
        return out
    from ..models import ChannelEndpoint

    ep = await db.get(ChannelEndpoint, row.endpoint_id)
    if ep is None:
        raise VoiceCampaignError(f"channel endpoint {row.endpoint_id!r} not found")
    ep_config = ep.config or {}
    pending = [t for t in await _targets(db, row.id) if t.status == "pending"]
    if limit:
        pending = pending[:max(1, int(limit))]
    dialed = 0
    skip_reason: str | None = None
    for t in pending:
        if skip_reason:
            t.status = "skipped"
            t.last_error = skip_reason
            t.updated_at = _now()
            db.add(t)
            continue
        try:
            request = _dial_request(row, ep_config, t)
        except VoiceCampaignError as exc:
            t.status = "skipped"
            t.last_error = str(exc)[:400]
            t.updated_at = _now()
            db.add(t)
            continue
        except ValueError as exc:
            t.status = "skipped"
            t.last_error = str(exc)[:400]
            t.updated_at = _now()
            db.add(t)
            continue
        result = await _deliver_dial(ep_config, request, sender=sender)
        t.attempts = (t.attempts or 0) + 1
        t.updated_at = _now()
        if result["delivery"] == "delivered" and result.get("call_control_id"):
            t.status = "dialing"
            t.call_control_id = result["call_control_id"][:180]
            t.dialed_at = _now()
            dialed += 1
        elif result["delivery"] == "delivered":
            t.status = "failed"
            t.last_error = "the dial was accepted but the response carried no call_control_id"
        elif result["delivery"] == "skipped":
            t.status = "skipped"
            t.last_error = str(result.get("detail") or "")[:400]
            skip_reason = t.last_error  # the same skip hits every remaining dial
        else:
            t.status = "failed"
            t.last_error = str(result.get("detail") or "")[:400]
        db.add(t)
    await db.flush()
    if dialed:
        if row.status == "draft":
            row.status = "running"
            row.started_at = row.started_at or _now()
        db.add(row)
    await db.flush()
    out = await campaign_out(db, row)
    out["start_note"] = (f"{dialed} dial(s) placed" if dialed
                         else "no dial was placed - see each target's last_error")
    return out


async def on_call_event(db: AsyncSession, *, call_control_id: str = "",
                        client_state: str = "", event_kind: str | None = None,
                        session=None) -> dict | None:
    """Bind a carrier call to its campaign target (webhook side).

    The target is found by call_control_id first, then by decoding the
    call's client_state. ``call.answered`` opens (or binds) the REAL
    session; hangup-family events close the target honestly
    (completed after an answered conversation, no_answer/failed when the
    dial never connected). Best-effort: None when the call is not a
    campaign call."""
    target: VoiceCampaignTarget | None = None
    campaign: VoiceCampaign | None = None
    if call_control_id:
        q = (select(VoiceCampaignTarget)
             .where(VoiceCampaignTarget.call_control_id == call_control_id)
             .order_by(VoiceCampaignTarget.created_at.desc()))
        target = (await db.execute(q)).scalars().first()
    if target is None and client_state:
        state = decode_client_state(client_state)
        cmp_id, tgt_id = state.get("cmp"), state.get("tgt")
        if cmp_id and tgt_id:
            campaign = await db.get(VoiceCampaign, str(cmp_id))
            if campaign is not None:
                target = await db.get(VoiceCampaignTarget, str(tgt_id))
                if target is not None and target.campaign_id != campaign.id:
                    target = None
    if target is None:
        return None
    campaign = campaign or await db.get(VoiceCampaign, target.campaign_id)
    if campaign is None:
        return None
    target.call_control_id = call_control_id or target.call_control_id
    target.updated_at = _now()

    if event_kind == "call.answered":
        if session is None:
            session = await voice_create_outbound(db, campaign, target,
                                                  call_control_id=call_control_id)
        else:
            target.session_id = session.id
        if session is not None:
            target.session_id = session.id
            if campaign.agent_id and not ((session.context or {}).get("voice_agent")):
                try:
                    from .voice_agents import bind_to_session

                    await bind_to_session(db, session, campaign.agent_id, session.owner_id)
                except VoiceAgentError:
                    pass
        target.status = "answered"
        target.last_error = ""
    elif event_kind in ("hangup", "no_answer", "busy", "failed") and target.status == "dialing":
        target.status = ("completed" if event_kind == "hangup"
                         else "no_answer" if event_kind == "no_answer" else "failed")
        target.last_error = f"call ended before answering ({event_kind})"
    db.add(target)
    await db.flush()
    return {"campaign_id": campaign.id, "target_id": target.id,
            "status": target.status, "session_id": target.session_id}


async def voice_create_outbound(db: AsyncSession, campaign: VoiceCampaign,
                                target: VoiceCampaignTarget, *,
                                call_control_id: str) -> VoiceSession:
    """The REAL answered session: outbound, bound to the campaign's agent,
    marked simulated only when the call_control_id says so."""
    from . import voice as voice_svc
    from .voice_agents import bind_to_session

    simulated = call_control_id.startswith("sim-")
    created = await voice_svc.create_session(
        db, owner_id=campaign.owner_id, direction="outbound", provider="telnyx",
        call_ref=call_control_id, from_ref="", to_ref=target.address,
        agent_id=campaign.agent_id)
    row = await db.get(VoiceSession, created["id"])
    await voice_svc.apply_event(db, row, "call.ringing", {"source": "campaign"})
    await voice_svc.apply_event(db, row, "call.answered",
                                {"source": "campaign_simulate" if simulated else "campaign"})
    ctx = dict(row.context or {})
    if simulated:
        ctx["campaign_simulated"] = True
    ctx["campaign_id"] = campaign.id
    ctx["campaign_target_id"] = target.id
    row.context = ctx
    db.add(row)
    await db.flush()
    return row


async def simulate_answer(db: AsyncSession, owner_id: str | None, campaign_id: str,
                          target_id: str) -> dict:
    """Walk ONE target through the answered path without a carrier.

    Named simulate, honest in the session's context (campaign_simulated)
    and in the response - a demo/test path, not a claimed real call."""
    row = await _load(db, campaign_id, owner_id)
    target = await db.get(VoiceCampaignTarget, target_id)
    if target is None or target.campaign_id != row.id:
        raise VoiceCampaignError(f"target {target_id!r} not found in campaign {campaign_id!r}")
    if target.status in ("answered", "completed"):
        raise VoiceCampaignError(
            f"target {target.address!r} is already {target.status} - one conversation per target")
    call_control_id = f"sim-{uuid.uuid4().hex[:12]}"
    session = await voice_create_outbound(db, row, target, call_control_id=call_control_id)
    target.status = "answered"
    target.call_control_id = call_control_id
    target.session_id = session.id
    target.attempts = (target.attempts or 0) + 1
    target.dialed_at = target.dialed_at or _now()
    target.updated_at = _now()
    target.last_error = ""
    db.add(target)
    if row.status == "draft":
        row.status = "running"
        row.started_at = row.started_at or _now()
        db.add(row)
    await db.flush()
    return {"target": target_out(target),
            "session_id": session.id,
            "simulated": True,
            "note": "the answered session was created through the SAME path a real "
                    "carrier answer takes - the session's context records it as simulated"}


async def stop_campaign(db: AsyncSession, owner_id: str | None, campaign_id: str) -> dict:
    row = await _load(db, campaign_id, owner_id)
    row.status = "stopped"
    row.ended_at = _now()
    db.add(row)
    await db.flush()
    return await campaign_out(db, row)
