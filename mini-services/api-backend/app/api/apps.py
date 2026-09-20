"""Apps API (v29) - the Excel → App builder flagship.

Admin endpoints (resolve by id OR case-insensitive name)
--------------------------------------------------------
GET    /apps                        list
POST   /apps                        create (blank, or generate from dataset)
GET    /apps/{ref}                  metadata
PATCH  /apps/{ref}                  rename (re-slugs) / re-describe / bind dataset / set config
DELETE /apps/{ref}                  drop app (dataset untouched)
POST   /apps/{ref}/generate         re-generate config from the bound dataset
POST   /apps/{ref}/publish          draft → published (guards: dataset + valid config)
POST   /apps/{ref}/unpublish        published → draft

Runtime endpoints (slug-addressable, PUBLISHED apps only)
---------------------------------------------------------
GET    /apps/{slug}/runtime              app + dataset schema + stats + chart data
GET    /apps/{slug}/records              paginated rows
GET    /apps/{slug}/export               download records as csv/xlsx/json (v142, scope-safe)
POST   /apps/{slug}/records              create a record (lands in the dataset parquet)
PATCH  /apps/{slug}/records/{index}      edit a record
DELETE /apps/{slug}/records/{index}      delete a record
GET    /apps/{slug}/form                 standalone form descriptor (v30)
POST   /apps/{slug}/form-submit          anonymous form submission (v30)

Rules management (v30) - the config lock does NOT apply: rules are
governance, editable on live apps without touching the layout
------------------------------------------------------------------------------
GET    /apps/{ref}/rules                 rules + the known ops/actions/events
PUT    /apps/{ref}/rules                 replace all rules (validated)
POST   /apps/{ref}/rules/test            dry-run a sample record against the rules

Google Sheets sync (v143) - owner-only, push the bound dataset on demand
------------------------------------------------------------------------------
GET    /apps/{ref}/sheets-sync           current sync settings
PUT    /apps/{ref}/sheets-sync           save sheet/tab/credential/write_mode
POST   /apps/{ref}/sheets-sync/run       push the dataset's full contents now

PWA install (v144) - published apps are installable straight from /run/{slug}
------------------------------------------------------------------------------
GET    /apps/{slug}/manifest.webmanifest per-app manifest (name, accent icon)
GET    /apps/{slug}/icon.svg             accent-colored initial-letter icon

Row-level share grants (v48) - named, per-viewer doors into the runtime:
------------------------------------------------------------------------------
GET    /apps/{ref}/grants                list grants (owner-only, + access stats)
POST   /apps/{ref}/grants                create {name, column, op, value}
PUT    /apps/{ref}/grants/{gid}          rename / re-scope / disable
DELETE /apps/{ref}/grants/{gid}          revoke (links die instantly)

Grant audit log (v49) - every request through a protected runtime surface
leaves one event (grant snapshot + action + allowed/denied), capped per app:
------------------------------------------------------------------------------
GET    /apps/{ref}/grants/audit          newest events (owner-only)

Every mutation commits explicitly (v4 lesson).
"""

from __future__ import annotations

import json
import secrets
from xml.sax.saxutils import escape as _xml_escape

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404, scope_rows
from ..config import settings
from ..db import get_db
from ..models import App, AppShareGrant, Dataset, GrantAuditEvent, Workflow
from ..schemas import AppCreate, AppOut, AppRecordIn, AppUpdate, RulesTestIn, RulesPut, ShareToggle
from ..services import apps as app_svc
from ..services import datasets as ds_svc
from ..services import grants as grant_svc
from ..services import rules as rule_svc

router = APIRouter(prefix="/apps", tags=["apps"])


def _out(row: App, dataset: Dataset | None = None) -> AppOut:
    return AppOut(
        id=row.id,
        name=row.name,
        slug=row.slug,
        description=row.description or "",
        dataset_id=row.dataset_id,
        dataset_name=dataset.name if dataset else None,
        config=row.config or {},
        status=row.status,
        share_token=row.share_token,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _dataset_for(db: AsyncSession, app_row: App) -> Dataset | None:
    if not app_row.dataset_id:
        return None
    return await db.get(Dataset, app_row.dataset_id)


async def _extra_datasets_for(db: AsyncSession, app_row: App) -> dict[str, Dataset]:
    """v149: multi-dataset apps - dataset_id -> Dataset for every EXTRA
    (non-primary) dataset the app's config.datasets list references. Empty
    for every legacy single-dataset app (the overwhelming majority)."""
    out: dict[str, Dataset] = {}
    for entry in (app_row.config or {}).get("datasets") or []:
        ds_id = entry.get("dataset_id")
        if ds_id and ds_id not in out:
            ds = await db.get(Dataset, ds_id)
            if ds is not None:
                out[ds_id] = ds
    return out


async def _extra_schemas_from_config(db: AsyncSession, config: dict | None) -> dict[str, list[dict]]:
    """v149: resolve {dataset_id -> schema} for every dataset a POSTED/PATCHed
    config's ``datasets`` list references, so validate_config can check
    multi-dataset components against the RIGHT schema instead of the app's
    primary one."""
    out: dict[str, list[dict]] = {}
    for entry in (config or {}).get("datasets") or []:
        ds_id = entry.get("dataset_id")
        if ds_id and ds_id not in out:
            ds = await db.get(Dataset, ds_id)
            if ds is not None:
                out[ds_id] = ds.schema_json or []
    return out


async def _dataset_for_page(db: AsyncSession, app_row: App, page: str | None) -> Dataset | None:
    """v149: resolve the dataset a given page's records/CRUD should hit -
    the primary dataset when ``page`` is omitted or maps to nothing special."""
    ds_id = app_svc.dataset_id_for_page(app_row.config, page, app_row.dataset_id)
    if not ds_id:
        return None
    return await db.get(Dataset, ds_id)


async def _out_with_dataset(db: AsyncSession, row: App) -> AppOut:
    return _out(row, await _dataset_for(db, row))


async def _get_or_404(db: AsyncSession, ref: str, user=None) -> App:
    row = await app_svc.get_app(db, ref)
    if row is None:
        raise HTTPException(status_code=404, detail="App not found")
    own_or_404(row.owner_id, user)  # v37 (runtime endpoints stay public)
    return row


def _records_mutator_gate(row: App, user) -> None:
    """Audit hardening: editing/deleting a published app's records is NOT part
    of the anonymous runtime surface (only listing/form-submit is). The app's
    owner may always mutate; anonymous callers only in legacy mode."""
    if user is not None:
        own_or_404(row.owner_id, user)
        return
    if settings.require_auth:
        raise HTTPException(status_code=401, detail="Authentication required")


async def _runtime_or_404(db: AsyncSession, slug: str) -> App:
    row = await app_svc.get_by_slug(db, slug)
    if row is None or row.status != "published":
        raise HTTPException(status_code=404, detail="App not found (or not published)")
    return row


def _share_gate(row: App | Dashboard, request: Request) -> None:
    """v47 share-token ACL: when the owner has enabled share protection,
    public callers must present the token via ``?t=`` or ``X-Share-Token``.
    NULL token = legacy open access (backward compatible).

    v48: apps additionally accept a row-scoped grant token - the grant path
    lives in :func:`_runtime_scope` which the app runtime endpoints use.
    Dashboards keep the simple v47 gate.
    """
    expected = getattr(row, "share_token", None)
    if not expected:
        return
    presented = request.query_params.get("t") or request.headers.get("x-share-token") or ""
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="Valid share token required (?t= or X-Share-Token)")


