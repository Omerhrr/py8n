"""Dashboards API (v31) - read-only analytics boards over MANY datasets.

Admin endpoints (resolve by id OR case-insensitive name)
----------------------------------------------------------
GET    /dashboards                    list
POST   /dashboards                    create (generate from dataset_ids, or explicit config, or blank)
GET    /dashboards/{ref}              metadata
PATCH  /dashboards/{ref}              rename (re-slugs) / re-describe / set config
DELETE /dashboards/{ref}              drop board (datasets untouched)
POST   /dashboards/{ref}/generate     re-generate config from fresh dataset order
POST   /dashboards/{ref}/preview      compute the CURRENT config (drafts welcome) - builder live preview
POST   /dashboards/{ref}/publish      draft → published (guards: ≥1 component, all datasets resolve, config valid)
POST   /dashboards/{ref}/unpublish    published → draft

Runtime endpoint (slug-addressable, PUBLISHED boards only)
----------------------------------------------------------
GET    /dashboards/{slug}/runtime     board + rendered component payload for /d/{slug}

Every mutation commits explicitly (v4 lesson). Datasets referenced by a
component are validated against the LIVE dataset table on write; compute
degrades to empty content if a dataset vanishes later (boards stay
renderable, never 500).
"""

from __future__ import annotations

import secrets

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404, scope_rows
from ..db import get_db
from ..models import Dashboard, DashboardAuditEvent, Dataset
from ..schemas import DashboardCreate, DashboardOut, DashboardUpdate, ShareToggle
from ..services import dashboards as db_svc
from ..services import datasets as ds_svc
from ..services import reports as report_svc  # v54: snapshot drilldowns

router = APIRouter(prefix="/dashboards", tags=["dashboards"])


def _out(row: Dashboard) -> DashboardOut:
    return DashboardOut(
        id=row.id,
        name=row.name,
        slug=row.slug,
        description=row.description or "",
        config=row.config or {},
        status=row.status,
        share_token=row.share_token,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _get_or_404(db: AsyncSession, ref: str, user=None) -> Dashboard:
    row = await db_svc.get_dashboard(db, ref)
    if row is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    own_or_404(row.owner_id, user)  # v37 (runtime stays public)
    return row


async def _runtime_or_404(db: AsyncSession, slug: str) -> Dashboard:
    row = await db_svc.get_by_slug(db, slug)
    if row is None or row.status != "published":
        raise HTTPException(status_code=404, detail="Dashboard not found (or not published)")
    return row


def _share_gate(row: Dashboard, request: Request) -> str | None:
    """v47 share-token ACL - same contract as apps._share_gate.

    Returns the presented token when the gate passes (so the audit helper
    can log an allowed view), raises 403 otherwise."""
    expected = row.share_token
    if not expected:
        return None
    presented = request.query_params.get("t") or request.headers.get("x-share-token") or ""
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="Valid share token required (?t= or X-Share-Token)")
    return presented


# ------------------------------------------------------------------ v51 audit
DASHBOARD_AUDIT_CAP = 500  # newest events kept per dashboard (mirrors apps)


async def _audit_share_access(
    db: AsyncSession,
    row: Dashboard,
    action: str,
    outcome: str = "allowed",
    detail: str | None = None,
    force: bool = False,
) -> None:
    """Append one share-surface audit event (v51) - parity with app grants.

    Only PROTECTED boards are logged (share_token set); ``force`` records
    the rejection after a failed gate. Owns its commit and never raises:
    an auditing hiccup must never fail, or reverse, the request it is
    observing. Trims to the newest DASHBOARD_AUDIT_CAP events per board.
    """
    try:
        if not (force or row.share_token):
            return  # open boards stay untracked - the owner's own traffic
        db.add(
            DashboardAuditEvent(
                dashboard_id=row.id,
                action=action,
                outcome=outcome,
                detail=detail[:290] if detail else None,
            )
        )
        await db.flush()
        keep = (
            select(DashboardAuditEvent.id)
            .where(DashboardAuditEvent.dashboard_id == row.id)
            .order_by(DashboardAuditEvent.created_at.desc(), DashboardAuditEvent.id.desc())
            .limit(DASHBOARD_AUDIT_CAP)
        )
        await db.execute(
            delete(DashboardAuditEvent).where(
                DashboardAuditEvent.dashboard_id == row.id,
                DashboardAuditEvent.id.not_in(keep),
            )
        )
        await db.commit()
    except Exception:  # noqa: BLE001 - auditing must never break the surface
        await db.rollback()


def _filters_from_request(request: Request) -> dict[str, list[str]]:
    """v47 cross-filtering: ``?filter.COLUMN=value`` (repeatable, comma-split).

    Same wire contract as the apps runtime, so one click on a chart segment
    re-computes every component over the filtered frames."""
    filters: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        if key.startswith("filter.") and value.strip():
            col = key[len("filter."):]
            filters.setdefault(col, []).extend(v.strip() for v in value.split(",") if v.strip())
    return filters


