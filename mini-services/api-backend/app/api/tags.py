"""Global tag inventory (v44) - one namespace across workflows AND datasets.

Tags already existed per-resource (workflow.tags since v20, dataset.tags in
v44); this router treats them as ONE estate-wide vocabulary:

  GET    /tags            inventory with usage counts ({name, workflows, datasets})
  PUT    /tags/rename     {from, to} renames the tag on every resource that
                          carries it (case-insensitive match, keeps casing of
                          other tags, dedupes when the rename merges two tags)
  DELETE /tags/{tag}      strips the tag from every resource (case-insensitive)

Both mutations return per-resource counts so the UI can confirm the sweep.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_optional_user
from ..db import get_db
from ..models import Dataset, Workflow

router = APIRouter(prefix="/tags", tags=["tags"])

MAX_TAG_LEN = 60


def _clean(value: str) -> str:
    tag = (value or "").strip()[:MAX_TAG_LEN]
    if not tag:
        raise HTTPException(status_code=400, detail="Tag cannot be empty")
    return tag


async def _load_resources(db: AsyncSession):
    wf_rows = (await db.execute(select(Workflow))).scalars().all()
    ds_rows = (await db.execute(select(Dataset))).scalars().all()
    return wf_rows, ds_rows


@router.get("")
async def tag_inventory(user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Every tag in the estate with its per-kind usage counts."""
    wf_rows, ds_rows = await _load_resources(db)
    counts: dict[str, dict[str, int]] = {}
    for w in wf_rows:
        for t in w.tags or []:
            entry = counts.setdefault(t, {"name": t, "workflows": 0, "datasets": 0})
            entry["workflows"] += 1
    for d in ds_rows:
        for t in d.tags or []:
            entry = counts.setdefault(t, {"name": t, "workflows": 0, "datasets": 0})
            entry["datasets"] += 1
    return sorted(counts.values(), key=lambda e: (-(e["workflows"] + e["datasets"]), e["name"].lower()))


class TagRename(BaseModel):
    from_tag: str = Field(min_length=1, max_length=MAX_TAG_LEN, alias="from")
    to_tag: str = Field(min_length=1, max_length=MAX_TAG_LEN, alias="to")

    model_config = {"populate_by_name": True}


@router.put("/rename")
async def rename_tag(body: TagRename, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Rename a tag everywhere. Matching is case-insensitive; the replacement
    keeps other tags untouched and dedupes if the rename merges tags."""
    old = _clean(body.from_tag)
    new = _clean(body.to_tag)
    if old.lower() == new.lower():
        raise HTTPException(status_code=400, detail="Old and new tag are the same")

    wf_rows, ds_rows = await _load_resources(db)
    wf_hit = ds_hit = 0
    for w in wf_rows:
        tags = w.tags or []
        if any(t.lower() == old.lower() for t in tags):
            merged = [t for t in tags if t.lower() != old.lower() and t.lower() != new.lower()]
            w.tags = [*merged, new]
            wf_hit += 1
    for d in ds_rows:
        tags = d.tags or []
        if any(t.lower() == old.lower() for t in tags):
            merged = [t for t in tags if t.lower() != old.lower() and t.lower() != new.lower()]
            d.tags = [*merged, new]
            ds_hit += 1
    await db.commit()
    return {"from": old, "to": new, "workflows": wf_hit, "datasets": ds_hit}


@router.delete("/{tag}")
async def delete_tag(tag: str, user=Depends(get_optional_user), db: AsyncSession = Depends(get_db)):
    """Strip one tag from every resource that carries it."""
    target = _clean(tag)
    wf_rows, ds_rows = await _load_resources(db)
    wf_hit = ds_hit = 0
    for w in wf_rows:
        tags = w.tags or []
        if any(t.lower() == target.lower() for t in tags):
            w.tags = [t for t in tags if t.lower() != target.lower()]
            wf_hit += 1
    for d in ds_rows:
        tags = d.tags or []
        if any(t.lower() == target.lower() for t in tags):
            d.tags = [t for t in tags if t.lower() != target.lower()]
            ds_hit += 1
    await db.commit()
    return {"tag": target, "workflows": wf_hit, "datasets": ds_hit}
