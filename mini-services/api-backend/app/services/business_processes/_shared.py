"""Shared prelude for the business_processes package (task #3 split from a
single 2714-line app/services/business_processes.py) - the error type, the
time helper, and every module-level constant, so every submodule can import
them without caring which submodule "owns" them originally. Moved verbatim,
no behavior change.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProcessError(ValueError):
    """Honest business-process failures."""


SIDE_LEGS_CHAIN = "side legs"


CHAIN_REPORT_MIN_CADENCE = 300


PAPERWORK_TRANSITIONS = frozenset({"annotate", "escalation_acknowledged",
                                   "escalation_digest"})


ANNOTATE_TRANSITION = "annotate"


ACK_TRANSITION = "escalation_acknowledged"


DIGEST_TRANSITION = "escalation_digest"


RESERVED_CONTEXT_KEYS = frozenset({"escalations"})


ESCALATION_TRANSITION = "escalate"   # the move a machine may define for the door


ESCALATION_HISTORY_TRANSITIONS = ("escalate", "escalated", ACK_TRANSITION,
                                  DIGEST_TRANSITION)


CHAIN_NAMES: dict[tuple[str, str], str] = {
    ("Lead pipeline", "won"): "Revenue chain",
    ("Purchase lifecycle", "ordered"): "Supply chain",
    ("Appointment journey", "billed"): "Care chain",
}


CHAIN_CSV_HEADER = [
    "chain", "leg_from", "leg_on_state", "leg_to", "leg_sla_seconds",
    "leg_opened", "leg_open_now", "leg_stuck", "history_truncated",
    "ref", "title", "state", "opened_at", "due_at", "is_stuck",
    "overdue_seconds", "acked_by", "snooze_remaining_seconds",
    "instance_id", "process_id",
    # v100: the systems the firing machine binds, "|"-joined - the
    # spreadsheet's own breakdown-by-system dimension on every row
    "system",
]


CHAIN_REPORT_MAX_RECIPIENTS = 8


CHAIN_REPORT_MAX_CHAINS = 8


CHAIN_REPORT_CADENCES: dict[str, int] = {
    "hourly": 3600,
    "daily": 86400,
    "weekly": 7 * 86400,
}

