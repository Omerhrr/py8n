"""Solution Marketplace API (v60) - gallery + packs, sold as outcomes.

* ``GET    /solutions``                 - the marketplace shelf (q/category filters)
* ``GET    /solutions/{slug}``          - capability checklist + embedded pack summary
* ``POST   /solutions/{slug}/install``  - import the pack into YOUR estate
* ``POST   /solutions``                 - author a solution from your own content
* ``DELETE /solutions/{slug}``          - unlist an authored solution (curator only)

Installing reuses the exact pack-import machinery (``_import_pack_doc``):
every workflow lands inactive, datasets carry their sample rows, and the
response returns the created refs. The shelf self-seeds the three curated
showcase solutions on first read (idempotent by slug).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user, own_or_404
from ..db import get_db
from ..models import Dataset, Solution, Workflow
from ..api.packs import PackDocument, _import_pack_doc
from ..services.solutions import (
    MODEL_SYSTEM_MODALITIES,
    ensure_seeded,
    finalize_pack_dataset_names,
    finalize_pack_model_names,
    pack_summary,
    solution_summary,
)

router = APIRouter(prefix="/solutions", tags=["solutions"])

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,120}$")


def _find_installed(created: list[dict], declared_name: str) -> str | None:
    """Resolve an installed object id by its pack-declared name.

    Dataset names are finalized at install (suffixed on collision), so the
    match is exact-first, then declared-name prefix ('faq 2' after 'faq').
    """
    if not declared_name:
        return created[0]["id"] if created else None
    for row in created:
        if row.get("name") == declared_name:
            return row["id"]
    for row in created:
        if str(row.get("name") or "").startswith(declared_name):
            return row["id"]
    return created[0]["id"] if created else None


class SolutionInstallRequest(BaseModel):
    note: str = Field(default="", max_length=300, description="Optional install note for the response")
    as_system: bool = Field(default=False, description="v61: also create a Py8n System binding everything this install created")
    as_model_system: bool = Field(default=False, description="v64: also create a Model System (datasets + training/serving workflows as one operating unit)")
    as_voice_agent: bool = Field(default=False, description="v72: also create a Voice Agent bound to the installed handler + knowledge dataset (one-click phone agent)")
    brain: str = Field(default="scaffold", description="v73: voice-agent brain - 'scaffold' (deterministic knowledge handler) or 'ai_agent' (LLM brain scaffolded over the SAME installed knowledge dataset)")


class SolutionAuthorRequest(BaseModel):
    name: str = Field(..., min_length=3, max_length=140)
    tagline: str = Field(default="", max_length=300)
    category: str = Field(default="Operations", max_length=60)
    icon: str = Field(default="package", max_length=60)
    color: str = Field(default="#22d3ee", max_length=20)
    outcomes: list[str] = Field(..., min_length=1, max_length=12,
                                description="The capability checklist - what the user GETS")
    docs: str = Field(default="", max_length=4000)
    workflow_ids: list[str] = Field(default_factory=list, max_length=20)
    dataset_ids: list[str] = Field(default_factory=list, max_length=20)
    include_rows: bool = Field(default=True, description="Bundle sample rows for datasets")


async def _get_solution(db: AsyncSession, slug: str) -> Solution:
    row = (await db.execute(select(Solution).where(Solution.slug == slug))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Solution not found")
    return row


@router.get("")
async def list_solutions(
    q: str = "",
    category: str = "",
    user=Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    await ensure_seeded(db)
    await db.commit()
    rows = (await db.execute(select(Solution).order_by(Solution.installs.desc(), Solution.created_at.desc()))).scalars().all()
    needle = (q or "").strip().lower()
    out = []
    for s in rows:
        if category and s.category.lower() != category.strip().lower():
            continue
        if needle and needle not in f"{s.name} {s.tagline} {s.category} {' '.join(s.outcomes_json or [])}".lower():
            continue
        out.append(solution_summary(s))
    categories = sorted({s.category for s in rows})
    return {"solutions": out, "categories": categories, "total": len(out)}


@router.get("/{slug}")
async def solution_detail(slug: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    await ensure_seeded(db)
    await db.commit()
    s = await _get_solution(db, slug)
    return {**solution_summary(s), "docs": s.docs, "pack": pack_summary(s)}


@router.post("/{slug}/install")
async def install_solution(slug: str, body: SolutionInstallRequest | None = None,
                           user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Import the solution's pack into your estate (workflows inactive)."""
    await ensure_seeded(db)
    await db.commit()
    s = await _get_solution(db, slug)
    owner = user.id if user else None
    pack_dict = await finalize_pack_dataset_names(db, s.pack_json or {})
    pack_dict = await finalize_pack_model_names(db, pack_dict, owner)
    pack = PackDocument.model_validate(pack_dict)
    result = await _import_pack_doc(pack, owner, db)
    s.installs = int(s.installs or 0) + 1

    system_ref = None
    if body and body.as_system:
        from ..models import Py8nSystem, SystemComponent

        sys_row = Py8nSystem(
            name=f"{s.name} system",
            description=f"Installed from the '{s.name}' solution - " + (body.note or s.tagline or "")[:400],
            icon=s.icon, color=s.color,
        )
        sys_row.owner_id = owner
        db.add(sys_row)
        await db.flush()
        for wf in result.get("workflows", []):
            db.add(SystemComponent(system_id=sys_row.id, kind="workflow", ref_id=wf["id"]))
        for ds in result.get("datasets", []):
            db.add(SystemComponent(system_id=sys_row.id, kind="dataset", ref_id=ds["id"]))
        await db.flush()
        system_ref = {"id": sys_row.id, "name": sys_row.name}

    model_system_ref = None
    if body and body.as_model_system:
        from ..models import ModelSystem, ModelSystemComponent
        from ..services.model_systems import MODALITY_NODE_TYPES

        # declared modalities for curated model solutions, otherwise derived
        # from the pack's own node-type evidence (fail-honest fallback: text
        # is NOT assumed - a pack with no modality nodes declares none)
        declared = list(MODEL_SYSTEM_MODALITIES.get(s.slug, []))
        if not declared:
            evidence: set[str] = set()
            for w in (s.pack_json or {}).get("workflows", []):
                for n in (w.get("graph") or {}).get("nodes", []):
                    mod = MODALITY_NODE_TYPES.get(n.get("type") or "")
                    if mod:
                        evidence.add(mod)
            declared = sorted(evidence)
        ms_row = ModelSystem(
            name=f"{s.name} model system",
            description=f"Installed from the '{s.name}' solution - " + (body.note or s.tagline or "")[:400],
            icon=s.icon if s.icon != "package" else "brain-circuit",
            color=s.color,
            modalities=declared,
        )
        ms_row.owner_id = owner
        db.add(ms_row)
        await db.flush()
        for wf in result.get("workflows", []):
            db.add(ModelSystemComponent(model_system_id=ms_row.id, kind="workflow", ref_id=wf["id"]))
        for ds in result.get("datasets", []):
            db.add(ModelSystemComponent(model_system_id=ms_row.id, kind="dataset", ref_id=ds["id"]))
        await db.flush()
        model_system_ref = {"id": ms_row.id, "name": ms_row.name, "modalities": declared}

    voice_agent_ref = None
    if body and body.as_voice_agent:
        from ..models import VoiceAgent
        from ..services import voice_agents as va_svc

        spec = (s.pack_json or {}).get("voice_agent") or {}
        if not spec:
            raise HTTPException(status_code=400, detail="this solution does not declare a voice agent pack")
        # resolve the INSTALLED objects: the handler workflow and the knowledge
        # dataset (final names may be suffixed - match by declared-name prefix)
        kb_decl = spec.get("knowledge") or {}
        handler_id = _find_installed(result.get("workflows", []),
                                     "Voice Agent Handler")
        if not handler_id and result.get("workflows"):
            handler_id = result["workflows"][0]["id"]  # single-workflow packs
        dataset_id = _find_installed(result.get("datasets", []),
                                     kb_decl.get("dataset") or "")
        speech = spec.get("speech") or {}
        brain = (body.brain or "scaffold").strip()
        if brain not in va_svc.BRAINS:
            raise HTTPException(status_code=400,
                                detail=f"brain must be {'|'.join(va_svc.BRAINS)}, got {brain!r}")
        try:
            # v73: brain='ai_agent' scaffolds a FRESH LLM-brain handler over the
            # SAME installed knowledge dataset (the pack's deterministic handler
            # still installs for comparison); brain='scaffold' binds it directly.
            use_pack_handler = brain == "scaffold"
            va = await va_svc.create_agent(
                db, owner_id=owner,
                name=f"{s.name} {spec.get('name_suffix') or 'phone agent'}"[:140],
                description=f"Installed from the '{s.name}' solution - " + (body.note or s.tagline or "")[:400],
                greeting_text=spec.get("greeting_text") or "",
                system_prompt=spec.get("system_prompt") or "",
                handler_workflow_id=handler_id if use_pack_handler else None,
                scaffold_handler=not use_pack_handler,
                knowledge_dataset_id=dataset_id,
                knowledge_text_column=kb_decl.get("text_column"),
                knowledge_answer_column=kb_decl.get("answer_column"),
                knowledge_top_k=1,
                asr_provider=speech.get("asr_provider") or "py8n_local",
                tts_provider=speech.get("tts_provider") or "openai_tts",
                tts_voice=speech.get("tts_voice") or "alloy",
                tts_format=speech.get("tts_format") or "wav",
                language=speech.get("language") or "en-US",
                barge_in=bool(speech.get("barge_in", True)),
                brain=brain)
        except va_svc.VoiceAgentError as exc:
            raise HTTPException(status_code=400, detail=f"voice agent install failed: {exc}") from exc
        voice_agent_ref = {"id": va["id"], "name": va["name"],
                           "handler_workflow_id": va["handler_workflow_id"],
                           "knowledge": va["knowledge"],
                           "wiring": va["wiring"]}

    await db.commit()
    return {
        "slug": s.slug,
        "name": s.name,
        "installs": s.installs,
        "note": body.note if body else "",
        "created_workflows": result.get("workflows", []),
        "created_datasets": result.get("datasets", []),
        "skipped": result.get("skipped", []),
        "warnings": result.get("warnings", []),
        "system": system_ref,
        "model_system": model_system_ref,
        "voice_agent": voice_agent_ref,
    }


