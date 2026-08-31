# 🐍 Py8n - a Python-native n8n

**Visual workflow automation, rebuilt on Python.** Drag nodes on a canvas, wire
them up, and Py8n evaluates the graph with `graphlib.TopologicalSorter`, a
Jinja2-templated execution context and a Pydantic-validated node registry -
then streams live progress back to the browser over WebSockets.

```
┌────────────────────────────────────────────────────────────────────────┐
│  Nuxt 3 + Vue Flow + Pinia (canvas UI)                                 │
├────────────────────────────────────────────────────────────────────────┤
│  FastAPI  (REST + WebSocket + APScheduler + webhook catcher)           │
│  ├── Py8n engine: GraphSpec → TopologicalSorter → Jinja2 → nodes       │
│  ├── Dispatcher ── inline (sandbox)  or  Celery + Redis (production)   │
│  └── Event bus   ── in-memory         or  Redis pub/sub                │
├────────────────────────────────────────────────────────────────────────┤
│  SQLAlchemy 2.0  ──  SQLite (dev)  /  PostgreSQL + JSONB (production)  │
│  Fernet-encrypted credential vault                                     │
└────────────────────────────────────────────────────────────────────────┘
```

## Node ecosystem (9 built-ins)

| Node | Category | What it does |
|---|---|---|
| `manual_trigger` | Triggers | ▶ Run button; injects a test payload |
| `webhook_trigger` | Triggers | `POST /api/v1/webhooks/{workflow_id}`; respond immediately or with the last node's output |
| `schedule_trigger` | Triggers | APScheduler interval or CRON, synced from the canvas |
| `http_request` | Actions | Any REST call; optional header-auth credential |
| `if_condition` | Logic | 9 operators → `true`/`false` branches (inactive branch = downstream skipped) |
| `set_variable` | Logic | Build objects from `{{ expressions }}` |
| `code` | Logic | Sandboxed Python (`input_data`, `nodes`, `result`) |
| `delay` | Logic | Pause the branch |
| `llm_chat` | AI | Free built-in bridge **or** your own OpenAI-compatible credential |

Node configuration forms are **generated from the backend's Pydantic JSON
schemas** (`GET /api/v1/node-definitions`) - add a node class in Python and it
appears in the UI palette with a working form, zero frontend changes.

## Expressions

Anywhere in node parameters:

```
{{ nodes.http_1.output.body.email }}
Hello {{ nodes.trigger_1.output.payload.name | upper }}!
{{ nodes.if_1.output.true.condition }}
```

A string that is exactly one `{{ … }}` keeps its native type (dicts, lists,
numbers). Unknown names fail loudly with `TemplateResolutionError`.

## Layout

```
mini-services/
  api-backend/          FastAPI app (engine, API, scheduler, worker, tests)
    app/engine/         GraphSpec · TopologicalSorter runner · Jinja2 · nodes
    app/api/            workflows · executions · webhooks · credentials · ws
    app/services/       dispatcher (inline↔Celery) · event bus · crypto · scheduler
    demo/phase1_demo.py standalone milestone script (3-node mock graph)
    tests/              pytest engine suite
  llm-bridge/           Bun service - free OpenAI-compatible shim for the sandbox
pages/                  dashboard + workflow editor (Vue Flow canvas)
stores/py8n.ts          Pinia store (definitions, execution progress, WS)
server/routes/api/v1/   Nitro proxy fallback for non-gateway deploys
docker-compose.yml      postgres + redis + api + worker + frontend + caddy
```

## Running

### Sandbox / single process (already wired)
- Frontend: `bun run dev` (Nuxt on :3000)
- Backend: `bash mini-services/api-backend/start.sh` (FastAPI on :8000)
- AI bridge: `cd mini-services/llm-bridge && bun run index.ts` (:3010)
- Container boot runs `.zscripts/dev.sh`, which starts all three automatically.

### Production cluster
```bash
docker compose up --build
# → http://localhost:8025
```
PostgreSQL, Redis, FastAPI, Celery workers (×4 concurrency), Nuxt and Caddy in
one command. `PY8N_EXECUTION_MODE=celery` moves executions off the API process;
events fan out over Redis pub/sub so any replica can stream WebSocket progress.

## API quick reference

```
GET    /api/v1/health
GET    /api/v1/node-definitions          ← Pydantic schemas for the UI
CRUD   /api/v1/workflows                 ← JSONB graph documents
POST   /api/v1/workflows/{id}/run        ← manual dispatch (202)
GET    /api/v1/workflows/{id}/webhook-url
POST   /api/v1/webhooks/{workflow_id}    ← public trigger
GET    /api/v1/executions?workflow_id=…
GET    /api/v1/executions/{id}
WS     /ws/executions/{execution_id}     ← live node-by-node progress
CRUD   /api/v1/credentials               ← Fernet-encrypted at rest
```

## Engine semantics

1. The graph is validated (schema, unknown types, **cycles → 400**).
2. `graphlib.TopologicalSorter` yields a dependency-safe order.
3. Exactly one trigger fires; other triggers are marked skipped.
4. A node with incoming edges but no *active* input is skipped - an IF's
   inactive branch deactivates exactly its own outgoing edges.
5. Node failures mark downstream nodes skipped, other branches keep running,
   and the execution ends with status `error`.

## Tests

```bash
cd mini-services/api-backend
python -m pytest tests/ -q          # engine: ordering, templating, branches, cycles
python demo/phase1_demo.py          # standalone 3-node milestone demo
```
