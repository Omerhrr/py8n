"""Marketplace operators (v83 + v86) - "Install a business operator".

A solution ships a PACK (workflows + datasets, installed inactive). An
operator ships the BUSINESS: the interaction primitives (AI agent,
meeting room, waiting queue, outbound campaign), the REACTIVE workflows
wired to the v80 event system (event_trigger -> shape -> dataset write),
the datasets the business writes into, and the STAFF DASHBOARD over
those datasets - all bound into a RUNNING Py8nSystem (v81) with the
operations log and the system.installed event.

"Install a business operator" not "Install a workflow template": one
click hires a department that is already wired - meetings that log
themselves when they end, leads that score themselves when calls end,
appointment requests that land from inbound texts. The credentials
(SMS channels, dialing endpoints, LLM brains) are the installer's to
bind; until they exist the passes record honest skips - never silent
ones. Channels stay interchangeable infrastructure: the operator owns
the system underneath them.

v86 grew the shelf to NINE and closed the intake loop: every
department-shaped operator (support, operations, hr, finance,
procurement, logistics) now ships a pre-wired PROCESS seeded from its
dataset, an intake workflow that opens tracked instances (the v86
business_start node), an advancer that moves them on real calls, and
escalate self-loops for the scheduler door - the machine watches every
entity from the moment it exists.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..engine.runner import validate_graph_document
from .operators_catalog import CATALOG as _CATALOG


class OperatorError(ValueError):
    """Honest operator install failures."""


# ---------------------------------------------------------------------------
# The curated shelf - every operator declares the exact topology it builds.
# The nine operator dicts themselves live in operators_catalog/ (task #3
# split: nine ~150-200 line declarative dicts back to back dominated this
# file's length) - _CATALOG above preserves their original shelf order.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# v113: the ERP CORE - the tenth operator, the backbone that composes the
# departments into a COMPANY. The shelf already hires departments (sales,
# finance, procurement, logistics); what it lacked is the order desk, the
# stock room and the books that tie them together. This operator ships the
# classic ERP spine as native primitives:
#
#   ORDER-TO-CASH:  Sales order lifecycle --shipped--> Invoice lifecycle
#     (the Finance operator's machine opens the receivable by itself)
#   REPLENISHMENT:  Inventory replenishment --reorder_placed--> Purchase
#     lifecycle --ordered--> Delivery pipeline --delivered--> Invoice
#     (a stock dip walks three departments to the vendor's bill)
#   THE BOOKS:      every order move posts to GL entries; every pick lands
#     a stock movement - the ledger is a WORKFLOW, not a report
#
# The journeys resolve by name at fire time, so the ERP installs standalone
# (the legs skip honestly naming the missing department) and COMPOSES when
# the departments are in - one click each and the company runs end to end.
# ---------------------------------------------------------------------------
_ERP_OPERATOR = {
    "slug": "erp-operator",
    "name": "ERP Core",
    "tagline": ("The company backbone in one click: the order desk, the stock "
                "room and the books - sales orders that ship themselves into "
                "Finance's receivables, stock dips that walk the Supply chain "
                "to the vendor's bill, and a ledger that posts every move."),
    "category": "ERP",
    "icon": "factory",
    "color": "#f59e0b",
    "outcomes": [
        "Products + Sales orders datasets (catalog, order desk)",
        "Sales order lifecycle business process (draft to paid, seeded from the order desk)",
        "Inventory replenishment process (healthy / low / reorder - one tracked entity per SKU)",
        "Month-end close process (open a period, reconcile, review, close)",
        "Employees roster + Payroll runs datasets and the Payroll lifecycle machine (v114: the people get paid)",
        "Stock movements + GL entries datasets - the ERP's own ledgers",
        "Order ledger poster: every order move posts BALANCED journal lines - receivable AND revenue on the ship, cash AND the clearing on the payment (reactive, double-entry)",
        "Payroll poster: a paid payroll run posts salary expense debit + cash credit (reactive, balanced)",
        "Purchase ledger poster: the Supply chain's vendor bill posts Inventory / Accounts payable on the match and Accounts payable / Cash on the payment - the company's spending hits the books (reactive, balanced, v115)",
        "Stock pick ledger: every pick lands a movement row and a restock lands the goods coming in (reactive)",
        "ORDER-TO-CASH journey: a shipped order opens the Finance operator's invoice by itself",
        "REPLENISHMENT journey: a reorder opens the Procurement operator's purchase order",
        "ERP clerk grounded in the ERP policy + the ERP review room",
        "Staff dashboard over the whole back office",
    ],
    "datasets": [
        {"name": "Products",
         "description": "The catalog and the stock room - one row per SKU; the "
                        "replenishment machine watches each one from install, "
                        "and cost + reorder_qty are what the buy side posts "
                        "from (the journey hands them to the vendor's bill)",
         "columns": ["sku", "name", "price", "cost", "stock", "reorder_point",
                     "reorder_qty", "stock_status"],
         "rows": [
             {"sku": "SKU-1001", "name": "Steel shelf", "price": "120.00",
              "cost": "72.00", "stock": "42", "reorder_point": "10",
              "reorder_qty": "10", "stock_status": "healthy"},
             {"sku": "SKU-1002", "name": "Pallet jack", "price": "480.00",
              "cost": "288.00", "stock": "7", "reorder_point": "5",
              "reorder_qty": "5", "stock_status": "healthy"},
             {"sku": "SKU-1003", "name": "Safety gloves", "price": "9.50",
              "cost": "5.10", "stock": "14", "reorder_point": "20",
              "reorder_qty": "30", "stock_status": "low"},
             {"sku": "SKU-1004", "name": "LED work lamp", "price": "34.00",
              "cost": "19.00", "stock": "61", "reorder_point": "15",
              "reorder_qty": "20", "stock_status": "healthy"},
             {"sku": "SKU-1005", "name": "Cordless drill", "price": "89.00",
              "cost": "51.00", "stock": "3", "reorder_point": "8",
              "reorder_qty": "12", "stock_status": "low"},
         ]},
        {"name": "Sales orders",
         "description": "The order desk - one row per order; the onboarding loop "
                        "tracks every row on the lifecycle machine at its own stage",
         "columns": ["order", "customer", "sku", "qty", "total", "status"],
         "rows": [
             {"order": "SO-1042", "customer": "Meridian Builders",
              "sku": "SKU-1001", "qty": "6", "total": "720.00",
              "status": "draft"},
             {"order": "SO-1039", "customer": "Harbor Works",
              "sku": "SKU-1002", "qty": "2", "total": "960.00",
              "status": "confirmed"},
             {"order": "SO-1036", "customer": "Northline Garage",
              "sku": "SKU-1004", "qty": "4", "total": "136.00",
              "status": "picked"},
             {"order": "SO-1031", "customer": "Crest Facilities",
              "sku": "SKU-1005", "qty": "5", "total": "445.00",
              "status": "invoiced"},
             {"order": "SO-1028", "customer": "Beacon Labs",
              "sku": "SKU-1001", "qty": "2", "total": "240.00",
              "status": "paid"},
         ]},
        {"name": "Stock movements",
         "description": "The stock room's ledger - one row per pick (the pick "
                        "ledger appends every move); on-hand truth lives here",
         "columns": ["sku", "delta", "reason", "at"],
         "rows": []},
        {"name": "GL entries",
         "description": "The books - BALANCED journal lines posted by the "
                        "reactive posters (orders, stock, payroll, and since "
                        "v115 the PURCHASES - the vendor bill the Supply chain "
                        "opened posts its own pairs); the month-end close "
                        "reconciles from here and the trial balance reads "
                        "from here",
         "columns": ["ref", "account", "debit", "credit", "memo", "at"],
         "rows": []},
        {"name": "Employees",
         "description": "The roster - one row per person on payroll; the "
                        "People tab reads it, the clerk answers from it",
         "columns": ["emp", "name", "role", "salary", "status"],
         "rows": [
             {"emp": "E-01", "name": "Ada Mensah", "role": "Order desk",
              "salary": "4200.00", "status": "active"},
             {"emp": "E-02", "name": "Rio Tanaka", "role": "Stock room",
              "salary": "3800.00", "status": "active"},
             {"emp": "E-03", "name": "Mara Osei", "role": "Bookkeeper",
              "salary": "5100.00", "status": "active"},
             {"emp": "E-04", "name": "Kofi Adjei", "role": "Dispatcher",
              "salary": "3600.00", "status": "active"},
             {"emp": "E-05", "name": "Lena Vogt", "role": "Buyer",
              "salary": "4700.00", "status": "on_leave"},
         ]},
        {"name": "Payroll runs",
         "description": "The payroll calendar - one row per run (arrives "
                        "empty: the period is the bookkeeper's to run); the "
                        "onboarding loop tracks every new run on the machine",
         "columns": ["ref", "period", "gross", "headcount", "status"],
         "rows": []},
        {"name": "ERP policy",
         "description": "The clerk's knowledge - how the company runs",
         "columns": ["question", "answer"],
         "rows": [
             {"question": "What are the order stages",
              "answer": "Draft, confirmed, picked, shipped, invoiced, paid - the order desk confirms, the stock room picks, shipping moves it, and Finance's receivable opens itself on the ship."},
             {"question": "When does an order get invoiced",
              "answer": "On the ship: the shipped order journey opens the invoice on the Finance operator's machine with the same order ref, due in five days."},
             {"question": "What happens when stock dips",
              "answer": "A SKU below its reorder point moves to low; placing the reorder opens a purchase order on the Procurement operator's machine - the delivery and the vendor's bill follow the Supply chain."},
             {"question": "How does the month-end close work",
              "answer": "Open a period on the close machine, reconcile the GL entries against the ledgers, move to reviewed when the books agree, then close - the door watches the deadline."},
             {"question": "Where do the books land",
              "answer": "GL entries carries BALANCED journal lines - the posters pair every debit with a credit (receivable and revenue on the ship, cash and the clearing on the payment) - and Stock movements one row per pick; nothing is typed twice."},
             {"question": "How does payroll run",
              "answer": "Run it on the People tab: a payroll run row opens a tracked instance on the Payroll lifecycle machine - calculate, approve, then pay - and paying posts the balanced pair to the books (salary expense debited, cash credited)."},
             {"question": "When do purchases hit the books",
              "answer": "The buy side posts itself since v115: a stock dip walks the Supply chain and the vendor bill it opens carries cost*qty in the journey's own memory - the bill landing on matched debits Inventory and credits Accounts payable, paying it debits Accounts payable and credits Cash. The restock lands the goods on the Stock movements ledger. Receivables stay the order poster's side; the books balance either way."},
         ]},
    ],
    "workflows": [
        {"name": "Order ledger poster",
         "description": "business.state_changed -> shape -> GL entries: the "
                        "order's moves post BALANCED journal lines - shipped "
                        "debits receivable AND credits revenue, paid debits "
                        "cash AND clears the receivable, the order's total "
                        "riding the event's own context. Every other move "
                        "lands a zero-value trail line; every other machine "
                        "skips honestly. (v114: the books became "
                        "double-entry - a trial balance off this ledger "
                        "actually balances and speaks.)",
         "trigger": {"type": "event_trigger",
                     "params": {"event_type": "business.state_changed"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the journal lines",
              "params": {"code": (
                  "r = df.iloc[0] if len(df) else {}\n"
                  "pl = r.get('payload') or {}\n"
                  "rows = []\n"
                  "if (pl.get('process_name') or '') == 'Sales order lifecycle':\n"
                  "    ctx = pl.get('context') or {}\n"
                  "    total = str(ctx.get('total') or '')\n"
                  "    to = str(pl.get('to') or '')\n"
                  "    ref = str(pl.get('ref') or '')\n"
                  "    ts = r.get('triggered_at', '')\n"
                  "    if to == 'shipped' and total:\n"
                  "        rows = [{'ref': ref, 'account': 'Accounts receivable', "
                  "'debit': total, 'credit': '', "
                  "'memo': ref + ' moved to ' + to, 'at': ts},\n"
                  "                {'ref': ref, 'account': 'Revenue', "
                  "'debit': '', 'credit': total, "
                  "'memo': 'revenue recognized on ' + ref, 'at': ts}]\n"
                  "    elif to == 'paid' and total:\n"
                  "        rows = [{'ref': ref, 'account': 'Cash', "
                  "'debit': total, 'credit': '', "
                  "'memo': 'payment received on ' + ref, 'at': ts},\n"
                  "                {'ref': ref, 'account': 'Accounts receivable', "
                  "'debit': '', 'credit': total, "
                  "'memo': ref + ' settled', 'at': ts}]\n"
                  "    else:\n"
                  "        rows = [{'ref': ref, 'account': 'Order desk', "
                  "'debit': '', 'credit': '', "
                  "'memo': ref + ' moved to ' + to, 'at': ts}]\n"
                  "result = rows")}},
             {"type": "dataset_write", "name": "Post to the books",
              "params": {"dataset": "GL entries", "mode": "append"}},
         ]},
        {"name": "Stock pick ledger",
         "description": "business.state_changed -> shape -> Stock movements: an "
                        "order landing on 'picked' lands its movement row (delta "
                        "= -qty, the sku and qty riding the event's context) and "
                        "a replenishment landing on 'replenished' lands the "
                        "restock (+reorder_qty - the goods the Supply chain "
                        "walked in are on the shelf, v115). Stock on-hand truth "
                        "is seed + the deltas, never a column typed twice.",
         "trigger": {"type": "event_trigger",
                     "params": {"event_type": "business.state_changed"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the movement",
              "params": {"code": (
                  "r = df.iloc[0] if len(df) else {}\n"
                  "pl = r.get('payload') or {}\n"
                  "rows = []\n"
                  "pname = pl.get('process_name') or ''\n"
                  "if pname == 'Sales order lifecycle' \\\n"
                  "        and str(pl.get('to') or '') == 'picked':\n"
                  "    ctx = pl.get('context') or {}\n"
                  "    try:\n"
                  "        delta = -abs(int(float(ctx.get('qty') or 0)))\n"
                  "    except (TypeError, ValueError):\n"
                  "        delta = 0\n"
                  "    rows = [{'sku': str(ctx.get('sku') or ''), 'delta': str(delta), "
                  "'reason': 'picked for ' + str(pl.get('ref') or ''), "
                  "'at': r.get('triggered_at', '')}]\n"
                  "elif pname == 'Inventory replenishment' \\\n"
                  "        and str(pl.get('to') or '') == 'replenished':\n"
                  "    ctx = pl.get('context') or {}\n"
                  "    try:\n"
                  "        delta = abs(int(float(ctx.get('reorder_qty') or 0)))\n"
                  "    except (TypeError, ValueError):\n"
                  "        delta = 0\n"
                  "    rows = [{'sku': str(ctx.get('sku') or ''), 'delta': str(delta), "
                  "'reason': 'replenished ' + str(pl.get('ref') or ''), "
                  "'at': r.get('triggered_at', '')}]\n"
                  "result = rows")}},
             {"type": "dataset_write", "name": "Land the movement",
              "params": {"dataset": "Stock movements", "mode": "append"}},
         ]},
        {"name": "Payroll poster",
         "description": "business.state_changed -> shape -> GL entries: a "
                        "payroll run landing on 'paid' posts its BALANCED "
                        "pair - salary expense debited, cash credited, the "
                        "run's gross riding the event's context. The people "
                        "get paid AND the books see it. Every other machine's "
                        "move skips honestly.",
         "trigger": {"type": "event_trigger",
                     "params": {"event_type": "business.state_changed"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the payroll pair",
              "params": {"code": (
                  "r = df.iloc[0] if len(df) else {}\n"
                  "pl = r.get('payload') or {}\n"
                  "rows = []\n"
                  "if ((pl.get('process_name') or '') == 'Payroll lifecycle'\n"
                  "        and str(pl.get('to') or '') == 'paid'):\n"
                  "    ctx = pl.get('context') or {}\n"
                  "    gross = str(ctx.get('gross') or '')\n"
                  "    ref = str(pl.get('ref') or '')\n"
                  "    if gross:\n"
                  "        rows = [{'ref': ref, 'account': 'Salary expense', "
                  "'debit': gross, 'credit': '', "
                  "'memo': 'payroll ' + ref + ' paid', "
                  "'at': r.get('triggered_at', '')},\n"
                  "                {'ref': ref, 'account': 'Cash', "
                  "'debit': '', 'credit': gross, "
                  "'memo': 'payroll ' + ref + ' paid', "
                  "'at': r.get('triggered_at', '')}]\n"
                  "result = rows")}},
             {"type": "dataset_write", "name": "Post the payroll",
              "params": {"dataset": "GL entries", "mode": "append"}},
         ]},
        {"name": "Purchase ledger poster",
         "description": "business.state_changed -> shape -> GL entries: the "
                        "vendor bill the Supply chain opened (via='delivered "
                        "journey', the replenishment walk's own tail) posts "
                        "its BALANCED pairs - landing on 'matched' debits "
                        "Inventory AND credits Accounts payable (the goods "
                        "are in, the liability recognized), landing on 'paid' "
                        "debits Accounts payable AND credits Cash (the bill "
                        "settled) - the bill's cost*qty riding the journey's "
                        "own memory. Moves the books cannot value, and every "
                        "bill that is NOT the supply path (receivables are "
                        "the order poster's side), skip honestly. (v115: the "
                        "company's SPENDING finally hits the books.)",
         "trigger": {"type": "event_trigger",
                     "params": {"event_type": "business.state_changed"}},
         "steps": [
             {"type": "python_transform", "name": "Shape the purchase pair",
              "params": {"code": (
                  "r = df.iloc[0] if len(df) else {}\n"
                  "pl = r.get('payload') or {}\n"
                  "rows = []\n"
                  "if (pl.get('process_name') or '') == 'Invoice lifecycle':\n"
                  "    ctx = pl.get('context') or {}\n"
                  "    if (ctx.get('via') or '') == 'delivered journey':\n"
                  "        try:\n"
                  "            amount = round(abs(float(ctx.get('unit_cost') or 0))\n"
                  "                           * abs(float(ctx.get('qty') or 0)), 2)\n"
                  "        except (TypeError, ValueError):\n"
                  "            amount = 0.0\n"
                  "        ref = str(pl.get('ref') or '')\n"
                  "        to = str(pl.get('to') or '')\n"
                  "        ts = r.get('triggered_at', '')\n"
                  "        if to == 'matched' and amount > 0:\n"
                  "            rows = [{'ref': ref, 'account': 'Inventory', "
                  "'debit': str(amount), 'credit': '', "
                  "'memo': 'goods received + matched for ' + ref, 'at': ts},\n"
                  "                    {'ref': ref, 'account': 'Accounts payable', "
                  "'debit': '', 'credit': str(amount), "
                  "'memo': 'bill owed on ' + ref, 'at': ts}]\n"
                  "        elif to == 'paid' and amount > 0:\n"
                  "            rows = [{'ref': ref, 'account': 'Accounts payable', "
                  "'debit': str(amount), 'credit': '', "
                  "'memo': 'bill paid on ' + ref, 'at': ts},\n"
                  "                    {'ref': ref, 'account': 'Cash', "
                  "'debit': '', 'credit': str(amount), "
                  "'memo': 'bill paid on ' + ref, 'at': ts}]\n"
                  "        else:\n"
                  "            rows = [{'ref': ref, 'account': 'Vendor desk', "
                  "'debit': '', 'credit': '', "
                  "'memo': ref + ' moved to ' + to, 'at': ts}]\n"
                  "result = rows")}},
             {"type": "dataset_write", "name": "Post the purchase",
              "params": {"dataset": "GL entries", "mode": "append"}},
         ]},
    ],
    "agent": {"name": "ERP clerk",
              "greeting": "ERP here - orders, stock, books - where do we dig in?",
              "system_prompt": ("You are the ERP clerk. Answer from the knowledge "
                                "matches in metadata.knowledge; never move an order "
                                "past a stage the machine has not reached - walk the "
                                "caller through the lifecycle instead, and point "
                                "stock questions at the replenishment machine."),
              "knowledge": {"dataset": "ERP policy", "text_column": "question",
                            "answer_column": "answer", "top_k": 1}},
    "rooms": [{"name": "ERP review room", "title": "ERP review room",
               "modality": "video"}],
    "queues": [{"name": "ERP order desk queue", "room": "ERP review room",
                "bind_agent": True,
                "config": {"max_size": 30, "max_wait_seconds": 300,
                           "announce": {"enabled": True, "interval_seconds": 120},
                           "sms": {"enabled": False, "channel_id": "", "template": ""},
                           "callback": {"enabled": False, "endpoint_id": ""}}}],
    # the ERP ships no dialer - collections is the Finance operator's
    # campaign; the order desk needs none
    "campaign": None,
    "processes": [
        {"name": "Sales order lifecycle",
         "description": ("The order as a state machine - draft to paid with the "
                         "cancel hatch; the desk confirms, the room picks, "
                         "shipping moves it, Finance's receivable opens itself "
                         "on the ship, and the door nudges an order that sits "
                         "past its SLA."),
         "definition": {
             "states": ["draft", "confirmed", "picked", "shipped", "invoiced",
                        "paid", "cancelled"],
             "initial": "draft",
             "transitions": [
                 {"name": "confirm", "from": "draft", "to": "confirmed"},
                 {"name": "pick", "from": "confirmed", "to": "picked"},
                 {"name": "ship", "from": "picked", "to": "shipped"},
                 {"name": "invoice", "from": "shipped", "to": "invoiced"},
                 {"name": "pay", "from": "invoiced", "to": "paid"},
                 {"name": "cancel", "from": "draft", "to": "cancelled"},
                 {"name": "cancel", "from": "confirmed", "to": "cancelled"},
                 {"name": "escalate", "from": "draft", "to": "draft",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "confirmed", "to": "confirmed",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "picked", "to": "picked",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "shipped", "to": "shipped",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "invoiced", "to": "invoiced",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "seed_from_dataset": "Sales orders",
         "ref_column": "order",
         "title_from": ["order", "customer"],
         "state_column": "status",
         "due_in_seconds": 5 * 24 * 3600,
        },
        {"name": "Inventory replenishment",
         "description": ("The stock position as a state machine - one tracked "
                         "entity per SKU; healthy dips to low, placing the "
                         "reorder opens the Procurement operator's purchase "
                         "order by itself, the restock closes the loop, and "
                         "the door watches a low SKU nobody reordered."),
         "definition": {
             "states": ["healthy", "low", "reorder_placed", "replenished"],
             "initial": "healthy",
             "transitions": [
                 {"name": "dip", "from": "healthy", "to": "low"},
                 {"name": "reorder", "from": "low", "to": "reorder_placed"},
                 {"name": "restock", "from": "reorder_placed", "to": "replenished"},
                 {"name": "reset", "from": "replenished", "to": "healthy"},
                 {"name": "escalate", "from": "healthy", "to": "healthy",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "low", "to": "low",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "reorder_placed",
                  "to": "reorder_placed",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "seed_from_dataset": "Products",
         "ref_column": "sku",
         "title_from": ["sku", "name"],
         "state_column": "stock_status",
         "due_in_seconds": 2 * 24 * 3600,
        },
        {"name": "Month-end close",
         "description": ("The period as a state machine - open it when the "
                         "month ends, reconcile the books, review, close; the "
                         "door watches the close deadline (arrives empty: the "
                         "calendar is the accountant's to open)."),
         "definition": {
             "states": ["open", "reconciling", "reviewed", "closed"],
             "initial": "open",
             "transitions": [
                 {"name": "begin_close", "from": "open", "to": "reconciling"},
                 {"name": "review", "from": "reconciling", "to": "reviewed"},
                 {"name": "close", "from": "reviewed", "to": "closed"},
                 {"name": "reopen", "from": "closed", "to": "open"},
                 {"name": "escalate", "from": "open", "to": "open",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "reconciling", "to": "reconciling",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "reviewed", "to": "reviewed",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "due_in_seconds": 7 * 24 * 3600,
        },
        {"name": "Payroll lifecycle",
         "description": ("The pay run as a state machine - draft it, "
                         "calculate it, approve it, pay it; paying posts the "
                         "balanced pair to the books (salary expense / "
                         "cash) and the door nudges a run nobody approved "
                         "(arrives empty: the period is the bookkeeper's to "
                         "run)."),
         "definition": {
             "states": ["draft", "calculated", "approved", "paid"],
             "initial": "draft",
             "transitions": [
                 {"name": "calculate", "from": "draft", "to": "calculated"},
                 {"name": "approve", "from": "calculated", "to": "approved"},
                 {"name": "pay", "from": "approved", "to": "paid"},
                 {"name": "recalculate", "from": "calculated", "to": "draft",
                  "description": "the numbers were wrong - back to draft"},
                 {"name": "escalate", "from": "draft", "to": "draft",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "calculated", "to": "calculated",
                  "description": "SLA breach nudge - the door's move"},
                 {"name": "escalate", "from": "approved", "to": "approved",
                  "description": "SLA breach nudge - the door's move"},
             ],
         },
         "seed_from_dataset": "Payroll runs",
         "ref_column": "ref",
         "title_from": ["ref", "period"],
         "state_column": "status",
         "due_in_seconds": 3 * 24 * 3600,
        },
    ],
    "dashboard": {"name": "ERP Console Board",
                  "description": "The company at a glance - orders by stage, the "
                                 "stock room, the ledgers, the close."},
    "notes": [
        "The workflows install INACTIVE - boot the system (activate_workflows) "
        "to open the reactive path; the onboarding loops track every new row "
        "the order desk or an import lands in the datasets.",
        "The books are DOUBLE-ENTRY since v114 and speak BOTH SIDES since "
        "v115: the posters pair every debit "
        "with a credit (receivable + revenue on the ship, cash + the clearing "
        "on the payment, salary expense + cash on payroll, inventory + "
        "accounts payable on the vendor bill's match, accounts payable + "
        "cash on its payment), so GET "
        "/erp/trial-balance over the GL entries dataset balances and speaks - "
        "revenue, expenses, net income, the cash position, and now the "
        "liabilities too.",
        "The journeys resolve by name at fire time: install the ERP alone and "
        "a shipped order names the missing invoice machine in an honest skip; "
        "install Finance (and Procurement for the buy side) and the handoffs "
        "become real - the chains page draws the walks.",
        "The machine moves carry the entity's memory: the event payload "
        "includes the instance context, so the ledger poster reads the order's "
        "total, the pick ledger the sku/qty and the payroll poster the run's "
        "gross straight off the wire.",
        "Stock on-hand truth is the Stock movements ledger (seed stock + the "
        "appended deltas), never a column typed twice.",
        "The close machine and the payroll calendar arrive EMPTY on purpose - "
        "open a period when the month ends, run payroll when it is due; the "
        "door watches both deadlines from birth.",
        "The policy dataset is the clerk's knowledge - edit it and the answers "
        "follow, no redeploy.",
    ],
}


# ---------------------------------------------------------------------------
# v87: the escalation POLICY per operator machine - the door's channel +
# repeat dimension. The machines above define the escalate move (the door
# takes it); these policies say WHO GETS TOLD (the channel, through the
# owner's channel endpoints - provider-agnostic) and HOW OFTEN the door
# knocks again (repeat_every_seconds, until 1 + max_repeats attempts; a
# state change starts a fresh episode). `to` stays empty on the shelf: the
# installer binds the target, the passes skip honestly until then.
# ---------------------------------------------------------------------------
_ESCALATION_POLICIES: dict[str, dict] = {
    "Lead pipeline":       {"channel": "email", "to": "", "repeat_every_seconds": 3600,  "max_repeats": 3},
    "Meeting lifecycle":   {"channel": "email", "to": "", "repeat_every_seconds": 1800,  "max_repeats": 2,
                            "message_template": ("[Meeting Operator] '{title}' (ref {ref}) is still "
                                                 "'{state}' after {overdue_minutes} minutes - please "
                                                 "confirm or cancel it (escalation {attempt}).")},
    "Appointment journey": {"channel": "sms",   "to": "", "repeat_every_seconds": 900,   "max_repeats": 3,
                            "message_template": ("[Clinic Operator] Appointment {ref} ('{title}') is "
                                                 "still '{state}' after {overdue_minutes} minutes - "
                                                 "confirm or call the patient (escalation {attempt}).")},
    "Case lifecycle":      {"channel": "email", "to": "", "repeat_every_seconds": 14400, "max_repeats": 3},
    "Request lifecycle":   {"channel": "email", "to": "", "repeat_every_seconds": 14400, "max_repeats": 3},
    "Leave pipeline":      {"channel": "email", "to": "", "repeat_every_seconds": 14400, "max_repeats": 3},
    "Onboarding pipeline": {"channel": "email", "to": "", "repeat_every_seconds": 86400, "max_repeats": 2},
    # v89: the invoice chaser is the classic digest case - a DAILY summary
    # of everything past due instead of four knocks per invoice per day
    "Invoice lifecycle":   {"channel": "email", "to": "", "mode": "digest",
                            "digest_every_seconds": 86400, "max_repeats": 3},
    "Purchase lifecycle":  {"channel": "email", "to": "", "repeat_every_seconds": 43200, "max_repeats": 3},
    "Delivery pipeline":   {"channel": "sms",   "to": "", "repeat_every_seconds": 21600, "max_repeats": 3},
    "Customer onboarding": {"channel": "email", "to": "", "repeat_every_seconds": 43200, "max_repeats": 3},
    # v113: the ERP core's machines - the order desk knocks every 4h, a low
    # SKU gets half a day, and the close deadline speaks in a DAILY digest
    # (the books' rhythm, not N knocks per period)
    "Sales order lifecycle":  {"channel": "email", "to": "", "repeat_every_seconds": 14400, "max_repeats": 3},
    "Inventory replenishment": {"channel": "email", "to": "", "repeat_every_seconds": 43200, "max_repeats": 3},
    "Month-end close":        {"channel": "email", "to": "", "mode": "digest",
                               "digest_every_seconds": 86400, "max_repeats": 2},
    # v114: payroll has its own clock - a run nobody approved knocks every
    # 12h (people waiting on pay should not wait on a quiet door)
    "Payroll lifecycle":      {"channel": "email", "to": "", "repeat_every_seconds": 43200, "max_repeats": 3},
}


# v89/v90: cross-operator JOURNEYS per operator machine - when the machine
# lands on the fire-state, the next leg OPENS ITSELF on the target machine
# (a won deal opening an onboarding case). The target is named here and
# resolved at FIRE time by name - operators install independently, so an
# uninstalled target skips honestly (the event names it; install the
# second operator and the handoff becomes real). v90 threads the legs into
# CHAINS that span three departments, each opened leg carrying its own SLA
# promise (due_in_seconds) so the door and its digests watch it from birth:
#
#   the REVENUE chain:  Sales (Lead pipeline) --won--> Operations (Customer
#     onboarding) --handed_off--> Finance (Invoice lifecycle)
#   the SUPPLY chain:   Procurement (Purchase lifecycle) --ordered-->
#     Logistics (Delivery pipeline) --delivered--> Finance (Invoice)
#   the CARE chain:     Clinic (Appointment journey) --billed--> Finance
#
# one ref (the phone the business already speaks in) rides every leg, so
# the whole journey is greppable across departments.
_JOURNEYS: dict[str, list[dict]] = {
    "Lead pipeline": [
        {"on_state": "won",
         "open": {"process": "Customer onboarding",
                  "title_template": "Onboarding - {title}",
                  "memory": {"via": "won-deal journey",
                             "source_operator": "sales"}}},
    ],
    "Customer onboarding": [
        {"on_state": "handed_off",
         "open": {"process": "Invoice lifecycle",
                  "title_template": "Billing - {title}",
                  "ref_template": "{ref}",
                  "memory": {"via": "handed-off journey",
                             "source_operator": "operations"},
                  "due_in_seconds": 5 * 24 * 3600}},
    ],
    "Purchase lifecycle": [
        {"on_state": "ordered",
         "open": {"process": "Delivery pipeline",
                  "title_template": "Delivery - {title}",
                  "ref_template": "{ref}",
                  "memory": {"via": "ordered journey",
                             "source_operator": "procurement",
                             "sku": "{sku}",
                             "unit_cost": "{unit_cost}",
                             "qty": "{qty}"},
                  "due_in_seconds": 2 * 24 * 3600}},
    ],
    "Delivery pipeline": [
        {"on_state": "delivered",
         "open": {"process": "Invoice lifecycle",
                  "title_template": "Bill - {title}",
                  "ref_template": "{ref}",
                  "memory": {"via": "delivered journey",
                             "source_operator": "logistics",
                             "sku": "{sku}",
                             "unit_cost": "{unit_cost}",
                             "qty": "{qty}"},
                  "due_in_seconds": 5 * 24 * 3600}},
    ],
    "Appointment journey": [
        {"on_state": "billed",
         "open": {"process": "Invoice lifecycle",
                  "title_template": "Visit - {title}",
                  "ref_template": "{ref}",
                  "memory": {"via": "billed visit journey",
                             "source_operator": "clinic"},
                  "due_in_seconds": 5 * 24 * 3600}},
    ],
    # v113: the ERP core's legs - the backbone composing the departments.
    # The shipped order opens the receivable (ORDER-TO-CASH); the placed
    # reorder opens the purchase order (REPLENISHMENT - and from there the
    # Supply chain walks the delivery to the vendor's bill). Targets
    # resolve by name at fire time: ERP standalone skips honestly,
    # departments installed make the handoffs real.
    "Sales order lifecycle": [
        {"on_state": "shipped",
         "open": {"process": "Invoice lifecycle",
                  "title_template": "Invoice - {title}",
                  "ref_template": "{ref}",
                  "memory": {"via": "shipped order journey",
                             "source_operator": "erp"},
                  "due_in_seconds": 5 * 24 * 3600}},
    ],
    "Inventory replenishment": [
        {"on_state": "reorder_placed",
         "open": {"process": "Purchase lifecycle",
                  "title_template": "Reorder - {title}",
                  "ref_template": "{ref}",
                  "memory": {"via": "reorder journey",
                             "source_operator": "erp",
                             "sku": "{sku}",
                             "unit_cost": "{cost}",
                             "qty": "{reorder_qty}"},
                  "due_in_seconds": 2 * 24 * 3600}},
    ],
}


def _journeys_for(pspec: dict) -> list[dict] | None:
    """The shelf journeys for a pack's process, applied into the
    definition at install time (validate_definition re-validates them
    loudly)."""
    name = str(pspec.get("name") or "").strip()
    journeys = _JOURNEYS.get(name)
    return [dict(j, open=dict(j["open"])) for j in journeys] if journeys else None


def _policy_for(pspec: dict) -> dict | None:
    """The shelf policy for a pack's process, applied into the definition
    at install time (validate_definition re-validates it loudly)."""
    name = str(pspec.get("name") or "").strip()
    policy = _ESCALATION_POLICIES.get(name)
    if policy is None:
        # any machine the table does not name still gets a sane default:
        # event-only, hourly, three repeats
        return {"channel": "", "to": "", "repeat_every_seconds": 3600,
                "max_repeats": 3}
    return dict(policy)




def _describe_pack_policy(pspec: dict) -> str:
    """The shelf's one-line escalation summary for a pack's process."""
    from . import escalations as escalations_svc

    return escalations_svc.describe_policy(_policy_for(pspec))


def _describe_pack_journeys(pspec: dict) -> list[dict]:
    """The shelf's journey display for a pack's process."""
    journeys = _journeys_for(pspec) or []
    return [{"on_state": j["on_state"], "opens": j["open"]["process"]}
            for j in journeys]


def _onboarding_loop(pspec: dict) -> dict | None:
    """v88: the department's DATA on-ramp - one workflow per machine,
    generated from the pack's own seed spec. The channel intakes
    (sms.received, call.ended) onboard the entities that ARRIVE through a
    channel; this loop onboards the entities that arrive as DATA: the
    spreadsheet that just landed, the rows the staff typed in the App
    Builder, the bulk import appended over the API. A dataset-trigger
    watches the machine's own intake dataset; every new version wakes the
    business_onboard step, which starts one tracked instance PER ROW at
    the row's own stage - idempotently (an open instance already carrying
    the ref skips, so the install-time seeding and the loop never fight).
    """
    ds = str(pspec.get("seed_from_dataset") or "").strip()
    if not ds:
        return None
    machine = str(pspec.get("name") or "").strip()
    due_s = pspec.get("due_in_seconds")
    return {
        "name": f"{machine} onboarding",
        "description": (
            f"The data on-ramp: new rows landing in {ds!r} onboard as tracked "
            f"instances of {machine!r} at each row's own stage - idempotent "
            "(already-tracked refs skip), so re-runs and trigger fires never "
            "double-track. Fires on dataset versions; run it by hand after a "
            "bulk import too."),
        "trigger": {"type": "dataset_trigger",
                    "params": {"dataset": ds, "poll_seconds": 60}},
        "steps": [
            {"type": "business_onboard",
             "name": f"Onboard rows into {machine}",
             "params": {"process": machine,
                        "dataset": ds,
                        "ref_column": str(pspec.get("ref_column") or ""),
                        "state_column": str(pspec.get("state_column") or ""),
                        "title_columns": [str(c) for c in (pspec.get("title_from") or [])],
                        "due_in_seconds": int(due_s) if due_s else None,
                        "on_duplicate": "skip",
                        "actor": "onboarding-loop"}},
        ],
    }


OPERATORS: list[dict] = [*list(_CATALOG), _ERP_OPERATOR]  # the nine departments + v113's backbone
OPERATORS_BY_SLUG = {op["slug"]: op for op in OPERATORS}

# v88: BROADENING THE DEPARTMENT ONBOARDING LOOPS - every machine gets its
# data on-ramp beside the channel intakes (generated, never hand-copied:
# the loop is built from the same seed spec the install seeds with, so
# the two doors can never drift apart)
for _op in OPERATORS:
    for _pspec in (_op.get("processes") or []):
        _loop = _onboarding_loop(_pspec)
        if _loop is not None:
            _op["workflows"].append(_loop)
del _op, _pspec, _loop


# v92: the NAMED chains - the drawn view on the operator detail page. A
# chain names its ordered walk as (source process, fire state) pairs;
# everything else (the opened process, the leg's own SLA) RESOLVES from
# _JOURNEYS so the two tables can never drift: a leg that is not a
# journey, or a walk that does not connect, refuses loudly at import.
_CHAINS: list[dict] = [
    {"slug": "revenue", "name": "Revenue",
     "story": "a deal won onboards the customer, and the hand-off lands the invoice",
     "path": [("Lead pipeline", "won"), ("Customer onboarding", "handed_off")]},
    {"slug": "supply", "name": "Supply",
     "story": "a purchase order dispatches the delivery, and the goods received land the bill",
     "path": [("Purchase lifecycle", "ordered"), ("Delivery pipeline", "delivered")]},
    {"slug": "care", "name": "Care",
     "story": "a billed visit hands the money to finance",
     "path": [("Appointment journey", "billed")]},
    # v113: the ERP core's walks - the backbone composing the company
    {"slug": "order-to-cash", "name": "Order to Cash",
     "story": "a confirmed order ships, and the shipping lands the invoice",
     "path": [("Sales order lifecycle", "shipped")]},
    {"slug": "replenishment", "name": "Replenishment",
     "story": "stock dips below the reorder point, the reorder dispatches "
              "the delivery, and the goods received land the bill",
     "path": [("Inventory replenishment", "reorder_placed"),
              ("Purchase lifecycle", "ordered"),
              ("Delivery pipeline", "delivered")]},
]


def _resolve_chains() -> list[dict]:
    """Resolve the chain walks against _JOURNEYS once at import - the
    drawn view renders RESOLVED legs, never re-derives them."""
    resolved: list[dict] = []
    for chain in _CHAINS:
        legs: list[dict] = []
        for i, (src, state) in enumerate(chain["path"]):
            hits = [j for j in _JOURNEYS.get(src, []) if j["on_state"] == state]
            if len(hits) != 1:
                raise RuntimeError(
                    f"chain {chain['slug']!r}: leg ({src!r} on {state!r}) is not "
                    "a journey in _JOURNEYS - the walk and the journeys drifted")
            open_spec = hits[0].get("open") or {}
            opens = str(open_spec.get("process") or "")
            nxt = chain["path"][i + 1][0] if i + 1 < len(chain["path"]) else None
            if nxt and opens != nxt:
                raise RuntimeError(
                    f"chain {chain['slug']!r}: leg ({src!r} on {state!r}) opens "
                    f"{opens!r}, but the walk continues at {nxt!r}")
            legs.append({"from_process": src, "on_state": state,
                         "opens": opens,
                         "due_in_seconds": open_spec.get("due_in_seconds")})
        resolved.append({**chain, "legs": legs})
    return resolved


_RESOLVED_CHAINS = _resolve_chains()


def _journey_owner_of() -> dict[str, str]:
    owner_of: dict[str, str] = {}
    for op in OPERATORS:
        for p in (op.get("processes") or []):
            owner_of[p["name"]] = op["slug"]
    return owner_of


def _operator_brief(slug: str) -> dict:
    op = OPERATORS_BY_SLUG.get(slug)
    if op is None:
        return {"slug": slug, "name": slug, "icon": "", "color": "#71717a"}
    return {"slug": op["slug"], "name": op["name"],
            "icon": op["icon"], "color": op["color"]}


def _chains_for(slug: str) -> list[dict]:
    """v92: the named chains THIS operator sits in - drawn on the detail
    page. ``position`` is where the operator's node sits in the walk
    (0 = the chain starts here, the last index = the terminus), and each
    leg names who FIRES it and who RECEIVES, so the drawing can flag the
    operator's own legs (out = it fires, in = it is fed)."""
    owner_of = _journey_owner_of()
    out: list[dict] = []
    for chain in _RESOLVED_CHAINS:
        walk = [chain["legs"][0]["from_process"]] + \
               [leg["opens"] for leg in chain["legs"]]
        ops_order: list[str] = []
        for pr in walk:
            s = owner_of.get(pr, "")
            if s and s not in ops_order:
                ops_order.append(s)
        if slug not in ops_order:
            continue
        legs = [{**leg,
                 "from_operator": owner_of.get(leg["from_process"], ""),
                 "opens_operator": owner_of.get(leg["opens"], "")}
                for leg in chain["legs"]]
        out.append({"slug": chain["slug"], "name": chain["name"],
                    "story": chain["story"],
                    "operators": [_operator_brief(s) for s in ops_order],
                    "legs": legs,
                    "position": ops_order.index(slug)})
    return out