@router.post("", status_code=201)
async def author_solution(body: SolutionAuthorRequest, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Publish your own workflows/datasets as an outcome-named solution."""
    if not body.workflow_ids and not body.dataset_ids:
        raise HTTPException(status_code=400, detail="Pick at least one workflow or dataset to publish")

    workflows: list[dict] = []
    for wid in dict.fromkeys(body.workflow_ids):
        wf = await db.get(Workflow, wid)
        if wf is None:
            raise HTTPException(status_code=404, detail=f"Workflow {wid} not found")
        own_or_404(wf.owner_id, user)
        workflows.append({"name": wf.name, "description": wf.description or "",
                          "graph": wf.graph or {"nodes": [], "edges": []}})

    datasets: list[dict] = []
    for did in dict.fromkeys(body.dataset_ids):
        ds = await db.get(Dataset, did)
        if ds is None:
            raise HTTPException(status_code=404, detail=f"Dataset {did} not found")
        own_or_404(ds.owner_id, user)
        rows: list = []
        if body.include_rows and ds.row_count:
            from ..services import datasets as ds_svc

            df = ds_svc.read_parquet_df(ds_svc.parquet_path(ds.id))
            rows = ds_svc.jsonable_rows(df)[:500]
        datasets.append({"name": ds.name, "description": ds.description or "",
                         "schema": ds.schema_json or [], "rows": rows})

    slug = re.sub(r"[^a-z0-9]+", "-", body.name.strip().lower()).strip("-")[:120] or "solution"
    if not SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Could not derive a valid slug from the name")
    if (await db.execute(select(Solution).where(Solution.slug == slug))).scalar_one_or_none():
        import uuid as _uuid

        slug = f"{slug}-{_uuid.uuid4().hex[:6]}"

    outcomes = [o.strip()[:200] for o in body.outcomes if o and o.strip()]
    row = Solution(
        slug=slug, name=body.name.strip(), tagline=body.tagline.strip(),
        category=body.category.strip() or "Operations", icon=body.icon, color=body.color,
        outcomes_json=outcomes,
        pack_json={"format": "py8n-pack", "pack_version": 1,
                   "workflows": workflows, "datasets": datasets},
        docs=body.docs, installs=0,
    )
    row.owner_id = user.id if user else None
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return solution_summary(row)


@router.delete("/{slug}", status_code=204)
async def delete_solution(slug: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    s = await _get_solution(db, slug)
    own_or_404(s.owner_id, user)  # NULL-owner curated rows stay 404 for everyone
    await db.delete(s)
    await db.commit()