def _refs(components: list[dict]) -> list[str]:
    """Deduped dataset_id references from a component list, order kept."""
    return [
        c.get("dataset_id")
        for c in components
        if isinstance(c, dict) and c.get("dataset_id")
    ]


async def _collect_datasets(
    db: AsyncSession, dataset_ids: list[str], strict: bool = True
) -> dict[str, Dataset]:
    """Resolve referenced dataset ids (deduped).

    strict=True (writes/publish): 404 on the first missing id.
    strict=False (compute): missing datasets are SKIPPED so boards stay
    renderable when a component outlives its dataset.
    """
    out: dict[str, Dataset] = {}
    for ds_id in dict.fromkeys(dataset_ids):
        row = await db.get(Dataset, ds_id)
        if row is None:
            if strict:
                raise HTTPException(status_code=404, detail=f"Dataset {ds_id!r} not found")
            continue
        out[ds_id] = row
    return out


async def _load_frames(datasets: dict[str, Dataset]) -> dict[str, pd.DataFrame]:
    return {ds_id: db_svc._load_df(ds) for ds_id, ds in datasets.items()}


async def _validate_or_400(config: dict, datasets: dict[str, Dataset]) -> None:
    try:
        db_svc.validate_config(config, {k: v.schema_json or [] for k, v in datasets.items()})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _load_generators(db: AsyncSession, dataset_ids: list[str]):
    """dataset_ids → ordered [(Dataset, DataFrame)] for generate_config."""
    rows = await _collect_datasets(db, dataset_ids)
    frames = await _load_frames(rows)
    return [(rows[ds_id], frames[ds_id]) for ds_id in rows]


async def _compute_payload(row: Dashboard, db: AsyncSession) -> dict:
    """Rendered board payload - shared by preview and runtime (tolerant)."""
    components = (row.config or {}).get("components", [])
    datasets = await _collect_datasets(db, _refs(components), strict=False)
    frames = await _load_frames(datasets)
    return db_svc.compute_config(components, frames)


# ----------------------------------------------------------------- admin
@router.get("", response_model=list[DashboardOut])
async def list_dashboards(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Dashboard).order_by(Dashboard.updated_at.desc()))).scalars().all()
    return [_out(r) for r in scope_rows(rows, user)]  # v37


@router.post("", response_model=DashboardOut, status_code=201)
async def create_dashboard(body: DashboardCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    name = body.name.strip()
    if not ds_svc.NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Name must start with a letter or digit and contain only letters, digits, spaces, dots, dashes or underscores",
        )
    if await db_svc.name_taken(db, name):
        raise HTTPException(status_code=409, detail=f"Dashboard {name!r} already exists")

    if body.config is not None:
        config = body.config
        datasets = await _collect_datasets(db, _refs(config.get("components", [])))
        await _validate_or_400(config, datasets)
    elif body.generate and body.dataset_ids:
        pairs = await _load_generators(db, body.dataset_ids)
        config = db_svc.generate_config(pairs)
    else:
        # No config and nothing to generate from (generate defaults to True,
        # so blank creates land here too - same tolerance as apps).
        config = {"components": []}

    row = Dashboard(
        name=name,
        slug=await db_svc.unique_slug(db, name),
        description=body.description.strip(),
        config=config,
        status="draft",
    )
    row.owner_id = user.id if user else None  # v37
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.get("/{dash_ref}", response_model=DashboardOut)
async def get_dashboard(dash_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    return _out(await _get_or_404(db, dash_ref, user))


@router.patch("/{dash_ref}", response_model=DashboardOut)
async def update_dashboard(dash_ref: str, body: DashboardUpdate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dash_ref, user)
    if body.name is not None:
        name = body.name.strip()
        if not ds_svc.NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="Invalid dashboard name")
        if await db_svc.name_taken(db, name, exclude_id=row.id):
            raise HTTPException(status_code=409, detail=f"Dashboard {name!r} already exists")
        row.name = name
        row.slug = await db_svc.unique_slug(db, name, exclude_id=row.id)
    if body.description is not None:
        row.description = body.description.strip()
    if body.config is not None:
        if row.status == "published":
            raise HTTPException(status_code=409, detail="Unpublish before editing the config")
        datasets = await _collect_datasets(db, _refs(body.config.get("components", [])))
        await _validate_or_400(body.config, datasets)
        row.config = body.config
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.delete("/{dash_ref}", status_code=204)
async def delete_dashboard(dash_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dash_ref, user)
    await db.delete(row)
    await db.commit()


