# Py8n - the Python-native business operations platform

**Build a system, deploy it, and run a part of your business on it.**

Py8n is not a workflow tool with a bigger canvas. The unit the platform
exists for is the **System**: a named, owned, health-scored operating
unit that binds the workflows, datasets, apps, dashboards, models and
business machines that belong together - and can then be DEPLOYED with
its own domain, its own branding, its own users and its own production
environment. A company using Py8n should feel "this is our operations
system", not "we are using an automation tool".

```
customer.com  →  Py8n  →  System  →  Application  →  Users  →  Business operations
```

The full arc, in order:

1. **Build** - workflows on a visual canvas (a Pydantic-validated node
   registry evaluated with `graphlib.TopologicalSorter` and a
   Jinja2-templated context), datasets with contracts and health, apps
   and dashboards on top.
2. **Operate** - business machines (state machines with SLAs, overdue
   tracking, an escalation door that knocks / digests / accepts
   acknowledgements), the three pre-wired operators (Revenue, Supply,
   Care) and their journey chains, all visible live on the installed
   system.
3. **Deploy** - a system gets a deployment identity: custom domain,
   staging/production, a loud status machine (offline → live → paused),
   the branding its login surface shows, and the users/roles that ride
   the v62 membership (owner / editor / viewer).
4. **Watch** - the estate health overview answers "is my business
   actually healthy?" per system: status dot, success rate, overdue,
   failed workflows, open escalations - and the dot is fed by scheduled
   liveness probes, so a custom domain that stops answering moves the
   row to attention on its own (no stored flag, only fresh evidence).
5. **Update** - the update lifecycle answers "what's going to change if
   I upgrade?" BEFORE anything moves, then every applied upgrade waits
   for a human ruling: accept (settled history) or roll back (unbind
   exactly what it bound).
6. **Integrate** - the system mints its own API keys (`py8n_sys_...`):
   machine credentials that speak AS the system - scoped to it alone,
   with the role their scopes spell - so the company's other software
   can talk to ITS operations system; the keys (and the system's
   people) ride the process doors: advance and ack on the machines the
   system binds, and the front door's work surface shows the pending
   work to whoever signs in at the system's own address.

## The platform, layer by layer

| Layer | What it gives you |
|---|---|
| Workflows | 68 node types, generated config forms, expressions, sandboxed Python, error workflows, pinned test outputs |
| Datasets | Parquet-backed tables, contracts + revisions, profiling, freshness/volume/quality health scores |
| Business machines | State machines with SLAs (`due_at`), transition audit logs, overdue-attention feed, ack/snooze/reschedule |
| Escalations | Per-machine policies (channel + repeat), knock or digest modes, on-call rotation, real email/SMS/Slack delivery |
| Systems | The operating unit: components, lifecycle gate (start/pause/stop), operations log, events, roles |
| Operators & marketplace | Pre-wired business operators, marketplace solutions that install as systems, chain views with live counts |
| Deployments | Custom domains, environments, branding, the public identity door, deployment status on the record, host routing + liveness probes |
| Estate health | Per-system status, success rates, overdue and escalation counters - derived, never stored |
| Reporting | Scheduled chain-history CSV reports, multi-recipient envelopes, hourly/daily/weekly rhythms |
| Real-time | WebSocket execution progress, the live event system (`system.*`, `business.*`, `call.*`), SSE model streaming |

## Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│  Nuxt 3 + Vue Flow + Pinia (canvas UI, systems, estate health)         │
├────────────────────────────────────────────────────────────────────────┤
│  FastAPI  (REST + WebSocket + APScheduler + webhook catcher)           │
│  ├── Engine: GraphSpec → TopologicalSorter → Jinja2 → nodes            │
│  ├── Business layer: processes · escalations · operators · chains      │
│  ├── Systems runtime: lifecycle gate · operations · deployments        │
│  ├── Dispatcher ── inline (sandbox)  or  Celery + Redis (production)   │
│  └── Event bus   ── in-memory         or  Redis pub/sub                │
├────────────────────────────────────────────────────────────────────────┤
│  SQLAlchemy 2.0  ──  SQLite (dev)  /  PostgreSQL + JSONB (production)  │
│  Fernet-encrypted credential vault                                     │
└────────────────────────────────────────────────────────────────────────┘
```

## Engine semantics

1. The graph is validated (schema, unknown types, **cycles → 400**).
2. `graphlib.TopologicalSorter` yields a dependency-safe order.
3. Exactly one trigger fires; other triggers are marked skipped.
4. A node with incoming edges but no *active* input is skipped - an IF's
   inactive branch deactivates exactly its own outgoing edges.
5. Node failures mark downstream nodes skipped, other branches keep
   running, and the execution ends with status `error`.

Node configuration forms are generated from the backend's Pydantic JSON
schemas (`GET /api/v1/node-definitions`) - add a node class in Python
and it appears in the UI palette with a working form.

## Systems and deployments

A system is created by hand, instantiated from a role template, or
installed from a marketplace solution (`as_system=true`). From there:

* the **lifecycle gate** (v81) holds the system's workflows on every
  reactive path - events, schedule ticks, webhooks - without touching
  their own `is_active`;
* the **operations log** records every accepted verb, and every verb
  emits on the system's event thread (`system.*`);
* the **deployment identity** (v102) puts the system on a custom domain
  with an environment and branding; `GET /api/v1/systems/by-domain/
  {domain}` is the public door that resolves a live domain back to the
  identity a login surface shows before authentication;
* the **front door** (v103) is the branded landing that door feeds: a
  visitor to the system's address sees the system's own face (accent,
  tagline, headline, logo) before signing in - and `POST /api/v1/
  systems/{id}/deployment/ping` asks the domain if it answers, keeping
  the evidence on the record (an honest answer even when it fails);
* the **host routing** (v104) maps a real hostname onto the door:
  Caddy serves `rewrite * /go/{host}` for each tenant domain (set
  `PY8N_TENANT_HOSTS` or import the derived sheet), and
  `GET /api/v1/systems/deployment/routes.caddy` writes that sheet FROM
  the live deployments - the edge never drifts from the estate, and a
  paused deployment is never routed;
* the **scheduled probes** (v104) feed the health dot automatically:
  the scheduler re-probes every live deployment on a rhythm
  (`PY8N_DEPLOY_PING_INTERVAL_SECONDS`, default 600s), stamps the same
  evidence the manual ping keeps, and is loud only on transitions -
  `deployment_ping_lost` / `deployment_ping_recovered` land in the
  operations log AND on the system's event thread, so a workflow can
  react to a dark domain;
* the **system API keys** (v104) are the machine identity:
  `POST /api/v1/systems/{id}/keys` mints a `py8n_sys_...` credential
  that authenticates AS the system (send as `X-API-Key`), speaks with
  the role its scopes spell (`write` -> editor, else viewer), answers
  only for its own system, and can never mint keys, manage members or
  restructure the system it speaks for;
* the **keys ride the process doors** (v105): a system key advances
  and acknowledges the machines BOUND to its system - the company's
  other software moves its own operations work AS the system (the
  journey receipt names the key) - and the system's PEOPLE act with
  the same boundary: an editor of the system may advance and take the
  escalations of the machines the system binds, a viewer reads them;
* the **front door's work surface** (v105):
  `GET /api/v1/systems/by-domain/{domain}/work` resolves a live domain
  to the system's own pending work - every bound machine with its live
  operation and the attention rows (open instances past their SLA, the
  escalation book per row) - so signing in at the system's address
  lands the company's people on THEIR work, not the builder's estate;
* the **update lifecycle** (v102) previews an upgrade from the source
  solution's pack (the same reconcile plan the upgrade runs), and
  leaves every applied upgrade PENDING until a human accepts it or
  rolls it back.

## Auth & multi-user

Py8n ships in open single-user mode by default (anonymous works
everywhere). Set `PY8N_REQUIRE_AUTH=true` to force sign-in:

- `POST /api/v1/auth/register` creates accounts (PBKDF2-SHA256, 240k
  iters); the FIRST account becomes `admin` and claims every
  pre-existing resource.
- `POST /api/v1/auth/login` returns a 7-day HS256 JWT (secret
  auto-created at `data/.jwt.key`); send it as
  `Authorization: Bearer <token>`.
- Owned resources are scoped per user; other users' rows 404. Systems
  add a second ring: membership (owner / editor / viewer) decides who
  can read, bind, operate or deploy.
- Machine + published surfaces stay token-free: webhooks, chat,
  published app and dashboard runtimes, artifact content, the public
  domain door and `POST /datasets/query`.

## Running

### Sandbox / single process (already wired)
- Frontend: `bun run dev` (Nuxt on :3000)
- Backend: `bash mini-services/api-backend/start.sh` (FastAPI on :8000)
- AI bridge: `cd mini-services/llm-bridge && bun run index.ts` (:3010)
- Container boot runs `.zscripts/dev.sh`, which starts all three
  automatically.

### Production cluster
```bash
docker compose up --build
# → http://localhost:8025
```
PostgreSQL, Redis, FastAPI, Celery workers (×4 concurrency), Nuxt and
Caddy in one command. `PY8N_EXECUTION_MODE=celery` moves executions off
the API process; events fan out over Redis pub/sub so any replica can
stream WebSocket progress.

## API quick reference

```
GET    /api/v1/health
GET    /api/v1/node-definitions          ← Pydantic schemas for the UI
CRUD   /api/v1/workflows                 ← JSONB graph documents
POST   /api/v1/workflows/{id}/run        ← manual dispatch (202)
GET    /api/v1/executions?workflow_id=…
WS     /ws/executions/{execution_id}     ← live node-by-node progress

CRUD   /api/v1/systems                   ← the operating units
POST   /api/v1/systems/{id}/start|pause|resume|stop
GET    /api/v1/systems/health/overview   ← the estate health rows
GET/PUT /api/v1/systems/{id}/deployment  ← domain · environment · branding
POST   /api/v1/systems/{id}/deployment/deploy|pause|retire
POST   /api/v1/systems/{id}/deployment/ping ← ask the domain if it answers
GET    /api/v1/systems/deployment/routes.caddy ← the DERIVED Caddy routes
GET/POST /api/v1/systems/{id}/keys      ← machine keys (owner only)
DELETE /api/v1/systems/{id}/keys/{key_id}
GET    /api/v1/systems/{id}/update/preview
POST   /api/v1/systems/{id}/update/accept|rollback
GET    /api/v1/systems/by-domain/{domain} ← the public identity door
GET    /api/v1/systems/by-domain/{domain}/work ← the system's work surface
GET    /api/v1/systems/{id}/chains       ← the drawn journey chains

CRUD   /api/v1/processes                 ← business machines + instances
POST   /api/v1/processes/{id}/advance    ← the state machine moves
GET    /api/v1/processes/attention       ← what needs a human today
GET    /api/v1/processes/chains/history.csv ← the per-leg CSV export

CRUD   /api/v1/credentials               ← Fernet-encrypted at rest
```

## Tests

```bash
cd mini-services/api-backend
python -m pytest tests/ -q          # 573 tests: engine, systems, machines,
                                    # escalations, deployments, reporting
python demo/phase1_demo.py          # standalone milestone demo
```