class RuntimeScope:
    """Resolved public access for one app runtime request (v48).

    ``filter`` is None for full access (owner token, legacy open) and a
    row_filter dict when the caller came in through a grant token.
    """

    def __init__(self, grant: AppShareGrant | None):
        self.grant = grant
        self.filter = (grant.row_filter or None) if grant is not None else None
        self.name = grant.name if grant is not None else None

    @property
    def scoped(self) -> bool:
        return self.filter is not None

    def echo(self) -> dict | None:
        if not self.scoped:
            return None
        return {
            "grant": self.name,
            "column": self.filter.get("column"),
            "op": self.filter.get("op"),
            "value": self.filter.get("value"),
        }


async def _runtime_scope(row: App, request: Request, db: AsyncSession, action: str = "access") -> RuntimeScope:
    """Resolve public access for an app runtime request (v48 supersedes the
    v47 gate on the app surface, which was token-or-nothing):

    1. the full-access share token still works exactly as before;
    2. otherwise an enabled row-scoped grant token may answer;
    3. any protection at all (share token OR grants) means an anonymous
       caller without a token gets 403 - grants exist to fail closed;
    4. no share token and no grants = legacy open access.

    v49: rejected callers are recorded on the audit trail before the 403
    (``action`` names the surface they were knocking on).
    """
    grants = (
        await db.execute(
            select(AppShareGrant).where(
                AppShareGrant.app_id == row.id, AppShareGrant.enabled.is_(True)
            )
        )
    ).scalars().all()

    presented = request.query_params.get("t") or request.headers.get("x-share-token") or ""
    if row.share_token and presented and secrets.compare_digest(presented, row.share_token):
        return RuntimeScope(None)  # full access, unchanged v47 behaviour
    if presented and grants:
        for g in grants:
            if secrets.compare_digest(presented, g.token):
                return RuntimeScope(g)
    if row.share_token or grants:
        await _audit(
            db, row, RuntimeScope(None), action, outcome="denied",
            detail="invalid token" if presented else "missing token", force=True,
        )
        raise HTTPException(status_code=403, detail="Valid share token required (?t= or X-Share-Token)")
    return RuntimeScope(None)  # legacy open access


GRANT_AUDIT_CAP = 500  # newest events kept per app


async def _audit(
    db: AsyncSession,
    app_row: App,
    scope: RuntimeScope,
    action: str,
    outcome: str = "allowed",
    detail: str | None = None,
    force: bool = False,
) -> None:
    """Append one share-surface audit event (v49).

    Only PROTECTED surfaces are logged by default (share token set or the
    caller arrived through a grant) - the log answers "what did shared
    viewers see and do", not the owner's own builder traffic. ``force``
    records a rejection when protection is known to exist (the caller just
    failed the gate).

    Owns its commit and never raises: an auditing hiccup must never fail,
    or reverse, the request it is observing. Trims to the newest
    GRANT_AUDIT_CAP events per app so a hot public link cannot grow the
    table without bound.
    """
    try:
        if not (force or app_row.share_token or scope.grant is not None):
            return
        db.add(
            GrantAuditEvent(
                app_id=app_row.id,
                grant_id=scope.grant.id if scope.grant else None,
                grant_name=scope.name,
                action=action,
                outcome=outcome,
                detail=detail[:290] if detail else None,
            )
        )
        await db.flush()
        keep = (
            select(GrantAuditEvent.id)
            .where(GrantAuditEvent.app_id == app_row.id)
            .order_by(GrantAuditEvent.created_at.desc(), GrantAuditEvent.id.desc())
            .limit(GRANT_AUDIT_CAP)
        )
        await db.execute(
            delete(GrantAuditEvent).where(
                GrantAuditEvent.app_id == app_row.id, GrantAuditEvent.id.not_in(keep)
            )
        )
        await db.commit()
    except Exception:  # noqa: BLE001 - auditing must never break the surface
        await db.rollback()


