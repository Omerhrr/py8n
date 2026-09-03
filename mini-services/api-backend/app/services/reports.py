"""Scheduled report exports (v48 + v52 delivery) - cron-driven snapshots.

A ScheduledReport points at one source (a dataset or a dashboard) plus an
export format and a crontab. When APScheduler fires the job, the service
serializes the source through the SAME code paths the download endpoints
use (``datasets.export_dataset_bytes`` / ``dashboards.compute_config``) and
stores the bytes as a regular Artifact. The report row remembers the last
artifact so the Reports page can deep-link the freshest download.

v52 adds DELIVERY: a report can carry outbound channels (webhook POST of
a JSON envelope, optionally with the file base64-inline; email over SMTP
with the report attached). Channels fire after every successful run, each
attempt lands a ReportDeliveryEvent (ok | error | skipped, capped per
report), and a delivery failure can never fail the report run itself.
SMTP is env-configured (PY8N_SMTP_*) and disabled while the host is empty.

Design contracts:

- The job callback NEVER raises: a broken source or a failed write only
  lands on the report row (last_status/last_error) - a bad export can
  never take the scheduler down.
- Owner-scoped: rows are listed/created for the caller; the scheduler job
  runs system-side and stamps artifact meta with the report identity.
- Cron-only: report exports are time-of-day concerns; one crontab string
  validates and previews with a single code path (APScheduler CronTrigger).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timezone

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Dataset, ReportDeliveryEvent, ScheduledReport

logger = logging.getLogger("py8n.reports")

DATASET_FORMATS = ("csv", "xlsx", "json", "parquet")
# v49: dashboards grow "png" - a server-rendered image of every component
# (services/report_images.py), produced from the SAME compute_config output
# as the JSON snapshot.
DASHBOARD_FORMATS = ("json", "png")

REPORT_DELIVERY_CAP = 200  # newest delivery events kept per report


def validate_cron(cron: str) -> str:
    """Raise ValueError when the crontab is malformed."""
    cleaned = (cron or "").strip()
    if not cleaned:
        raise ValueError("A cron expression is required (e.g. '0 6 * * *')")
    try:
        CronTrigger.from_crontab(cleaned, timezone="UTC")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid cron expression {cleaned!r}: {exc}") from exc
    return cleaned


# ----------------------------------------------------------------- delivery (v52)
MAX_DELIVERY_CHANNELS = 4


def _as_addresses(raw, field: str) -> list[str]:
    """Normalize 'a@b.c, d@e.f' or ['a@b.c', 'd@e.f'] into a clean list."""
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, list):
        parts = [str(x) for x in raw]
    else:
        raise ValueError(f"email delivery '{field}' must be a string or a list of addresses")
    out: list[str] = []
    for part in parts:
        addr = part.strip()
        if not addr:
            continue
        if " " in addr or "@" not in addr:
            raise ValueError(f"email delivery '{field}' contains an invalid address: {addr!r}")
        if addr not in out:
            out.append(addr)
    return out


def validate_delivery(delivery: dict | list | None) -> dict | None:
    """Normalize + validate the delivery config; None = artifact-only.

    Raises ValueError with a user-safe message on any malformed channel.
    Normalized shape (also what gets STORED)::

        {"channels": [
            {"type": "webhook", "url", "headers", "include_attachment"},
            {"type": "email", "to", "cc", "subject", "include_attachment"},
        ]}
    """
    if delivery is None or delivery == {} or delivery == []:
        return None
    if not isinstance(delivery, dict):
        raise ValueError("delivery must be an object like {\"channels\": [...]}")
    channels = delivery.get("channels", [])
    if channels in (None, []):
        return None
    if not isinstance(channels, list):
        raise ValueError("delivery.channels must be a list")
    if len(channels) > MAX_DELIVERY_CHANNELS:
        raise ValueError(f"at most {MAX_DELIVERY_CHANNELS} delivery channels are allowed")

    norm: list[dict] = []
    for ch in channels:
        if not isinstance(ch, dict):
            raise ValueError("each delivery channel must be an object")
        ctype = (ch.get("type") or "").strip().lower()
        if ctype == "webhook":
            url = (ch.get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                raise ValueError("webhook delivery needs a http(s) url")
            headers = ch.get("headers") or {}
            if not isinstance(headers, dict):
                raise ValueError("webhook delivery 'headers' must be an object")
            clean_headers = {str(k): str(v) for k, v in headers.items()}
            norm.append({
                "type": "webhook",
                "url": url,
                "headers": clean_headers,
                "include_attachment": bool(ch.get("include_attachment", False)),
            })
        elif ctype == "email":
            to = _as_addresses(ch.get("to") or "", "to")
            if not to:
                raise ValueError("email delivery needs at least one 'to' address")
            cc = _as_addresses(ch.get("cc") or [], "cc")
            subject = (ch.get("subject") or "").strip()
            if len(subject) > 200:
                raise ValueError("email delivery 'subject' must be 200 characters or fewer")
            norm.append({
                "type": "email",
                "to": to,
                "cc": cc,
                "subject": subject,
                "include_attachment": bool(ch.get("include_attachment", True)),
            })
        else:
            raise ValueError(f"unknown delivery channel type {ctype!r} (use webhook or email)")
    return {"channels": norm}


def delivery_out(delivery_json: dict | None) -> dict:
    """API view of the delivery config (never raises on legacy rows)."""
    try:
        normalized = validate_delivery(delivery_json) or {"channels": []}
    except ValueError:
        return {"channels": []}
    return normalized


def _oversize(data: bytes) -> bool:
    return len(data) > settings.max_delivery_attachment_bytes


def _webhook_payload(
    row: ScheduledReport,
    *,
    source_name: str | None,
    artifact_id: str,
    filename: str,
    content_type: str,
    data: bytes,
    attach: bool,
) -> dict:
    payload: dict = {
        "event": "py8n.report.completed",
        "report": {"id": row.id, "name": row.name, "cron": row.cron},
        "source": {"type": row.source_type, "id": row.source_id, "name": source_name},
        "fmt": row.fmt,
        "artifact": {
            "id": artifact_id,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(data),
        },
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    if attach:
        payload["artifact"]["data_base64"] = base64.b64encode(data).decode("ascii")
    return payload


async def _post_webhook(url: str, headers: dict, payload: dict, timeout: int) -> tuple[bool, str]:
    """POST the envelope; returns (ok, detail). Never raises."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.post(url, json=payload, headers=headers)
        ok = 200 <= resp.status_code < 300
        return ok, f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        return False, f"{exc.__class__.__name__}: {exc}"[:280]
    except Exception as exc:  # noqa: BLE001 - a broken channel must not break the run
        return False, f"{exc.__class__.__name__}: {exc}"[:280]


