"""Folder CRUD (v16) - hierarchical grouping for workflows.

Folders nest up to MAX_FOLDER_DEPTH levels. Integrity (parent existence,
cycles, depth) is enforced here in the API layer with a cycle-safe ancestor
walk rather than DB FKs - the same pattern as error-workflow bindings.

Delete policy: refused while subfolders exist (409). Workflows inside the
deleted folder fall back to the root (folder_id=None) - nothing is destroyed.
"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Folder, Workflow
from ..schemas import FolderCreate, FolderOut, FolderUpdate

router = APIRouter(prefix="/folders", tags=["folders"])

MAX_FOLDER_DEPTH = 3  # root folder = depth 1; children 2; grandchildren 3


def _clean_name(name: str) -> str:
    """Collapse whitespace, forbid empty-after-trim names."""
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise HTTPException(status_code=400, detail="Folder name cannot be empty")
    return cleaned[:120]


async def _ancestor_chain(db: AsyncSession, folder_id: str) -> list[Folder]:
    """Folder → root ancestor list (oldest first). Cycle-safe (visited set)."""
    chain: list[Folder] = []
    seen: set[str] = set()
    cursor: str | None = folder_id
    while cursor:
        if cursor in seen:  # corrupt data guard - never loop forever
            break
        seen.add(cursor)
        row = await db.get(Folder, cursor)
        if row is None:
            break
        chain.append(row)
        cursor = row.parent_id
    return chain


async def _descendant_ids(db: AsyncSession, root_id: str) -> set[str]:
    """All folder ids below root_id (inclusive). Cycle-safe."""
    rows = (await db.execute(select(Folder))).scalars().all()
    children_of: dict[str | None, list[str]] = {}
    for f in rows:
        children_of.setdefault(f.parent_id, []).append(f.id)
    out: set[str] = set()
    stack = [root_id]
    while stack:
        fid = stack.pop()
        if fid in out:
            continue  # cycle guard
        out.add(fid)
        stack.extend(children_of.get(fid, []))
    return out


async def _folder_out(db: AsyncSession, folder: Folder, wf_counts: Counter | None = None) -> FolderOut:
    """Serialize one folder with direct + recursive workflow counts."""
    if wf_counts is None:
        wf_counts = Counter()
        for wf_id in (await db.execute(select(Workflow.folder_id))).scalars():
            if wf_id:
                wf_counts[wf_id] += 1
    descendants = await _descendant_ids(db, folder.id)
    return FolderOut(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        workflow_count=wf_counts.get(folder.id, 0),
        total_count=sum(wf_counts.get(fid, 0) for fid in descendants),
        created_at=folder.created_at,
        updated_at=folder.updated_at,
    )


async def _validate_parent(db: AsyncSession, parent_id: str | None) -> Folder | None:
    if not parent_id:
        return None
    parent = await db.get(Folder, parent_id)
    if parent is None:
        raise HTTPException(status_code=400, detail="Parent folder not found")
    return parent


@router.get("", response_model=list[FolderOut])
async def list_folders(db: AsyncSession = Depends(get_db)):
    """All folders with direct + recursive workflow counts (dashboard chips)."""
    rows = (await db.execute(select(Folder).order_by(Folder.name))).scalars().all()
    wf_counts: Counter = Counter()
    for wf_id in (await db.execute(select(Workflow.folder_id))).scalars():
        if wf_id:
            wf_counts[wf_id] += 1
    return [await _folder_out(db, f, wf_counts) for f in rows]


@router.get("/{folder_id}", response_model=FolderOut)
async def get_folder(folder_id: str, db: AsyncSession = Depends(get_db)):
    """Single folder detail with counts (edit dialogs / verification)."""
    folder = await db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    return await _folder_out(db, folder)


@router.post("", response_model=FolderOut, status_code=201)
async def create_folder(body: FolderCreate, db: AsyncSession = Depends(get_db)):
    parent = await _validate_parent(db, body.parent_id)
    if parent is not None:
        # _ancestor_chain includes the parent itself → its length IS the
        # parent's depth; the new folder sits one level below it.
        parent_depth = len(await _ancestor_chain(db, parent.id))
        if parent_depth + 1 > MAX_FOLDER_DEPTH:  # +1 for the folder being created
            raise HTTPException(
                status_code=400,
                detail=f"Folder nesting is limited to {MAX_FOLDER_DEPTH} levels",
            )
    folder = Folder(name=_clean_name(body.name), parent_id=parent.id if parent else None)
    db.add(folder)
    await db.commit()  # explicit - teardown commit races follow-up reads
    await db.refresh(folder)
    return await _folder_out(db, folder)


@router.patch("/{folder_id}", response_model=FolderOut)
async def update_folder(folder_id: str, body: FolderUpdate, db: AsyncSession = Depends(get_db)):
    folder = await db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found")

    if body.name is not None:
        folder.name = _clean_name(body.name)

    if "parent_id" in body.model_dump(exclude_unset=True):
        new_parent_id = body.parent_id or None  # "" moves to root
        if new_parent_id is not None:
            if new_parent_id == folder.id:
                raise HTTPException(status_code=400, detail="A folder cannot be its own parent")
            parent = await _validate_parent(db, new_parent_id)
            # Cycle guard: the new parent must not be a descendant of the folder.
            subtree = await _descendant_ids(db, folder.id)
            if new_parent_id in subtree:
                raise HTTPException(status_code=400, detail="Cannot move a folder into its own subtree")
            # Depth guard: subtree height + new ancestor chain ≤ MAX_FOLDER_DEPTH.
            rows = (await db.execute(select(Folder))).scalars().all()
            children_of: dict[str | None, list[str]] = {}
            for f in rows:
                children_of.setdefault(f.parent_id, []).append(f.id)

            def _height(fid: str, seen: set[str] | None = None) -> int:
                seen = seen or set()
                if fid in seen:
                    return 0
                seen.add(fid)
                kids = children_of.get(fid, [])
                return 1 + max((_height(k, seen) for k in kids), default=0)

            parent_chain_len = len(await _ancestor_chain(db, parent.id))  # parent → root
            subtree_height = _height(folder.id)
            if parent_chain_len + subtree_height > MAX_FOLDER_DEPTH:
                raise HTTPException(
                    status_code=400,
                    detail=f"Folder nesting is limited to {MAX_FOLDER_DEPTH} levels",
                )
        folder.parent_id = new_parent_id

    await db.commit()
    await db.refresh(folder)
    return await _folder_out(db, folder)


@router.delete("/{folder_id}", status_code=204)
async def delete_folder(folder_id: str, db: AsyncSession = Depends(get_db)):
    folder = await db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    children = (
        await db.execute(select(Folder.id).where(Folder.parent_id == folder_id))
    ).scalars().all()
    if children:
        raise HTTPException(
            status_code=409,
            detail="Folder has subfolders - delete or move them first",
        )
    # Workflows inside fall back to the root (folder_id=None).
    workflows = (
        await db.execute(select(Workflow).where(Workflow.folder_id == folder_id))
    ).scalars().all()
    for wf in workflows:
        wf.folder_id = None
    await db.delete(folder)
    await db.commit()  # explicit - teardown commit races follow-up reads