def _catalog_journeys(slug: str) -> list[dict]:
    """v91: the journey legs ON the shelf card - what installing this
    operator wires into the cross-department chains. Two directions,
    both honest: 'out' = this operator's machine lands on a state and the
    next department's leg opens ITSELF; 'in' = another operator's machine
    hands work TO this one (the leg completes when BOTH are installed -
    targets resolve by name at fire time, so a missing operator is an
    honest skip, never a broken install)."""
    own = {p["name"] for p in (OPERATORS_BY_SLUG[slug].get("processes") or [])}
    owner_of: dict[str, str] = {}
    for op in OPERATORS:
        for p in (op.get("processes") or []):
            owner_of[p["name"]] = op["slug"]
    legs: list[dict] = []
    for src_name, js in _JOURNEYS.items():
        for j in js:
            open_spec = j.get("open") or {}
            target = str(open_spec.get("process") or "")
            if src_name in own:
                legs.append({"direction": "out", "from_process": src_name,
                             "from_operator": owner_of.get(src_name, ""),
                             "on_state": j["on_state"], "opens": target,
                             "due_in_seconds": open_spec.get("due_in_seconds")})
            elif target in own:
                legs.append({"direction": "in", "from_process": src_name,
                             "from_operator": owner_of.get(src_name, ""),
                             "on_state": j["on_state"], "opens": target,
                             "due_in_seconds": open_spec.get("due_in_seconds")})
    return legs