def _send_report_email(
    *,
    to: list[str],
    cc: list[str],
    subject: str,
    body: str,
    data: bytes,
    filename: str,
    content_type: str,
    attach: bool,
) -> None:
    """Blocking SMTP send (run through asyncio.to_thread)."""
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = settings.smtp_from or "py8n@localhost"
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.set_content(body)
    if attach:
        maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
        msg.add_attachment(
            data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=filename,
        )
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        if settings.smtp_use_tls:
            server.ehlo()
            server.starttls()
            server.ehlo()
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(msg)


async def deliver_report(
    session: AsyncSession,
    row: ScheduledReport,
    *,
    data: bytes,
    filename: str,
    content_type: str,
    artifact_id: str,
    source_name: str | None,
) -> list[dict]:
    """Fire every configured delivery channel after a SUCCESSFUL run.

    Never raises: each channel degrades into an event row (ok | error |
    skipped), and even the event writes are failure-isolated. Returns the
    per-channel results for the run summary.
    """
    try:
        normalized = validate_delivery(row.delivery_json)
    except ValueError:
        normalized = None
    channels = (normalized or {}).get("channels") or []
    results: list[dict] = []

    for ch in channels:
        if ch["type"] == "webhook":
            attach = bool(ch.get("include_attachment")) and not _oversize(data)
            payload = _webhook_payload(
                row,
                source_name=source_name,
                artifact_id=artifact_id,
                filename=filename,
                content_type=content_type,
                data=data,
                attach=attach,
            )
            headers = {"X-Py8n-Event": "report.completed", **ch.get("headers", {})}
            ok, detail = await _post_webhook(
                ch["url"], headers, payload, settings.webhook_delivery_timeout_seconds
            )
            if ch.get("include_attachment") and _oversize(data):
                detail += " (attachment omitted: over PY8N_MAX_DELIVERY_ATTACHMENT_BYTES)"
            results.append({
                "channel": "webhook",
                "target": ch["url"],
                "status": "ok" if ok else "error",
                "detail": detail,
                "attached": attach,
            })
        else:  # email
            target = ", ".join(ch["to"] + ch.get("cc", []))
            if not settings.smtp_host:
                results.append({
                    "channel": "email",
                    "target": target,
                    "status": "skipped",
                    "detail": "SMTP not configured (set PY8N_SMTP_HOST)",
                    "attached": False,
                })
                continue
            attach = bool(ch.get("include_attachment", True)) and not _oversize(data)
            subject = ch.get("subject") or (
                f"[py8n] {row.name} - {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
            )
            body = (
                f"Report '{row.name}' finished successfully.\n\n"
                f"Source: {row.source_type} '{source_name or row.source_id}'\n"
                f"Format: {row.fmt}\n"
                f"Artifact: {filename} ({len(data)} bytes) - id {artifact_id}\n"
                + (
                    f"Attachment: {filename}\n"
                    if attach
                    else "Attachment: omitted (disabled or oversized)\n"
                )
                + "\nDownload the artifact any time from py8n > Reports."
            )
            try:
                await asyncio.to_thread(
                    _send_report_email,
                    to=ch["to"],
                    cc=ch.get("cc", []),
                    subject=subject,
                    body=body,
                    data=data if attach else b"",
                    filename=filename,
                    content_type=content_type,
                    attach=attach,
                )
                results.append({
                    "channel": "email",
                    "target": target,
                    "status": "ok",
                    "detail": f"sent to {len(ch['to'])} recipient(s)"
                    + (f" + {len(ch.get('cc', []))} cc" if ch.get("cc") else ""),
                    "attached": attach,
                })
            except Exception as exc:  # noqa: BLE001 - SMTP errors are events, not failures
                results.append({
                    "channel": "email",
                    "target": target,
                    "status": "error",
                    "detail": f"{exc.__class__.__name__}: {exc}"[:280],
                    "attached": attach,
                })

    # Persist the trail (failure-isolated: delivery already happened).
    try:
        for r in results:
            session.add(
                ReportDeliveryEvent(
                    report_id=row.id,
                    channel=r["channel"],
                    target=r["target"][:290],
                    status=r["status"],
                    detail=r["detail"][:290] if r["detail"] else None,
                    artifact_id=artifact_id,
                    attached=r["attached"],
                )
            )
        await session.flush()
        keep = (
            select(ReportDeliveryEvent.id)
            .where(ReportDeliveryEvent.report_id == row.id)
            .order_by(ReportDeliveryEvent.created_at.desc(), ReportDeliveryEvent.id.desc())
            .limit(REPORT_DELIVERY_CAP)
        )
        await session.execute(
            delete(ReportDeliveryEvent).where(
                ReportDeliveryEvent.report_id == row.id, ReportDeliveryEvent.id.not_in(keep)
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - the run is already safe; the trail is best-effort
        await session.rollback()
    return results


def validate_format(source_type: str, fmt: str) -> str:
    cleaned = (fmt or "").strip().lower()
    allowed = DATASET_FORMATS if source_type == "dataset" else DASHBOARD_FORMATS
    if cleaned not in allowed:
        raise ValueError(
            f"Format {fmt!r} is not valid for a {source_type} report (allowed: {', '.join(allowed)})"
        )
    return cleaned


def out(row: ScheduledReport, source_name: str | None = None) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "source_name": source_name,
        "fmt": row.fmt,
        "cron": row.cron,
        "enabled": bool(row.enabled),
        "delivery": delivery_out(getattr(row, "delivery_json", None)),
        "created_at": row.created_at,
        "last_run_at": row.last_run_at,
        "fire_count": row.fire_count or 0,
        "last_status": row.last_status,
        "last_error": row.last_error,
        "last_artifact_id": row.last_artifact_id,
    }


async def build_report_bytes(
    db: AsyncSession,
    *,
    source_type: str,
    source_id: str,
    fmt: str,
) -> tuple[bytes, str, str, str]:
    """Serialize the source -> (bytes, content_type, ext, suggested_name).

    Raises ValueError (bad format), LookupError (source missing) - callers
    translate for their surface.
    """
    if source_type == "dataset":
        from ..services import datasets as ds_svc

        ds = await db.get(Dataset, source_id)
        if ds is None:
            raise LookupError("Dataset not found")
        data, content_type, ext = ds_svc.export_dataset_bytes(ds, fmt)
        safe_name = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in ds.name)
        return data, content_type, ext, f"{safe_name or 'dataset'}.{ext}"

    if source_type == "dashboard":
        from ..models import Dashboard
        from ..services import dashboards as dash_svc
        from ..services import datasets as ds_svc

        board = await db.get(Dashboard, source_id)
        if board is None:
            raise LookupError("Dashboard not found")
        components = (board.config or {}).get("components", [])
        # dedupe dataset ids, preload each referenced frame once
        ids: list[str] = []
        for comp in components:
            dsid = comp.get("dataset_id")
            if dsid and dsid not in ids:
                ids.append(dsid)
        loaders: dict[str, "object"] = {}
        names: dict[str, str] = {}
        for dsid in ids:
            ds = await db.get(Dataset, dsid)
            if ds is not None:
                loaders[dsid] = ds_svc.read_parquet_df(ds_svc.parquet_path(ds.id))
                names[dsid] = ds.name
        rendered = dash_svc.compute_config(components, loaders)
        generated_at = datetime.now(timezone.utc)
        if fmt == "png":  # v49: image snapshot of the rendered board
            from ..services.report_images import render_dashboard_png

            return (
                render_dashboard_png(board.name, rendered, generated_at=generated_at),
                "image/png",
                "png",
                f"{board.slug or board.name}.report.png",
            )
        import json as _json

        payload = {
            "dashboard": {"name": board.name, "slug": board.slug},
            "datasets": names,
            "generated_at": generated_at.isoformat(),
            "filters": {},
            "components": rendered,
        }
        data = _json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        return data, "application/json", "json", f"{board.slug or board.name}.report.json"

    raise ValueError(f"Unknown report source_type {source_type!r}")


