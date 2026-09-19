"""The dispatch (v112) - a patrol's findings walk OUT of py8n.

v111 made the harness proactive: patrols fire rounds on their own rhythm.
But the findings died in the transcript - the human had to come LOOK.
The estate has owned a delivery discipline since v52 (email over the
env-configured SMTP, skipped while the host is unset, every attempt
stamped, a failure that can never fail the run). v112 wires the patrol
round's outcome through THE SAME discipline: a patrol with ``dispatch_to``
mails its answer to the humans when the round ends - and a round that
died mails that too. The estate watches itself AND writes home.

The rules (inherited, not invented):

* ONE trigger - the receipt reaching a TERMINAL state. ``completed``
  mails the answer; ``exhausted`` / ``failed`` / ``refused`` mail why.
  ``waiting_approval`` and ``held`` never dispatch (a pause is not a
  finding) - the gated round's mail goes out when the decision lands.
* THE SAME email path - ``reports._send_report_email``, the function the
  report envelope uses. Zero parallel mail machinery.
* NEVER RAISES - a broken SMTP is a receipt line on the patrol row
  (last_dispatch_status/detail), never a wedge in the round, the sweep,
  or the decision path.
* QUIET BY CONFIG - no recipients, no dispatch, not even a receipt line.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...models import HarnessPatrol, HarnessTurn
from .. import reports as report_svc

logger = logging.getLogger("py8n.harness.dispatch")

# the round states that mail home - a pause (waiting_approval) and a
# hold (held) are not findings, so they stay silent until they end
TERMINAL = ("completed", "exhausted", "failed", "refused")

MAX_DIGEST_CHARS = 4000  # the answer travels whole up to this wall


def validate_recipients(raw: Any) -> str:
    """Normalize 'a@b.c, d@e.f' (or a list) into a clean comma-joined
    string; raises ValueError with a user-safe message otherwise. The
    SAME address discipline the report envelope enforces."""
    addresses = report_svc._as_addresses(raw if raw not in (None, "") else "", "dispatch")
    return ", ".join(addresses)


def _stamp(patrol: HarnessPatrol, status: str, detail: str) -> None:
    patrol.last_dispatch_status = status
    patrol.last_dispatch_detail = detail
    patrol.last_dispatch_at = datetime.now(timezone.utc)


def _clip(text: str) -> str:
    text = (text or "").strip()
    if len(text) > MAX_DIGEST_CHARS:
        return text[:MAX_DIGEST_CHARS] + "\n... (clipped)"
    return text


def _digest(patrol: HarnessPatrol, turn: HarnessTurn | None, *,
            error_text: str = "", handshake: bool = False) -> tuple[str, str]:
    """The digest: (subject, body). One shape for every outcome."""
    if handshake:
        subject = f"[py8n patrol] {patrol.name} - handshake"
        body = (
            f"Patrol '{patrol.name}' wired up dispatch.\n\n"
            f"Mission: {patrol.mission}\n"
            "This is a handshake, not a finding - the next round's "
            "outcome arrives the same way.\n"
        )
        return subject, body

    status = turn.status if turn is not None else "failed"
    subject = f"[py8n patrol] {patrol.name} - {status}"
    when = (patrol.last_run_at or datetime.now(timezone.utc))
    body = [
        f"Patrol '{patrol.name}' finished a round.",
        "",
        f"Status: {status}",
        f"Mission: {patrol.mission}",
        f"Round: #{patrol.run_count or 0} at {when.isoformat()}",
    ]
    if turn is not None:
        if (turn.reply or "").strip():
            body += ["", "The round's answer:", _clip(turn.reply)]
        if (turn.error or "").strip():
            body += ["", "Why it ended:", _clip(turn.error)]
        body += ["", f"Turn {turn.id} - the full trace is one hop away on the harness console."]
    else:
        body += ["", "The round never became a turn:", _clip(error_text or patrol.last_error)]
    return subject, "\n".join(body)


async def dispatch_round(db: AsyncSession, patrol: HarnessPatrol,
                         turn: HarnessTurn | None = None, *,
                         error_text: str = "", handshake: bool = False) -> None:
    """Mail one round's outcome to ``patrol.dispatch_to``. NEVER raises:
    a broken channel degrades into the dispatch receipt columns and even
    the receipt write is failure-isolated - dispatch can never flip the
    round's own status or wedge the sweep."""
    try:
        raw = (patrol.dispatch_to or "").strip()
        if not raw:
            return  # quiet by config - not even a receipt line
        try:
            addresses = report_svc._as_addresses(raw, "dispatch")
        except ValueError as exc:
            _stamp(patrol, "error", f"undeliverable recipients: {exc}")
            await db.commit()
            return

        subject, body = _digest(patrol, turn, error_text=error_text,
                                handshake=handshake)
        if not settings.smtp_host:
            _stamp(patrol, "skipped", "SMTP not configured (set PY8N_SMTP_HOST)")
            await db.commit()
            return
        try:
            await asyncio.to_thread(
                report_svc._send_report_email,
                to=addresses, cc=[], subject=subject, body=body,
                data=b"", filename="", content_type="", attach=False)
            _stamp(patrol, "ok", f"sent to {len(addresses)} recipient(s)")
        except Exception as exc:  # noqa: BLE001 - SMTP errors are receipts
            _stamp(patrol, "error", f"{exc.__class__.__name__}: {exc}"[:280])
        await db.commit()
    except Exception:  # noqa: BLE001 - the mail can never break the round
        logger.exception("patrol dispatch failed for %s", patrol.id)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