def operator_catalog() -> dict:
    """The operators shelf - what each install BUILDS, counted honestly."""
    return {
        "operators": [
            {"slug": op["slug"], "name": op["name"], "tagline": op["tagline"],
             "category": op["category"], "icon": op["icon"], "color": op["color"],
             "outcomes": list(op["outcomes"]),
             "journeys": _catalog_journeys(op["slug"]),
             "chains": [c["slug"] for c in _chains_for(op["slug"])],
             "topology": {
                 "datasets": len(op["datasets"]),
                 "workflows": len(op["workflows"]),
                 "agents": 1 if op.get("agent") else 0,
                 "rooms": len(op["rooms"]),
                 "queues": len(op["queues"]),
                 "campaign": 1 if op.get("campaign") else 0,
                 "processes": len(op.get("processes") or []),
                 "dashboard": 1,
             }}
            for op in OPERATORS
        ]
    }


def operator_detail(slug: str) -> dict:
    op = OPERATORS_BY_SLUG.get(slug)
    if op is None:
        raise OperatorError(f"unknown operator {slug!r}")
    return {
        "slug": op["slug"], "name": op["name"], "tagline": op["tagline"],
        "category": op["category"], "icon": op["icon"], "color": op["color"],
        "outcomes": list(op["outcomes"]),
        "chains": _chains_for(slug),
        "installs": {
            "datasets": [{"name": d["name"], "description": d["description"],
                          "columns": list(d["columns"]), "rows": len(d["rows"])}
                         for d in op["datasets"]],
            "workflows": [{"name": w["name"], "description": w["description"],
                           "trigger": (w["trigger"]["params"].get("event_type")
                                       or (f"dataset:{w['trigger']['params'].get('dataset')}"
                                           if w["trigger"]["type"] == "dataset_trigger" else ""))}
                          for w in op["workflows"]],
            "processes": [{"name": p["name"],
                           "states": p["definition"]["states"],
                           "seeded_from": p.get("seed_from_dataset"),
                           "escalates": any(t.get("name") == "escalate"
                                            for t in p["definition"]["transitions"]),
                           "escalation": _describe_pack_policy(p),
                           "journeys": _describe_pack_journeys(p)}
                          for p in (op.get("processes") or [])],
            "agent": {"name": op["agent"]["name"],
                      "knowledge": (op["agent"]["knowledge"] or {}).get("dataset")},
            "rooms": [{"name": r["name"], "modality": r["modality"]} for r in op["rooms"]],
            "queues": [q["name"] for q in op["queues"]],
            "campaign": (op["campaign"] or {}).get("name"),
            "dashboard": op["dashboard"]["name"],
        },
        "notes": list(op["notes"]),
    }


