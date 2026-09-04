"""Channel endpoints (v69) - CRUD and delivery for the REAL adapter surface.

A ChannelEndpoint turns a provider's native webhook into interaction-layer
ingests. This module owns the storage and the HTTP plumbing; the dialect
rules live in channel_adapters.py (pure, unit-testable).

The receive path (one call, shared by all three receivers):

  verify (provider-native) -> parse (native dialect -> normalized
  messages + honest skips) -> for each message: interactions.ingest()
  (find-or-create conversation, run the bound handler, record both sides)
  -> deliver the reply through the provider's send API.

Delivery is honest about the last mile: with a credential configured the
built request is actually sent (httpx, short timeout) and the provider's
status recorded; without one (or with the endpoint disabled mid-flight)
the reply is recorded in the transcript but delivery is reported
``skipped`` with the exact missing key - py8n never pretends a message
was delivered when it wasn't.
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ChannelEndpoint, Workflow
from . import channel_adapters as adapters
from . import interactions as inter_svc


class ChannelEndpointError(ValueError):
    """Honest 4xx-grade endpoint failures."""


def endpoint_out(row: ChannelEndpoint, handler_name: str | None = None) -> dict:
    provider_meta = adapters.REQUIRED_CONFIG.get(row.provider, {})
    return {
        "id": row.id,
        "name": row.name,
        "provider": row.provider,
        "channel": row.channel,
        "enabled": bool(row.enabled),
        "handler_workflow_id": row.handler_workflow_id,
        "handler_workflow_name": handler_name,
        "config": adapters.mask_config(row.config or {}),
        "required_config": {k: v for k, v in provider_meta.items() if k != "description"},
        "webhook_url": f"/api/v1/channels/{adapters.PROVIDER_PATHS.get(row.provider, row.provider)}/{row.id}/webhook",
        "events_received": row.events_received,
        "last_event_at": row.last_event_at.isoformat() if row.last_event_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _handler_name(db: AsyncSession, workflow_id: str | None) -> str | None:
    if not workflow_id:
        return None
    wf = await db.get(Workflow, workflow_id)
    return wf.name if wf else None


def _validate_config(provider: str, config: dict, *, partial: bool = False) -> None:
    meta = adapters.REQUIRED_CONFIG.get(provider)
    if meta is None:
        raise ChannelEndpointError(
            f"unknown provider {provider!r} - known providers: "
            f"{', '.join(sorted(adapters.REQUIRED_CONFIG))}")
    if partial:
        return
    missing = [k for k in meta.get("secret", []) if not str(config.get(k) or "").strip()]
    if missing:
        raise ChannelEndpointError(
            f"provider {provider} requires {', '.join(missing)} in config "
            "(the keys that verify the provider's webhook)")


def _extract_sender_to(msg: adapters.NormalizedInbound) -> str:
    """Where the outbound reply goes, per provider."""
    if msg.channel == "telegram":
        return msg.extra.get("chat_id") or msg.sender_id
    if msg.channel == "whatsapp":
        return msg.sender_id
    if msg.channel == "discord":
        return ""  # the endpoint's configured webhook_url IS the destination
    return msg.sender_id


async def deliver_outbound(endpoint: ChannelEndpoint, to: str, text: str) -> dict:
    """Build the provider request and (when possible) actually send it.

    Returns a delivery record: {delivery, detail, request} where request
    carries the url + json body (auth headers masked) for traceability.
    """
    config = endpoint.config or {}
    request = adapters.build_outbound(endpoint.provider, config, to, text)
    masked_request = {"method": request["method"], "url": request["url"],
                      "json": request.get("json")}
    cred_keys = adapters.REQUIRED_CONFIG.get(endpoint.provider, {}).get("credential", [])
    missing = [k for k in cred_keys if not str(config.get(k) or "").strip()]
    if missing:
        return {"delivery": "skipped",
                "detail": f"no outbound credential configured (missing {', '.join(missing)}) - "
                          "the reply is recorded in the transcript but NOT delivered",
                "request": masked_request}
    try:
        import httpx
        headers = {k: v for k, v in request.get("headers", {}).items()}
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            resp = await client.request(request["method"], request["url"],
                                        headers=headers, json=request.get("json"))
        ok = 200 <= resp.status_code < 300
        detail = f"provider answered {resp.status_code}"
        if not ok:
            try:
                body = resp.json()
                detail += f": {_json.dumps(body)[:200]}"
            except Exception:  # noqa: BLE001 - provider error bodies vary
                pass
        return {"delivery": "delivered" if ok else "failed", "detail": detail,
                "status_code": resp.status_code, "request": masked_request}
    except Exception as exc:  # noqa: BLE001 - the sandbox may have no egress at all
        return {"delivery": "failed", "detail": f"outbound request failed: {exc}",
                "request": masked_request}


async def receive_webhook(db: AsyncSession, endpoint: ChannelEndpoint, *,
                          raw_body: bytes, headers: dict,
                          query_params: dict | None = None) -> dict:
    """The shared receiver: verify -> parse -> ingest each -> deliver replies.

    Raises ChannelEndpointError on verification failure (the API layer
    maps that to 401/403). Provider status events (delivery receipts,
    edits) are counted and reported as skips - they are not messages.
    """
    if not endpoint.enabled:
        raise ChannelEndpointError("this endpoint is disabled")
    provider = endpoint.provider

    # 1. verify - provider-native, always, before anything else runs
    ok, detail = adapters.verify_request(provider, endpoint.config or {},
                                         raw_body=raw_body, headers={
                                             k.lower(): v for k, v in headers.items()})
    if not ok:
        raise ChannelEndpointError(detail or "webhook verification failed")
    try:
        payload = _json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}

    # 2. parse the provider dialect
    result = adapters.parse_inbound(provider, payload if isinstance(payload, dict) else {})
    handled: list[dict] = []

    # 3. ingest each message through the interaction layer (owner = endpoint owner)
    for msg in result.messages:
        ingest = await inter_svc.ingest(
            db, owner_id=endpoint.owner_id, channel=msg.channel,
            sender_id=msg.sender_id, sender_name=msg.sender_name, text=msg.text,
            handler_workflow_id=endpoint.handler_workflow_id,
            metadata={"provider": provider, "endpoint_id": endpoint.id,
                      "event_id": msg.event_id, **(msg.extra or {})})
        # 4. deliver the reply through the provider's send API
        delivery = await deliver_outbound(endpoint, _extract_sender_to(msg), ingest.get("reply") or "")
        handled.append({"sender_id": msg.sender_id, "text": msg.text[:200],
                        "conversation_id": ingest["conversation_id"],
                        "reply": ingest.get("reply"), **delivery})

    # 5. event counters on the endpoint (write-side derived bookkeeping)
    endpoint.events_received = (endpoint.events_received or 0) + 1
    endpoint.last_event_at = datetime.now(timezone.utc)
    db.add(endpoint)

    return {"ok": True, "endpoint": endpoint.id, "provider": provider,
            "received": result.count, "skipped": result.skipped,
            "handled": handled}


async def list_endpoints(db: AsyncSession, owner_id: str | None) -> list[dict]:
    q = select(ChannelEndpoint).order_by(ChannelEndpoint.created_at.desc())
    rows = (await db.execute(q)).scalars().all()
    if owner_id is not None:
        rows = [r for r in rows if r.owner_id is None or r.owner_id == owner_id]
    return [endpoint_out(r, await _handler_name(db, r.handler_workflow_id)) for r in rows]


async def get_endpoint(db: AsyncSession, endpoint_id: str, owner_id: str | None) -> dict | None:
    row = await db.get(ChannelEndpoint, endpoint_id)
    if row is None:
        return None
    if owner_id is not None and row.owner_id is not None and row.owner_id != owner_id:
        return None
    return endpoint_out(row, await _handler_name(db, row.handler_workflow_id))


async def create_endpoint(db: AsyncSession, *, owner_id: str | None, name: str,
                          provider: str, handler_workflow_id: str | None,
                          config: dict) -> dict:
    if not name or not name.strip():
        raise ChannelEndpointError("an endpoint name is required")
    _validate_config(provider, config or {})
    if handler_workflow_id:
        wf = await db.get(Workflow, handler_workflow_id)
        if wf is None or (owner_id is not None and wf.owner_id is not None
                          and wf.owner_id != owner_id):
            raise ChannelEndpointError(f"handler workflow {handler_workflow_id!r} not found")
    row = ChannelEndpoint(
        name=name.strip()[:140],
        provider=provider,
        channel=adapters.REQUIRED_CONFIG[provider]["channel"],
        handler_workflow_id=handler_workflow_id,
        config=config or {},
    )
    row.owner_id = owner_id
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return endpoint_out(row, await _handler_name(db, row.handler_workflow_id))


async def update_endpoint(db: AsyncSession, row: ChannelEndpoint, *, name: str | None = None,
                          handler_workflow_id: str | None = None, config: dict | None = None,
                          enabled: bool | None = None) -> dict:
    if name is not None and name.strip():
        row.name = name.strip()[:140]
    if enabled is not None:
        row.enabled = bool(enabled)
    if config:
        merged = dict(row.config or {})
        merged.update(config)
        _validate_config(row.provider, merged)
        row.config = merged
    if handler_workflow_id is not None:
        if handler_workflow_id:
            wf = await db.get(Workflow, handler_workflow_id)
            if wf is None:
                raise ChannelEndpointError(f"handler workflow {handler_workflow_id!r} not found")
        row.handler_workflow_id = handler_workflow_id or None
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return endpoint_out(row, await _handler_name(db, row.handler_workflow_id))


async def delete_endpoint(db: AsyncSession, row: ChannelEndpoint) -> dict:
    payload = {"id": row.id, "name": row.name, "provider": row.provider,
               "note": "endpoint removed - conversations and transcripts survive"}
    await db.delete(row)
    await db.flush()
    return payload


async def preview_outbound(row: ChannelEndpoint, to: str, text: str) -> dict:
    """Dry-run: the exact request py8n would send, secrets masked."""
    request = adapters.build_outbound(row.provider, row.config or {}, to, text)
    headers = dict(request.get("headers") or {})
    for key in list(headers):
        if key.lower() in ("authorization", "x-telegram-bot-api-secret-token"):
            v = str(headers[key])
            headers[key] = f"{v[:8]}...({len(v)} chars)" if len(v) > 8 else v
    cred_keys = adapters.REQUIRED_CONFIG.get(row.provider, {}).get("credential", [])
    missing = [k for k in cred_keys if not str((row.config or {}).get(k) or "").strip()]
    return {"method": request["method"], "url": request["url"],
            "headers": headers, "json": request.get("json"),
            "would_deliver": not missing,
            "missing_credentials": missing}
