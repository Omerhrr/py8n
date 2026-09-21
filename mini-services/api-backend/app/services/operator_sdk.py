"""The operator SDK (v121) - the shelf opens.

Until v121 the operator shelf was CLOSED: ten curated operators compiled
into ``operators.py``, installable but not extendable without a fork.
The SDK opens the shelf: drop a package directory next to py8n, point
``PY8N_EXTRA_OPERATORS`` at its parent, and the operator rides the SAME
marketplace doors - the catalog, the install plan, the install - beside
the compiled ones.

A package is a directory holding ``operator.json``::

    {
      "slug": "cafe-operator",              # required, [a-z0-9-]+
      "name": "Cafe Operator",              # required
      "tagline": "Orders to the pass",      # required
      "category": "Food",                   # optional (default "Custom")
      "icon": "coffee", "color": "#f59e0b", # optional
      "bind_system": true,                  # optional: land a Py8nSystem
      "docs": "one paragraph for the shelf card",
      "pack": {                             # optional but install needs it
        "workflows": [{"name": "...", "description": "...", "graph": {...}}],
        "datasets":  [{"name": "...", "description": "...",
                       "schema": [...], "rows": [...]}]
      }
    }

The pack installs through the SAME ``_import_pack_doc`` the marketplace
solutions use (workflows inactive-honest, datasets with rows) - zero
parallel install machinery. Nothing here is hot-reloaded mid-request:
the directory is re-read on every catalog call, so adding a package is
"save the file, refresh the shelf".
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
MANIFEST_NAME = "operator.json"


def extra_operators_dir() -> Path | None:
    """The directory ``PY8N_EXTRA_OPERATORS`` points at - re-read per call,
    so pointing it at a fresh package needs no restart."""
    raw = (os.environ.get("PY8N_EXTRA_OPERATORS") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_dir() else None


def load_extra_operators() -> tuple[list[dict], list[dict]]:
    """Every valid SDK operator package, plus the honest refusals.

    Returns ``(operators, skipped)`` - ``skipped`` carries
    ``{"package": ..., "reason": ...}`` for anything malformed, because a
    typo in a third-party manifest must be visible, never silent.
    """
    root = extra_operators_dir()
    if root is None:
        return [], []
    operators: list[dict] = []
    skipped: list[dict] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / MANIFEST_NAME
        if not manifest_path.is_file():
            continue  # a stray directory is not a package - quiet
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            skipped.append({"package": child.name,
                            "reason": f"unreadable manifest: {exc}"})
            continue
        problem = _validate_manifest(manifest)
        if problem:
            skipped.append({"package": child.name, "reason": problem})
            continue
        operators.append(_normalize(manifest))
    return operators, skipped


def find_extra_operator(slug: str) -> dict | None:
    slug = (slug or "").strip()
    if not slug:
        return None
    operators, _ = load_extra_operators()
    for op in operators:
        if op["slug"] == slug:
            return op
    return None


def _validate_manifest(manifest: object) -> str | None:
    if not isinstance(manifest, dict):
        return "manifest must be a JSON object"
    slug = str(manifest.get("slug") or "")
    if not SLUG_RE.match(slug):
        return f"slug {slug!r} must match {SLUG_RE.pattern}"
    for key in ("name", "tagline"):
        if not str(manifest.get(key) or "").strip():
            return f"{key} is required"
    pack = manifest.get("pack")
    if pack is not None:
        if not isinstance(pack, dict):
            return "pack must be an object"
        workflows = pack.get("workflows") or []
        datasets = pack.get("datasets") or []
        if not workflows and not datasets:
            return "pack must carry at least one workflow or dataset"
        for w in workflows:
            if not isinstance(w, dict) or not str(w.get("name") or "").strip():
                return "every pack workflow needs a name"
            if not isinstance(w.get("graph"), dict):
                return f"pack workflow {w.get('name')!r} needs a graph object"
        for d in datasets:
            if not isinstance(d, dict) or not str(d.get("name") or "").strip():
                return "every pack dataset needs a name"
    return None


def _normalize(manifest: dict) -> dict:
    pack = manifest.get("pack") or {}
    workflows = list(pack.get("workflows") or [])
    datasets = list(pack.get("datasets") or [])
    return {
        "slug": str(manifest["slug"]),
        "name": str(manifest["name"]).strip(),
        "tagline": str(manifest["tagline"]).strip(),
        "category": str(manifest.get("category") or "Custom").strip() or "Custom",
        "icon": str(manifest.get("icon") or "package"),
        "color": str(manifest.get("color") or "#71717a"),
        "description": str(manifest.get("docs") or ""),
        "bind_system": manifest.get("bind_system", True) is not False,
        "source": "sdk",
        "pack": pack,
        "topology": {
            "datasets": len(datasets),
            "workflows": len(workflows),
            "agents": 0, "rooms": 0, "queues": 0, "campaign": 0,
            "processes": 0, "dashboard": 0,
        },
    }


async def sdk_install(db, slug: str, owner_id: str | None,
                      note: str = "") -> dict:
    """Install an SDK operator through the pack path (the SAME
    _import_pack_doc the marketplace solutions ride - zero parallel
    install machinery), then bind the landed pieces as a Py8nSystem
    when the manifest asks for it (the default: an operator IS a
    system)."""
    import pandas as pd

    from ..api.packs import PackDocument, _import_pack_doc
    from ..models import Py8nSystem, SystemComponent
    from pydantic import ValidationError

    op = find_extra_operator(slug)
    if op is None:
        raise OperatorSdkError(f"unknown SDK operator {slug!r}")
    if not op["pack"]:
        raise OperatorSdkError(
            f"SDK operator {slug!r} declares no pack - nothing to install")

    try:
        pack = PackDocument.model_validate({
            "format": "py8n-pack", "pack_version": 1, **op["pack"]})
    except ValidationError as exc:
        raise OperatorSdkError(f"invalid pack: {exc}") from exc

    result = await _import_pack_doc(pack, owner_id, db)

    system_ref = None
    if op["bind_system"] and (result.get("workflows") or result.get("datasets")):
        description = (note or op["description"]
                       or f"Installed from the SDK operator {op['slug']!r}")[:400]
        sys_row = Py8nSystem(name=op["name"], description=description,
                             icon=op["icon"], color=op["color"])
        sys_row.owner_id = owner_id
        db.add(sys_row)
        await db.flush()
        for wf in result.get("workflows", []):
            db.add(SystemComponent(system_id=sys_row.id, kind="workflow",
                                   ref_id=wf["id"]))
        for ds in result.get("datasets", []):
            db.add(SystemComponent(system_id=sys_row.id, kind="dataset",
                                   ref_id=ds["id"]))
        await db.flush()
        system_ref = {"id": sys_row.id, "name": sys_row.name}

    return {
        "slug": op["slug"], "source": "sdk",
        "installed": {
            "workflows": [{"id": w["id"], "name": w.get("name")}
                          for w in result.get("workflows", [])],
            "datasets": [{"id": d["id"], "name": d.get("name")}
                         for d in result.get("datasets", [])],
        },
        "skipped": result.get("skipped", []),
        "system": system_ref,
    }


class OperatorSdkError(Exception):
    """An SDK-level refusal (unknown slug, no pack, invalid manifest)."""