# ---------------------------------------------------------------------------
# v93: the chains LIVE on the installed system - the drawn walk with the
# real instance counts underneath it
# ---------------------------------------------------------------------------

async def chains_for_system(db: AsyncSession, system, *,
                            now: datetime | None = None) -> list[dict]:
    """The chains this system's bound processes actually sit in, with the
    LIVE counts per node and per leg - the operator detail page's drawn
    walk, re-rendered after the install against real rows.

    The chain walks resolve by PROCESS NAME against the system's bound
    kind="process" components (the same name-resolution the journeys
    themselves use at fire time), so a system that binds only part of a
    chain still shows it - honestly: a leg with a missing end is drawn
    dashed (``bound_from``/``bound_opens`` name who is absent) and the
    chain carries ``complete``. Counts are computed in PYTHON per the
    house SQLite naive/aware discipline:

    * per NODE (a bound process in the walk): open instances + how many
      are past their SLA (terminal states skipped - a closed entity does
      not count as late), plus v94's ack/snooze surfacing: ``acked`` /
      ``snoozed`` count the overdue ones the door is holding (the ack
      holds the DOOR, never the clock - the row stays overdue either
      way), and ``overdue_instances`` names them (most overdue first,
      capped at 5) with each one's escalation book - the same receipt the
      attention feed carries, so the chain node answers "who has this"
      without leaving the drawing;
    * per LEG: ``in_state`` = the source process's open instances sitting
      in the fire state right now (the hand-off is armed), ``fired`` =
      the target process's OPEN instances this leg opened itself (they
      carry the journey link in their context), ``overdue`` = the fired
      ones past the leg's own SLA promise - with the same ``acked`` /
      ``snoozed`` sub-counts among them.

    A system that binds no processes draws no chains - the section is
    simply absent, never an empty lie."""
    from datetime import datetime as _dt, timezone as _tz

    from ..models import BusinessProcess, BusinessProcessInstance
    from . import business_processes as process_svc
    from . import escalations as escalations_svc  # v94: the ack book reader

    if now is None:
        now = _dt.now(_tz.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_tz.utc)  # SQLite returns naive - normalize
    ref_ids = [c.ref_id for c in (system.components or [])
               if c.kind == "process"]
    if not ref_ids:
        return []
    procs = (await db.execute(
        select(BusinessProcess).where(BusinessProcess.id.in_(ref_ids)))).scalars().all()
    if not procs:
        return []
    bound: dict[str, BusinessProcess] = {p.name: p for p in procs}
    terminal_of: dict[str, set[str]] = {
        p.name: process_svc._terminal_states(p.definition or {}) for p in procs}

    open_rows = (await db.execute(
        select(BusinessProcessInstance)
        .where(BusinessProcessInstance.process_id.in_([p.id for p in procs]),
               BusinessProcessInstance.ended_at.is_(None)))).scalars().all()

    def _late(row) -> bool:
        due = row.due_at
        if due is None:
            return False
        if due.tzinfo is None:
            due = due.replace(tzinfo=_tz.utc)
        return now >= due

    # v94: the ack/snooze surfacing - read from the episode book the door
    # keeps on the instance's memory (the SAME receipt the attention feed
    # carries). The ack holds the door, never the clock: an acked row is
    # still overdue, so acked/snoozed are sub-counts of overdue. A snooze
    # is a LOAN - ``snooze_active`` is False once snooze_until has passed
    # (the door re-knocks; the ack itself still shows).
    def _book_of(row) -> dict:
        book = process_svc.escalations_svc.episode_book(row)
        acked = book.get("acked") if isinstance(book.get("acked"), dict) else None
        snooze_until = str((acked or {}).get("snooze_until") or "")
        snooze_active = False
        if snooze_until:
            until = escalations_svc._parse_iso(snooze_until)
            snooze_active = until is not None and now < until
        return {
            "count": int(book.get("count") or 0),
            "last_delivery": str(book.get("last_delivery") or ""),
            "acked_by": str((acked or {}).get("by") or ""),
            "snooze_until": snooze_until,
            "snooze_active": snooze_active,
        }

    def _ack_counts(rows: list) -> tuple[int, int]:
        acked = snoozed = 0
        for r in rows:
            b = _book_of(r)
            if b["acked_by"]:
                acked += 1
            if b["snooze_active"]:
                snoozed += 1
        return acked, snoozed

    nodes_stat: dict[str, dict] = {}
    for name, proc in bound.items():
        rows = [r for r in open_rows if r.process_id == proc.id]
        term = terminal_of.get(name) or set()
        late_rows = [r for r in rows
                     if r.due_at is not None and r.state not in term and _late(r)]
        # most overdue first, capped - the node names who needs the human
        # without turning the drawing into a feed
        late_rows.sort(
            key=lambda r: (now - (r.due_at.replace(tzinfo=_tz.utc)
                                  if r.due_at.tzinfo is None else r.due_at)
                           ).total_seconds(),
            reverse=True)
        acked, snoozed = _ack_counts(late_rows)
        nodes_stat[name] = {
            "open": len(rows),
            "overdue": len(late_rows),
            "acked": acked,
            "snoozed": snoozed,
            "overdue_instances": [{
                "process_id": proc.id, "instance_id": r.id,
                "ref": r.ref, "title": r.title, "state": r.state,
                "overdue_seconds": round((now - (
                    r.due_at.replace(tzinfo=_tz.utc)
                    if r.due_at.tzinfo is None else r.due_at)).total_seconds()),
                "escalation": _book_of(r),
            } for r in late_rows[:5]],
        }

    # the legs' fired children: open instances carrying the journey link
    fired_index: dict[tuple[str, str], list] = {}
    for r in open_rows:
        link = (r.context or {}).get("journey")
        if isinstance(link, dict) and link.get("from_process"):
            key = (str(link["from_process"]), str(link.get("from_state") or ""))
            fired_index.setdefault(key, []).append(r)

    owner_of = _journey_owner_of()
    out: list[dict] = []
    for chain in _RESOLVED_CHAINS:
        legs = chain["legs"]
        walk = [legs[0]["from_process"]] + [leg["opens"] for leg in legs]
        if not any(pr in bound for pr in walk):
            continue  # none of this chain lives on this system
        leg_out: list[dict] = []
        for leg in legs:
            src, dst = leg["from_process"], leg["opens"]
            bound_from, bound_opens = src in bound, dst in bound
            in_state = 0
            if bound_from:
                in_state = len([r for r in open_rows
                                if r.process_id == bound[src].id
                                and r.state == leg["on_state"]])
            children = [r for r in fired_index.get((src, leg["on_state"]), [])
                        if bound_opens and r.process_id == bound[dst].id]
            late_children = [r for r in children if _late(r)]
            child_acked, child_snoozed = _ack_counts(late_children)
            leg_out.append({
                "from_process": src, "on_state": leg["on_state"],
                "opens": dst,
                "due_in_seconds": leg.get("due_in_seconds"),
                "from_operator": owner_of.get(src, ""),
                "opens_operator": owner_of.get(dst, ""),
                "bound_from": bound_from, "bound_opens": bound_opens,
                "counts": {"in_state": in_state, "fired": len(children),
                           "overdue": len(late_children),
                           "acked": child_acked, "snoozed": child_snoozed},
            })
        ops_order: list[str] = []
        for pr in walk:
            s = owner_of.get(pr, "")
            if s and s not in ops_order:
                ops_order.append(s)
        out.append({
            "slug": chain["slug"], "name": chain["name"], "story": chain["story"],
            "operators": [_operator_brief(s) for s in ops_order],
            "legs": leg_out,
            "nodes": [{"process": pr, "operator": owner_of.get(pr, ""),
                       "bound": pr in bound,
                       **nodes_stat.get(pr, {"open": 0, "overdue": 0})}
                      for pr in walk],
            "complete": all(l["bound_from"] and l["bound_opens"] for l in leg_out),
        })
    return out


