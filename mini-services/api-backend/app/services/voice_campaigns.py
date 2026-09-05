"""Outbound voice campaigns (v74) - dial a list through an agent.

A campaign dials a list of addresses through the meeting/campaign dial
primitive (``telnyx_build_dial``) with a client_state binding every
carrier event back to the target row. When a call is ANSWERED, the
webhook path (or the honest simulate endpoint) opens a REAL VoiceSession
bound to the campaign's agent - the answered conversation is a full
v69..v73 session: greeting, ASR engine, knowledge binding, brain.

v75 adds the two things a real dialer cannot live without:

* RETRY SCHEDULES - a campaign config carries ``retry``:
  ``{max_attempts, delays_minutes, retry_on}``. When a target lands in a
  retryable outcome (no_answer by default), the NEXT attempt is scheduled
  at ``now + delay[attempts-1]`` into the target's meta (traffic state
  about future traffic, same deliberate exception as the rows
  themselves); ``POST /campaigns/{id}/retry`` dials what is DUE and
  reports due/deferred/exhausted honestly.
* ANSWERING MACHINE DETECTION - the dial request carries Telnyx's
  ``machine_detection`` mode (``detect`` keeps the call up and reports;
  ``greeting_end`` waits out the greeting); when the carrier's AMD
  verdict arrives as ``call.machine.detection.ended`` (mapped by v70 to
  ``voicemail_detected``), the campaign marks the target ``voicemail``
  and - with the default ``on_machine: hangup`` policy - stops spending
  the agent on the machine. ``on_machine: continue`` records the verdict
  and lets the conversation run.

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
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import VoiceCampaign, VoiceCampaignTarget, VoiceSession
from .interactions import _handler_name as _wf_name
from .voice_agents import VoiceAgentError, _load as _load_agent


class VoiceCampaignError(ValueError):
    """Honest 4xx-grade campaign failures."""


MAX_TARGETS = 500
TARGET_STATUSES = ("pending", "dialing", "answered", "completed",
                   "no_answer", "failed", "skipped", "voicemail")

# v75: retry schedules + answering machine detection - the validated shapes
RETRY_ON_CHOICES = ("no_answer", "failed", "voicemail")
AMD_MODES = ("disabled", "detect", "greeting_end")
# v76: "voicemail_drop" joins the policy list - when the greeting ends the
# campaign LEAVES A MESSAGE (amd.voicemail_message) instead of hanging up
# on the beep or spending the agent on a machine
AMD_ON_MACHINE = ("hangup", "continue", "voicemail_drop")
DEFAULT_RETRY = {"max_attempts": 3, "delays_minutes": [15, 60, 1440],
                 "retry_on": ["no_answer"]}
DEFAULT_AMD = {"mode": "disabled", "on_machine": "hangup", "voicemail_message": ""}


def _now():
    return datetime.now(timezone.utc)


def validate_config(config: dict | None) -> dict:
    """Validate the v75 campaign config blocks (retry + amd) at write time
    - fail loud on garbage, fill defaults on omission."""
    config = dict(config or {})
    retry_in = dict(config.get("retry") or {})
    amd_in = dict(config.get("amd") or {})
    retry = dict(DEFAULT_RETRY)
    if retry_in:
        try:
            ma = int(retry_in.get("max_attempts", retry["max_attempts"]))
        except (TypeError, ValueError):
            raise VoiceCampaignError("retry.max_attempts must be an integer")
        if not 1 <= ma <= 5:
            raise VoiceCampaignError("retry.max_attempts must be 1..5 (a dialer that "
                                     "hammers a number is a spammer, not a system)")
        retry["max_attempts"] = ma
        delays = retry_in.get("delays_minutes", retry["delays_minutes"])
        if not isinstance(delays, (list, tuple)) or not delays:
            raise VoiceCampaignError("retry.delays_minutes must be a non-empty list of "
                                     "minute delays, e.g. [15, 60, 1440]")
        try:
            delays = [int(d) for d in delays]
        except (TypeError, ValueError):
            raise VoiceCampaignError("retry.delays_minutes entries must be integers (minutes)")
        if any(d < 0 or d > 10080 for d in delays):
            raise VoiceCampaignError("retry.delays_minutes entries must be 0..10080 "
                                     "(a week) - past that, re-import the list instead")
        retry["delays_minutes"] = delays
        retry_on = retry_in.get("retry_on", retry["retry_on"])
        if (not isinstance(retry_on, (list, tuple))
                or not retry_on
                or not all(s in RETRY_ON_CHOICES for s in retry_on)):
            raise VoiceCampaignError(
                f"retry.retry_on must be a non-empty subset of {RETRY_ON_CHOICES}")
        retry["retry_on"] = list(dict.fromkeys(retry_on))
    amd = dict(DEFAULT_AMD)
    if amd_in:
        mode = str(amd_in.get("mode", amd["mode"]) or "disabled")
        if mode not in AMD_MODES:
            raise VoiceCampaignError(f"amd.mode must be {'|'.join(AMD_MODES)}, got {mode!r}")
        amd["mode"] = mode
        on_machine = str(amd_in.get("on_machine", amd["on_machine"]) or "hangup")
        if on_machine not in AMD_ON_MACHINE:
            raise VoiceCampaignError(
                f"amd.on_machine must be {'|'.join(AMD_ON_MACHINE)}, got {on_machine!r}")
        amd["on_machine"] = on_machine
        # v76: the drop policy has two honest prerequisites - a message to
        # leave, and greeting_end detection (the drop is TRIGGERED by the
        # greeting ending; detect-mode verdicts give no such signal)
        message = str(amd_in.get("voicemail_message") or "").strip()
        if on_machine == "voicemail_drop":
            if amd["mode"] != "greeting_end":
                raise VoiceCampaignError(
                    "amd.on_machine='voicemail_drop' needs amd.mode='greeting_end' - "
                    "the drop is triggered by the greeting ENDING, which only "
                    "greeting_end detection reports")
            if not message:
                raise VoiceCampaignError(
                    "amd.on_machine='voicemail_drop' needs amd.voicemail_message - "
                    "a drop with nothing to say is not a drop")
        if message:
            if len(message) > 600:
                raise VoiceCampaignError(
                    "amd.voicemail_message must be 600 characters or fewer - "
                    "nobody listens to a minute of recorded menu")
            amd["voicemail_message"] = message
    config["retry"] = retry
    config["amd"] = amd
    return config


def retry_plan(campaign: VoiceCampaign) -> dict:
    """The campaign's normalized retry plan (validated at write, defaulted
    for rows created before v75)."""
    raw = (campaign.config or {}).get("retry") or {}
    return {"max_attempts": int(raw.get("max_attempts") or DEFAULT_RETRY["max_attempts"]),
            "delays_minutes": list(raw.get("delays_minutes") or DEFAULT_RETRY["delays_minutes"]),
            "retry_on": list(raw.get("retry_on") or DEFAULT_RETRY["retry_on"])}


def amd_plan(campaign: VoiceCampaign) -> dict:
    raw = (campaign.config or {}).get("amd") or {}
    return {"mode": str(raw.get("mode") or DEFAULT_AMD["mode"]),
            "on_machine": str(raw.get("on_machine") or DEFAULT_AMD["on_machine"]),
            "voicemail_message": str(raw.get("voicemail_message") or "")}


def schedule_retry(campaign: VoiceCampaign, target: VoiceCampaignTarget) -> dict:
    """Book the target's NEXT attempt after it landed in a retryable
    outcome. The schedule lives in target.meta (``retry_at`` iso + the
    reason) - traffic state about future traffic, the same deliberate
    exception as the rows themselves. Returns the decision honestly."""
    plan = retry_plan(campaign)
    attempts = int(target.attempts or 0)
    meta = dict(target.meta or {})
    decision: dict = {"eligible": False}
    if (target.status in plan["retry_on"] and attempts < plan["max_attempts"]):
        delay = plan["delays_minutes"][min(attempts - 1, len(plan["delays_minutes"]) - 1)]
        retry_at = _now() + timedelta(minutes=delay)
        meta["retry_at"] = retry_at.isoformat()
        meta["retry_delay_minutes"] = delay
        decision = {"eligible": True, "retry_at": retry_at.isoformat(),
                    "delay_minutes": delay, "next_attempt": attempts + 1}
    else:
        meta.pop("retry_at", None)
        meta["retry_done"] = True
        decision["reason"] = ("exhausted: attempts cap reached" if attempts >= plan["max_attempts"]
                              else f"{target.status} is not in retry_on {plan['retry_on']}")
    target.meta = meta
    return decision


def _retry_due_at(target: VoiceCampaignTarget) -> datetime | None:
    raw = ((target.meta or {}).get("retry_at")) or ""
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


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
    meta = t.meta or {}
    return {
        "id": t.id, "address": t.address, "name": t.name, "status": t.status,
        "attempts": t.attempts, "session_id": t.session_id or None,
        "call_control_id": t.call_control_id or None,
        "last_error": t.last_error or None,
        "retry_at": meta.get("retry_at"),
        "retry_delay_minutes": meta.get("retry_delay_minutes"),
        "amd": meta.get("amd"),
        "voicemail_drop": meta.get("voicemail_drop"),
        "dialed_at": t.dialed_at.isoformat() if t.dialed_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _retry_progress(row: VoiceCampaign,
                    legs: list[VoiceCampaignTarget]) -> dict:
    """The retry picture of the campaign, derived at read time: how many
    targets are eligible (retryable outcome, attempts below the cap), how
    many are due now, how many are exhausted."""
    plan = retry_plan(row)
    now = _now()
    eligible = due = exhausted = 0
    for t in legs:
        attempts = int(t.attempts or 0)
        if t.status in plan["retry_on"]:
            if attempts >= plan["max_attempts"]:
                exhausted += 1
            else:
                eligible += 1
                at = _retry_due_at(t)
                if at is None or at <= now:
                    due += 1
    return {"plan": plan, "eligible": eligible, "due": due, "exhausted": exhausted}


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
                      + counts["no_answer"] + counts["failed"] + counts["voicemail"],
            # v75: the retry picture, derived from the rows at read time
            "retry": _retry_progress(row, legs),
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
    # v75: retry schedule + AMD config validated at write time, defaults filled
    config = validate_config(config)
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
    # v75: answering machine detection rides the dial when the campaign
    # opted in (amd.mode) - the carrier listens for the beep and reports
    # the verdict through call.machine.detection.ended
    amd = amd_plan(db_row)
    return telnyx_build_dial(
        ep_config, to=target.address,
        from_ref=str(ep_config.get("from_number") or "").strip() or target.address,
        connection_id=connection_id, webhook_url=webhook_url,
        client_state=client_state_for(db_row.id, target.id),
        machine_detection=(amd["mode"] if amd["mode"] != "disabled" else ""))


async def _dial_pending(db: AsyncSession, row: VoiceCampaign,
                        ep_config: dict, targets: list[VoiceCampaignTarget],
                        sender=None, *, mark_skipped: bool = True) -> tuple[int, int, int]:
    """Dial the given targets (the shared loop of start + retry).

    Returns (dialed, skipped, failed) honestly. The first hard skip (no
    api_key etc.) marks every remaining dial skipped with the same reason
    rather than hammering the gateway that already said no. A dial that
    FAILED (and outcomes that land later through the webhook) schedules
    the target's next attempt per the campaign's retry plan.

    ``mark_skipped``: START marks never-attempted pending targets
    ``skipped`` (they will not dial themselves later); a RETRY pass keeps
    the target's last real outcome so the schedule stays intact."""
    dialed = skipped = failed = 0
    skip_reason: str | None = None
    for t in targets:
        if skip_reason:
            if mark_skipped and t.status == "pending":
                t.status = "skipped"
            t.last_error = skip_reason
            t.updated_at = _now()
            db.add(t)
            skipped += 1
            continue
        try:
            request = _dial_request(row, ep_config, t)
        except (VoiceCampaignError, ValueError) as exc:
            t.status = "skipped"
            t.last_error = str(exc)[:400]
            t.updated_at = _now()
            db.add(t)
            skipped += 1
            continue
        result = await _deliver_dial(ep_config, request, sender=sender)
        t.attempts = (t.attempts or 0) + 1
        t.updated_at = _now()
        if result["delivery"] == "delivered" and result.get("call_control_id"):
            t.status = "dialing"
            t.call_control_id = result["call_control_id"][:180]
            t.dialed_at = _now()
            t.last_error = ""
            dialed += 1
        elif result["delivery"] == "delivered":
            t.status = "failed"
            t.last_error = "the dial was accepted but the response carried no call_control_id"
            schedule_retry(row, t)
            failed += 1
        elif result["delivery"] == "skipped":
            # the dial could not even be attempted: START marks the target
            # skipped honestly; a RETRY keeps the last real outcome (the
            # schedule stays) and records why the pass produced nothing
            if mark_skipped and t.status == "pending":
                t.status = "skipped"
            t.last_error = str(result.get("detail") or "")[:400]
            skip_reason = t.last_error  # the same skip hits every remaining dial
            skipped += 1
        else:
            t.status = "failed"
            t.last_error = str(result.get("detail") or "")[:400]
            schedule_retry(row, t)
            failed += 1
        db.add(t)
    await db.flush()
    return dialed, skipped, failed