def _form_comp(row: App, page: str | None = None) -> dict | None:
    """First form component, if any (v30 forms + rules key off it).

    v149: when ``page`` is given, prefer a form component on THAT page (a
    multi-dataset app has one form per page); falls back to the first form
    anywhere so single-dataset/legacy apps are unaffected."""
    comps = (row.config or {}).get("components", [])
    if page:
        for comp in comps:
            if comp.get("type") == "form" and app_svc.component_page(comp) == page:
                return comp
    for comp in comps:
        if comp.get("type") == "form":
            return comp
    return None


def _row_in_scope(dataset: Dataset, index: int, filt: dict) -> bool:
    """v48: is RAW row ``index`` inside the grant's slice? Any failure to
    evaluate fails CLOSED (a grant viewer can never touch a row we could
    not prove is theirs)."""
    try:
        df = ds_svc.read_parquet_df(ds_svc.parquet_path(dataset.id))
        if index < 0 or index >= len(df):
            return False
        return bool(grant_svc.scope_mask(df, filt).iloc[index])
    except Exception:  # noqa: BLE001
        return False


async def _validate_workflow_ref(db: AsyncSession, workflow_id: str | None) -> None:
    """v46: config.workflow_id must reference an existing workflow."""
    if not workflow_id:
        return
    if await db.get(Workflow, workflow_id) is None:
        raise HTTPException(status_code=404, detail="config.workflow_id: workflow not found")


async def _validate_relations(db: AsyncSession, config: dict) -> None:
    """v141: every form field's ``relation`` (if any) must point at a real
    dataset whose schema actually has the named column(s) - structural shape
    is already checked by app_svc.validate_fields; this is the DB-backed
    existence check, same split as _validate_workflow_ref above."""
    for comp in (config or {}).get("components", []):
        if not isinstance(comp, dict) or comp.get("type") != "form":
            continue
        for f in comp.get("fields", []):
            if not isinstance(f, dict):
                continue
            rel = f.get("relation")
            if not rel:
                continue
            ds = await db.get(Dataset, rel.get("dataset_id"))
            if ds is None:
                raise HTTPException(status_code=400, detail=f"field {f.get('name')!r}: relation dataset not found")
            names = {c["name"] for c in (ds.schema_json or [])}
            if rel.get("display_column") not in names:
                raise HTTPException(status_code=400, detail=f"field {f.get('name')!r}: relation.display_column not in that dataset's schema")
            vc = rel.get("value_column")
            if vc and vc not in names:
                raise HTTPException(status_code=400, detail=f"field {f.get('name')!r}: relation.value_column not in that dataset's schema")


def _filters_from_request(request: Request) -> dict[str, list[str]]:
    """?filter.COLUMN=value (repeatable, comma-split) → filter dict (v46)."""
    filters: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        if key.startswith("filter.") and value.strip():
            col = key[len("filter."):]
            filters.setdefault(col, []).extend(v.strip() for v in value.split(",") if v.strip())
    return filters


# ----------------------------------------------------------------- admin
@router.get("", response_model=list[AppOut])
async def list_apps(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(App).order_by(App.updated_at.desc()))).scalars().all()
    return [await _out_with_dataset(db, r) for r in scope_rows(rows, user)]  # v37


@router.post("", response_model=AppOut, status_code=201)
async def create_app(body: AppCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    name = body.name.strip()
    if not ds_svc.NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Name must start with a letter or digit and contain only letters, digits, spaces, dots, dashes or underscores",
        )
    if await app_svc.name_taken(db, name):
        raise HTTPException(status_code=409, detail=f"App {name!r} already exists")

    dataset = None
    if body.dataset_id:
        dataset = await db.get(Dataset, body.dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

    config = body.config
    if config is None and dataset is not None and body.generate:
        df = ds_svc.read_parquet_df(ds_svc.parquet_path(dataset.id))
        config = app_svc.generate_config(df, dataset.schema_json or [])
    config = config or {"components": []}
    if dataset is not None:
        extra_schemas = await _extra_schemas_from_config(db, config)  # v149: multi-dataset apps
        try:
            app_svc.validate_config(config, dataset.schema_json or [], extra_schemas=extra_schemas or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _validate_workflow_ref(db, (config or {}).get("workflow_id"))  # v46
    await _validate_relations(db, config)  # v141

    row = App(
        name=name,
        slug=await app_svc.unique_slug(db, name),
        description=body.description.strip(),
        dataset_id=dataset.id if dataset else None,
        config=config,
        status="draft",
    )
    row.owner_id = user.id if user else None  # v37
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row, dataset)


@router.get("/{app_ref}", response_model=AppOut)
async def get_app(app_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    return await _out_with_dataset(db, await _get_or_404(db, app_ref, user))


@router.patch("/{app_ref}", response_model=AppOut)
async def update_app(app_ref: str, body: AppUpdate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref, user)
    dataset = await _dataset_for(db, row)

    if body.name is not None:
        name = body.name.strip()
        if not ds_svc.NAME_RE.match(name):
            raise HTTPException(status_code=400, detail="Invalid app name")
        if await app_svc.name_taken(db, name, exclude_id=row.id):
            raise HTTPException(status_code=409, detail=f"App {name!r} already exists")
        row.name = name
        row.slug = await app_svc.unique_slug(db, name, exclude_id=row.id)
    if body.description is not None:
        row.description = body.description.strip()
    if body.dataset_id is not None:
        if body.dataset_id == "":
            row.dataset_id = None
            dataset = None
        else:
            dataset = await db.get(Dataset, body.dataset_id)
            if dataset is None:
                raise HTTPException(status_code=404, detail="Dataset not found")
            row.dataset_id = dataset.id
    if body.config is not None:
        if row.status == "published":
            raise HTTPException(status_code=409, detail="Unpublish before editing the config")
        extra_schemas = await _extra_schemas_from_config(db, body.config)  # v149: multi-dataset apps
        try:
            app_svc.validate_config(
                body.config, (dataset.schema_json if dataset else []) or [], extra_schemas=extra_schemas or None
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _validate_workflow_ref(db, (body.config or {}).get("workflow_id"))  # v46
        await _validate_relations(db, body.config)  # v141
        row.config = body.config

    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row, dataset)


@router.delete("/{app_ref}", status_code=204)
async def delete_app(app_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref, user)
    await db.delete(row)
    await db.commit()


@router.post("/{app_ref}/generate", response_model=AppOut)
async def regenerate_app(app_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref, user)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="Bind a dataset first")
    if row.status == "published":
        raise HTTPException(status_code=409, detail="Unpublish before regenerating")
    df = ds_svc.read_parquet_df(ds_svc.parquet_path(dataset.id))
    row.config = app_svc.generate_config(df, dataset.schema_json or [])
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row, dataset)


