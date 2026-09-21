# Installing Py8n

## One command

```bash
./install.sh
```

That builds the frontend, starts the whole stack (PostgreSQL + Redis +
migrations + API + Celery worker + LLM bridge + frontend + the Caddy
reverse proxy) and waits for the health door. Open
**http://localhost:8025** - the first account you register becomes the
owner.

## What the compose stack runs

| service | role |
|---|---|
| postgres | the primary datastore (`PY8N_DATABASE_URL=postgresql+asyncpg://...`) |
| redis | the Celery broker (production execution mode) |
| migrate | one-shot `migrations.bootstrap` before the API boots |
| api | FastAPI backend (`:8000`, published at `:8025`) |
| worker | the Celery execution worker (`PY8N_EXECUTION_MODE=celery`) |
| llm-bridge | the local model bridge for agent brains |
| frontend | the Nuxt console |
| proxy | TLS/host routing (`Caddyfile`) |

## Execution modes

`PY8N_EXECUTION_MODE` picks how workflow runs travel:

* `inline` (default) - background tasks on the API process;
* `queue` - runs land as `queued` rows and the house scheduler's queue
  tick (`PY8N_QUEUE_TICK_SECONDS`, default 5, 0 disables) claims and runs
  them one at a time - back-pressure relief without a broker;
* `celery` - distributed workers over Redis (the compose default).

## Two-factor (TOTP)

1. sign in, then `POST /api/v1/auth/2fa/setup` (bearer token) - it returns
   the base32 `secret` and an `otpauth_uri` (render it as a QR for any
   authenticator app);
2. `POST /api/v1/auth/2fa/enable` with a current 6-digit code;
3. the next sign-in answers `{"mfa_required": true, "mfa_token": ...}` -
   the console asks for the code and `POST /api/v1/auth/2fa/verify` mints
   the real session. A challenge token is never a bearer token.

## Signed webhooks (Stripe/GitHub style)

On any Webhook Trigger node set **auth_mode = `hmac`**, a
**signature_header** (default `X-Signature`) and a **signature_secret**.
Senders sign the RAW body with HMAC-SHA256 and send the hex digest (a
`sha256=` prefix is accepted). Missing/invalid signatures are a 401
before the flow runs.

## Portability

`GET /api/v1/systems/{id}/export` bundles a whole system (meta + every
bound workflow's graph + every dataset's schema and rows) as one JSON
document; `POST /api/v1/systems/import` lands it anywhere - fresh ids,
inactive workflows (turn them on via the system lifecycle), rows intact.