async def start_campaign(db: AsyncSession, owner_id: str | None, campaign_id: str,
                         *, limit: int | None = None, sender=None) -> dict:
    """Dial the pending targets through the bound endpoint.

    Every dial is built with a client_state binding the carrier call to
    the target row; delivery is honest (skipped without credentials -
    the remaining pending targets are then marked skipped with the same
    reason rather than hammered one by one with a dial that cannot go).
    Outcomes that arrive later (no_answer, busy) schedule the target's
    retry per the campaign's plan; POST /campaigns/{id}/retry dials them
    when due.
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
    dialed, skipped, failed = await _dial_pending(db, row, ep_config, pending,
                                                  sender=sender)
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


async def retry_due(db: AsyncSession, owner_id: str | None, campaign_id: str,
                    *, limit: int | None = None, force: bool = False,
                    sender=None) -> dict:
    """The retry pass (v75): dial the targets whose next attempt is DUE.

    Eligible = the target's last outcome is in the plan's ``retry_on``
    and its attempts sit below ``max_attempts``. Due = its scheduled
    ``retry_at`` has passed (``force: true`` dials eligible targets even
    when not yet due - a manual override that says so in the response).
    Deferred targets are left untouched with their schedule intact."""
    row = await _load(db, campaign_id, owner_id)
    if row.status in ("stopped", "completed"):
        raise VoiceCampaignError(f"the campaign is {row.status} - no further dials")
    if not row.endpoint_id:
        raise VoiceCampaignError(
            "the campaign has no endpoint_id - retries dial through the same "
            "endpoint the campaign dialed originally")
    from ..models import ChannelEndpoint

    ep = await db.get(ChannelEndpoint, row.endpoint_id)
    if ep is None:
        raise VoiceCampaignError(f"channel endpoint {row.endpoint_id!r} not found")
    plan = retry_plan(row)
    now = _now()
    legs = await _targets(db, row.id)
    due: list[VoiceCampaignTarget] = []
    deferred = exhausted = 0
    for t in legs:
        if t.status not in plan["retry_on"]:
            continue
        attempts = int(t.attempts or 0)
        if attempts >= plan["max_attempts"]:
            exhausted += 1
            continue
        at = _retry_due_at(t)
        if at is not None and at > now and not force:
            deferred += 1
            continue
        due.append(t)
    beyond_limit = 0
    if limit:
        due_sorted = due
        beyond_limit = max(0, len(due_sorted) - max(1, int(limit)))
        due = due_sorted[:max(1, int(limit))]
    dialed, skipped, failed = (0, 0, 0)
    if due:
        dialed, skipped, failed = await _dial_pending(db, row, ep.config or {},
                                                      due, sender=sender,
                                                      mark_skipped=False)
    out = await campaign_out(db, row)
    out["retry_note"] = (f"{dialed} retry dial(s) placed, {deferred} deferred "
                         f"(not yet due), {exhausted} exhausted (attempts cap)"
                         + (f", {beyond_limit} left beyond the pass limit" if beyond_limit else "")
                         if (dialed or deferred or exhausted or beyond_limit)
                         else "nothing was due for retry")
    out["retry_pass"] = {"considered": len(due) + deferred + exhausted + beyond_limit,
                         "dialed": dialed, "skipped": skipped, "failed": failed,
                         "deferred": deferred, "exhausted": exhausted,
                         "beyond_limit": beyond_limit,
                         "forced": bool(force)}
    return out


async def on_call_event(db: AsyncSession, *, call_control_id: str = "",
                        client_state: str = "", event_kind: str | None = None,
                        session=None) -> dict | None:
    """Bind a carrier call to its campaign target (webhook side).

    The target is found by call_control_id first, then by decoding the
    call's client_state. ``call.answered`` opens (or binds) the REAL
    session; hangup-family events close the target honestly
    (completed after an answered conversation, no_answer/failed when the
    dial never connected) and SCHEDULE the target's retry per the
    campaign's plan; ``voicemail_detected`` (the carrier's answering
    machine verdict, v75) marks the target and reports whether the AMD
    policy says hang up. Best-effort: None when the call is not a
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

    amd_decision: dict | None = None
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
    elif event_kind == "voicemail_detected":
        # v75: the carrier's AMD verdict - record it, act on the policy.
        # The session-side voicemail state + hangup are the RECEIVER's job
        # (it owns the state machine + the provider command); this marks
        # the campaign's target row and returns the decision.
        amd = amd_plan(campaign)
        meta = dict(target.meta or {})
        meta["amd"] = {"result": "machine", "at": _now().isoformat(),
                       "mode": amd["mode"]}
        target.meta = meta
        amd_decision = {"on_machine": amd["on_machine"], "hangup": amd["on_machine"] == "hangup"}
        if amd["on_machine"] == "hangup":
            target.status = "voicemail"
            target.last_error = ""
        # on_machine=continue: the conversation proceeds, target stays answered
    elif event_kind == "greeting_end":
        # v76: the machine's greeting FINISHED (greeting_end AMD mode) -
        # the voicemail-drop trigger. The target is a machine; the policy
        # decides what the campaign says to it: hang up, keep talking, or
        # DROP the configured message. The receiver + record_voicemail_drop
        # own the session-side mechanics; this books the verdict + decision.
        amd = amd_plan(campaign)
        meta = dict(target.meta or {})
        meta["amd"] = {"result": "greeting_ended", "at": _now().isoformat(),
                       "mode": amd["mode"]}
        target.meta = meta
        target.status = "voicemail"
        target.last_error = ""
        amd_decision = {"on_machine": amd["on_machine"],
                        "hangup": amd["on_machine"] == "hangup"}
        if amd["on_machine"] == "voicemail_drop":
            amd_decision["drop"] = True
            amd_decision["message"] = amd["voicemail_message"]
    elif event_kind in ("hangup", "no_answer", "busy", "failed") and target.status == "dialing":
        target.status = ("completed" if event_kind == "hangup"
                         else "no_answer" if event_kind == "no_answer" else "failed")
        target.last_error = f"call ended before answering ({event_kind})"
        if target.status != "completed":
            # v75: the dial connected to no one - schedule the next attempt
            schedule_retry(campaign, target)
    elif event_kind == "hangup" and target.status == "answered":
        # v75: the conversation happened and the call is over - the target
        # completes (the session already carries the transcript)
        target.status = "completed"
    db.add(target)
    await db.flush()
    out = {"campaign_id": campaign.id, "target_id": target.id,
           "status": target.status, "session_id": target.session_id}
    if amd_decision is not None:
        out["amd"] = amd_decision
    return out