@router.post("/{app_ref}/publish", response_model=AppOut)
async def publish_app(app_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref, user)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="Bind a dataset before publishing")
    extra_schemas = await _extra_schemas_from_config(db, row.config)  # v149: multi-dataset apps
    try:
        app_svc.validate_config(row.config or {}, dataset.schema_json or [], extra_schemas=extra_schemas or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid config: {exc}") from exc
    await _validate_workflow_ref(db, (row.config or {}).get("workflow_id"))  # v46
    row.status = "published"
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _out(row, dataset)


@router.post("/{app_ref}/unpublish", response_model=AppOut)
async def unpublish_app(app_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref, user)
    row.status = "draft"
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return await _out_with_dataset(db, row)


@router.put("/{app_ref}/share", response_model=AppOut)
async def toggle_share(app_ref: str, body: ShareToggle, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """v47: enable (generate) or disable (clear) the public share token.

    Enabled  -> a fresh ``secrets.token_urlsafe`` token; the runtime surface
    (runtime/records/form/form-submit) then demands it via ?t= or header.
    Disabled -> NULL token, legacy open access restored. Regenerating the
    token (disable + enable) revokes every previously shared link."""
    row = await _get_or_404(db, app_ref, user)
    row.share_token = secrets.token_urlsafe(24) if body.enabled else None
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return await _out_with_dataset(db, row)


# ------------------------------------------------------- grants (v48: row-level ACL)
class GrantCreate(BaseModel):
    """Body for POST /apps/{ref}/grants - one named, row-scoped share door."""

    name: str = Field(min_length=1, max_length=120)
    column: str = Field(min_length=1, max_length=200)
    op: str = Field(default="eq", pattern="^(eq|in|neq)$")
    value: object = Field(description="scalar for eq/neq, non-empty list for in")


class GrantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    column: str | None = Field(default=None, min_length=1, max_length=200)
    op: str | None = Field(default=None, pattern="^(eq|in|neq)$")
    value: object | None = None
    enabled: bool | None = None


class SheetsSyncIn(BaseModel):
    """Body for PUT /apps/{ref}/sheets-sync (v143)."""

    sheet: str = Field(default="", max_length=500, description="Full Sheets URL or bare spreadsheet ID")
    tab: str = Field(default="", max_length=200)
    credential_id: str | None = Field(default=None, description="google_service_account credential id")
    write_mode: str = Field(default="overwrite", pattern="^(overwrite|append)$")


def _grant_out(row: AppShareGrant, slug: str, stats: tuple = (0, None)) -> dict:
    count, last_at = stats
    return {
        "id": row.id,
        "app_id": row.app_id,
        "name": row.name,
        "token": row.token,  # owner-facing: the share link needs it
        "row_filter": row.row_filter or {},
        "enabled": bool(row.enabled),
        "created_at": row.created_at,
        "url": f"/run/{slug}?t={row.token}",
        "access_count": count,  # v49 audit aggregates
        "last_access_at": last_at,
    }


@router.get("/{app_ref}/grants")
async def list_grants(app_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Owner-facing list of the app's row-level share grants, each with its
    v49 audit aggregates (allowed access count + last access time)."""
    row = await _get_or_404(db, app_ref, user)
    grants = (
        await db.execute(
            select(AppShareGrant)
            .where(AppShareGrant.app_id == row.id)
            .order_by(AppShareGrant.created_at.asc())
        )
    ).scalars().all()
    stat_rows = (
        await db.execute(
            select(
                GrantAuditEvent.grant_id,
                func.count(),
                func.max(GrantAuditEvent.created_at),
            )
            .where(
                GrantAuditEvent.app_id == row.id,
                GrantAuditEvent.grant_id.isnot(None),
                GrantAuditEvent.outcome == "allowed",
            )
            .group_by(GrantAuditEvent.grant_id)
        )
    ).all()
    stats = {gid: (count, last) for gid, count, last in stat_rows}
    return [_grant_out(g, row.slug, stats.get(g.id, (0, None))) for g in grants]


@router.get("/{app_ref}/grants/audit")
async def grants_audit(
    app_ref: str,
    limit: int = Query(50, ge=1, le=200),
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner-facing audit trail (v49): the newest share-surface access
    events, newest first. Includes denials and post-revocation history
    (grant_name is snapshotted on the event)."""
    row = await _get_or_404(db, app_ref, user)
    events = (
        await db.execute(
            select(GrantAuditEvent)
            .where(GrantAuditEvent.app_id == row.id)
            .order_by(GrantAuditEvent.created_at.desc(), GrantAuditEvent.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": e.id,
            "grant_id": e.grant_id,
            "grant_name": e.grant_name,
            "action": e.action,
            "outcome": e.outcome,
            "detail": e.detail,
            "created_at": e.created_at,
        }
        for e in events
    ]


@router.post("/{app_ref}/grants", status_code=201)
async def create_grant(app_ref: str, body: GrantCreate, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref, user)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="Bind a dataset before creating grants")
    try:
        filt = grant_svc.validate_row_filter(
            {"column": body.column, "op": body.op, "value": body.value},
            dataset.schema_json or [],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    grant = AppShareGrant(
        app_id=row.id,
        name=body.name.strip(),
        token=secrets.token_urlsafe(24),
        row_filter=filt,
        enabled=True,
    )
    grant.owner_id = row.owner_id
    db.add(grant)
    await db.commit()
    await db.refresh(grant)
    return _grant_out(grant, row.slug)


@router.put("/{app_ref}/grants/{grant_id}")
async def update_grant(
    app_ref: str,
    grant_id: str,
    body: GrantUpdate,
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Update name/filter/enabled. Disabling keeps the row (re-enablable);
    deleting the grant is the revocation that instantly kills the link."""
    row = await _get_or_404(db, app_ref, user)
    grant = await db.get(AppShareGrant, grant_id)
    if grant is None or grant.app_id != row.id:
        raise HTTPException(status_code=404, detail="Grant not found")
    data = body.model_dump(exclude_none=True)
    if data:
        dataset = await _dataset_for(db, row)
        if dataset is None:
            raise HTTPException(status_code=409, detail="App has no dataset bound")
        merged = {
            "column": data.get("column", (grant.row_filter or {}).get("column", "")),
            "op": data.get("op", (grant.row_filter or {}).get("op", "eq")),
            "value": data.get("value", (grant.row_filter or {}).get("value")),
        }
        try:
            grant.row_filter = grant_svc.validate_row_filter(merged, dataset.schema_json or [])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if "name" in data:
            grant.name = data["name"].strip()
        if "enabled" in data:
            grant.enabled = bool(data["enabled"])
        db.add(grant)
        await db.commit()
        await db.refresh(grant)
    return _grant_out(grant, row.slug)


@router.delete("/{app_ref}/grants/{grant_id}")
async def delete_grant(app_ref: str, grant_id: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Revoke one grant: every link holding its token stops working at once."""
    row = await _get_or_404(db, app_ref, user)
    grant = await db.get(AppShareGrant, grant_id)
    if grant is None or grant.app_id != row.id:
        raise HTTPException(status_code=404, detail="Grant not found")
    await db.delete(grant)
    await db.commit()
    return {"ok": True}


# ----------------------------------------------------------------- runtime
@router.get("/{slug}/runtime")
async def runtime(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    row = await _runtime_or_404(db, slug)
    scope = await _runtime_scope(row, request, db, "view_runtime")  # v48: token-or-grant resolver
    dataset = await _dataset_for(db, row)
    extra_datasets = await _extra_datasets_for(db, row)  # v149: multi-dataset apps
    components = (row.config or {}).get("components", [])
    filters = _filters_from_request(request)  # v46: ?filter.COLUMN=value
    payload: dict = {
        "app": {
            "name": row.name,
            "slug": row.slug,
            "description": row.description or "",
            "config": row.config or {},
            "status": row.status,
        },
        "dataset": None,
        "stats": {},
        "chart": None,
        "components": [],  # v46: every component rendered server-side
        "pages": app_svc.pages_of(components),  # v134: sidebar/tab navigation
        # v149: which dataset backs each page - {} for every legacy single-dataset app
        "page_datasets": {
            entry.get("page"): {"id": ds_id, "name": extra_datasets[ds_id].name}
            for entry in ((row.config or {}).get("datasets") or [])
            if (ds_id := entry.get("dataset_id")) in extra_datasets
        },
        "filters": filters,
        "scope": scope.echo(),  # v48: row-level grant echo for the UI chip
    }
    payload["relations"] = {}  # v141: field name -> {value: label}, for the table's cell display
    if dataset is not None:
        df = ds_svc.read_parquet_df(ds_svc.parquet_path(dataset.id))
        if scope.scoped:  # v48: grant viewers compute over THEIR slice only
            df = grant_svc.apply_scope(df, scope.filter)
        loaders = {
            ds_id: ds_svc.read_parquet_df(ds_svc.parquet_path(ds_id))
            for ds_id in extra_datasets
        } if extra_datasets else None
        relation_lookups = await app_svc.compute_relation_lookups(db, components)  # v141
        rendered = app_svc.compute_components(components, df, filters, relation_lookups, loaders=loaders)
        payload["components"] = rendered
        payload["relations"] = relation_lookups
        # v29 backward-compatible keys (stats dict + first chart)
        payload["stats"] = {c["id"]: c["value"] for c in rendered if c["type"] in ("stat", "kpi")}
        payload["chart"] = next((c for c in rendered if c["type"] == "chart" and c.get("chart_type") != "scatter"), None)
        if scope.scoped:
            payload["dataset"] = {
                "id": dataset.id,
                "name": dataset.name,
                "schema_json": dataset.schema_json or [],
                "row_count": int(len(df)),  # scoped viewers see their slice size
            }
        else:
            payload["dataset"] = {
                "id": dataset.id,
                "name": dataset.name,
                "schema_json": dataset.schema_json or [],
                "row_count": dataset.row_count,
            }
    await _audit(db, row, scope, "view_runtime", detail=f"rows={payload['dataset']['row_count'] if payload['dataset'] else 0}")
    return payload


@router.post("/{app_ref}/preview")
async def preview_config(
    app_ref: str,
    body: dict,
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """v46: server-side builder preview - compute the posted components over
    the bound dataset (same code path as the runtime, zero drift)."""
    row = await _get_or_404(db, app_ref, user)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="Bind a dataset first")
    extra_datasets = await _extra_datasets_for(db, row)  # v149: multi-dataset apps
    components = body.get("components")
    if components is None:
        components = (row.config or {}).get("components", [])
    extra_schemas = {ds_id: (ds.schema_json or []) for ds_id, ds in extra_datasets.items()}
    try:
        app_svc.validate_config(
            {"components": components}, dataset.schema_json or [], extra_schemas=extra_schemas or None
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    df = ds_svc.read_parquet_df(ds_svc.parquet_path(dataset.id))
    loaders = {
        ds_id: ds_svc.read_parquet_df(ds_svc.parquet_path(ds_id))
        for ds_id in extra_datasets
    } if extra_datasets else None
    relation_lookups = await app_svc.compute_relation_lookups(db, components)  # v141
    rendered = app_svc.compute_components(components, df, body.get("filters"), relation_lookups, loaders=loaders)
    return {"components": rendered, "relations": relation_lookups}


# ----------------------------------------------------------------- rules (v30)
@router.get("/{app_ref}/rules")
async def get_rules(app_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref, user)
    return {
        "rules": (row.config or {}).get("rules", []),
        "ops": sorted(rule_svc.RULE_OPS),
        "actions": sorted(rule_svc.RULE_ACTIONS),
        "events": sorted(rule_svc.RULE_EVENTS),
    }


@router.put("/{app_ref}/rules")
async def put_rules(app_ref: str, body: RulesPut, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Replace all rules - allowed on PUBLISHED apps too (layout stays locked)."""
    row = await _get_or_404(db, app_ref, user)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="Bind a dataset before adding rules")
    try:
        rule_svc.validate_rules(body.rules, dataset.schema_json or [])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row.config = {**(row.config or {}), "rules": body.rules}
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"ok": True, "rules": body.rules}


@router.post("/{app_ref}/rules/test")
async def test_rules(app_ref: str, body: RulesTestIn, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Dry-run a sample record - which rules fire, what they would do."""
    row = await _get_or_404(db, app_ref, user)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="Bind a dataset before testing rules")
    return rule_svc.dry_run(
        (row.config or {}).get("rules", []),
        body.record,
        body.event if body.event in ("create", "update") else "create",
        dataset.schema_json or [],
    )


# --------------------------------------------------- Google Sheets sync (v143)
# Owner-only builder action - push the bound dataset into a Google Sheet tab
# on demand. Settings live in config["sheets_sync"], same pattern as rules:
# editable without touching the locked layout config of a published app.
@router.get("/{app_ref}/sheets-sync")
async def get_sheets_sync(app_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref, user)
    return {"sheets_sync": app_svc.sheets_sync_config(row.config)}


@router.put("/{app_ref}/sheets-sync")
async def put_sheets_sync(app_ref: str, body: SheetsSyncIn, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, app_ref, user)
    try:
        cfg = app_svc.validate_sheets_sync(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row.config = {**(row.config or {}), "sheets_sync": cfg}
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"ok": True, "sheets_sync": cfg}


@router.post("/{app_ref}/sheets-sync/run")
async def run_sheets_sync_now(app_ref: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Push the bound dataset's CURRENT full contents into the configured
    sheet right now. Always the whole dataset - this is an owner/builder
    action, not a scoped runtime surface (unlike /export)."""
    row = await _get_or_404(db, app_ref, user)
    dataset = await _dataset_for(db, row)
    if dataset is None:
        raise HTTPException(status_code=409, detail="App has no dataset bound")
    cfg = app_svc.sheets_sync_config(row.config)
    df = ds_svc.read_parquet_df(ds_svc.parquet_path(dataset.id))
    try:
        result = await app_svc.run_sheets_sync(df, cfg, owner_id=row.owner_id)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - connector helpers raise NodeExecutionError,
        # and the google-auth libs raise their own types (RefreshError etc.) on a bad
        # key or an unshared sheet; all of that is a legitimate "sync failed" outcome
        # for the caller, not a server bug, so it surfaces as 400 rather than 500.
        raise HTTPException(status_code=400, detail=f"Sheets sync failed: {exc}") from exc
    return {"ok": True, **result}


# ----------------------------------------------------------- PWA install (v144)
# Every published app is installable straight from /run/{slug} - a per-app
# manifest (name, accent-colored icon, standalone display) plus the site-wide
# service worker (public/sw.js) registered by the runtime page. No auth: an
# icon and a color are not sensitive, and installability must work for
# anonymous public links too. A share-protected app's token rides on
# start_url (?t=...) so the installed icon reopens without re-prompting.
#
# Icon URLs are absolute gateway paths (?XTransformPort=8000) rather than
# plain "/api/v1/..." - Caddy in this deployment routes purely on the
# presence of that query param (see Caddyfile), and a manifest's icon/start
# URLs are resolved by the BROWSER against the manifest's own URL, so they
# must carry the gateway param themselves to ever reach the API instead of
# 404ing against the Nuxt frontend. 8000 matches this compose file's fixed
# internal API port (NUXT_PUBLIC_API_PORT / PY8N_API_ORIGIN).
_GATEWAY_API_PORT = 8000


@router.get("/{slug}/icon.svg")
async def app_icon(slug: str, db: AsyncSession = Depends(get_db)):
    row = await _runtime_or_404(db, slug)
    accent = ((row.config or {}).get("theme") or {}).get("accent") or "#8b5cf6"
    initial = _xml_escape((row.name or "A").strip()[:1].upper() or "A")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192" width="192" height="192">'
        f'<rect width="192" height="192" rx="40" fill="{_xml_escape(accent)}"/>'
        f'<text x="96" y="132" font-family="system-ui, -apple-system, sans-serif" font-size="104" '
        f'font-weight="700" fill="#ffffff" text-anchor="middle">{initial}</text></svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")


@router.get("/{slug}/manifest.webmanifest")
async def app_manifest(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    row = await _runtime_or_404(db, slug)
    accent = ((row.config or {}).get("theme") or {}).get("accent") or "#8b5cf6"
    token = request.query_params.get("t") or ""
    start_url = f"/run/{row.slug}" + (f"?t={token}" if token else "")
    icon_url = f"/api/v1/apps/{row.slug}/icon.svg?XTransformPort={_GATEWAY_API_PORT}"
    manifest = {
        "name": row.name,
        "short_name": (row.name or "App")[:30],
        "description": row.description or f"{row.name} - built with py8n",
        "start_url": start_url,
        "id": f"/run/{row.slug}",
        "scope": f"/run/{row.slug}",
        "display": "standalone",
        "background_color": "#0a0a0f",
        "theme_color": accent,
        "icons": [
            {"src": icon_url, "sizes": "192x192", "type": "image/svg+xml", "purpose": "any"},
            {"src": icon_url, "sizes": "512x512", "type": "image/svg+xml", "purpose": "maskable"},
        ],
    }
    return Response(content=json.dumps(manifest), media_type="application/manifest+json")


# ----------------------------------------------------------------- forms (v30)
@router.get("/{slug}/form")
async def form_descriptor(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Standalone form descriptor for the public /f/{slug} page."""
    row = await _runtime_or_404(db, slug)
    scope = await _runtime_scope(row, request, db, "view_form")  # v48
    page = request.query_params.get("page")  # v149: multi-dataset apps
    form = _form_comp(row, page)
    if form is None:
        raise HTTPException(status_code=409, detail="App has no form component")
    dataset = await _dataset_for_page(db, row, page or app_svc.component_page(form))
    fields = app_svc.form_fields(form)
    relation_lookups = await app_svc.compute_relation_lookups(db, [form])  # v141
    for f in fields:
        if f.get("relation"):
            f["relation_options"] = app_svc.relation_options_list(relation_lookups.get(f["name"], {}))
    await _audit(db, row, scope, "view_form")
    return {
        "app": {"name": row.name, "slug": row.slug, "description": row.description or ""},
        "form": {
            "title": form.get("title", "Submit"),
            "submit_label": form.get("submit_label", "Submit"),
            "fields": fields,
        },
        "dataset": {"name": dataset.name, "row_count": dataset.row_count} if dataset else None,
        "scope": scope.echo(),  # v48: grant context for the public form page
    }


@router.post("/{slug}/form-submit", status_code=201)
async def form_submit(slug: str, body: AppRecordIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Anonymous single-form submission - same pipeline as records POST."""
    row = await _runtime_or_404(db, slug)
    scope = await _runtime_scope(row, request, db, "submit_form")  # v48
    page = request.query_params.get("page")  # v149: multi-dataset apps
    form = _form_comp(row, page)
    if form is None:
        raise HTTPException(status_code=409, detail="App has no form component")
    dataset = await _dataset_for_page(db, row, page or app_svc.component_page(form))
    if dataset is None:
        raise HTTPException(status_code=409, detail="App has no dataset bound")
    record, stamp_err = grant_svc.stamp_record(scope.filter, body.record)  # v48: eq grants stamp
    if stamp_err:
        await _audit(db, row, scope, "submit_form", outcome="denied", detail=stamp_err)
        raise HTTPException(status_code=403, detail=stamp_err)
    try:
        result = await app_svc.append_record(
            dataset, record, dataset.schema_json or [],
            form=form, rules=(row.config or {}).get("rules", []),
            db=db,  # v44: form submissions land on the dataset version timeline
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(dataset)
    await db.commit()
    await db.refresh(dataset)

    # v46: form → workflow action - the app's bound workflow runs with the
    # submitted record as payload. Fire-and-forget: a broken workflow never
    # fails the submission itself (the record is already safely stored).
    workflow_triggered = False
    workflow_id = (row.config or {}).get("workflow_id")
    if workflow_id:
        try:
            from ..services.dispatcher import dispatch_execution

            wf = await db.get(Workflow, workflow_id)
            if wf is not None:
                await dispatch_execution(
                    workflow_id,
                    trigger_type="app_form",
                    trigger_payload={"source": "app", "app": row.slug, "app_name": row.name, "record": result["record"]},
                )
                workflow_triggered = True
        except Exception:  # noqa: BLE001 - dispatch must never break the submit
            workflow_triggered = False
    await _audit(db, row, scope, "submit_form")
    return {"ok": True, "row_count": dataset.row_count, "warnings": result["warnings"], "workflow_triggered": workflow_triggered}


@router.get("/{slug}/records")
async def runtime_records(
    slug: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    q: str = Query("", description="Search across all columns (case-insensitive)", max_length=200),
    sort_by: str = Query("", description="Column to sort by", max_length=120),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    page: str = Query("", description="Page/module name (v149 multi-dataset apps)"),
    db: AsyncSession = Depends(get_db),
):
    row = await _runtime_or_404(db, slug)
    scope = await _runtime_scope(row, request, db, "list_records")  # v48
    dataset = await _dataset_for_page(db, row, page or None)
    if dataset is None:
        await _audit(db, row, scope, "list_records", detail="rows=0")
        return {"rows": [], "row_count": 0, "offset": offset, "limit": limit, "columns": [], "relations": {}}
    df = ds_svc.read_parquet_df(ds_svc.parquet_path(dataset.id))
    if scope.scoped and dataset.id == row.dataset_id:  # v48/v149: grants scope the PRIMARY dataset only
        df = grant_svc.apply_scope(df, scope.filter)
    df = app_svc.search_sort_df(df, q, sort_by, sort_dir)  # v46: server-side search+sort
    page = df.iloc[offset : offset + limit]
    relation_lookups = await app_svc.compute_relation_lookups(db, (row.config or {}).get("components", []))  # v141
    await _audit(db, row, scope, "list_records", detail=f"rows={len(df)}")
    return {
        "rows": ds_svc.jsonable_rows(page),
        "row_count": int(len(df)),
        "total_unfiltered": dataset.row_count,
        "offset": offset,
        "limit": limit,
        "columns": [c["name"] for c in (dataset.schema_json or [])],
        "scope": scope.echo(),  # v48
        "relations": relation_lookups,  # v141: field name -> {value: label}
    }


@router.get("/{slug}/export")
async def export_records(
    slug: str,
    request: Request,
    fmt: str = Query("csv", description="csv|xlsx|json"),
    page: str = Query("", description="Page/module name (v149 multi-dataset apps)"),
    db: AsyncSession = Depends(get_db),
):
    """v142: download the app's records directly - scope-safe counterpart to
    /datasets/{id}/export, which always reads the dataset's WHOLE parquet
    and would leak rows outside a grant viewer's slice. v144: xlsx exports
    the APP (a Dashboard sheet mirroring the live stat/chart components,
    plus the styled Data table), not a bare data dump."""
    row = await _runtime_or_404(db, slug)
    scope = await _runtime_scope(row, request, db, "export_records")
    dataset = await _dataset_for_page(db, row, page or None)
    if dataset is None:
        raise HTTPException(status_code=409, detail="App has no dataset bound")
    df = ds_svc.read_parquet_df(ds_svc.parquet_path(dataset.id))
    if scope.scoped and dataset.id == row.dataset_id:  # v48/v149: grants scope the PRIMARY dataset only
        df = grant_svc.apply_scope(df, scope.filter)
    try:
        data, content_type, ext = app_svc.export_records_bytes(row, df, fmt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filename = f"{ds_svc.view_name(row.name)}.{ext}"
    await _audit(db, row, scope, "export_records", detail=f"fmt={fmt} rows={len(df)}")
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{slug}/records", status_code=201)
async def create_record(slug: str, body: AppRecordIn, request: Request, db: AsyncSession = Depends(get_db)):
    row = await _runtime_or_404(db, slug)
    scope = await _runtime_scope(row, request, db, "create_record")  # v48
    page = request.query_params.get("page")  # v149: multi-dataset apps
    dataset = await _dataset_for_page(db, row, page)
    if dataset is None:
        raise HTTPException(status_code=409, detail="App has no dataset bound")
    record, stamp_err = grant_svc.stamp_record(scope.filter, body.record) if dataset.id == row.dataset_id else (body.record, None)  # v48: eq grants stamp (primary dataset only)
    if stamp_err:
        await _audit(db, row, scope, "create_record", outcome="denied", detail=stamp_err)
        raise HTTPException(status_code=403, detail=stamp_err)
    try:
        result = await app_svc.append_record(
            dataset, record, dataset.schema_json or [],
            form=_form_comp(row, page), rules=(row.config or {}).get("rules", []),
            db=db,  # v44: form submissions land on the dataset version timeline
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(dataset)
    await db.commit()
    await db.refresh(dataset)
    await _audit(db, row, scope, "create_record")
    return {"ok": True, "row_count": dataset.row_count, "warnings": result["warnings"]}


@router.patch("/{slug}/records/{index}")
async def edit_record(
    slug: str,
    index: int,
    body: AppRecordIn,
    request: Request,
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _runtime_or_404(db, slug)
    scope = await _runtime_scope(row, request, db, "update_record")  # v48
    _records_mutator_gate(row, user)  # audit hardening
    page = request.query_params.get("page")  # v149: multi-dataset apps
    dataset = await _dataset_for_page(db, row, page)
    if dataset is None:
        raise HTTPException(status_code=409, detail="App has no dataset bound")
    if scope.scoped and dataset.id == row.dataset_id and not _row_in_scope(dataset, index, scope.filter):  # v48/v149
        await _audit(db, row, scope, "update_record", outcome="denied", detail=f"row {index} out of scope")
        raise HTTPException(status_code=404, detail="Record not found")
    try:
        result = await app_svc.update_record(
            dataset, index, body.record,
            form=_form_comp(row, page), rules=(row.config or {}).get("rules", []),
        )
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(dataset)
    await db.commit()
    await db.refresh(dataset)
    await _audit(db, row, scope, "update_record")
    return {"ok": True, "record": result["record"], "row_count": dataset.row_count, "warnings": result["warnings"]}


@router.delete("/{slug}/records/{index}")
async def remove_record(
    slug: str,
    index: int,
    request: Request,
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _runtime_or_404(db, slug)
    scope = await _runtime_scope(row, request, db, "delete_record")  # v48
    _records_mutator_gate(row, user)  # audit hardening
    page = request.query_params.get("page")  # v149: multi-dataset apps
    dataset = await _dataset_for_page(db, row, page)
    if dataset is None:
        raise HTTPException(status_code=409, detail="App has no dataset bound")
    if scope.scoped and dataset.id == row.dataset_id and not _row_in_scope(dataset, index, scope.filter):  # v48/v149
        await _audit(db, row, scope, "delete_record", outcome="denied", detail=f"row {index} out of scope")
        raise HTTPException(status_code=404, detail="Record not found")
    try:
        remaining = await app_svc.delete_record(dataset, index)
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.add(dataset)
    await db.commit()
    await db.refresh(dataset)
    await _audit(db, row, scope, "delete_record")
    return {"ok": True, "row_count": remaining}