@router.post("/{dash_ref}/generate", response_model=DashboardOut)
async def regenerate_dashboard(dash_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Re-generate the layout from the datasets the current components reference."""
    row = await _get_or_404(db, dash_ref, user)
    if row.status == "published":
        raise HTTPException(status_code=409, detail="Unpublish before regenerating")
    refs = _refs((row.config or {}).get("components", []))
    if not refs:
        raise HTTPException(status_code=409, detail="No datasets referenced yet - add a component first")
    pairs = await _load_generators(db, refs)
    row.config = db_svc.generate_config(pairs)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.post("/{dash_ref}/preview")
async def preview_dashboard(dash_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Compute the CURRENT config - the builder's live data preview (drafts OK)."""
    row = await _get_or_404(db, dash_ref, user)
    return {
        "dashboard": {"name": row.name, "slug": row.slug, "status": row.status},
        "components": await _compute_payload(row, db),
    }


@router.post("/{dash_ref}/publish", response_model=DashboardOut)
async def publish_dashboard(dash_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dash_ref, user)
    components = (row.config or {}).get("components", [])
    datasets = await _collect_datasets(db, _refs(components))
    await _validate_or_400(row.config or {}, datasets)
    row.status = "published"
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.post("/{dash_ref}/unpublish", response_model=DashboardOut)
async def unpublish_dashboard(dash_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, dash_ref, user)
    row.status = "draft"
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


# ------------------------------------------------------------- snapshot (v54)
@router.get("/{dash_ref}/snapshot")
async def dashboard_snapshot(
    dash_ref: str,
    fmt: str = Query(default="json", pattern="^(json|png)$"),
    component: str = Query(default="", max_length=80, description="Component id - renders ONLY that component (the drilldown target)"),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Snapshot the board as JSON or PNG (v54).

    The JSON payload stamps every rendered component with its drilldown
    metadata (``dataset`` name + ``ref`` = ``/d/{slug}?c={id}``); the PNG
    prints the same as caption strips. Pass ``component=<id>`` for a
    standalone image/payload of ONE component - the target a report
    drilldown link points at.
    """
    row = await _get_or_404(db, dash_ref, user)
    data, content_type, _ext, filename = await report_svc.dashboard_snapshot(
        db, row, fmt, component_id=component or None,
    )
    if fmt == "png":
        return Response(content=data, media_type=content_type, headers={"Content-Disposition": f'inline; filename="{filename}"'})
    import json as _json

    return _json.loads(data)


# ----------------------------------------------------------------- runtime
@router.get("/{slug}/runtime")
async def runtime(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    row = await _runtime_or_404(db, slug)
    presented = None
    try:
        presented = _share_gate(row, request)  # v47 share-token ACL
    except HTTPException:
        # v51: rejected share access lands in the audit trail before the 403
        await _audit_share_access(
            db, row, "view_dashboard", outcome="denied", force=True,
            detail="invalid token" if (request.query_params.get("t") or request.headers.get("x-share-token")) else "missing token",
        )
        raise
    # v51: allowed share views are audited only when the board is protected
    await _audit_share_access(db, row, "view_dashboard")
    components = (row.config or {}).get("components", [])
    filters = _filters_from_request(request)  # v47: cross-filtering
    datasets = await _collect_datasets(db, _refs(components), strict=False)
    dataset_meta = [
        {"id": ds.id, "name": ds.name, "row_count": ds.row_count} for ds in datasets.values()
    ]
    frames = await _load_frames(datasets)
    if filters:  # v47: filter BEFORE compute - charts/stats/tables all re-render
        from ..services.apps import apply_filters

        frames = {
            ds_id: apply_filters(df, filters)
            for ds_id, df in frames.items()
            if isinstance(df, pd.DataFrame)
        }
    return {
        "dashboard": {
            "name": row.name,
            "slug": row.slug,
            "description": row.description or "",
            "status": row.status,
        },
        "datasets": dataset_meta,
        "components": db_svc.compute_config(components, frames),
        "refresh_seconds": (row.config or {}).get("refresh_seconds", 60),  # v46
        "filters": filters,  # v47: echo for active-filter chips
    }


@router.put("/{dash_ref}/share", response_model=DashboardOut)
async def toggle_share(dash_ref: str, body: ShareToggle, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """v47: enable (generate) or disable (clear) the public share token."""
    row = await _get_or_404(db, dash_ref, user)
    row.share_token = secrets.token_urlsafe(24) if body.enabled else None
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row)


# ----------------------------------------------------------------- v51 audit
@router.get("/{dash_ref}/share/audit")
async def share_audit(
    dash_ref: str,
    limit: int = Query(50, ge=1, le=200),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner-facing share audit trail (v51): who rendered /d/{slug}, when,
    and every rejected attempt - the dashboard twin of the apps grant log."""
    row = await _get_or_404(db, dash_ref, user)
    events = (
        await db.execute(
            select(DashboardAuditEvent)
            .where(DashboardAuditEvent.dashboard_id == row.id)
            .order_by(DashboardAuditEvent.created_at.desc(), DashboardAuditEvent.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    total = (
        await db.execute(
            select(DashboardAuditEvent.id).where(DashboardAuditEvent.dashboard_id == row.id)
        )
    ).scalars().all()
    return {
        "dashboard_id": row.id,
        "protected": bool(row.share_token),
        "total": len(total),
        "events": [
            {
                "id": e.id,
                "action": e.action,
                "outcome": e.outcome,
                "detail": e.detail,
                "created_at": e.created_at,
            }
            for e in events
        ],
    }