# ---------------------------------------------------------------------------
# The install - compose the business into real primitives, bind it RUNNING
# ---------------------------------------------------------------------------

def _node(nid: str, ntype: str, params: dict, name: str) -> dict:
    return {"id": nid, "type": ntype, "name": name,
            "position": {"x": 0, "y": 0}, "parameters": params}


def _edge(eid: str, source: str, target: str) -> dict:
    return {"id": eid, "source": source, "target": target,
            "sourceHandle": "main", "targetHandle": "main"}


def _workflow_graph(wspec: dict) -> dict:
    """trigger + linear step chain - a REAL graph of registered node types."""
    nodes: list[dict] = []
    edges: list[dict] = []
    trig = wspec.get("trigger") or {}
    nodes.append(_node("n_trigger", str(trig.get("type") or "event_trigger"),
                       dict(trig.get("params") or {}), "Trigger"))
    prev = "n_trigger"
    for i, s in enumerate(wspec.get("steps") or []):
        s = s if isinstance(s, dict) else {}
        nid = f"n_step_{i}"
        nodes.append(_node(nid, str(s.get("type")), dict(s.get("params") or {}),
                           str(s.get("name") or s.get("type") or f"Step {i + 1}")))
        edges.append(_edge(f"n_edge_{i}", prev, nid))
        prev = nid
    return {"nodes": nodes, "edges": edges}


