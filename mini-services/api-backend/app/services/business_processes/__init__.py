"""Business processes (v84) - long-running autonomy: the business state
machine.

Task #3: split from a single 2714-line app/services/business_processes.py
into this package - moved verbatim into submodules grouped by topic
(definitions, process_crud, instances, escalate_stuck, chains, ...), no
behavior change. This __init__ re-exports every name (including the
underscore-prefixed helpers several call sites import directly, e.g.
`from .business_processes import _terminal_states`) so every existing
`from ..services.business_processes import X` / `from . import
business_processes as bp_svc` / `bp_svc.<anything>` keeps working
completely unchanged.

Workflows are MOMENTS (trigger in, run, done). Businesses run on things
that stay open for days or months: a lead moving lead -> contacted ->
interested -> demo -> proposal -> negotiating -> won, a support case, an
insurance claim, an appointment, a delivery, a loan application. A
BusinessProcess is the MACHINE (states + named transitions); instances
are the tracked entities that REMEMBER state and context across weeks;
every advance is on the record (the transition log) and emits
business.state_changed through the v80 event door - so workflows and
agents react to the business moving ("proposal_sent -> send the
follow-up"; "stuck > SLA -> escalate"). Business state machine + agents +
workflows + data + interactions = long-running autonomy.
"""

from __future__ import annotations

from ...models import ChainReportSchedule
from .. import escalations as escalations_svc  # v87: the channel + repeat policy layer
from ._shared import (ACK_TRANSITION, ANNOTATE_TRANSITION, CHAIN_CSV_HEADER,
                      CHAIN_NAMES, CHAIN_REPORT_CADENCES, CHAIN_REPORT_MAX_CHAINS,
                      CHAIN_REPORT_MAX_RECIPIENTS, CHAIN_REPORT_MIN_CADENCE,
                      DIGEST_TRANSITION, ESCALATION_HISTORY_TRANSITIONS,
                      ESCALATION_TRANSITION, PAPERWORK_TRANSITIONS,
                      ProcessError, RESERVED_CONTEXT_KEYS, SIDE_LEGS_CHAIN, _now)
from .definitions import validate_definition, _terminal_states, _allowed_from
from .process_crud import process_out, create_process, _load_process, list_processes, get_process, _policy_diff_keys, update_escalation_policy, _instance_counts
from .instances import _aware, instance_out, _log_out, _load_instance, start_instance, advance_instance, instance_journey, _resolve_process_ref, _render_journey_template, fire_journeys, get_instance, list_instances, query_instances
from .escalate_stuck import _systems_holding, _escalated_this_stint, escalate_stuck
from .chains import _stuck_state, _ack_summary, _resolve_leg_target, chain_map, _chain_csv_rows, chain_history_csv
from .annotations import annotate_instance
from .acknowledgements import acknowledge_escalation
from .analytics import process_analytics
from .attention import attention_feed, system_work_surface
from .escalation_preview import escalation_preview
from .chain_reports import parse_report_recipients, parse_report_chains, _chain_report_out, chain_report_subject, get_chain_report, upsert_chain_report, delete_chain_report, dispatch_chain_report, dispatch_due_chain_reports, send_chain_report_now, escalation_history_grid, escalation_day_detail
