"""System deployment identity (v102) - the commercial bridge.

The arc this service closes: build a system -> DEPLOY it -> people
actually use it. A Py8n System that only lives inside the builder's
estate is an internal artifact; a system with a domain, an environment
and a live status is something a company can call THEIRS. One row per
system (``system_deployments``), four facts:

* ``domain``      - the custom hostname ("ops.acme.com"). Normalized
  (lowercase, trailing dot dropped), validated (real hostname shape,
  at least one dot, label length), checked against a small reserved
  set, and GLOBALLY UNIQUE - two systems cannot answer on one name.
* ``environment`` - staging | production. A label the estate wears.
* ``status``      - offline | live | paused. Moved ONLY by the loud
  verbs (deploy / pause / retire); every move writes the system's
  operations log and emits on the system's event thread, so "when did
  this go live" is always on the record.
* ``branding``    - the login surface's voice: accent color, tagline,
  login headline, logo glyph. What a user sees BEFORE they sign in.

What is deliberately NOT here: users and roles. Membership is v62's
``system_members`` (owner / editor / viewer); the deployed surface
rides it - a "system-specific login" is the platform login resolved
against the domain's system, with the caller's role in the answer.

The URL is derived at read time (https:// + domain) and never stored,
so it can never drift from the domain. The public identity resolver
(``public_identity``) answers only for LIVE deployments - an offline
or paused system does not present a login surface, and honesty at
that door is 404, not a pretty error.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Py8nSystem, SystemDeployment
from . import system_runtime

ENVIRONMENTS = ("staging", "production")
STATUSES = ("offline", "live", "paused")

# verb -> (allowed from-statuses, to-status). Invalid transitions fail
# loud with the current status in the message (400 at the API layer) -
# the same discipline the lifecycle gate (v81) set.
TRANSITIONS: dict[str, tuple[set[str], str]] = {
    "deploy": ({"offline", "paused", "live"}, "live"),
    "pause": ({"live"}, "paused"),
    "retire": ({"live", "paused"}, "offline"),
}

VERB_EVENTS = {
    "deploy": "system.deployed",
    "pause": "system.deployment_paused",
    "retire": "system.deployment_retired",
}

VERB_OPS = {
    "deploy": "deployed",
    "pause": "deployment_paused",
    "retire": "deployment_retired",
}

BRANDING_KEYS = ("accent", "tagline", "login_headline", "logo")
_BRANDING_LIMITS = {"accent": 20, "tagline": 160, "login_headline": 160, "logo": 8}

# a hostname that cannot be someone's operations system
RESERVED_EXACT = {
    "localhost", "ip6-localhost", "ip6-loopback",
    "example.com", "example.org", "example.net", "example.edu",
    "py8n.local", "py8n.com", "py8n.io", "py8n.dev",
    "test", "invalid", "localhost.localdomain",
}
RESERVED_SUFFIXES = (".local", ".localhost", ".internal", ".example", ".test")

# one dot required (a bare "ops" is not a domain), labels of hostname
# shape, total length <= 253 (the RFC limit the column already carries)
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")


class DeploymentError(ValueError):
    """Honest 4xx-grade deployment failures."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_domain(raw: str) -> str:
    """Lowercase, strip, drop one trailing dot. Refuses loud."""
    domain = str(raw or "").strip().lower().rstrip(".")
    if not domain:
        raise DeploymentError("a custom domain is required (e.g. ops.acme.com)")
    if len(domain) > 253:
        raise DeploymentError("domain is longer than the 253-character DNS limit")
    if not DOMAIN_RE.match(domain):
        raise DeploymentError(
            f"{domain!r} is not a usable hostname (need label.label, e.g. ops.acme.com)")
    if domain in RESERVED_EXACT or any(domain.endswith(s) for s in RESERVED_SUFFIXES):
        raise DeploymentError(f"{domain!r} is a reserved name and cannot be a system domain")
    return domain


