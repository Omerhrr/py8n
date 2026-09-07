"""Escalation policies (v87) - the door's channel + repeat dimension.

v85's door escalates a stuck instance ONCE PER STATE STINT, on the
record, through the machine's own ``escalate`` move (or a no-move
'escalated' row) - but it cannot TELL anyone and it cannot REPEAT on a
cadence. An escalation policy rides the BusinessProcess definition and
declares the rest:

    escalation_policy = {
        "channel": "email" | "sms" | "whatsapp" | "telegram" | "discord" | "",
        "to": "ops@acme.com",            # the pinned target - installer's to bind
        "handlers": ["a@x.com", "b@x.com"],  # v88: the rotation - attempt N rides handlers[(N-1) % len]
        "repeat_every_seconds": 3600,    # re-escalate cadence (>= 60)
        "max_repeats": 3,                # repeats AFTER the first attempt
        "message_template": "...",       # {process} {ref} {title} {state} {overdue_minutes} {attempt}
    }

    v88: an episode a human ACKNOWLEDGES goes quiet - the door holds for
    the rest of the stint (the person who said "I have this" owns it); a
    state change starts a fresh episode and the door may knock again.

    v89: two more dimensions. SNOOZE - the ack may carry snooze_hours: the
    hold is a loan, not a pardon, and when the snooze runs out the door
    RE-KNOCKS on its cadence (re-acking extends; an ack without a snooze
    still owns the rest of the stint, v88 semantics untouched). DIGEST -
    mode="digest" replaces the N knocks with ONE summary per window: the
    door lists the stuck items in a daily (digest_every_seconds) digest
    over the policy's channel instead of messaging per attempt.

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
  * mode="digest" (v89) walks a different beat: per instance the door
    only decides WHO belongs in the summary (fresh episode, not acked -
    or the snooze ran out - and under the cap); the bucket is due when
    its oldest pending item has waited digest_every_seconds, and ONE
    business.escalation_digest event + ONE channel message covers them
    all. The machine's own escalate move is not taken in digest mode
    (the summary is the nudge; a self-loop per tick would re-arm stints
    the digest bookkeeping depends on).
  * a machine WITHOUT a policy keeps the v85 semantics exactly: one
    knock per stint, on the record, no channel.

Channels stay interchangeable infrastructure: the policy names the
CHANNEL, the owner's endpoints name the providers. The clock is
injectable (now=) so tests and replay tools walk time without sleeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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

# v90: email is a long-form channel - the subject line carries the scan
# line (the body carries the detail). Other channels ignore it.
def knock_subject(*, process_name: str, ref: str, title: str, state: str,
                  attempt: int) -> str:
    return (f"[py8n] {process_name}: '{title}' (ref {ref}) past SLA "
            f"in '{state}' (attempt {attempt})")


def digest_subject(*, process_name: str, item_count: int) -> str:
    return (f"[py8n] Escalation digest - {process_name} "
            f"({item_count} item(s) past SLA)")


# v89: the digest mode - one summary per window instead of N knocks
MODES = ("knock", "digest")
MIN_DIGEST_SECONDS = 60
DEFAULT_DIGEST_SECONDS = 86400  # one summary per day, as sold
DIGEST_TEMPLATE = ("[py8n] Escalation digest - {count} item(s) past SLA on "
                   "{process}:\n{items}")
DIGEST_ITEM_TEMPLATE = ("- '{title}' (ref {ref}) in '{state}' for {overdue_minutes}m "
                        "past SLA (digest {attempt})")

_POLICY_KEYS = {"channel", "to", "handlers", "repeat_every_seconds",
                "max_repeats", "message_template", "mode", "digest_every_seconds"}
MAX_HANDLERS = 10


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
    # v88: the rotation - a roster of handlers the attempts walk through
    raw_handlers = policy.get("handlers")
    handlers: list[str] = []
    if raw_handlers not in (None, "", []):
        if not isinstance(raw_handlers, list):
            raise EscalationPolicyError("escalation_policy.handlers must be a list of targets")
        handlers = [str(h).strip() for h in raw_handlers]
        if not all(handlers):
            raise EscalationPolicyError("every handler in escalation_policy.handlers "
                                        "must be a non-empty target")
        if len(handlers) > MAX_HANDLERS:
            raise EscalationPolicyError(
                f"escalation_policy.handlers caps at {MAX_HANDLERS} - got {len(handlers)}")
    if handlers and to:
        raise EscalationPolicyError(
            "set escalation_policy.handlers (rotation) OR 'to' (pinned target), "
            "not both - the door refuses to guess which promise is real")
    # v89: the mode - knock (the default, N messages on a cadence) or
    # digest (one summary per window). The two rhythms refuse to ride
    # together - the door refuses to carry both clocks.
    mode = str(policy.get("mode") or "knock").strip().lower()
    if mode not in MODES:
        raise EscalationPolicyError(
            f"escalation mode {mode!r} is not a mode (known: {', '.join(MODES)}; "
            "knock = a message per attempt, digest = one summary per window)")
    if mode == "digest" and (policy.get("repeat_every_seconds") or "") not in \
            ("", DEFAULT_REPEAT_SECONDS):
        raise EscalationPolicyError(
            "mode='digest' sets its rhythm with digest_every_seconds, not "
            "repeat_every_seconds - the door refuses to carry both clocks")
    if mode == "knock" and (policy.get("digest_every_seconds") or "") not in \
            ("", DEFAULT_DIGEST_SECONDS):
        raise EscalationPolicyError(
            "digest_every_seconds only means something with mode='digest' - "
            "set mode='digest' or drop the key")
    repeat = DEFAULT_REPEAT_SECONDS
    if mode == "knock":
        try:
            repeat = int(policy.get("repeat_every_seconds") or DEFAULT_REPEAT_SECONDS)
        except (TypeError, ValueError):
            raise EscalationPolicyError("repeat_every_seconds must be an integer") from None
        if repeat < MIN_REPEAT_SECONDS:
            raise EscalationPolicyError(
                f"repeat_every_seconds must be >= {MIN_REPEAT_SECONDS} (the door ticks "
                "on an interval - a faster cadence cannot be honestly honored)")
    digest_every = DEFAULT_DIGEST_SECONDS
    if mode == "digest":
        try:
            digest_every = int(policy.get("digest_every_seconds") or DEFAULT_DIGEST_SECONDS)
        except (TypeError, ValueError):
            raise EscalationPolicyError("digest_every_seconds must be an integer") from None
        if digest_every < MIN_DIGEST_SECONDS:
            raise EscalationPolicyError(
                f"digest_every_seconds must be >= {MIN_DIGEST_SECONDS} (the door ticks "
                "on an interval - a faster cadence cannot be honestly honored)")
    try:
        max_repeats = int(policy.get("max_repeats") if policy.get("max_repeats") is not None
                          else DEFAULT_MAX_REPEATS)
    except (TypeError, ValueError):
        raise EscalationPolicyError("max_repeats must be an integer") from None
    if max_repeats < 0:
        raise EscalationPolicyError("max_repeats must be >= 0 (0 = escalate once, never repeat)")
    template = str(policy.get("message_template") or "").strip() or DEFAULT_TEMPLATE
    return {"channel": channel, "to": to, "handlers": handlers,
            "repeat_every_seconds": repeat, "max_repeats": max_repeats,
            "message_template": template, "mode": mode,
            "digest_every_seconds": digest_every}


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
    channel = policy["channel"] or "event-only"
    if policy.get("mode") == "digest":
        window = policy["digest_every_seconds"]
        cadence = "daily" if window == DEFAULT_DIGEST_SECONDS else f"every {window}s"
        line = (f"stuck -> {cadence} digest over {channel} "
                f"(x{1 + policy['max_repeats']})")
    else:
        cadence = f"every {policy['repeat_every_seconds']}s"
        line = f"stuck -> {channel} (x{1 + policy['max_repeats']}, {cadence})"
    if policy.get("handlers"):
        roster = " -> ".join(policy["handlers"])
        return f"{line} rotate {len(policy['handlers'])}: {roster}"
    return line + (f" -> {policy['to']}" if policy["to"] else "")


def rotation_target(policy: dict, attempt: int) -> str:
    """v88: who attempt N is delivered to - the roster round-robins by
    attempt number (the on-call rotation); a pinned 'to' ignores it."""
    handlers = policy.get("handlers") or []
    if handlers:
        return handlers[(max(1, int(attempt)) - 1) % len(handlers)]
    return policy.get("to") or ""


# ---------------------------------------------------------------------------
# the episode - bookkeeping on the instance's running memory
# ---------------------------------------------------------------------------

def episode_book(instance: BusinessProcessInstance) -> dict:
    """The door's episode bookkeeping on the instance's running memory
    (context.escalations) - {} when the door has never knocked here."""
    book = (instance.context or {}).get("escalations")
    return book if isinstance(book, dict) else {}


def episode_gate(instance: BusinessProcessInstance, policy: dict,
                 now: datetime) -> dict:
    """Decide what the door does for a policy-carrying machine's stuck
    instance RIGHT NOW: escalate (with the attempt number) or hold.

    An episode belongs to ONE stuck stint: the bookkeeping remembers the
    state it started in - the instance moving states starts fresh. An
    ACKNOWLEDGED episode goes quiet (v88 - the human who said "I have
    this" owns it). v89: an ack that carried snooze_hours holds only
    until the snooze runs out - the door RE-KNOCKS on its cadence, the
    attempt count continuing inside the same episode (and its cap).
    Past 1 + max_repeats the episode is complete; before
    repeat_every_seconds has elapsed since the last attempt the door
    holds (too_soon)."""
    now = _aware(now) or _now()
    book = episode_book(instance)
    fresh = book.get("state") != instance.state
    count = 0 if fresh else int(book.get("count") or 0)
    # the human's acknowledgement outranks the cadence and the cap -
    # until the snooze runs out (v89: the hold is a loan, not a pardon)
    if not fresh and isinstance(book.get("acked"), dict):
        acked = book["acked"]
        until = _parse_iso(acked.get("snooze_until"))
        if until is None or now < until:
            hold = {"action": "hold", "reason": "acknowledged",
                    "attempts": count,
                    "acked_by": str(acked.get("by") or "")}
            if until is not None:
                hold["snooze_until"] = until.isoformat()
                hold["snooze_remaining_seconds"] = round(
                    (until - now).total_seconds())
            return hold
        # the snooze ran out - fall through: the door re-knocks
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
    discipline - the JSON column is never mutated in place). An ack that
    already landed in THIS episode rides along (the door holds acked
    episodes anyway - this is race-safety, not a second opinion)."""
    now = _aware(now) or _now()
    book = episode_book(instance)
    acked = (book.get("acked")
             if book.get("state") == instance.state
             and isinstance(book.get("acked"), dict) else None)
    entry = {"count": attempt, "last_at": now.isoformat(),
             "state": instance.state,
             "last_delivery": delivery.get("delivery", ""),
             "last_detail": (delivery.get("detail") or "")[:300]}
    if acked:
        entry["acked"] = acked
    new_ctx = dict(instance.context or {})
    new_ctx["escalations"] = entry
    instance.context = new_ctx
    db.add(instance)


def record_ack(db: AsyncSession, instance: BusinessProcessInstance,
               *, by: str, note: str = "",
               snooze_hours: float | None = None,
               now: datetime | None = None) -> dict:
    """v88: write the acknowledgement onto the episode's bookkeeping -
    the door reads it through episode_gate and holds the episode. The
    rest of the book (count, last delivery) rides along untouched.

    v89: snooze_hours turns the hold into a LOAN - the ack carries a
    snooze_until stamp and the door re-knocks once it runs out (an ack
    without one still owns the rest of the stint)."""
    now = _aware(now) or _now()
    ack = {"by": (by or "").strip()[:140], "at": now.isoformat(),
           "note": (note or "").strip()[:500]}
    if snooze_hours is not None:
        hours = max(0.0, float(snooze_hours))
        ack["snooze_hours"] = round(hours, 4)
        ack["snooze_until"] = (now + timedelta(hours=hours)).isoformat()
    book = episode_book(instance)
    entry = {"count": int(book.get("count") or 0),
             "last_at": book.get("last_at"),
             "state": instance.state,
             "last_delivery": book.get("last_delivery", ""),
             "last_detail": book.get("last_detail", ""),
             "acked": ack}
    if entry["last_at"] is None:
        entry.pop("last_at")
    new_ctx = dict(instance.context or {})
    new_ctx["escalations"] = entry
    instance.context = new_ctx
    db.add(instance)
    return ack


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
    thread carrying the delivery result (an honest skip IS a result).

    v88: the target comes from the rotation when the policy carries a
    handlers roster (attempt N -> handlers[(N-1) % len]) or the pinned
    'to' otherwise; the event payload names WHO attempt N went to."""
    from . import system_events as events_svc

    now = _aware(now) or _now()
    overdue_minutes = max(0, overdue_seconds // 60)
    target = rotation_target(policy, attempt)
    base = {"process_id": instance.process_id, "process_name": process_name,
            "instance_id": instance.id, "ref": instance.ref,
            "title": instance.title, "state": instance.state,
            "overdue_seconds": overdue_seconds,
            "overdue_minutes": overdue_minutes, "attempt": attempt,
            "max_repeats": policy["max_repeats"], "moved_to": moved_to,
            "to": target or None,
            "rotated": bool(policy.get("handlers"))}
    if not policy["channel"]:
        delivery = {"delivery": "skipped",
                    "detail": "policy is event-only (no channel configured) - "
                              "the escalation is on the event timeline"}
    elif not target:
        delivery = {"delivery": "skipped",
                    "detail": f"no target configured - bind escalation_policy.to "
                              f"(or a handlers roster) to deliver over {policy['channel']}"}
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
            result = await cep_svc.deliver_outbound(
                endpoint, target, text,
                subject=knock_subject(process_name=process_name, ref=instance.ref,
                                      title=instance.title, state=instance.state,
                                      attempt=attempt))
            delivery = {"delivery": result.get("delivery", "failed"),
                        "detail": result.get("detail", ""),
                        "endpoint": endpoint.name, "provider": endpoint.provider,
                        "subject": (result.get("request") or {}).get("subject")}
    await events_svc.emit(
        db, instance.owner_id, "business.escalated", source="business",
        actor=actor, target_type="process_instance", target_id=instance.id,
        payload={**base, "channel": policy["channel"] or None,
                 **delivery},
        correlation_id=instance.id)
    return delivery


def _parse_iso(value) -> datetime | None:
    return parse_iso(value)


def parse_iso(value) -> datetime | None:
    """An isoformat stamp (naive ones normalized to UTC) or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# the digest (v89) - one summary per window instead of N knocks
# ---------------------------------------------------------------------------

def digest_gate(instance: BusinessProcessInstance, policy: dict,
                now: datetime) -> dict:
    """Digest mode's per-instance gate: not a knock decision - a question
    of WHO belongs in the summary.

    * hold (acknowledged): the human's take holds, snooze and all (the
      same loan semantics as knock mode - an expired snooze falls back
      into the bucket);
    * hold (episode_complete): the item has appeared in 1 + max_repeats
      digests - the team was told, repeatedly;
    * candidate otherwise, with ``pending_since`` - the moment the
      current episode first showed up (first_at) or the last digest that
      listed it (digest_last_at). The bucket is due when its OLDEST
      pending_since has waited digest_every_seconds; a fresh candidate
      (``fresh``) must have its book written NOW or the window would
      restart every tick and never elapse.

    v91: ``fresh`` is about the digest CLOCK - an item that arrived from
    knock mode mid-episode (a live policy switch) has a book without
    first_at, so it joins as fresh: the door anchors its book (the
    window starts when the digest rhythm starts; the cap counts digest
    appearances) with ``anchor_only`` naming the fact that the breach
    itself was already announced by the knock episode."""
    now = _aware(now) or _now()
    book = episode_book(instance)
    state_fresh = book.get("state") != instance.state
    count = 0 if state_fresh else int(book.get("count") or 0)
    # the human's acknowledgement outranks the cadence and the cap -
    # until the snooze runs out (v89: the hold is a loan, not a pardon).
    # v91: it survives a mid-episode knock -> digest switch too (the
    # receipt rides the same episode - the switch does not un-take it).
    if not state_fresh and isinstance(book.get("acked"), dict):
        acked = book["acked"]
        until = _parse_iso(acked.get("snooze_until"))
        if until is None or now < until:
            hold = {"action": "hold", "reason": "acknowledged",
                    "attempts": count,
                    "acked_by": str(acked.get("by") or "")}
            if until is not None:
                hold["snooze_until"] = until.isoformat()
                hold["snooze_remaining_seconds"] = round(
                    (until - now).total_seconds())
            return hold
        # the snooze ran out - the item rides the next digest
    # v91: ``fresh`` is about the DIGEST clock, not just the state - the
    # anchor the window rides on is first_at, and an item switching from
    # knock mode mid-episode has a book WITHOUT one (its knocks kept
    # last_at). Treating it as fresh means the door anchors its book at
    # the switch (the window starts when the digest rhythm starts) and
    # the digest cap counts digest appearances - without this the
    # pending_since re-anchored to now on every tick and the window
    # NEVER elapsed. ``anchor_only`` says the breach was already
    # announced (the knock episode's business.stuck) - only the clock is
    # new, not the fact.
    fresh = state_fresh or book.get("first_at") is None
    anchor_only = fresh and not state_fresh
    count = 0 if fresh else count
    if count >= 1 + policy["max_repeats"]:
        return {"action": "hold", "reason": "episode_complete", "attempts": count}
    pending_since = (None if fresh
                     else _parse_iso(book.get("digest_last_at"))
                     or _parse_iso(book.get("first_at")))
    return {"action": "candidate", "attempt": count + 1, "fresh": fresh,
            **({"anchor_only": True} if anchor_only else {}),
            "pending_since": (pending_since or now).isoformat(),
            "waited_seconds": round((now - (pending_since or now)).total_seconds())}


def record_digest_book(db: AsyncSession, instance: BusinessProcessInstance,
                       *, count: int, now: datetime,
                       first_at: datetime | None = None,
                       digest_last_at: datetime | None = None,
                       delivery: dict | None = None) -> None:
    """The digest bookkeeping on the instance's running memory (fresh-dict
    discipline). first_at anchors the episode's pending-since; digest_last_at
    is stamped when a digest actually lists the item; an ack rides along."""
    now = _aware(now) or _now()
    book = episode_book(instance)
    entry = {"count": int(count),
             "state": instance.state,
             "first_at": (first_at or now).isoformat()}
    if digest_last_at is not None:
        entry["digest_last_at"] = digest_last_at.isoformat()
    elif book.get("state") == instance.state and book.get("digest_last_at"):
        entry["digest_last_at"] = book["digest_last_at"]
    entry["last_delivery"] = (delivery or {}).get("delivery",
                                                  book.get("last_delivery", ""))
    entry["last_detail"] = ((delivery or {}).get("detail",
                            book.get("last_detail", "")))[:300]
    if book.get("state") == instance.state and isinstance(book.get("acked"), dict):
        entry["acked"] = book["acked"]
    new_ctx = dict(instance.context or {})
    new_ctx["escalations"] = entry
    instance.context = new_ctx
    db.add(instance)


def render_digest(*, process_name: str, items: list[dict]) -> str:
    """The one summary that replaces the N knocks - a line per stuck item."""
    body = "\n".join(DIGEST_ITEM_TEMPLATE.format(**it) for it in items)
    try:
        return DIGEST_TEMPLATE.format(count=len(items), process=process_name,
                                      items=body)
    except Exception:  # noqa: BLE001 - a broken render must not stop the door
        return (f"[py8n] Escalation digest - {len(items)} item(s) past SLA on "
                f"{process_name}.")


async def deliver_digest(db: AsyncSession, *, owner_id: str | None,
                         policy: dict, process_id: str, process_name: str,
                         items: list[dict], actor: str = "scheduler",
                         now: datetime | None = None) -> dict:
    """Send ONE digest message over the policy's channel for the bucket
    and put it on the record: a single ``business.escalation_digest``
    event carrying every listed item (the bucket is the journey here -
    the digest's whole point is not being per-entity noise). The delivery
    skips honestly (no channel / no target / no endpoint), and the skip
    IS the result - named in the payload, never silent."""
    from . import system_events as events_svc

    now = _aware(now) or _now()
    # the roster advances with the deepest attempt in the bucket
    target = rotation_target(policy, max((it.get("attempt") or 1) for it in items))
    base = {"process_id": process_id, "process_name": process_name,
            "mode": "digest", "window_seconds": policy["digest_every_seconds"],
            "items": items, "item_count": len(items),
            "to": target or None, "rotated": bool(policy.get("handlers"))}
    if not policy["channel"]:
        delivery = {"delivery": "skipped",
                    "detail": "policy is event-only (no channel configured) - "
                              "the digest is on the event timeline"}
    elif not target:
        delivery = {"delivery": "skipped",
                    "detail": f"no target configured - bind escalation_policy.to "
                              f"(or a handlers roster) to deliver over {policy['channel']}"}
    else:
        endpoint = await _resolve_endpoint(db, owner_id, policy["channel"])
        if endpoint is None:
            delivery = {"delivery": "skipped",
                        "detail": f"no {policy['channel']} channel endpoint bound - "
                                  "the digest was rendered but not delivered"}
        else:
            from . import channel_endpoints as cep_svc

            text = render_digest(process_name=process_name, items=items)
            result = await cep_svc.deliver_outbound(
                endpoint, target, text,
                subject=digest_subject(process_name=process_name,
                                       item_count=len(items)))
            delivery = {"delivery": result.get("delivery", "failed"),
                        "detail": result.get("detail", ""),
                        "endpoint": endpoint.name, "provider": endpoint.provider,
                        "subject": (result.get("request") or {}).get("subject")}
    await events_svc.emit(
        db, owner_id, "business.escalation_digest", source="business",
        actor=actor, target_type="process", target_id=process_id,
        payload={**base, "channel": policy["channel"] or None, **delivery})
    return {**delivery, "to": target or None}
