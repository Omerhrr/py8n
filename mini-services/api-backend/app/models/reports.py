"""Model classes: ScheduledReport, ReportDeliveryEvent, ChainReportSchedule.

Split from the original app/models.py (task #3) - moved verbatim, no
behavior change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ._shared import Base, JSONVariant, _now, _uuid

class ScheduledReport(Base):
    """Scheduled export job (v48) - snapshot a dataset or dashboard on a cron.

    When the APScheduler job fires, the service serializes the source
    (dataset -> csv/xlsx/json/parquet, dashboard -> a JSON snapshot of every
    rendered component) and stores it as a regular Artifact; the report row
    keeps the last artifact id so the UI can deep-link the download.

    Cron-only by design: a report export is a time-of-day concern, and a
    single crontab string validates + previews with one code path.
    """

    __tablename__ = "scheduled_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # dataset | dashboard
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default="dataset")
    source_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # dataset exports: csv | xlsx | json | parquet; dashboard exports: json
    fmt: Mapped[str] = mapped_column(String(10), nullable=False, default="csv")
    cron: Mapped[str] = mapped_column(String(100), nullable=False, default="0 6 * * *")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # v52: outbound delivery channels evaluated after every successful run.
    # {"channels": [{"type": "webhook", "url", "headers", "include_attachment"},
    #               {"type": "email", "to", "cc", "subject", "include_attachment"}]}
    # NULL/{} = artifact-only (pre-v52 behaviour, nothing leaves the instance).
    delivery_json: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fire_count: Mapped[int] = mapped_column(Integer, default=0)
    last_status: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ok|error
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ReportDeliveryEvent(Base):
    """Scheduled-report delivery log (v52) - did the push-out succeed?

    One row per (report run x delivery channel): a webhook POST or an
    SMTP send attempted after a report run produced its artifact. The
    log answers "did the 6am email actually go out?" without grepping
    server logs - including the negative space (SMTP not configured,
    oversized attachment skipped, webhook answered 500).

    Capped at the newest REPORT_DELIVERY_CAP events per report (trimmed
    on insert) and never written before the artifact exists - a delivery
    failure NEVER fails the report run it belongs to.
    """

    __tablename__ = "report_delivery_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    report_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # webhook | email
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    target: Mapped[str] = mapped_column(String(300), default="")  # url or comma-joined recipients
    # ok | error | skipped
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ok")
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attached: Mapped[bool] = mapped_column(Boolean, default=False)  # file went inline/attached
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class ChainReportSchedule(Base):
    """The chain-history file on a cadence (v99) - one per owner.

    The digest pattern applied to the FILE: the escalation door's sweep
    already walks every owner's machines on a cadence; when a chain
    report is due it renders the SAME per-leg chain history the
    operator-detail chain draws (chain_history_csv, zero drift) and
    dispatches it over the owner's bound EMAIL endpoint as a real
    MIME attachment - the subject carries the scan line
    ("[py8n] Chain history - N ride(s) across M leg(s)") the way the
    digest's subject names its shape.

    One row per owner (unique): the report is an ESTATE concern - the
    whole chain map, a LIST of chains (v101: ``chains`` is the tag list -
    comma/semicolon-separated like the recipients, normalized and
    ceilinged, the file covers every chain it names; the legacy single
    ``chain`` stays truthful for one-name scopes) or one SYSTEM's slice
    (v100: ``system`` names it, the same filter the systems page's
    per-system export sends - the report breaks down by the systems the
    machines actually bind). The named rhythm (v101: hourly | daily |
    weekly in ``cadence_seconds``) paces the beat - the WEEKLY digest
    rides the SAME envelope path to the report's OWN list, only slower.
    The report rides to ONE envelope with
    EVERY name on it (v100: ``to`` carries the recipient LIST,
    comma/semicolon-separated, normalized and ceilinged - one SMTP
    conversation, one attachment, all of the names). The schedule stamps
    its own bookkeeping: last_sent_at / next_due (advanced on EVERY due
    attempt - an honest skip consumes the window, the same way the
    digest's window elapses when the bucket stays empty) and
    last_result (the delivery record: delivered | skipped | failed with
    the detail and the counts - "did the file actually go out?" without
    grepping logs).
    """

    __tablename__ = "chain_report_schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True,
                                                 unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # the minimum honest cadence: a file dispatch is a minutes concern,
    # not a seconds one (the door's own tick defaults to 300s)
    cadence_seconds: Mapped[int] = mapped_column(Integer, default=86400)
    # v100: the recipient LIST - comma/semicolon-separated, normalized on
    # save (whitespace stripped, duplicates collapsed, ceiling enforced);
    # one envelope carries every name
    to: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    history_limit: Mapped[int] = mapped_column(Integer, default=50)
    # optional single-chain filter ("" = the whole estate map) - kept
    # truthful for one-name scopes (v99/v100 readers); the v101 TAG LIST
    # below is the scope's real home
    chain: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # v101: the chain TAG LIST - comma-joined, parsed like the recipient
    # list (strip, dedupe case-insensitively, ceiling loud); the report
    # covers every chain the list names, "" = the whole estate map
    chains: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # v100: optional single-system scope ("" = every system) - the report
    # covers only the machines the named system binds
    system: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                          nullable=True)
    next_due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                      nullable=True)
    last_result: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True,
                                                     default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

