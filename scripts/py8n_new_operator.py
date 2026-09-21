#!/usr/bin/env python3
"""Scaffold a new Py8n SDK operator package (v121).

Usage:
    python scripts/py8n_new_operator.py my-idea [--dir ~/py8n-operators]

Creates ``<dir>/<slug>/operator.json`` (a valid SDK manifest with a tiny
working pack: one manual-trigger workflow writing to one dataset) and a
README. Point ``PY8N_EXTRA_OPERATORS`` at ``<dir>`` and refresh the
marketplace shelf - the operator rides the same catalog and install doors
as the compiled ones.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MANIFEST_TEMPLATE = {
    "slug": "SLUG",
    "name": "NAME",
    "tagline": "One line for the shelf card.",
    "category": "Custom",
    "icon": "package",
    "color": "#f59e0b",
    "bind_system": True,
    "docs": ("Describe what this operator builds, how to run its workflow, "
             "and what lands in its dataset."),
    "pack": {
        "workflows": [
            {
                "name": "SLUG intake",
                "description": "Manual trigger -> validate -> land a row.",
                "graph": {
                    "nodes": [
                        {"id": "m", "type": "manual_trigger", "name": "Intake",
                         "position": {"x": 0, "y": 0}, "parameters": {}},
                        {"id": "s", "type": "set_variable", "name": "Row",
                         "position": {"x": 220, "y": 0},
                         "parameters": {"assignments": {"note": "hello"},
                                        "keep_input": False}},
                        {"id": "w", "type": "dataset_write", "name": "Ledger",
                         "position": {"x": 440, "y": 0},
                         "parameters": {"dataset": "SLUG_notes",
                                        "mode": "append"}},
                    ],
                    "edges": [
                        {"id": "e1", "source": "m", "target": "s"},
                        {"id": "e2", "source": "s", "target": "w"},
                    ],
                },
            },
        ],
        "datasets": [
            {
                "name": "SLUG_notes",
                "description": "Every intake row.",
                "schema": [{"name": "note", "dtype": "text"}],
                "rows": [{"note": "hello"}],
            },
        ],
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="the operator slug, e.g. cafe-operator")
    parser.add_argument("--dir", default=".",
                        help="parent directory for the package (default .)")
    parser.add_argument("--name", default="", help="display name (default title-cased slug)")
    args = parser.parse_args()

    slug = args.slug.strip().lower()
    if not re.match(r"^[a-z0-9][a-z0-9-]{1,60}$", slug):
        print(f"error: slug {slug!r} must match ^[a-z0-9][a-z0-9-]{{1,60}}$", file=sys.stderr)
        return 1
    name = args.name or " ".join(w.capitalize() for w in slug.split("-")) + " Operator"

    package = Path(args.dir).expanduser() / slug
    if package.exists():
        print(f"error: {package} already exists", file=sys.stderr)
        return 1
    package.mkdir(parents=True)

    manifest = json.loads(json.dumps(MANIFEST_TEMPLATE).replace("SLUG", slug))
    manifest["name"] = name
    (package / "operator.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (package / "README.md").write_text(
        f"# {name}\n\n{manifest['docs']}\n\n"
        "## Install\n\nPoint `PY8N_EXTRA_OPERATORS` at this directory's "
        "parent, then `POST /api/v1/operators/" + slug + "/install`.\n",
        encoding="utf-8")
    print(f"scaffolded {package}/operator.json - point PY8N_EXTRA_OPERATORS "
          f"at {package.parent} and refresh the shelf")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
