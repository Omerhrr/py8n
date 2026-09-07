"""Escalation policies (v87) - the door's channel + repeat dimension.

v85's door escalates a stuck instance ONCE PER STATE STINT, on the
record, through the machine's own ``escalate`` move (or a no-move
'escalated' row) - but it cannot TELL anyone and it cannot REPEAT on a
cadence. An escalation policy rides the BusinessProcess definition and
declares the rest:

    escalation_policy = {
        "channel": "email" | "sms" | "whatsapp" | "telegram" | "discord" | "",
        "to": "ops@acme.com",            # the target - installer's to bind
        "repeat_every_seconds": 3600,    # re-escalate cadence (>= 60)
        "max_repeats": 3,                # repeats AFTER the first attempt
        "message_template": "...",       # {process} {ref} {title} {state} {overdue_minutes} {attempt}
    }

The door (business_processes.escalate_stuck - the APScheduler sweep and
POST /scheduler/escalations/tick) consults the policy per stuck instance:

  * a policy-carrying machine gates its repeats HERE: attempt 1 on the
    first observation of the episode, then one attempt per
    repeat_every_seconds until 1 + max_repeats, then the episode is
    complete and the door holds - a state change starts a fresh episode
    (the bookkeeping rides the instance's running memory,
    context.escalations, fresh-dict discipline);
  * every attempt DELIVERS over the policy's channel through the owner's
    ChannelEndpoint and emits ``business.escalated`` on the instance's
    correlation thread (beside the door's ``business.stuck``) - absent
    target, endpoint or credentials is an HONEST SKIP recorded in the
    payload, never a silent one;
  * a machine WITHOUT a policy keeps the v85 semantics exactly: one
    knock per stint, on the record, no channel.

Channels stay interchangeable infrastructure: the policy names the
CHANNEL, the owner's endpoints name the providers. The clock is
injectable (now=) so tests and replay tools walk time without sleeping.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import BusinessProcessInstance, ChannelEndpoint

# text channels the escalation can ride (voice needs a call - not a tick's job)
ESCALATION_CHANNELS = ("email", "sms", "whatsapp", "telegram", "discord")

MIN_REPEAT_SECONDS = 60
DEFAULT_REPEAT_SECONDS = 3600
DEFAULT_MAX_REPEATS = 3
DEFAULT_TEMPLATE = ("[py8n] {process}: '{title}' (ref {ref}) has been in state "
                    "'{state}' for {overdue_minutes} minutes past its SLA "
                    "(escalation attempt {attempt}).")

_POLICY_KEYS = {"channel", "to", "repeat_every_seconds", "max_repeats",
                "message_template"}


class EscalationPolicyError(ValueError):
    """Honest escalation-policy failures."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes - normalize before arithmetic."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# the policy - validated loudly, normalized once
# ---------------------------------------------------------------------------

def validate_escalation_policy(policy: dict | None) -> dict | None:
    """Normalize an escalation policy; None/empty means 'no policy'.

    Loud refusals name the exact problem: unknown keys, unknown channels,
    a repeat cadence faster than the door can honestly honor.
    """
    if policy is None:
        return None
    if not isinstance(policy, dict):
        raise EscalationPolicyError("escalation_policy must be an object")
    if not policy:
        return None
    unknown = sorted(set(policy) - _POLICY_KEYS)
    if unknown:
        raise EscalationPolicyError(
            f"escalation_policy has unknown key(s) {unknown} - allowed: {sorted(_POLICY_KEYS)}")
    channel = str(policy.get("channel") or "").strip().lower()
    if channel and channel not in ESCALATION_CHANNELS:
        raise EscalationPolicyError(
            f"escalation channel {channel!r} is not a deliverable channel "
            f"(known: {', '.join(ESCALATION_CHANNELS)}; empty = event-only escalation)")
    to = str(policy.get("to") or "").strip()
    try:
        repeat = int(policy.get("repeat_every_seconds") or DEFAULT_REPEAT_SECONDS)
    except (TypeError, ValueError):
        raise EscalationPolicyError("repeat_every_seconds must be an integer") from None
    if repeat < MIN_REPEAT_SECONDS:
        raise EscalationPolicyError(
            f"repeat_every_seconds must be >= {MIN_REPEAT_SECONDS} (the door ticks "
            "on an interval - a faster cadence cannot be honestly honored)")
    try:
        max_repeats = int(policy.get("max_repeats") if policy.get("max_repeats") is not None
                          else DEFAULT_MAX_REPEATS)
    except (TypeError, ValueError):
        raise EscalationPolicyError("max_repeats must be an integer") from None
    if max_repeats < 0:
        raise EscalationPolicyError("max_repeats must be >= 0 (0 = escalate once, never repeat)")
    template = str(policy.get("message_template") or "").strip() or DEFAULT_TEMPLATE
    return {"channel": channel, "to": to,
            "repeat_every_seconds": repeat, "max_repeats": max_repeats,
            "message_template": template}