async def record_voicemail_drop(db: AsyncSession, campaign: VoiceCampaign,
                                target: VoiceCampaignTarget, session: VoiceSession, *,
                                hangup_session: bool = True) -> dict:
    """The campaign's side of the DROP (v76): run the drop primitive on the
    answered session with the campaign's configured message, book the drop
    on the target row (meta.voicemail_drop - traffic state about traffic
    that happened), and mark the target voicemail.

    ``hangup_session=False`` lets the RECEIVER order the wire properly
    (speak command first, hangup command second) while py8n's own state
    machine still ends honestly right after; the default closes the call
    inside the primitive (the simulate path)."""
    from . import voice as voice_svc

    amd = amd_plan(campaign)
    drop = await voice_svc.voicemail_drop(db, session, message=amd["voicemail_message"])
    if hangup_session is False:
        # the receiver still owes the wire its hangup command; py8n's state
        # machine already ended (reason voicemail_drop) inside the primitive
        pass
    meta = dict(target.meta or {})
    meta["voicemail_drop"] = {"message": drop["message"], "dropped_at": drop["dropped_at"],
                              "tts_provider": drop["tts"]["provider"],
                              "tts_id": drop["tts"]["tts_id"], "session_id": session.id}
    target.meta = meta
    target.status = "voicemail"
    target.last_error = ""
    target.updated_at = _now()
    db.add(target)
    await db.flush()
    return drop


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
                          target_id: str, *, as_machine: bool | str = False) -> dict:
    """Walk ONE target through the answered path without a carrier.

    ``as_machine: true`` compresses the carrier's AMD sequence into the
    same walk: the call is answered, then the machine verdict lands and
    the campaign's ``on_machine`` policy is applied exactly as a real
    ``call.machine.detection.ended`` would drive it.
    ``as_machine: "greeting_end"`` (v76) walks the greeting_end sequence
    instead - the verdict AND the greeting-finished signal - so a drop
    policy performs its real drop (message + hangup) inside the walk.

    Named simulate, honest in the session's context (campaign_simulated)
    and in the response - a demo/test path, not a claimed real call."""
    row = await _load(db, campaign_id, owner_id)
    target = await db.get(VoiceCampaignTarget, target_id)
    if target is None or target.campaign_id != row.id:
        raise VoiceCampaignError(f"target {target_id!r} not found in campaign {campaign_id!r}")
    if target.status in ("answered", "completed", "voicemail"):
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
    amd_result: dict | None = None
    if as_machine == "greeting_end":
        # v76: the greeting_end walk - verdict + greeting-finished signal,
        # then the policy: a drop policy DROPS the message right here
        from . import voice as voice_svc

        await voice_svc.apply_event(db, session, "greeting_end",
                                    {"source": "campaign_simulate"})
        link = await on_call_event(db, call_control_id=call_control_id,
                                   event_kind="greeting_end", session=session)
        amd_result = link.get("amd") if link else None
        await db.refresh(target)
        if amd_result and amd_result.get("drop"):
            drop = await record_voicemail_drop(db, row, target, session)
            amd_result["drop_record"] = {"message": drop["message"],
                                         "tts_id": drop["tts"]["tts_id"]}
        elif amd_result and amd_result.get("hangup") and session.state != "ended":
            await voice_svc.hangup(db, session, reason="answering_machine")
    elif as_machine:
        from . import voice as voice_svc

        # the AMD verdict lands on the ANSWERED session, exactly the order
        # a carrier produces: answered -> machine detection -> policy
        link = await on_call_event(db, call_control_id=call_control_id,
                                   event_kind="voicemail_detected", session=session)
        amd_result = link.get("amd") if link else None
        await db.refresh(target)
        if amd_result and amd_result.get("hangup") and session.state != "ended":
            await voice_svc.apply_event(db, session, "voicemail_detected",
                                        {"source": "campaign_simulate"})
            await voice_svc.hangup(db, session, reason="answering_machine")
    if row.status == "draft":
        row.status = "running"
        row.started_at = row.started_at or _now()
        db.add(row)
    await db.flush()
    out = {"target": target_out(target),
           "session_id": session.id,
           "simulated": True,
           "note": "the answered session was created through the SAME path a real "
                   "carrier answer takes - the session's context records it as simulated"}
    if as_machine:
        out["amd"] = {"simulated": True, **(amd_result or {}),
                      "target_status": target.status,
                      "session_state": session.state}
        if session.state == "ended":
            out["amd"]["session_end_reason"] = session.end_reason
    return out


async def stop_campaign(db: AsyncSession, owner_id: str | None, campaign_id: str) -> dict:
    row = await _load(db, campaign_id, owner_id)
    row.status = "stopped"
    row.ended_at = _now()
    db.add(row)
    await db.flush()
    return await campaign_out(db, row)
