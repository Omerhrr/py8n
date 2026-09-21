# The Py8n Operator SDK (v121)

The operator shelf used to be closed: ten curated operators compiled into
`app/services/operators.py`. The SDK opens it - a business operator is a
**directory with one JSON manifest**, and it rides the SAME marketplace
doors as the compiled ones: the catalog (`GET /api/v1/operators`), the
install plan (`GET /api/v1/operators/{slug}`) and the install
(`POST /api/v1/operators/{slug}/install`, which lands the pack through
the marketplace's own import path and binds it as a Py8nSystem).

## Scaffold one

```bash
python scripts/py8n_new_operator.py cafe-operator --dir ./operators
```

## The manifest

`operators/cafe-operator/operator.json`:

```json
{
  "slug": "cafe-operator",
  "name": "Cafe Operator",
  "tagline": "Orders to the pass - intake, validation, a ledger.",
  "category": "Food",
  "icon": "coffee",
  "color": "#f59e0b",
  "bind_system": true,
  "docs": "What it builds and how to run it.",
  "pack": {
    "workflows": [{"name": "...", "description": "...", "graph": {"nodes": [], "edges": []}}],
    "datasets": [{"name": "cafe_orders", "description": "...",
                  "schema": [{"name": "note", "dtype": "text"}],
                  "rows": [{"note": "hello"}]}]
  }
}
```

Rules the loader enforces (a broken manifest is refused LOUDLY, with the
reason, never silently dropped):

* `slug` matches `^[a-z0-9][a-z0-9-]{1,60}$`; `name` and `tagline` are
  required;
* `pack` (optional but install needs it) carries at least one workflow or
  dataset; every workflow needs a name + a `graph` object; every dataset
  needs a name;
* `bind_system: false` installs the pieces WITHOUT binding a Py8nSystem.

## Install one

```bash
export PY8N_EXTRA_OPERATORS=/path/to/operators   # the PARENT directory
curl -s localhost:8025/api/v1/operators | jq '.operators[-1]'
curl -s -X POST localhost:8025/api/v1/operators/cafe-operator/install
```

The directory is re-read on every catalog call - adding a package is
"save the file, refresh the shelf", no restart. Pack workflows install
inactive-honest (turn them on via the system lifecycle or the workflow
door); datasets land with their sample rows.

## Registry-compatible next step

A manifest whose `pack` mirrors a shelf operator's shape is the
credential to eventually promote a package INTO the compiled shelf -
the same doors, the same install plan, the same discipline.