async def _unique_dataset_name(db: AsyncSession, base: str) -> str:
    from . import datasets as ds_svc

    name = re.sub(r"\s+", " ", base).strip()[:100] or "Operator Dataset"
    if not ds_svc.NAME_RE.match(name):
        name = "Operator Dataset"
    candidate = name
    n = 1
    while await ds_svc.name_taken(db, candidate):
        n += 1
        candidate = f"{name} {n}"
    return candidate


async def _unique_dashboard_name(db: AsyncSession, base: str) -> str:
    from . import dashboards as dash_svc

    name = re.sub(r"\s+", " ", base).strip()[:140] or "Operator Board"
    candidate = name
    n = 1
    while await dash_svc.name_taken(db, candidate):
        n += 1
        candidate = f"{name} {n}"
    return candidate


async def install_operator(db: AsyncSession, slug: str, *, owner_id: str | None,
                           llm_credential_id: str | None = None,
                           brain: str = "scaffold", note: str = "") -> dict:
    """Compose the operator's business into real primitives and bind it into
    a RUNNING Py8nSystem. The caller owns the commit.

    Order matters: datasets first (knowledge + writes land on them), then
    the PROCESSES (v85: seeded from the datasets just built - the business
    state machine arrives pre-wired), then workflows (inactive - the boot
    door opens them; business_advance refs resolve to the BUILT process
    ids), the agent (rooms and queues bind it), rooms, queues, the
    campaign (composed directly - create_campaign refuses empty target
    lists by design), the dashboard (generated over the BUILT datasets),
    and finally the system with the durable installed operation + the
    system.installed event.
    """
    import pandas as pd

    from ..models import (Dashboard, Py8nSystem, SystemComponent, VoiceCampaign,
                          Workflow)
    from . import dashboards as dash_svc
    from . import datasets as ds_svc
    from . import business_processes as process_svc
    from . import system_runtime
    from . import voice_agents as va_svc
    from . import voice_campaigns as campaigns_svc
    from . import voice_meetings as meetings_svc
    from . import voice_queue as queue_svc
    from .versions import snapshot_workflow_version

    op = OPERATORS_BY_SLUG.get(slug)
    if op is None:
        raise OperatorError(f"unknown operator {slug!r}")
    brain = (brain or "scaffold").strip()
    if brain not in va_svc.BRAINS:
        raise OperatorError(f"brain must be {'|'.join(va_svc.BRAINS)}, got {brain!r}")
    if brain == "ai_agent" and not (llm_credential_id or "").strip():
        raise OperatorError("brain=ai_agent needs llm_credential_id "
                            "(the brain routes through a real provider credential)")

    built: dict = {"datasets": [], "processes": [], "workflows": [], "agents": [],
                   "rooms": [], "queues": [], "campaign": None, "dashboard": None,
                   "system": None}
    wiring_notes = list(op["notes"])
    ds_rows: list[tuple[object, object]] = []  # (Dataset, DataFrame) for the board
    ds_by_name: dict[str, dict] = {}
    ds_seed_rows: dict[str, list[dict]] = {}

    # ---- 1) datasets first (knowledge bindings and writes land on them) ---
    for d in op["datasets"]:
        cols = [str(c) for c in d["columns"]]
        rows = [r for r in (d["rows"] or []) if isinstance(r, dict)]
        df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
        ds = await ds_svc.create_from_df(
            db, await _unique_dataset_name(db, d["name"]), df,
            source="operator",
            description=str(d.get("description") or "")[:500],
            owner_id=owner_id)
        ds_by_name[d["name"]] = {"id": ds.id, "name": ds.name}
        ds_rows.append((ds, df))
        ds_seed_rows[d["name"]] = rows
        built["datasets"].append({"id": ds.id, "name": ds.name,
                                  "columns": cols, "rows": len(rows)})

    # ---- 1.5) the PROCESSES (v85) - the machine arrives pre-wired and ----
    # SEEDED: one instance per seed row of the named dataset, starting at
    # the row's own stage (the CRM is imported, not rewound), ref = the
    # external key the business already tracks (the phone a call can match)
    proc_by_name: dict[str, dict] = {}
    for pspec in op.get("processes") or []:
        definition = dict(pspec.get("definition") or {})
        policy = _policy_for(pspec)  # v87: the channel + repeat policy rides the definition
        if policy:
            definition["escalation_policy"] = policy
        journeys = _journeys_for(pspec)  # v89: the legs this machine opens
        if journeys:
            definition["journeys"] = journeys
        proc = await process_svc.create_process(
            db, owner_id=owner_id, name=str(pspec["name"])[:140],
            description=str(pspec.get("description") or "")[:500],
            definition=definition)
        seeded = 0
        skipped_seed = 0
        src_name = str(pspec.get("seed_from_dataset") or "").strip()
        ref_col = str(pspec.get("ref_column") or "").strip()
        state_col = str(pspec.get("state_column") or "").strip()
        title_cols = [str(c) for c in (pspec.get("title_from") or [])]
        due_s = pspec.get("due_in_seconds")
        for row in ds_seed_rows.get(src_name, []):
            if not isinstance(row, dict):
                continue
            ref = str(row.get(ref_col) or "").strip() if ref_col else ""
            if not ref:
                skipped_seed += 1
                continue
            title = " - ".join(str(row.get(c) or "").strip()
                               for c in title_cols if str(row.get(c) or "").strip())
            begin = (str(row.get(state_col) or "").strip() if state_col else "") or None
            await process_svc.start_instance(
                db, proc["id"], owner_id=owner_id, ref=ref,
                title=title[:200], context=dict(row),
                due_in_seconds=int(due_s) if due_s else None,
                state=begin, actor="operator-install")
            seeded += 1
        if skipped_seed:
            wiring_notes.append(f"process {proc['name']!r}: {skipped_seed} seed row(s) "
                                "had no ref and were skipped honestly.")
        proc_by_name[pspec["name"]] = {"id": proc["id"], "name": proc["name"]}
        built["processes"].append({"id": proc["id"], "name": proc["name"],
                                   "states": proc["states"],
                                   "seeded_instances": seeded})

    # ---- 2) workflows (event-reactive, installed INACTIVE - honest) -------
    # dataset names wired into step params resolve to the BUILT names (a
    # second install of the same operator suffixes its datasets - the write
    # must land on the dataset THIS system owns)
    for w in op["workflows"]:
        steps = []
        for s in w.get("steps") or []:
            params = dict(s.get("params") or {})
            ref = str(params.get("dataset") or "").strip()
            if ref in ds_by_name:
                params["dataset"] = ds_by_name[ref]["name"]
            # v85 + v86 + v88: business_advance, business_start AND
            # business_onboard steps resolve the process by NAME in the
            # spec, but bind to the BUILT process id (a second install of
            # the same operator must move, track and onboard ITS pipeline,
            # never the first's)
            proc_ref = str(params.get("process") or "").strip()
            if (proc_ref and str(s.get("type") or "")
                    in ("business_advance", "business_start", "business_onboard")):
                hit = proc_by_name.get(proc_ref)
                if not hit:
                    raise OperatorError(
                        f"workflow {w['name']!r}: process {proc_ref!r} did not build")
                params["process"] = hit["id"]
            steps.append({**s, "params": params})
        graph = validate_graph_document(_workflow_graph({**w, "steps": steps})).model_dump()
        wf = Workflow(name=str(w["name"])[:200],
                      description=str(w.get("description") or "")[:500],
                      graph=graph, is_active=False)
        wf.owner_id = owner_id
        db.add(wf)
        await db.flush()
        await db.refresh(wf)
        await snapshot_workflow_version(db, wf)
        built["workflows"].append({"id": wf.id, "name": wf.name,
                                   "trigger": w["trigger"]["params"].get("event_type", ""),
                                   "active": False})

    # ---- 3) the agent (rooms and queues bind it) --------------------------
    agent_spec = op.get("agent") or {}
    agent_ref = None
    if agent_spec:
        kb = agent_spec.get("knowledge") if isinstance(agent_spec.get("knowledge"), dict) else {}
        kb_kwargs: dict = {}
        if kb and str(kb.get("dataset") or "").strip():
            ds_ref = ds_by_name.get(str(kb.get("dataset")).strip())
            if not ds_ref:
                raise OperatorError(f"knowledge dataset {kb.get('dataset')!r} did not build")
            kb_kwargs = {
                "knowledge_dataset_id": ds_ref["id"],
                "knowledge_text_column": kb.get("text_column"),
                "knowledge_answer_column": kb.get("answer_column"),
                "knowledge_top_k": max(1, min(int(kb.get("top_k") or 1), 5)),
            }
        try:
            va = await va_svc.create_agent(
                db, owner_id=owner_id,
                name=str(agent_spec["name"])[:140],
                description=f"Installed with the {op['name']} operator - " + (note or op["tagline"] or "")[:300],
                greeting_text=str(agent_spec.get("greeting") or "")[:400],
                system_prompt=str(agent_spec.get("system_prompt") or "")[:2000],
                scaffold_handler=True,
                brain=brain,
                brain_provider=("openai_compatible" if brain == "ai_agent" else "sandbox_bridge"),
                llm_credential_id=(llm_credential_id or None) if brain == "ai_agent" else None,
                **kb_kwargs)
        except va_svc.VoiceAgentError as exc:
            raise OperatorError(f"agent {agent_spec['name']!r} failed to build: {exc}") from exc
        agent_ref = va
        built["agents"].append({"id": va["id"], "name": va["name"],
                                "handler_workflow_id": va.get("handler_workflow_id"),
                                "knowledge": va.get("knowledge")})
    agent_id = (agent_ref or {}).get("id")

    # ---- 4) rooms (video-first when the operator says so) -----------------
    room_by_name: dict[str, dict] = {}
    for r in op["rooms"]:
        room = await meetings_svc.create_meeting(
            db, owner_id=owner_id, agent_id=agent_id,
            title=str(r.get("title") or r["name"])[:200])
        if str(r.get("modality") or "audio").lower() == "video":
            from ..models import VoiceMeeting

            row = await db.get(VoiceMeeting, room["id"])
            ctx = dict(row.context or {})
            ctx["modality"] = "audio+video"
            ctx["media_session_kind"] = "video"
            row.context = ctx
            db.add(row)
            await db.flush()
        room_by_name[r["name"]] = {"id": room["id"], "title": room["title"]}
        built["rooms"].append({"id": room["id"], "title": room["title"],
                               "modality": str(r.get("modality") or "audio")})

    # ---- 5) queues (seating into the built rooms) -------------------------
    for q in op["queues"]:
        room = room_by_name.get(str(q.get("room") or "").strip())
        if not room:
            raise OperatorError(f"queue {q['name']!r}: room {q.get('room')!r} did not build")
        try:
            queue = await queue_svc.create_queue(
                db, owner_id=owner_id, name=str(q["name"])[:140],
                meeting_id=room["id"],
                agent_id=agent_id if q.get("bind_agent") else None,
                config=dict(q.get("config") or {}))
        except queue_svc.VoiceQueueError as exc:
            raise OperatorError(f"queue {q['name']!r} failed to build: {exc}") from exc
        built["queues"].append({"id": queue["id"], "name": queue["name"],
                                "meeting": room["title"], "config": queue["config"]})

    # ---- 6) the campaign (composed directly: create_campaign refuses ------
    # empty target lists BY DESIGN - an operator ships an empty dialer the
    # team fills from the CRM; validate_config fills the retry/AMD defaults)
    camp_spec = op.get("campaign")
    if camp_spec:
        cfg = campaigns_svc.validate_config(dict(camp_spec.get("config") or {}))
        if not agent_id:
            raise OperatorError("a campaign operator needs its agent to build first")
        camp_row = VoiceCampaign(owner_id=owner_id, agent_id=agent_id,
                                 name=str(camp_spec["name"])[:140],
                                 endpoint_id=None, config=cfg)
        db.add(camp_row)
        await db.flush()
        await db.refresh(camp_row)
        built["campaign"] = {"id": camp_row.id, "name": camp_row.name,
                             "targets": 0, "config": cfg}
        wiring_notes.append("bind a telnyx voice endpoint on the campaign (channels page) "
                            "and add targets from the CRM - until then dials skip honestly.")

    # ---- 7) the STAFF DASHBOARD - generated over the BUILT datasets -------
    dash_spec = op["dashboard"]
    dash = Dashboard(
        name=await _unique_dashboard_name(db, str(dash_spec["name"])),
        slug=await dash_svc.unique_slug(db, dash_spec["name"]),
        description=str(dash_spec.get("description") or "")[:500],
        config=dash_svc.generate_config(ds_rows),
        status="draft")
    dash.owner_id = owner_id
    db.add(dash)
    await db.flush()
    built["dashboard"] = {"id": dash.id, "name": dash.name, "slug": dash.slug,
                          "components": len((dash.config or {}).get("components", []))}

    # ---- 8) THE SYSTEM - a running business entity from day one -----------
    sys_row = Py8nSystem(
        name=str(op["name"])[:140],
        description=f"Installed from the {op['name']} operator - " + (note or op["tagline"] or "")[:400],
        icon=op["icon"], color=op["color"])
    sys_row.owner_id = owner_id
    db.add(sys_row)
    await db.flush()
    for ds in built["datasets"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="dataset", ref_id=ds["id"]))
    for wf in built["workflows"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="workflow", ref_id=wf["id"]))
    for pr in built["processes"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="process", ref_id=pr["id"]))
    for a in built["agents"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="voice_agent", ref_id=a["id"]))
        if a.get("handler_workflow_id"):
            db.add(SystemComponent(system_id=sys_row.id, kind="workflow",
                                   ref_id=a["handler_workflow_id"]))
    for room in built["rooms"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="meeting", ref_id=room["id"]))
    for q in built["queues"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="queue", ref_id=q["id"]))
    if built["campaign"]:
        db.add(SystemComponent(system_id=sys_row.id, kind="campaign",
                               ref_id=built["campaign"]["id"]))
    db.add(SystemComponent(system_id=sys_row.id, kind="dashboard", ref_id=dash.id))
    await db.flush()

    comp_rows = (await db.execute(
        select(SystemComponent).where(SystemComponent.system_id == sys_row.id))).scalars().all()
    counts: dict[str, int] = {}
    for c in comp_rows:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    await system_runtime.install_mark(
        db, sys_row, solution_slug=f"operator:{op['slug']}",
        actor=owner_id or "operator", component_counts=counts)

    built["system"] = {"id": sys_row.id, "name": sys_row.name,
                       "lifecycle": sys_row.lifecycle or "running",
                       "components": counts}
    built["notes"] = wiring_notes
    return built