def policy_from_definition(definition: dict | None) -> dict | None:
    """Read the policy out of a stored definition (normalized at write
    time by validate_definition - re-validated here so direct writes
    cannot smuggle one in behind the machine's back)."""
    d = definition if isinstance(definition, dict) else {}
    raw = d.get("escalation_policy")
    if not raw:
        return None
    return validate_escalation_policy(raw)


def render_message(template: str, *, process_name: str, ref: str, title: str,
                   state: str, overdue_minutes: int, attempt: int) -> str:
    """Fill the template - unknown placeholders stay literal, missing
    values render empty (a message goes out with what is known, never
    fails the door)."""
    try:
        return template.format(process=process_name, ref=ref, title=title,
                               state=state, overdue_minutes=overdue_minutes,
                               attempt=attempt)
    except Exception:  # noqa: BLE001 - a broken template must not stop the door
        return (f"[py8n] {process_name}: '{title}' (ref {ref}) stuck in '{state}' "
                f"past its SLA (escalation attempt {attempt}).")


def describe_policy(policy: dict | None) -> str:
    """One-line human summary for boards and shelves."""
    if not policy:
        return "no escalation policy"
    cadence = f"every {policy['repeat_every_seconds']}s"
    channel = policy["channel"] or "event-only"
    return (f"stuck -> {channel} (x{1 + policy['max_repeats']}, {cadence})"
            + (f" -> {policy['to']}" if policy["to"] else ""))


# ---------------------------------------------------------------------------
# the episode - bookkeeping on the instance's running memory
# ---------------------------------------------------------------------------

def _episode_book(instance: BusinessProcessInstance) -> dict:
    book = (instance.context or {}).get("escalations")
    return book if isinstance(book, dict) else {}


def episode_gate(instance: BusinessProcessInstance, policy: dict,
                 now: datetime) -> dict:
    """Decide what the door does for a policy-carrying machine's stuck
    instance RIGHT NOW: escalate (with the attempt number) or hold.

    An episode belongs to ONE stuck stint: the bookkeeping remembers the
    state it started in - the instance moving states starts fresh. Past
    1 + max_repeats the episode is complete; before repeat_every_seconds
    has elapsed since the last attempt the door holds (too_soon)."""
    now = _aware(now) or _now()
    book = _episode_book(instance)
    fresh = book.get("state") != instance.state
    count = 0 if fresh else int(book.get("count") or 0)
    if count >= 1 + policy["max_repeats"]:
        return {"action": "hold", "reason": "episode_complete", "attempts": count}
    last = _parse_iso(book.get("last_at")) if not fresh else None
    if last is not None and (now - last).total_seconds() < policy["repeat_every_seconds"]:
        return {"action": "hold", "reason": "too_soon",
                "next_in_seconds": round(policy["repeat_every_seconds"]
                                         - (now - last).total_seconds())}
    return {"action": "escalate", "attempt": count + 1}