def validate_branding(branding: dict | None) -> dict:
    """Keep only the four known keys, loud about unknown ones."""
    if branding is None:
        return {}
    if not isinstance(branding, dict):
        raise DeploymentError("branding must be an object")
    out: dict = {}
    for key, value in branding.items():
        if key not in BRANDING_KEYS:
            raise DeploymentError(
                f"unknown branding key {key!r} (allowed: {', '.join(BRANDING_KEYS)})")
        if value is None:
            continue
        text = str(value).strip()
        if len(text) > _BRANDING_LIMITS[key]:
            raise DeploymentError(
                f"branding.{key} is longer than {_BRANDING_LIMITS[key]} characters")
        if key == "accent" and text and not re.match(r"^#[0-9a-fA-F]{6}$", text):
            raise DeploymentError("branding.accent must be a #rrggbb hex color")
        if not text:
            continue
        out[key] = text
    return out


async def get_deployment(db: AsyncSession, system_id: str) -> SystemDeployment | None:
    return (
        await db.execute(
            select(SystemDeployment).where(SystemDeployment.system_id == system_id)
        )
    ).scalar_one_or_none()


async def get_by_domain(db: AsyncSession, domain: str) -> SystemDeployment | None:
    normalized = normalize_domain(domain)
    return (
        await db.execute(
            select(SystemDeployment).where(SystemDeployment.domain == normalized)
        )
    ).scalar_one_or_none()