async def run_report(report_id: str) -> dict:
    """Execute one report NOW (also the APScheduler callback).

    Opens its own session end to end; every failure path lands on the row
    as last_status=error and the function still returns a summary instead
    of raising, so a broken report can never wedge the scheduler.
    """
    from ..db import AsyncSessionLocal
    from ..services import artifacts as artifact_svc

    async with AsyncSessionLocal() as session:
        row = await session.get(ScheduledReport, report_id)
        if row is None or not row.enabled:
            return {"ok": False, "error": "Report not found or disabled"}

        source_name: str | None = None
        try:
            data, content_type, ext, filename = await build_report_bytes(
                session,
                source_type=row.source_type,
                source_id=row.source_id,
                fmt=row.fmt,
            )
            if row.source_type == "dataset":
                src = await session.get(Dataset, row.source_id)
                source_name = src.name if src else None
            else:
                from ..models import Dashboard

                src = await session.get(Dashboard, row.source_id)
                source_name = src.name if src else None

            artifact = await artifact_svc.save_artifact(
                session,
                kind="report",
                data=data,
                content_type=content_type,
                meta={
                    "title": f"{row.name} ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')})",
                    "report_id": row.id,
                    "report_name": row.name,
                    "source_type": row.source_type,
                    "source_id": row.source_id,
                    "source_name": source_name,
                    "fmt": row.fmt,
                },
                filename=filename,
            )
            row.last_artifact_id = artifact.id
            row.last_status = "ok"
            row.last_error = None
            ok = True
            error = None
        except Exception as exc:  # noqa: BLE001 - the job must never raise
            ok = False
            error = f"{exc.__class__.__name__}: {exc}"[:290]
            logger.warning("scheduled report %s failed: %s", report_id, error)
            row.last_status = "error"
            row.last_error = error

        row.last_run_at = datetime.now(timezone.utc)
        row.fire_count = (row.fire_count or 0) + 1
        await session.commit()

        # v52: push the artifact out through the report's delivery channels
        # (after the run state is durable; delivery can never flip `ok`).
        delivery_results: list[dict] = []
        if ok:
            try:
                delivery_results = await deliver_report(
                    session,
                    row,
                    data=data,
                    filename=filename,
                    content_type=content_type,
                    artifact_id=row.last_artifact_id,
                    source_name=source_name,
                )
            except Exception:  # noqa: BLE001 - belt and braces: delivery never raises
                delivery_results = []

        return {
            "ok": ok,
            "error": error,
            "artifact_id": row.last_artifact_id if ok else None,
            "ran_at": row.last_run_at.isoformat() if row.last_run_at else None,
            "delivery": delivery_results,
        }