def record_episode(db: AsyncSession, instance: BusinessProcessInstance,
                   attempt: int, delivery: dict, now: datetime) -> None:
    """Remember the attempt on the instance's running memory (fresh-dict
    discipline - the JSON column is never mutated in place)."""
    now = _aware(now) or _now()
    new_ctx = dict(instance.context or {})
    new_ctx["escalations"] = {"count": attempt, "last_at": now.isoformat(),
                              "state": instance.state,
                              "last_delivery": delivery.get("delivery", ""),
                              "last_detail": (delivery.get("detail") or "")[:300]}
    instance.context = new_ctx
    db.add(instance)


# ---------------------------------------------------------------------------
# the delivery - channels are interchangeable infrastructure
# ---------------------------------------------------------------------------

async def _resolve_endpoint(db: AsyncSession, owner_id: str | None,
                            channel: str) -> ChannelEndpoint | None:
    """The owner's first enabled endpoint on the channel (the policy names
    the CHANNEL, the endpoints name the providers)."""
    q = (select(ChannelEndpoint)
         .where(ChannelEndpoint.channel == channel,
                ChannelEndpoint.enabled.is_(True))
         .order_by(ChannelEndpoint.created_at.asc()))
    if owner_id is not None:
        q = q.where(ChannelEndpoint.owner_id.in_((owner_id, None)))
    return (await db.execute(q)).scalars().first()


async def deliver_escalation(db: AsyncSession, instance: BusinessProcessInstance,
                             policy: dict, *, process_name: str,
                             overdue_seconds: int, attempt: int,
                             moved_to: str | None, actor: str = "scheduler",
                             now: datetime | None = None) -> dict:
    """Deliver the escalation over the policy's channel and put it on the
    record: one ``business.escalated`` event on the instance's correlation
    thread carrying the delivery result (an honest skip IS a result)."""
    from . import system_events as events_svc

    now = _aware(now) or _now()
    overdue_minutes = max(0, overdue_seconds // 60)
    base = {"process_id": instance.process_id, "process_name": process_name,
            "instance_id": instance.id, "ref": instance.ref,
            "title": instance.title, "state": instance.state,
            "overdue_seconds": overdue_seconds,
            "overdue_minutes": overdue_minutes, "attempt": attempt,
            "max_repeats": policy["max_repeats"], "moved_to": moved_to}
    if not policy["channel"]:
        delivery = {"delivery": "skipped",
                    "detail": "policy is event-only (no channel configured) - "
                              "the escalation is on the event timeline"}
    elif not policy["to"]:
        delivery = {"delivery": "skipped",
                    "detail": f"no target configured - bind escalation_policy.to "
                              f"to deliver over {policy['channel']}"}
    else:
        endpoint = await _resolve_endpoint(db, instance.owner_id, policy["channel"])
        if endpoint is None:
            delivery = {"delivery": "skipped",
                        "detail": f"no {policy['channel']} channel endpoint bound - "
                                  "the escalation is on the event timeline but "
                                  "was not delivered"}
        else:
            from . import channel_endpoints as cep_svc

            text = render_message(policy["message_template"],
                                  process_name=process_name, ref=instance.ref,
                                  title=instance.title, state=instance.state,
                                  overdue_minutes=overdue_minutes, attempt=attempt)
            result = await cep_svc.deliver_outbound(endpoint, policy["to"], text)
            delivery = {"delivery": result.get("delivery", "failed"),
                        "detail": result.get("detail", ""),
                        "endpoint": endpoint.name, "provider": endpoint.provider}
    await events_svc.emit(
        db, instance.owner_id, "business.escalated", source="business",
        actor=actor, target_type="process_instance", target_id=instance.id,
        payload={**base, "channel": policy["channel"] or None,
                 "to": policy["to"] or None, **delivery},
        correlation_id=instance.id)
    return delivery


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None