def deployment_out(row: SystemDeployment | None) -> dict | None:
    """The read projection - the url DERIVED from the domain, never stored.
    The last probe's evidence rides along (v103) - what the domain
    answered the last time somebody asked, or null when never asked."""
    if row is None:
        return None
    last_ping = None
    if row.last_ping_at is not None:
        last_ping = {
            "at": row.last_ping_at.isoformat() if row.last_ping_at else None,
            "ok": bool(row.last_ping_ok),
            "ms": row.last_ping_ms,
            "code": row.last_ping_code,
            "detail": row.last_ping_detail or "",
        }
    return {
        "system_id": row.system_id,
        "domain": row.domain or "",
        "url": f"https://{row.domain}" if row.domain else "",
        "environment": row.environment,
        "status": row.status,
        "branding": row.branding or {},
        "last_ping": last_ping,
        "deployed_at": row.deployed_at.isoformat() if row.deployed_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def upsert_deployment(db: AsyncSession, system: Py8nSystem, *,
                            domain: str | None, environment: str | None,
                            branding: dict | None, actor: str = "") -> SystemDeployment:
    """Create-or-patch the system's deployment record. The domain can be
    CLEARED explicitly (empty string) - going off a custom domain is a
    real move, not an error."""
    row = await get_deployment(db, system.id)
    changed: list[str] = []

    if domain is not None:
        normalized = normalize_domain(domain) if str(domain).strip() else ""
        if normalized:
            clash = (
                await db.execute(
                    select(SystemDeployment).where(SystemDeployment.domain == normalized)
                )
            ).scalar_one_or_none()
            if clash is not None and clash.system_id != system.id:
                raise DeploymentError(
                    f"{normalized!r} already answers for another system - "
                    "one domain, one system")
            if row is None or (row.domain or "") != normalized:
                changed.append(f"domain={normalized or '(none)'}")
        elif row is not None and row.domain:
            changed.append("domain=(none)")
        target_domain = normalized or None
    else:
        target_domain = row.domain if row else None

    if environment is not None:
        if environment not in ENVIRONMENTS:
            raise DeploymentError(
                f"unknown environment {environment!r} (allowed: {', '.join(ENVIRONMENTS)})")
        if row is None or row.environment != environment:
            changed.append(f"environment={environment}")

    clean_branding = validate_branding(branding) if branding is not None else None

    if row is None:
        row = SystemDeployment(
            system_id=system.id,
            domain=target_domain,
            environment=environment or "staging",
            status="offline",
            branding=clean_branding or {},
        )
        db.add(row)
        await db.flush()
        changed.append("deployment created")
    else:
        row.domain = target_domain
        if environment is not None:
            row.environment = environment
        if clean_branding is not None:
            row.branding = {**(row.branding or {}), **clean_branding}
            changed.append("branding updated")

    if changed:
        await system_runtime.record_operation(
            db, system, "deployment_updated", actor or "system",
            {"changes": changed, "status": row.status,
             "domain": row.domain or "", "environment": row.environment})
    return row


async def apply_verb(db: AsyncSession, system: Py8nSystem, verb: str,
                     *, actor: str = "") -> SystemDeployment:
    """Move the deployment status - loud, on the record, on the wire."""
    if verb not in TRANSITIONS:
        raise DeploymentError(
            f"unknown deployment verb {verb!r} (allowed: {', '.join(TRANSITIONS)})")
    row = await get_deployment(db, system.id)
    if row is None:
        raise DeploymentError(
            "this system has no deployment record yet - configure one (PUT "
            "/systems/{id}/deployment) before deploying")
    sources, target = TRANSITIONS[verb]
    current = row.status or "offline"
    if current not in STATUSES:
        raise DeploymentError(
            f"deployment status is corrupted ({current!r}); repair the row first")
    if current not in sources:
        raise DeploymentError(f"cannot {verb} a deployment that is {current!r}")
    row.status = target
    if verb == "deploy":
        row.deployed_at = _now()
    await system_runtime.record_operation(
        db, system, VERB_OPS[verb], actor or "system",
        {"from": current, "to": target,
         "domain": row.domain or "", "environment": row.environment})
    await system_runtime._emit_system_event(
        db, system, VERB_EVENTS[verb],
        {"from": current, "to": target, "domain": row.domain or "",
         "environment": row.environment})
    return row


def public_identity(row: SystemDeployment, system: Py8nSystem) -> dict | None:
    """What the login surface may show BEFORE authentication - only a
    LIVE deployment presents itself (None -> the caller answers 404)."""
    if row.status != "live":
        return None
    branding = row.branding or {}
    return {
        "system": {
            "id": system.id, "name": system.name,
            "icon": system.icon, "color": system.color,
        },
        "domain": row.domain or "",
        "url": f"https://{row.domain}" if row.domain else "",
        "environment": row.environment,
        "status": row.status,
        "branding": {
            "accent": branding.get("accent") or system.color,
            "tagline": branding.get("tagline") or system.description[:160],
            "login_headline": branding.get("login_headline") or system.name,
            "logo": branding.get("logo") or system.icon,
        },
    }


# ------------------------------------------------------------------ v103
# the liveness probe - "does this system's domain actually answer?"

def _probe_target(domain: str) -> str:
    """Where the probe goes. Production derives the URL from the domain
    (the same derivation ``deployment_out`` speaks - zero drift); the
    dev/test override (PY8N_DEPLOY_PING_OVERRIDE) exists because custom
    domains do not resolve inside a sandbox."""
    override = (settings.deploy_ping_override or "").strip()
    return override or f"https://{domain}"


async def probe_url(url: str) -> dict:
    """One outbound GET, timed. NEVER raises: a domain that does not
    answer is an honest answer (ok=False + what happened), not a 500.
    Only the status line is read - the body is a stranger's."""
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=settings.deploy_ping_timeout_seconds, follow_redirects=True,
        ) as client:
            res = await client.get(url)
        ms = int((time.perf_counter() - started) * 1000)
        return {"ok": res.status_code < 500, "ms": ms, "code": res.status_code,
                "detail": f"HTTP {res.status_code}"}
    except Exception as exc:  # noqa: BLE001 - the failure IS the evidence
        ms = int((time.perf_counter() - started) * 1000)
        detail = f"{type(exc).__name__}: {exc}"[:200]
        return {"ok": False, "ms": ms, "code": None, "detail": detail}


async def ping_deployment(db: AsyncSession, system: Py8nSystem, *,
                          actor: str = "") -> tuple[SystemDeployment, dict]:
    """Ask the domain if it answers, and keep the answer on the record.
    Refuses loud when there is nothing to ask (no deployment record, no
    domain). The result is stamped on the row (last_ping_*) and written
    to the operations log - evidence, not a stored 'healthy' flag."""
    row = await get_deployment(db, system.id)
    if row is None or not row.domain:
        raise DeploymentError(
            "no custom domain to ping - set one on the deployment record first")
    result = await probe_url(_probe_target(row.domain))
    row.last_ping_at = _now()
    row.last_ping_ok = result["ok"]
    row.last_ping_ms = result["ms"]
    row.last_ping_code = result["code"]
    row.last_ping_detail = result["detail"][:200]
    await system_runtime.record_operation(
        db, system, "deployment_ping", actor or "system",
        {"domain": row.domain, "ok": result["ok"], "code": result["code"],
         "ms": result["ms"], "detail": result["detail"][:120]})
    return row, result
