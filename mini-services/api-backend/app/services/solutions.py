"""Solution marketplace (v60) - outcome-named solutions over packs.

The gallery said "Webhook workflow"; the marketplace says "Customer
Support Automation" and shows WHAT YOU GET. A Solution is a thin,
outcome-named shell around a standard py8n-pack document (workflows +
datasets), so:

- **installing** a solution reuses the exact pack-import machinery
  (every created workflow lands inactive, datasets carry sample rows);
- **authoring** is two-way: anyone can publish their own
  workflows/datasets as a solution with a capability checklist;
- **curation** ships three showcase solutions from the roadmap
  (Customer Support Automation, Invoice Processing, API Monitoring),
  seeded idempotently by slug - and every seeded graph passes the same
  ``validate_graph_document`` gate as user workflows, so a solution can
  never ship a broken graph.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..engine.runner import validate_graph_document
from ..models import Solution

PACK_FORMAT = "py8n-pack"
PACK_VERSION = 1


def _wf(name: str, description: str, graph: dict) -> dict:
    return {"name": name, "description": description, "graph": graph}


def _ds(name: str, description: str, schema: list, rows: list[dict]) -> dict:
    return {"name": name, "description": description, "schema": schema, "rows": rows}


def _pack(workflows: list[dict], datasets: list[dict] | None = None) -> dict:
    return {
        "format": PACK_FORMAT,
        "pack_version": PACK_VERSION,
        "workflows": workflows,
        "datasets": datasets or [],
    }


# ---------------------------------------------------------------------------
# Curated showcase solutions (roadmap v1.60) - offline-runnable graphs
# ---------------------------------------------------------------------------

SUPPORT_TICKETS_GRAPH = {
    "nodes": [
        {"id": "intake", "type": "manual_trigger", "name": "Ticket Intake",
         "position": {"x": 0, "y": 0},
         "parameters": {"payload": {"customer": "Acme Corp", "channel": "email",
                                    "message": "The export button has been broken since Monday and I need the Q3 numbers for the board meeting tomorrow.",
                                    "plan": "enterprise"}}},
        {"id": "classify", "type": "llm_chat", "name": "AI Classification",
         "position": {"x": 220, "y": 0},
         "parameters": {"provider": "sandbox_bridge",
                        "system_prompt": "You triage support tickets. Reply with ONE JSON line: {\"category\": \"bug|billing|howto|feature\", \"priority\": \"low|normal|high|critical\", \"sentiment\": \"calm|frustrated|angry\"}. No prose.",
                        "user_prompt": "Ticket from {{ nodes.intake.output.payload.customer }} ({{ nodes.intake.output.payload.plan }} plan, channel {{ nodes.intake.output.payload.channel }}): {{ nodes.intake.output.payload.message }}"}},
        {"id": "parse", "type": "code", "name": "Parse Verdict",
         "position": {"x": 440, "y": 0},
         "parameters": {"code": ("import json\n"
                                 "raw = str({{ nodes.classify.output.text }}).strip()\n"
                                 "start, end = raw.find('{'), raw.rfind('}')\n"
                                 "verdict = json.loads(raw[start:end + 1]) if start >= 0 else {}\n"
                                 "result = {'customer': {{ nodes.intake.output.payload.customer }}, 'message': {{ nodes.intake.output.payload.message }}, 'category': verdict.get('category', 'howto'), 'priority': verdict.get('priority', 'normal'), 'sentiment': verdict.get('sentiment', 'calm')}}")}},
        {"id": "write", "type": "dataset_write", "name": "Ticket Ledger",
         "position": {"x": 660, "y": 0},
         "parameters": {"dataset": "support_tickets", "mode": "append"}},
    ],
    "edges": [
        {"id": "e1", "source": "intake", "target": "classify"},
        {"id": "e2", "source": "classify", "target": "parse"},
        {"id": "e3", "source": "parse", "target": "write"},
    ],
}

INVOICE_GRAPH = {
    "nodes": [
        {"id": "intake", "type": "manual_trigger", "name": "Invoice Intake",
         "position": {"x": 0, "y": 0},
         "parameters": {"payload": {"invoice_id": "INV-2044", "vendor": "Northwind Oy",
                                    "amount": 4820.0, "currency": "EUR", "cost_center": "OPS"}}},
        {"id": "validate", "type": "code", "name": "Validation Rules",
         "position": {"x": 220, "y": 0},
         "parameters": {"code": ("p = {{ nodes.intake.output.payload }}\n"
                                 "errors = []\n"
                                 "if not str(p.get('invoice_id', '')).startswith('INV-'):\n"
                                 "    errors.append('invoice id format')\n"
                                 "if float(p.get('amount', 0)) <= 0:\n"
                                 "    errors.append('amount must be positive')\n"
                                 "if p.get('currency') not in ('EUR', 'USD', 'GBP'):\n"
                                 "    errors.append('unsupported currency')\n"
                                 "if not p.get('cost_center'):\n"
                                 "    errors.append('cost center missing')\n"
                                 "result = {'invoice': p, 'valid': not errors, 'errors': errors}")}},
        {"id": "gate", "type": "if_condition", "name": "Valid?",
         "position": {"x": 440, "y": 0},
         "parameters": {"left_value": "{{ nodes.validate.output.result.valid }}",
                        "operator": "is_true"}},
        {"id": "shape_ok", "type": "set_variable", "name": "Approved Row",
         "position": {"x": 660, "y": -80},
         "parameters": {"assignments": {
             "invoice_id": "{{ nodes.validate.output.result.invoice.invoice_id }}",
             "vendor": "{{ nodes.validate.output.result.invoice.vendor }}",
             "amount": "{{ nodes.validate.output.result.invoice.amount }}",
             "currency": "{{ nodes.validate.output.result.invoice.currency }}",
             "cost_center": "{{ nodes.validate.output.result.invoice.cost_center }}",
             "valid": True}, "keep_input": False}},
        {"id": "approve", "type": "dataset_write", "name": "Approved Invoices",
         "position": {"x": 880, "y": -80},
         "parameters": {"dataset": "invoices_approved", "mode": "append"}},
        {"id": "shape_bad", "type": "set_variable", "name": "Exception Row",
         "position": {"x": 660, "y": 80},
         "parameters": {"assignments": {
             "invoice_id": "{{ nodes.validate.output.result.invoice.invoice_id }}",
             "vendor": "{{ nodes.validate.output.result.invoice.vendor }}",
             "errors": "{{ nodes.validate.output.result.errors }}",
             "valid": False}, "keep_input": False}},
        {"id": "exception", "type": "dataset_write", "name": "Exceptions",
         "position": {"x": 880, "y": 80},
         "parameters": {"dataset": "invoice_exceptions", "mode": "append"}},
    ],
    "edges": [
        {"id": "e1", "source": "intake", "target": "validate"},
        {"id": "e2", "source": "validate", "target": "gate"},
        {"id": "e3", "source": "gate", "target": "shape_ok", "sourceHandle": "true"},
        {"id": "e4", "source": "shape_ok", "target": "approve"},
        {"id": "e5", "source": "gate", "target": "shape_bad", "sourceHandle": "false"},
        {"id": "e6", "source": "shape_bad", "target": "exception"},
    ],
}

API_MONITOR_GRAPH = {
    "nodes": [
        {"id": "tick", "type": "schedule_trigger", "name": "Every 5 Minutes",
         "position": {"x": 0, "y": 0},
         "parameters": {"mode": "interval", "interval_seconds": 300}},
        {"id": "probe", "type": "http_request", "name": "Probe Endpoint",
         "position": {"x": 220, "y": 0},
         "parameters": {"url": "https://your-api.example.com/health", "method": "GET"}},
        {"id": "check", "type": "code", "name": "Latency + Status Check",
         "position": {"x": 440, "y": 0},
         "parameters": {"code": ("r = {{ nodes.probe.output }}\n"
                                 "status = r.get('status_code') or r.get('statusCode') or 0\n"
                                 "result = {'checked_at': {{ nodes.tick.output.scheduled_time }}, 'status': status, 'ok': 200 <= int(status or 0) < 400, 'latency_ms': r.get('duration_ms') or r.get('latency_ms') or 0}")}},
        {"id": "metrics", "type": "dataset_write", "name": "Uptime Metrics",
         "position": {"x": 660, "y": 0},
         "parameters": {"dataset": "api_uptime", "mode": "append"}},
    ],
    "edges": [
        {"id": "e1", "source": "tick", "target": "probe"},
        {"id": "e2", "source": "probe", "target": "check"},
        {"id": "e3", "source": "check", "target": "metrics"},
    ],
}

# ---------------------------------------------------------------------------
# v64: model solutions - packs whose install IS a model system. Each trains
# (or serves) a real model through the engine, fully offline.
# ---------------------------------------------------------------------------

SENTIMENT_TRAIN_GRAPH = {
    "nodes": [
        {"id": "intake", "type": "manual_trigger", "name": "Run Training",
         "position": {"x": 0, "y": 0}, "parameters": {}},
        {"id": "read", "type": "dataset_read", "name": "Labeled Corpus",
         "position": {"x": 220, "y": 0}, "parameters": {"dataset": "sentiment_corpus"}},
        {"id": "feats", "type": "text_features", "name": "Text Features (fit)",
         "position": {"x": 440, "y": 0},
         "parameters": {"column": "text", "mode": "fit", "featurizer": "sentiment-vec",
                        "svd_dims": 6, "ngram_max": 1, "prefix": "txt"}},
        {"id": "train", "type": "neural_train", "name": "Train Sentiment Model",
         "position": {"x": 660, "y": 0},
         "parameters": {"task": "classification", "target": "label",
                        "features": "txt_vec_0,txt_vec_1,txt_vec_2,txt_vec_3,txt_vec_4,txt_vec_5",
                        "hidden_layers": "16,8", "epochs": 40, "batch_size": 8,
                        "learning_rate": 0.02, "model_name": "sentiment_model", "register": True}},
    ],
    "edges": [
        {"id": "e1", "source": "intake", "target": "read"},
        {"id": "e2", "source": "read", "target": "feats"},
        {"id": "e3", "source": "feats", "target": "train"},
    ],
}

SENTIMENT_SERVE_GRAPH = {
    "nodes": [
        {"id": "intake", "type": "manual_trigger", "name": "Run Scorer",
         "position": {"x": 0, "y": 0}, "parameters": {}},
        {"id": "read", "type": "dataset_read", "name": "Inbox",
         "position": {"x": 220, "y": 0}, "parameters": {"dataset": "sentiment_inbox"}},
        {"id": "feats", "type": "text_features", "name": "Text Features (transform)",
         "position": {"x": 440, "y": 0},
         "parameters": {"column": "text", "mode": "transform", "featurizer": "sentiment-vec",
                        "prefix": "txt"}},
        {"id": "predict", "type": "model_predict", "name": "Score Sentiment",
         "position": {"x": 660, "y": 0}, "parameters": {"model": "sentiment_model"}},
        {"id": "write", "type": "dataset_write", "name": "Score Ledger",
         "position": {"x": 880, "y": 0},
         "parameters": {"dataset": "sentiment_scores", "mode": "append"}},
    ],
    "edges": [
        {"id": "e1", "source": "intake", "target": "read"},
        {"id": "e2", "source": "read", "target": "feats"},
        {"id": "e3", "source": "feats", "target": "predict"},
        {"id": "e4", "source": "predict", "target": "write"},
    ],
}

LM_PRETRAIN_GRAPH = {
    "nodes": [
        {"id": "intake", "type": "manual_trigger", "name": "Run Pretraining",
         "position": {"x": 0, "y": 0}, "parameters": {}},
        {"id": "read", "type": "dataset_read", "name": "Support Corpus",
         "position": {"x": 220, "y": 0}, "parameters": {"dataset": "lm_corpus"}},
        {"id": "train", "type": "lm_train", "name": "Pretrain Language Model",
         "position": {"x": 440, "y": 0},
         "parameters": {"text_column": "doc", "vocab_size": 200, "d_model": 32,
                        "n_heads": 2, "n_ctx": 12, "epochs": 10, "batch_size": 8,
                        "learning_rate": 0.005, "model_name": "support_lm", "register": True}},
    ],
    "edges": [
        {"id": "e1", "source": "intake", "target": "read"},
        {"id": "e2", "source": "read", "target": "train"},
    ],
}

LM_CONTINUE_GRAPH = {
    "nodes": [
        {"id": "intake", "type": "manual_trigger", "name": "Run Continued Pretraining",
         "position": {"x": 0, "y": 0}, "parameters": {}},
        {"id": "read", "type": "dataset_read", "name": "Legal Corpus",
         "position": {"x": 220, "y": 0}, "parameters": {"dataset": "lm_corpus_legal"}},
        {"id": "train", "type": "lm_train", "name": "Continue Pretraining",
         "position": {"x": 440, "y": 0},
         "parameters": {"text_column": "doc", "base_model": "support_lm",
                        "epochs": 8, "batch_size": 8, "learning_rate": 0.003,
                        "model_name": "support_lm", "register": True}},
    ],
    "edges": [
        {"id": "e1", "source": "intake", "target": "read"},
        {"id": "e2", "source": "read", "target": "train"},
    ],
}

LM_GENERATE_GRAPH = {
    "nodes": [
        {"id": "intake", "type": "manual_trigger", "name": "Prompt",
         "position": {"x": 0, "y": 0},
         "parameters": {"payload": {"prompt": "the agent"}}},
        {"id": "gen", "type": "lm_generate", "name": "Generate Text",
         "position": {"x": 220, "y": 0},
         "parameters": {"model": "support_lm",
                        "prompt": "{{ nodes.intake.output.payload.prompt }}",
                        "max_tokens": 8, "temperature": 0.7, "top_k": 20}},
        {"id": "write", "type": "dataset_write", "name": "Sample Ledger",
         "position": {"x": 440, "y": 0},
         "parameters": {"dataset": "lm_samples", "mode": "append"}},
    ],
    "edges": [
        {"id": "e1", "source": "intake", "target": "gen"},
        {"id": "e2", "source": "gen", "target": "write"},
    ],
}

CURATED_SOLUTIONS: list[dict] = [
    {
        "slug": "customer-support-automation",
        "name": "Customer Support Automation",
        "tagline": "Tickets in, triaged and ledgered out - AI classification, priority detection and a searchable ticket ledger.",
        "category": "Support",
        "icon": "headset",
        "color": "#22d3ee",
        "outcomes_json": [
            "Ticket intake",
            "AI classification",
            "Priority detection",
            "Sentiment capture",
            "Ticket ledger dataset",
            "Escalation-ready verdicts",
        ],
        "pack_json": _pack(
            [_wf("Support Ticket Triage",
                 "Intake -> AI classification -> verdict parsing -> ticket ledger.",
                 SUPPORT_TICKETS_GRAPH)],
            [_ds("support_tickets", "Every triaged support ticket",
                 [{"name": "customer", "dtype": "text"}, {"name": "message", "dtype": "text"},
                  {"name": "category", "dtype": "text"}, {"name": "priority", "dtype": "text"},
                  {"name": "sentiment", "dtype": "text"}],
                 [{"customer": "Globex", "message": "API key stopped working after rotation",
                   "category": "bug", "priority": "high", "sentiment": "frustrated"},
                  {"customer": "Initech", "message": "How do I bulk-import users?",
                   "category": "howto", "priority": "normal", "sentiment": "calm"}]),
             ],
        ),
        "docs": ("Install, then open 'Support Ticket Triage' and press Run - the sample ticket is triaged "
                 "by the free built-in LLM and lands in the support_tickets dataset. Wire the Manual Trigger "
                 "to your form/webhook for real intake. Runs fully offline."),
    },
    {
        "slug": "invoice-processing",
        "name": "Invoice Processing",
        "tagline": "Validation rules, an approval gate and an exception lane - invoices land clean or get flagged.",
        "category": "Finance",
        "icon": "receipt",
        "color": "#a3e635",
        "outcomes_json": [
            "Invoice intake",
            "Validation rules",
            "Approval gate",
            "Exception handling",
            "Approved-invoice ledger",
            "Exception ledger",
        ],
        "pack_json": _pack(
            [_wf("Invoice Approval Flow",
                 "Intake -> validation rules -> IF gate -> approved / exception ledgers.",
                 INVOICE_GRAPH)],
            [_ds("invoices_approved", "Invoices that passed validation", [],
                 [{"invoice_id": "INV-2001", "vendor": "Acme Oy", "amount": 120.0,
                   "currency": "EUR", "cost_center": "OPS", "valid": True, "errors": None}]),
             _ds("invoice_exceptions", "Invoices that failed validation", [],
                 [{"invoice_id": "INV-2002", "vendor": "Globex Ltd", "amount": -5.0,
                   "currency": "EUR", "cost_center": "", "valid": False,
                   "errors": ["amount must be positive"]}]),
             ],
        ),
        "docs": ("Install and press Run on 'Invoice Approval Flow' - the sample invoice passes validation and "
                 "lands in invoices_approved; flip the payload to see the exception lane. Add the email/document "
                 "extraction nodes in the editor to ingest real PDFs."),
    },
    {
        "slug": "api-monitoring",
        "name": "API Monitoring",
        "tagline": "Scheduled probes, latency and status checks, and an uptime metrics trail you can chart.",
        "category": "Operations",
        "icon": "activity",
        "color": "#fbbf24",
        "outcomes_json": [
            "Scheduled checks",
            "Latency monitoring",
            "Status/health detection",
            "Uptime metrics dataset",
            "Failure-detection fields",
            "Report-ready history",
        ],
        "pack_json": _pack(
            [_wf("API Uptime Monitor",
                 "Schedule -> HTTP probe -> latency/status check -> uptime metrics.",
                 API_MONITOR_GRAPH)],
            [_ds("api_uptime", "Probe results over time",
                 [{"name": "checked_at", "dtype": "datetime"}, {"name": "status", "dtype": "integer"},
                  {"name": "ok", "dtype": "boolean"}, {"name": "latency_ms", "dtype": "number"}],
                 [{"checked_at": "2026-01-01T09:00:00+00:00", "status": 200, "ok": True, "latency_ms": 118},
                  {"checked_at": "2026-01-01T09:05:00+00:00", "status": 503, "ok": False, "latency_ms": 4021}]),
             ],
        ),
        "docs": ("Install, point 'Probe Endpoint' at your real health endpoint, activate the trigger - "
                 "every 5 minutes the result lands in api_uptime. Chart it from the Reports page or "
                 "generate a dashboard over the dataset."),
    },
    {
        "slug": "sentiment-model-system",
        "name": "Text Sentiment Model System",
        "tagline": "A complete sentiment model as one operating unit: labeled corpus in, featurizer + trained classifier out, serving workflow on top - install it as a MODEL SYSTEM.",
        "category": "AI Models",
        "icon": "smile",
        "color": "#34d399",
        "model_system": {"modalities": ["text"]},
        "outcomes_json": [
            "Labeled sentiment corpus",
            "Fitted text featurizer (serving parity)",
            "From-scratch neural classifier",
            "Model registry entry",
            "Serving workflow (transform -> predict)",
            "Score ledger dataset",
        ],
        "pack_json": _pack(
            [
                _wf("Train Sentiment Model",
                    "Corpus -> text_features(fit) -> neural_train -> registry.",
                    SENTIMENT_TRAIN_GRAPH),
                _wf("Serve Sentiment Scorer",
                    "Inbox -> text_features(transform) -> model_predict -> score ledger.",
                    SENTIMENT_SERVE_GRAPH),
            ],
            [
                _ds("sentiment_corpus", "Labeled sentiment training corpus",
                    [{"name": "text", "dtype": "text"}, {"name": "label", "dtype": "text"}],
                    [{"text": "the new update is fantastic and fast", "label": "positive"},
                     {"text": "absolutely love the smooth experience", "label": "positive"},
                     {"text": "great support and quick resolution", "label": "positive"},
                     {"text": "the team delivered beyond expectations", "label": "positive"},
                     {"text": "brilliant product worth every cent", "label": "positive"},
                     {"text": "happy with the improved dashboard", "label": "positive"},
                     {"text": "excellent quality and fast delivery", "label": "positive"},
                     {"text": "the release works beautifully", "label": "positive"},
                     {"text": "pricing is fair and the tool is great", "label": "positive"},
                     {"text": "wonderful onboarding and clear docs", "label": "positive"},
                     {"text": "the service keeps breaking weekly", "label": "negative"},
                     {"text": "terrible latency and constant errors", "label": "negative"},
                     {"text": "support never replied to my ticket", "label": "negative"},
                     {"text": "the billing mistakes are frustrating", "label": "negative"},
                     {"text": "worst upgrade we ever installed", "label": "negative"},
                     {"text": "the app crashes on every export", "label": "negative"},
                     {"text": "disappointed by the broken search", "label": "negative"},
                     {"text": "slow imports ruin the workflow", "label": "negative"},
                     {"text": "the connector fails without warning", "label": "negative"},
                     {"text": "regret switching to this platform", "label": "negative"}]),
                _ds("sentiment_inbox", "Fresh messages waiting to be scored",
                    [{"name": "text", "dtype": "text"}],
                    [{"text": "the export finally works great"}, {"text": "another crash during import"}]),
                _ds("sentiment_scores", "Scored messages land here", [], []),
            ],
        ),
        "docs": ("Install AS A MODEL SYSTEM, then run 'Train Sentiment Model' once - it fits the "
                 "text featurizer and trains the classifier into the registry. Run 'Serve Sentiment "
                 "Scorer' to score the inbox through the SAME featurizer into sentiment_scores. "
                 "Runs fully offline."),
    },
    {
        "slug": "language-model-system",
        "name": "Language Model System",
        "tagline": "A tiny causal transformer trained FROM SCRATCH on your support corpus, continued-pretrained on a legal corpus, then sampled for text - the full LM life cycle offline.",
        "category": "AI Models",
        "icon": "languages",
        "color": "#c084fc",
        "model_system": {"modalities": ["text"]},
        "outcomes_json": [
            "Support-domain training corpus",
            "From-scratch transformer pretraining",
            "Continued pretraining on a new domain",
            "Version lineage (continued_pretrained_from)",
            "Held-out perplexity evaluation",
            "Autoregressive text generation workflow",
        ],
        "pack_json": _pack(
            [
                _wf("Pretrain Language Model",
                    "Corpus -> lm_train (from-scratch transformer, registered as support_lm).",
                    LM_PRETRAIN_GRAPH),
                _wf("Continue Pretraining",
                    "Legal corpus -> lm_train(base_model=support_lm) -> version 2 with lineage.",
                    LM_CONTINUE_GRAPH),
                _wf("Generate With Language Model",
                    "Prompt -> lm_generate -> sample ledger.",
                    LM_GENERATE_GRAPH),
            ],
            [
                _ds("lm_corpus", "Support-domain pretraining corpus",
                    [{"name": "doc", "dtype": "text"}],
                    [{"doc": "the agent replies to the customer about the login issue"},
                     {"doc": "the agent fixes the login bug today"},
                     {"doc": "the customer asks about the refund policy"},
                     {"doc": "the agent ships the order to the customer"},
                     {"doc": "the ticket about the login issue is closed"},
                     {"doc": "the refund policy covers the order"},
                     {"doc": "the agent escalates the ticket to the team"},
                     {"doc": "the customer thanks the agent today"},
                     {"doc": "the login bug blocks the order today"},
                     {"doc": "the team reviews the refund ticket"},
                     {"doc": "the agent answers the ticket about the policy"},
                     {"doc": "the customer reopens the login ticket"},
                     {"doc": "the agent closes the refund ticket today"},
                     {"doc": "the team fixes the policy bug"},
                     {"doc": "the order ships after the agent reviews it"}]),
                _ds("lm_corpus_legal", "Legal-domain corpus for continued pretraining",
                    [{"name": "doc", "dtype": "text"}],
                    [{"doc": "the contract defines the refund policy for the customer"},
                     {"doc": "the policy states the order terms clearly"},
                     {"doc": "the agent reviews the contract with the team"},
                     {"doc": "the customer signs the policy today"},
                     {"doc": "the contract covers the ticket escalation terms"},
                     {"doc": "the team updates the refund contract"},
                     {"doc": "the policy agent explains the terms"},
                     {"doc": "the contract team closes the policy ticket"}]),
                _ds("lm_samples", "Generated text samples land here", [], []),
            ],
        ),
        "docs": ("Install AS A MODEL SYSTEM, then run the three workflows in order: 'Pretrain Language "
                 "Model' trains the transformer from scratch, 'Continue Pretraining' adapts it to the "
                 "legal corpus (weights + tokenizer carry over, lineage recorded), 'Generate With "
                 "Language Model' samples text into lm_samples. Runs fully offline."),
    },
]


def solution_summary(s: Solution) -> dict:
    return {
        "id": s.id,
        "slug": s.slug,
        "name": s.name,
        "tagline": s.tagline,
        "category": s.category,
        "icon": s.icon,
        "color": s.color,
        "outcomes": list(s.outcomes_json or []),
        "installs": int(s.installs or 0),
        "curated": s.owner_id is None,
        "model_system_ready": s.slug in MODEL_SYSTEM_MODALITIES,  # v64: installs as a model system
        "workflow_count": len((s.pack_json or {}).get("workflows", [])),
        "dataset_count": len((s.pack_json or {}).get("datasets", [])),
    }


# ---------------------------------------------------------------------------
# v64: model-system metadata + install-time model-name finalization
# ---------------------------------------------------------------------------

# Curated solutions whose install IS a model system: slug -> declared modalities.
MODEL_SYSTEM_MODALITIES: dict[str, list[str]] = {
    "sentiment-model-system": ["text"],
    "language-model-system": ["text"],
}

_TRAINER_NODE_TYPES = {"model_train", "neural_train", "lm_train"}
_SCORER_NODE_TYPES = {"model_predict", "drift_check", "lm_generate"}


async def finalize_pack_model_names(db: AsyncSession, pack: dict, owner_id: str | None) -> dict:
    """Second half of install-time finalization (v64): model registry names.

    ``next_version`` and ``deactivate_others`` scope by NAME, so two users
    installing the same solution would otherwise SHARE one version chain -
    the second install would register as v2 of the FIRST user's model and
    deactivate it. When a trainer node's model_name collides with a
    registry chain owned by someone else, suffix it ("name 2", "name 3", ...)
    and remap EVERY model reference in the pack (model_name on trainers,
    base_model for fine-tune/continued-pretraining, model on scorers)
    consistently - each install stays a self-consistent model system.
    """
    if owner_id is None:
        return pack  # single-tenant installs intentionally share the namespace

    created: set[str] = set()
    for w in pack.get("workflows", []):
        for n in (w.get("graph") or {}).get("nodes", []):
            if n.get("type") in _TRAINER_NODE_TYPES:
                name = str((n.get("parameters") or {}).get("model_name") or "").strip()
                if name:
                    created.add(name)
    if not created:
        return pack

    from ..models import TrainedModel

    async def _chain_owners(name: str) -> list[str | None]:
        return list(
            (await db.execute(select(TrainedModel.owner_id).where(TrainedModel.name == name)))
            .scalars()
            .all()
        )

    mapping: dict[str, str] = {}
    for name in sorted(created):
        owners = await _chain_owners(name)
        if not owners or all(o in (owner_id, None) for o in owners):
            continue  # unclaimed or already this user's chain
        candidate = name
        for suffix in range(2, 100):
            candidate = f"{name} {suffix}"
            owners2 = await _chain_owners(candidate)
            if not owners2 or all(o in (owner_id, None) for o in owners2):
                break
        mapping[name] = candidate
    if not mapping:
        return pack

    def _remap(graph: dict) -> dict:
        g = {**graph, "nodes": [dict(n) for n in graph.get("nodes", [])]}
        for n in g["nodes"]:
            nt = n.get("type")
            if nt not in _TRAINER_NODE_TYPES and nt not in _SCORER_NODE_TYPES:
                continue
            params = dict(n.get("parameters") or {})
            touched = False
            if nt in _TRAINER_NODE_TYPES:
                for key in ("model_name", "base_model"):
                    val = str(params.get(key) or "").strip()
                    if val in mapping:
                        params[key] = mapping[val]
                        touched = True
            val = str(params.get("model") or "").strip()
            if val in mapping:
                params["model"] = mapping[val]
                touched = True
            if touched:
                n["parameters"] = params
        return g

    workflows_out = [{**w, "graph": _remap(w.get("graph") or {"nodes": [], "edges": []})}
                     for w in pack.get("workflows", [])]
    return {**pack, "workflows": workflows_out}


def pack_summary(s: Solution) -> dict:
    """Inspect-style summary of the embedded pack (no import needed)."""
    pack = s.pack_json or {}
    node_types: list[str] = []
    for w in pack.get("workflows", []):
        for n in (w.get("graph") or {}).get("nodes", []):
            nt = (n or {}).get("type")
            if nt and nt not in node_types:
                node_types.append(nt)
    return {
        "workflows": [{"name": w.get("name"), "description": w.get("description", "")}
                      for w in pack.get("workflows", [])],
        "datasets": [{"name": d.get("name"), "rows": len(d.get("rows") or [])}
                     for d in pack.get("datasets", [])],
        "node_types": node_types,
    }


async def ensure_seeded(db: AsyncSession) -> int:
    """Idempotent top-up of the curated showcase solutions (by slug)."""
    existing = {
        row[0]
        for row in (await db.execute(select(Solution.slug))).all()
    }
    added = 0
    for spec in CURATED_SOLUTIONS:
        if spec["slug"] in existing:
            continue
        # a curated solution can never ship a broken graph - validate every
        # workflow graph with the same gate user workflows go through
        for w in spec["pack_json"].get("workflows", []):
            validate_graph_document(w["graph"])
        row = Solution(
            slug=spec["slug"], name=spec["name"], tagline=spec["tagline"],
            category=spec["category"], icon=spec["icon"], color=spec["color"],
            outcomes_json=spec["outcomes_json"], pack_json=spec["pack_json"],
            docs=spec["docs"], installs=0, owner_id=None,
        )
        db.add(row)
        added += 1
    if added:
        await db.flush()
    return added


# ---------------------------------------------------------------------------
# Install-time name finalization - solutions install self-consistently
# ---------------------------------------------------------------------------

_REF_NODE_TYPES = {"dataset_write", "dataset_read", "sql_query", "dataset_export"}


async def finalize_pack_dataset_names(db: AsyncSession, pack: dict) -> dict:
    """Return a copy of the pack whose dataset names are globally unique.

    Dataset names are unique across the whole instance (single table), but a
    solution's workflows REFERENCE its datasets BY NAME. Two users installing
    the same solution would otherwise collide: the second user's datasets land
    suffixed ('... 2') while their workflow still writes to the bare name -
    which belongs to the first user. Remapping every graph reference to the
    FINAL name makes each install a self-consistent system.
    """
    from . import datasets as ds_svc

    mapping: dict[str, str] = {}
    datasets_out: list[dict] = []
    for d in pack.get("datasets", []):
        old = str(d.get("name") or "").strip()
        candidate = old
        n = 1
        while await ds_svc.name_taken(db, candidate):
            n += 1
            candidate = f"{old} {n}"
        mapping[old] = candidate
        datasets_out.append({**d, "name": candidate})

    def _remap_graph(graph: dict) -> dict:
        g = {**graph, "nodes": [dict(n) for n in graph.get("nodes", [])]}
        for n in g["nodes"]:
            params = dict(n.get("parameters") or {})
            target = str(params.get("dataset") or "").strip()
            if target in mapping:
                params["dataset"] = mapping[target]
                n["parameters"] = params
            elif n.get("type") in _REF_NODE_TYPES:
                n["parameters"] = params  # keep as-is, normalized
        return g

    workflows_out = []
    for w in pack.get("workflows", []):
        w2 = {**w, "graph": _remap_graph(w.get("graph") or {"nodes": [], "edges": []})}
        workflows_out.append(w2)

    return {**pack, "datasets": datasets_out, "workflows": workflows_out}
