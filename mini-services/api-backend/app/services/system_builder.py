"""AI System Builder (v59) - describe a system, interview, build it.

The roadmap's Describe -> Discover -> Clarify -> Design -> Build loop:

1. **synthesize_spec()** turns a plain-language description into a
   SystemSpec: a purpose, a detected persona (business user vs data
   engineer - adaptive technical depth), a schedule, a source, and a
   checklist of components split into core / recommended / optional
   tiers. The keyword synthesis is DETERMINISTIC so the answer is
   testable and can never hallucinate a component that py8n cannot
   actually build.
2. **The interview** - questions are generated for whatever is still
   unknown (source table, contract columns, dedupe keys, exact schedule,
   alert webhook); answers are applied back into the spec.
3. **LLM enhancement (optional, fail-soft)** - the description plus the
   component library go to the same free sandbox bridge the llm_chat
   node uses; the model may rename the system, adjust the persona,
   re-pick components and add questions. An unreachable bridge leaves
   the deterministic spec intact with a note.
4. **build_system()** translates the SELECTED components into real
   primitives: a dataset, a workflow graph wired from registered node
   types (schedule/db/s3/http source, dedupe, incremental upsert write,
   LLM summary), an execution policy, a data contract, a dashboard, a
   scheduled report and a failure-notification rule. The built refs come
   back for one-glance review - and everything is a normal py8n object
   the user can keep editing.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx

from ..config import settings

NARRATE_TIMEOUT = 30.0

# ---------------------------------------------------------------------------
# The component library - every entry maps to a REAL primitive at build time.
# ---------------------------------------------------------------------------

COMPONENT_LIBRARY: list[dict] = [
    {"id": "target_dataset", "tier": "core", "label": "Target dataset",
     "detail": "A parquet-backed dataset the system writes to"},
    {"id": "pipeline_workflow", "tier": "core", "label": "Ingestion workflow",
     "detail": "Trigger -> source -> write graph built from registered nodes"},
    {"id": "schedule", "tier": "recommended", "label": "Schedule trigger",
     "detail": "Interval or cron firing for the pipeline"},
    {"id": "schema_contract", "tier": "recommended", "label": "Schema contract",
     "detail": "Column-level contract enforced at every write (needs fields)"},
    {"id": "dedupe", "tier": "recommended", "label": "Deduplication",
     "detail": "remove_duplicates node on the identity column(s)"},
    {"id": "incremental", "tier": "recommended", "label": "Incremental write",
     "detail": "dataset_write upsert mode with watermark + lookback"},
    {"id": "retry_policy", "tier": "recommended", "label": "Retry policy",
     "detail": "Workflow-level retries with transient-only backoff (v51)"},
    {"id": "quality_gate", "tier": "optional", "label": "Quality gate (error mode)",
     "detail": "Contract violations hard-stop the write instead of warning"},
    {"id": "failure_notification", "tier": "optional", "label": "Failure notification",
     "detail": "Webhook rule on execution_failed, scoped to the pipeline"},
    {"id": "dashboard", "tier": "optional", "label": "Dashboard",
     "detail": "Auto-generated board over the target dataset"},
    {"id": "scheduled_report", "tier": "optional", "label": "Scheduled report",
     "detail": "Periodic dataset export as an artifact"},
    {"id": "ai_summary", "tier": "optional", "label": "AI run summary",
     "detail": "llm_chat node (free sandbox bridge) that summarizes each run"},
]

_COMPONENTS_BY_ID = {c["id"]: c for c in COMPONENT_LIBRARY}

# keywords -> component ids
_KEYWORD_COMPONENTS = [
    (("dedup", "duplicate", "unique record"), ["dedupe"]),
    (("incremental", "late-arriving", "late arriving", "lookback", "watermark", "cdc", "upsert"), ["incremental"]),
    (("schema", "contract", "validate", "validation"), ["schema_contract"]),
    (("quality", "quality gate", "null check"), ["quality_gate", "schema_contract"]),
    (("alert", "notify", "notification", "slack", "webhook"), ["failure_notification"]),
    (("dashboard", "monitor ", "metrics"), ["dashboard"]),
    (("report", "pdf", "excel", "export"), ["scheduled_report"]),
    (("summar", "llm", " gpt", "ai "), ["ai_summary"]),
    (("retry", "transient", "throttl"), ["retry_policy"]),
]

# keywords -> persona ("data engineer" language vs outcome language)
_ENGINEER_MARKERS = (
    "watermark", "checkpoint", "lookback", "cdc", "schema contract", "contract",
    "upsert", "parquet", "idempotent", "dedup", "staging", "curated", "quality gate",
    "incremental", "backfill", "key column",
)

_SOURCE_RULES = [
    (("postgres", "postgresql", "postgre"), {"kind": "db", "backend": "postgres", "label": "PostgreSQL"}),
    (("mysql", "mariadb"), {"kind": "db", "backend": "mysql", "label": "MySQL"}),
    (("sqlite",), {"kind": "db", "backend": "sqlite", "label": "SQLite"}),
    ((" s3", "s3 ", "s3 bucket", "minio"), {"kind": "s3", "backend": "s3", "label": "S3/MinIO"}),
    (("google sheet", "sheets", "spreadsheet"), {"kind": "sheets", "backend": "sheets", "label": "Google Sheets"}),
    (("ftp", "ftps"), {"kind": "ftp", "backend": "ftp", "label": "FTP/FTPS"}),
    (("api", "http", "url", "endpoint", "rest"), {"kind": "http", "backend": "http", "label": "HTTP API"}),
    (("csv upload", "upload", "manual", "email attachment"), {"kind": "upload", "backend": "upload", "label": "Manual upload"}),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_schedule(text: str) -> dict | None:
    """Natural-language schedule -> schedule_trigger params (best effort)."""
    t = (text or "").lower()
    m = re.search(r"every\s+(\d+)\s*min(?:ute)?s?", t)
    if m:
        return {"mode": "interval", "interval_seconds": max(5, int(m.group(1)) * 60)}
    m = re.search(r"every\s+(\d+)\s*hours?", t)
    if m:
        return {"mode": "interval", "interval_seconds": max(5, int(m.group(1)) * 3600)}
    if "hourly" in t or "every hour" in t:
        return {"mode": "interval", "interval_seconds": 3600}
    m = re.search(r"daily\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?", t)
    if m or "every morning" in t or "every day" in t or "daily" in t:
        hour = int(m.group(1)) if m else 8
        minute = int(m.group(2)) if m and m.group(2) else 0
        hour = min(max(hour, 0), 23)
        minute = min(max(minute, 0), 59)
        return {"mode": "cron", "cron": f"{minute} {hour} * * *"}
    if "weekly" in t or "every week" in t:
        return {"mode": "cron", "cron": "0 8 * * 1"}
    return None


def _detect_source(text: str) -> dict:
    t = f" {text.lower()} "
    for keys, src in _SOURCE_RULES:
        if any(k in t for k in keys):
            return {**src, "table": "", "connection": ""}
    return {"kind": "upload", "backend": "upload", "label": "Manual upload", "table": "", "connection": ""}


def _title_from(description: str) -> str:
    """Short system name from the description (first few words)."""
    words = re.sub(r"[^\w\s]", " ", description).split()
    stop = {"a", "an", "the", "that", "which", "i", "want", "need", "to", "and", "for", "my", "our", "build", "create", "system", "every", "from", "into"}
    picked = [w for w in words if w.lower() not in stop][:4]
    title = " ".join(picked).strip().title() or "My System"
    return title[:80]


def synthesize_spec(description: str) -> dict:
    """Deterministic description -> SystemSpec (persona, schedule, source,
    component checklist with selected flags, clarifying questions)."""
    text = (description or "").strip()
    low = text.lower()
    if not text:
        raise ValueError("description is required")

    persona = "data_engineer" if any(m in low for m in _ENGINEER_MARKERS) else "business"

    components: dict[str, dict] = {}
    for c in COMPONENT_LIBRARY:
        components[c["id"]] = {
            "id": c["id"], "label": c["label"], "tier": c["tier"],
            "selected": c["tier"] == "core", "note": "",
        }
    # core is always selected; recommended/optional start selected ONLY when
    # the description actually calls for them - adaptive technical depth:
    # a business user does not get watermark/lookback plumbing they never asked for
    for cid in ("target_dataset", "pipeline_workflow", "schedule"):
        components[cid]["selected"] = True
    for keys, cids in _KEYWORD_COMPONENTS:
        if any(k in low for k in keys):
            for cid in cids:
                components[cid]["selected"] = True
    # a schedule was mentioned -> the schedule component earns its place;
    # nothing mentioned -> keep it selected anyway (pipelines usually need one)
    schedule = parse_schedule(text) or {"mode": "interval", "interval_seconds": 3600}

    src = _detect_source(text)
    if "hours" in low and "lookback" in low:
        m = re.search(r"(\d+)\s*hours?\s*lookback|lookback\s*(?:of\s*)?(\d+)\s*hours?", low)
        lookback = float(m.group(1) or m.group(2)) if m else 24.0
    elif "late-arriving" in low or "late arriving" in low:
        lookback = 24.0
    else:
        lookback = 0.0

    spec = {
        "title": _title_from(text),
        "purpose": text,
        "persona": persona,
        "source": src,
        "schedule": schedule,
        "fields": [],            # [{name, dtype}] for the contract
        "dedupe_keys": [],       # identity column(s)
        "lookback_hours": lookback,
        "webhook_url": "",
        "report_fmt": "csv",
        "components": [components[c["id"]] for c in COMPONENT_LIBRARY],
        "notes": [],
    }
    spec["questions"] = _questions_for(spec)
    return spec


def _questions_for(spec: dict) -> list[dict]:
    """Clarifying questions for whatever the spec still does not know."""
    questions: list[dict] = []
    src = spec.get("source") or {}
    if src.get("kind") in ("db", "s3", "sheets", "ftp", "http"):
        questions.append({
            "id": "q_table",
            "question": f"Which {src.get('label', 'source')} table/endpoint should be ingested?",
            "key": "table",
            "answered": False,
        })
    if not spec.get("fields"):
        questions.append({
            "id": "q_fields",
            "question": "Which columns should the dataset carry (comma-separated, e.g. id, customer, revenue)?",
            "key": "fields",
            "answered": False,
        })
    if any(c["selected"] and c["id"] == "dedupe" for c in spec.get("components", [])):
        questions.append({
            "id": "q_dedupe",
            "question": "Which column identifies a unique record (dedupe/upsert key)?",
            "key": "dedupe_keys",
            "answered": False,
        })
    if any(c["selected"] and c["id"] == "failure_notification" for c in spec.get("components", [])):
        questions.append({
            "id": "q_webhook",
            "question": "Which webhook URL should receive failure alerts?",
            "key": "webhook_url",
            "answered": False,
        })
    return questions


def _parse_fields(raw) -> list[dict]:
    """"id:integer, revenue:number, name" -> contract-ready field list."""
    out: list[dict] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, _, dtype = part.partition(":")
            dtype = dtype.strip().lower()
            if dtype not in ("text", "integer", "number", "boolean", "datetime"):
                dtype = "text"
        else:
            name, dtype = part, "text"
        name = name.strip()
        if name:
            out.append({"name": name, "dtype": dtype})
    return out


def apply_answers(spec: dict, answers: dict) -> dict:
    """Fold interview answers back into the spec (deterministic)."""
    for q in spec.get("questions", []):
        key = q["key"]
        if key in answers:
            q["answered"] = True
            value = answers[key]
            if key == "table":
                spec.setdefault("source", {})["table"] = str(value).strip()
            elif key == "fields":
                spec["fields"] = _parse_fields(value)
            elif key == "dedupe_keys":
                spec["dedupe_keys"] = [k.strip() for k in str(value).split(",") if k.strip()]
            elif key == "webhook_url":
                spec["webhook_url"] = str(value).strip()
            elif key == "schedule":
                parsed = parse_schedule(str(value))
                if parsed:
                    spec["schedule"] = parsed
                    spec.setdefault("components", [])
                    for c in spec["components"]:
                        if c["id"] == "schedule":
                            c["selected"] = True
    # a fields answer may unlock the contract note
    if spec.get("fields"):
        for c in spec.get("components", []):
            if c["id"] == "schema_contract":
                c["note"] = ""
    return spec


def toggle_component(spec: dict, component_id: str, selected: bool) -> dict:
    """Tick/untick a component with dependency validation."""
    comps = {c["id"]: c for c in spec.get("components", [])}
    if component_id not in comps:
        raise ValueError(f"unknown component {component_id!r}")
    if component_id in ("target_dataset", "pipeline_workflow") and not selected:
        raise ValueError("the target dataset and the pipeline workflow are the system's backbone - they cannot be removed")
    if component_id == "quality_gate" and selected and not comps.get("schema_contract", {}).get("selected"):
        raise ValueError("the quality gate needs the schema contract component selected")
    comps[component_id]["selected"] = bool(selected)
    return spec


# ---------------------------------------------------------------------------
# Optional LLM enhancement (fail-soft)
# ---------------------------------------------------------------------------

def _llm_available_payload(spec: dict, description: str) -> tuple[str, str]:
    lib = "\n".join(f"- {c['id']}: {c['label']} ({c['tier']})" for c in COMPONENT_LIBRARY)
    system = (
        "You are py8n's system architect. Given a user's plain-language request and py8n's "
        "component library, return STRICT JSON only: {\"title\": str, \"persona\": "
        "\"business\"|\"data_engineer\", \"selected\": [component ids], \"questions\": [str], "
        "\"notes\": [str]}. Components must come from the library. Max 4 questions. No prose."
    )
    user = f"Request: {description}\n\nComponent library:\n{lib}\n\nCurrent draft JSON: {json.dumps(spec, default=str)[:1500]}"
    return system, user


async def enhance_spec_with_llm(spec: dict, description: str) -> dict:
    """One sandbox-bridge call to refine the deterministic spec. Fail-soft:
    any bridge/parse problem keeps the deterministic spec and adds a note."""
    system, user = _llm_available_payload(spec, description)
    url = f"{settings.llm_bridge_url.rstrip('/')}/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=NARRATE_TIMEOUT) as client:
            resp = await client.post(url, json={
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0.1, "max_tokens": 700,
            })
        if resp.status_code >= 400:
            spec["notes"].append(f"LLM enhancement skipped: bridge returned HTTP {resp.status_code}")
            return spec
        data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except Exception as exc:
        spec["notes"].append(f"LLM enhancement skipped: bridge unreachable ({type(exc).__name__})")
        return spec

    # extract the first JSON object from the reply
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        spec["notes"].append("LLM enhancement skipped: reply was not JSON")
        return spec
    try:
        refined = json.loads(match.group(0))
    except ValueError:
        spec["notes"].append("LLM enhancement skipped: reply was not parseable JSON")
        return spec

    known = {c["id"] for c in COMPONENT_LIBRARY}
    if isinstance(refined.get("title"), str) and refined["title"].strip():
        spec["title"] = refined["title"].strip()[:80]
    if refined.get("persona") in ("business", "data_engineer"):
        spec["persona"] = refined["persona"]
    picked = refined.get("selected")
    if isinstance(picked, list):
        wanted = {p for p in picked if p in known}
        if {"target_dataset", "pipeline_workflow"} <= wanted:  # LLM may not drop the backbone
            for c in spec["components"]:
                c["selected"] = c["id"] in wanted
    if isinstance(refined.get("questions"), list):
        existing_q = {q["question"] for q in spec.get("questions", [])}
        for i, q in enumerate(refined["questions"][:4]):
            if isinstance(q, str) and q.strip() and q.strip() not in existing_q:
                spec.setdefault("questions", []).append({
                    "id": f"q_llm{i}", "question": q.strip()[:300],
                    "key": f"llm_{i}", "answered": False, "llm": True,
                })
    for note in refined.get("notes", []) if isinstance(refined.get("notes"), list) else []:
        if isinstance(note, str) and note.strip():
            spec["notes"].append(f"AI: {note.strip()[:200]}")
    return spec


# ---------------------------------------------------------------------------
# The build step - SystemSpec -> real py8n primitives
# ---------------------------------------------------------------------------

def _interval_to_cron(seconds: int) -> str:
    if seconds >= 86_400:
        return "0 6 * * *"
    if seconds >= 3_600:
        return "0 * * * *"
    return "*/15 * * * *"


def _node(nid: str, ntype: str, params: dict, name: str) -> dict:
    return {"id": nid, "type": ntype, "name": name, "position": {"x": 0, "y": 0}, "parameters": params}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target, "sourceHandle": "main", "targetHandle": "main"}


async def _unique_dataset_name(db, base: str) -> str:
    """Base name, suffixed -2/-3/... while the name is taken."""
    from . import datasets as ds_svc

    name = re.sub(r"\s+", " ", base).strip()[:100] or "System Dataset"
    if not ds_svc.NAME_RE.match(name):
        name = "System Dataset"
    candidate = name
    n = 1
    while await ds_svc.name_taken(db, candidate):
        n += 1
        candidate = f"{name} {n}"
    return candidate


async def build_system(db, draft) -> dict:
    """Translate the draft's SELECTED components into real primitives.

    Returns the ``built_json`` payload: refs to everything created plus
    honest notes for the pieces that still need user input (e.g. a
    contract without columns). The caller owns the commit.
    """
    import pandas as pd
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401

    from ..engine.runner import validate_graph_document
    from ..models import NotificationRule, ScheduledReport, Workflow
    from . import contracts as contracts_svc
    from . import dashboards as dash_svc
    from . import datasets as ds_svc
    from .versions import snapshot_workflow_version

    spec = draft.spec_json or {}
    selected = {c["id"] for c in spec.get("components", []) if c.get("selected")}
    notes = [n for n in spec.get("notes", [])]
    title = (spec.get("title") or "System").strip()
    built: dict = {"workflow_id": None, "workflow_name": None, "dataset_id": None,
                   "dataset_name": None, "contract_version": None, "on_violation": None,
                   "dashboard_id": None, "report_id": None, "notification_rule_id": None,
                   "policy": None}

    # --- 1) the target dataset ---------------------------------------------
    ds = await ds_svc.create_from_df(
        db, await _unique_dataset_name(db, f"{title} dataset"), pd.DataFrame(),
        source="system_builder", description=(spec.get("purpose") or "")[:500],
        owner_id=draft.owner_id,
    )
    built["dataset_id"] = ds.id
    built["dataset_name"] = ds.name

    # --- 2) the pipeline workflow -------------------------------------------
    fields = spec.get("fields") or []
    dedupe_keys = spec.get("dedupe_keys") or []
    nodes: list[dict] = []
    edges: list[dict] = []
    prev = None

    if "schedule" in selected:
        sched = spec.get("schedule") or {"mode": "interval", "interval_seconds": 3600}
        params = ({"mode": "cron", "cron": sched["cron"]}
                  if sched.get("mode") == "cron" else
                  {"mode": "interval", "interval_seconds": int(sched.get("interval_seconds") or 3600)})
        nodes.append(_node("trigger", "schedule_trigger", params, "Schedule"))
    else:
        nodes.append(_node("trigger", "manual_trigger", {}, "Manual"))
    prev = "trigger"

    src = spec.get("source") or {}
    src_kind = src.get("kind") or "upload"
    if src_kind == "db":
        nodes.append(_node("source", "db_source", {
            "backend": src.get("backend") or "sqlite",
            "connection": src.get("connection") or "",
            "table": src.get("table") or "",
            "limit": 5000,
        }, f"{src.get('label') or 'DB'} source"))
        edges.append(_edge("e_trigger", prev, "source"))
        prev = "source"
    elif src_kind == "s3":
        nodes.append(_node("source", "s3_source", {"uri": src.get("table") or "s3://bucket/path.csv"}, "S3 source"))
        edges.append(_edge("e_trigger", prev, "source"))
        prev = "source"
    elif src_kind == "http":
        nodes.append(_node("source", "http_request", {"url": src.get("table") or "https://example.com/api"}, "HTTP source"))
        edges.append(_edge("e_trigger", prev, "source"))
        prev = "source"
    elif src_kind == "sheets":
        nodes.append(_node("source", "google_sheets_source", {"url": src.get("table") or ""}, "Sheets source"))
        edges.append(_edge("e_trigger", prev, "source"))
        prev = "source"
    elif src_kind == "ftp":
        nodes.append(_node("source", "ftp_source", {"host": src.get("table") or ""}, "FTP source"))
        edges.append(_edge("e_trigger", prev, "source"))
        prev = "source"
    # upload kind: the write node consumes the run payload directly

    if "dedupe" in selected:
        nodes.append(_node("dedupe", "remove_duplicates",
                           {"field": (dedupe_keys[0] if dedupe_keys else "")}, "Dedupe"))
        edges.append(_edge("e_dedupe", prev, "dedupe"))
        prev = "dedupe"

    incremental = "incremental" in selected
    write_params: dict = {"dataset": ds.name, "mode": "upsert" if incremental else "replace"}
    if incremental:
        # the cursor: a datetime field if the contract has one, else the first
        # field, else the dataset_write default behaviour on 'updated_at'
        dt_fields = [f["name"] for f in fields if f.get("dtype") == "datetime"]
        write_params["watermark_column"] = (dt_fields or [f["name"] for f in fields] or ["updated_at"])[0]
        write_params["key_columns"] = dedupe_keys or ([fields[0]["name"]] if fields else ["id"])
        if spec.get("lookback_hours"):
            write_params["lookback"] = float(spec["lookback_hours"]) * 3600.0
    nodes.append(_node("write", "dataset_write", write_params, "Write dataset"))
    edges.append(_edge("e_write", prev, "write"))
    prev = "write"

    if "ai_summary" in selected:
        nodes.append(_node("summary", "llm_chat", {
            "provider": "sandbox_bridge",
            "system_prompt": "You summarize data pipeline runs for an operator. Two sentences max.",
            "user_prompt": "Summarize this pipeline run result: {{ nodes.write.output }}",
        }, "AI run summary"))
        edges.append(_edge("e_ai", prev, "summary"))

    graph = validate_graph_document({"nodes": nodes, "edges": edges}).model_dump()

    policy = None
    if "retry_policy" in selected:
        policy = {"retries": 2, "backoff_ms": 2000, "backoff_multiplier": 2.0, "retry_on": "transient"}

    wf = Workflow(
        name=f"{title} pipeline",
        description=f"Built by the AI System Builder from: {(spec.get('purpose') or '')[:300]}",
        graph=graph,
        is_active=False,  # the user activates after filling source credentials
        policy_json=policy,
        tags=["system-builder"],
    )
    wf.owner_id = draft.owner_id
    db.add(wf)
    await db.flush()
    await db.refresh(wf)
    await snapshot_workflow_version(db, wf)
    built["workflow_id"] = wf.id
    built["workflow_name"] = wf.name
    built["policy"] = policy

    # --- 3) the schema contract ---------------------------------------------
    if "schema_contract" in selected:
        if fields:
            cols = [{"name": f["name"], "dtype": f.get("dtype") or "text", "nullable": True} for f in fields]
            on_violation = "error" if "quality_gate" in selected else "warn"
            contract = await contracts_svc.put_contract(db, ds, cols, on_violation=on_violation)
            built["contract_version"] = contract.version
            built["on_violation"] = on_violation
        else:
            notes.append("Contract pending: no columns defined yet - answer the fields question, "
                         "then save the contract from the dataset editor.")

    # --- 4) the dashboard -----------------------------------------------------
    if "dashboard" in selected:
        from ..models import Dashboard

        config = dash_svc.generate_config([(ds, pd.DataFrame())])
        board = Dashboard(
            name=f"{title} dashboard",
            slug=await dash_svc.unique_slug(db, f"{title} dashboard"),
            description=f"Auto-built for the {title} system",
            config=config,
            status="draft",
        )
        board.owner_id = draft.owner_id
        db.add(board)
        await db.flush()
        built["dashboard_id"] = board.id

    # --- 5) the scheduled report ----------------------------------------------
    if "scheduled_report" in selected:
        sched = spec.get("schedule") or {}
        cron = sched.get("cron") or _interval_to_cron(int(sched.get("interval_seconds") or 86_400))
        rep = ScheduledReport(
            name=f"{title} report", source_type="dataset", source_id=ds.id,
            fmt=spec.get("report_fmt") or "csv", cron=cron, enabled=True,
        )
        rep.owner_id = draft.owner_id
        db.add(rep)
        await db.flush()
        built["report_id"] = rep.id
        built["report_cron"] = cron

    # --- 6) the failure-notification rule --------------------------------------
    if "failure_notification" in selected:
        if spec.get("webhook_url"):
            rule = NotificationRule(
                name=f"{title} failure alerts", events=["execution_failed"],
                webhook_url=spec["webhook_url"], workflow_id=wf.id, enabled=True,
            )
            rule.owner_id = draft.owner_id
            db.add(rule)
            await db.flush()
            built["notification_rule_id"] = rule.id
        else:
            notes.append("Failure alerts pending: no webhook URL yet - answer the webhook "
                         "question or add a rule on the Notifications page.")

    built["notes"] = notes
    return built
