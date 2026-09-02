"""Business rules engine (v30) - validate, warn and auto-compute on records.

Rules live in ``app.config["rules"]``:

    {"rules": [
        {"id": "rule_ltv", "name": "LTV cap", "event": "create",
         "when": {"all": [{"field": "ltv", "op": "gt", "value": 10000}]},
         "action": "block", "message": "LTV above 10000 needs sign-off"},

        {"id": "rule_comm", "name": "Commission", "event": "always",
         "when": {"all": [{"field": "plan", "op": "eq", "value": "pro"}]},
         "action": "set", "field": "commission", "formula": "ltv * 0.1"},

        {"id": "rule_big", "name": "Big deal", "event": "update",
         "when": {"all": [{"field": "ltv", "op": "gte", "value": 5000}]},
         "action": "warn", "message": "Big deal - call the customer"},
    ]}

Events: ``create`` (append only), ``update`` (edit only), ``always`` (both).
Actions: ``block`` (reject with 400 + message), ``warn`` (accept, surface
message in the response), ``set`` (compute/insert a field value - either a
constant ``value`` or an arithmetic ``formula`` over row fields).

Formulas are parsed with :mod:`ast` and restricted to numbers, field names
and ``+ - * / % **`` - no calls, no attributes, no imports. Non-numeric
field values make the formula skip (data owns the fallout, as in v29).
"""

from __future__ import annotations

import ast
import operator

import pandas as pd

RULE_OPS = {
    "eq", "ne", "gt", "gte", "lt", "lte",
    "contains", "not_contains", "starts_with", "ends_with",
    "empty", "not_empty",
}
RULE_ACTIONS = {"block", "warn", "set"}
RULE_EVENTS = {"create", "update", "always"}
VALUELESS_OPS = {"empty", "not_empty"}
MAX_RULES = 50

# Audit guard: formulas are tiny arithmetic expressions; anything longer is
# abuse (the evaluator walks the whole AST).
MAX_FORMULA_LEN = 1000

_ALLOWED_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


class RuleBlocked(ValueError):
    """Raised when a ``block`` rule fires - surfaces as a 400."""


# ----------------------------------------------------------------- matching
def _num(v: object) -> float | None:
    """Loose numeric coercion - bools are NOT numbers for rule purposes."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if pd.isna(v) else float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _is_empty(v: object) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and pd.isna(v):
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    return False


def _loose_eq(a: object, b: object) -> bool:
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return na == nb
    if a is None or (isinstance(a, str) and a.strip() == ""):
        return b is None or (isinstance(b, str) and b.strip() == "")
    return str(a).strip().lower() == str(b).strip().lower()


def _match(op: str, actual: object, expected: object) -> bool:
    if op == "empty":
        return _is_empty(actual)
    if op == "not_empty":
        return not _is_empty(actual)
    if op == "eq":
        return _loose_eq(actual, expected)
    if op == "ne":
        return not _loose_eq(actual, expected)
    if op in ("gt", "gte", "lt", "lte"):
        a, e = _num(actual), _num(expected)
        if a is not None and e is not None:
            return {"gt": a > e, "gte": a >= e, "lt": a < e, "lte": a <= e}[op]
        s, t = str(actual if actual is not None else ""), str(expected if expected is not None else "")
        return {"gt": s > t, "gte": s >= t, "lt": s < t, "lte": s <= t}[op]
    s = str(actual if actual is not None else "")
    t = str(expected if expected is not None else "")
    if op == "contains":
        return t.lower() in s.lower()
    if op == "not_contains":
        return t.lower() not in s.lower()
    if op == "starts_with":
        return s.lower().startswith(t.lower())
    if op == "ends_with":
        return s.lower().endswith(t.lower())
    return False


def eval_condition(when: dict | None, row: dict) -> bool:
    """``{"all": [...]}`` conjunction - missing/empty ``when`` matches everything."""
    clauses = (when or {}).get("all") if isinstance(when, dict) else None
    if not clauses:
        return True
    for c in clauses:
        if not isinstance(c, dict):
            return False
        if not _match(str(c.get("op")), row.get(c.get("field")), c.get("value")):
            return False
    return True


# ----------------------------------------------------------------- formulas
def eval_formula(expr: str, row: dict) -> float:
    """Safely evaluate ``ltv * 0.1``-style arithmetic over row fields.

    Guards (audit hardening):
    * expression length capped at MAX_FORMULA_LEN;
    * parsed with :mod:`ast` and restricted to numbers, field names and
      ``+ - * / % **`` - NO calls, attributes, subscripts, lambdas, imports
      or builtins are reachable at all (the allowlist is the empty set);
    * dunder names (``__class__``, ``__import__``, ...) are rejected
      outright, even when the row happens to carry such a key;
    * math failures (division by zero, overflow) surface as ValueError so
      callers treat them as rule-data fallout, never a 500.
    """
    expr = (expr or "").strip()
    if len(expr) > MAX_FORMULA_LEN:
        raise ValueError(f"formula too long (max {MAX_FORMULA_LEN} chars)")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid formula: {exc.msg}") from exc

    def ev(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.BinOp) and type(n.op) in _ALLOWED_BIN:
            return _ALLOWED_BIN[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            v = ev(n.operand)
            return v if isinstance(n.op, ast.UAdd) else -v
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.Name):
            if "__" in n.id:
                raise ValueError(f"forbidden name {n.id!r} in formula")
            if n.id not in row:
                raise ValueError(f"unknown field {n.id!r} in formula")
            v = _num(row[n.id])
            if v is None:
                raise ValueError(f"field {n.id!r} is not numeric")
            return v
        if isinstance(n, (ast.Call, ast.Attribute, ast.Subscript, ast.Lambda)):
            raise ValueError("formula allows only numbers, fields and + - * / % **")
        raise ValueError("formula allows only numbers, fields and + - * / % **")

    try:
        return float(ev(tree))
    except (ZeroDivisionError, OverflowError) as exc:
        # 1/0 or 1e308 ** 2 must surface as rule data fallout, not a 500.
        raise ValueError(f"formula math error: {exc}") from exc


def formula_fields(expr: str) -> set[str]:
    """Field names referenced by a formula (for validation)."""
    try:
        tree = ast.parse((expr or "").strip(), mode="eval")
    except SyntaxError:
        return set()
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _validate_formula(expr: str) -> None:
    """Shared config-time formula checks (length, syntax, dunder names)."""
    if len((expr or "").strip()) > MAX_FORMULA_LEN:
        raise ValueError(f"formula too long (max {MAX_FORMULA_LEN} chars)")
    try:
        tree = ast.parse((expr or "").strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid formula: {exc.msg}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and "__" in node.id:
            raise ValueError(f"forbidden name {node.id!r} in formula")


# ----------------------------------------------------------------- validation
def validate_rules(rules: object, schema: list[dict]) -> None:
    """Raise ValueError with an end-user message on any bad rule."""
    if rules is None:
        return
    if not isinstance(rules, list):
        raise ValueError("config.rules must be a list")
    if len(rules) > MAX_RULES:
        raise ValueError(f"too many rules (max {MAX_RULES})")
    names = {c["name"] for c in schema}
    ids: set[str] = set()
    for i, r in enumerate(rules):
        ctx = f"rule[{i}]"
        if not isinstance(r, dict):
            raise ValueError(f"{ctx} must be an object")
        rid = str(r.get("id") or f"rule_{i + 1}").strip()
        if not rid:
            raise ValueError(f"{ctx}: id must not be empty")
        if rid in ids:
            raise ValueError(f"{ctx}: duplicate rule id {rid!r}")
        ids.add(rid)
        event = r.get("event", "always")
        if event not in RULE_EVENTS:
            raise ValueError(f"{ctx} ({rid}): event must be one of {sorted(RULE_EVENTS)}")
        action = r.get("action")
        if action not in RULE_ACTIONS:
            raise ValueError(f"{ctx} ({rid}): action must be one of {sorted(RULE_ACTIONS)}")

        when = r.get("when")
        if when is not None:
            if not isinstance(when, dict) or not isinstance(when.get("all", []), list):
                raise ValueError(f"{ctx} ({rid}): when must be {{\"all\": [...]}}")
            for j, c in enumerate(when.get("all", [])):
                cctx = f"{ctx} clause[{j}]"
                if not isinstance(c, dict):
                    raise ValueError(f"{cctx} must be an object")
                if c.get("field") not in names:
                    raise ValueError(f"{cctx}: field {c.get('field')!r} not in dataset schema")
                if c.get("op") not in RULE_OPS:
                    raise ValueError(f"{cctx}: op must be one of {sorted(RULE_OPS)}")
                if c.get("op") not in VALUELESS_OPS and "value" not in c:
                    raise ValueError(f"{cctx}: op {c['op']!r} requires a value")

        if action in ("block", "warn"):
            msg = r.get("message")
            if msg is not None and not isinstance(msg, str):
                raise ValueError(f"{ctx} ({rid}): message must be a string")
        else:  # set
            field = r.get("field")
            if not field:
                raise ValueError(f"{ctx} ({rid}): action=set requires a field")
            if field not in names:
                raise ValueError(f"{ctx} ({rid}): field {field!r} not in dataset schema")
            has_value = "value" in r and r.get("value") is not None
            has_formula = bool(r.get("formula"))
            if not has_value and not has_formula:
                raise ValueError(f"{ctx} ({rid}): action=set needs value or formula")
            if has_formula:
                if not isinstance(r["formula"], str):
                    raise ValueError(f"{ctx} ({rid}): formula must be a string")
                try:
                    _validate_formula(r["formula"])
                except ValueError as exc:
                    raise ValueError(f"{ctx} ({rid}): {exc}") from exc
                unknown = formula_fields(r["formula"]) - names
                if unknown:
                    raise ValueError(f"{ctx} ({rid}): formula references unknown fields: {sorted(unknown)}")


# ----------------------------------------------------------------- execution
def _cast_set(field: str, value: object, schema: list[dict]) -> object:
    dtype = next((c.get("dtype") for c in schema if c["name"] == field), None)
    if isinstance(value, str):
        if value.strip() == "":
            return None
        if dtype in ("integer", "number"):
            try:
                return int(float(value)) if dtype == "integer" else float(value)
            except ValueError:
                return value
        if dtype == "boolean" and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
    if dtype == "integer" and isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def apply_rules(
    rules: list[dict] | None,
    record: dict,
    event: str,
    schema: list[dict],
) -> tuple[dict, list[str]]:
    """Evaluate rules against ``record`` for ``event`` (create|update).

    Returns ``(possibly modified record, warnings)``; raises :class:`RuleBlocked`
    when a block rule fires. Rules run in config order; block short-circuits.
    """
    warnings: list[str] = []
    out = dict(record)
    for i, rule in enumerate(rules or []):
        if not isinstance(rule, dict):
            continue
        ev = rule.get("event", "always")
        if ev != "always" and ev != event:
            continue
        if not eval_condition(rule.get("when"), out):
            continue
        rid = str(rule.get("id") or f"rule_{i + 1}")
        label = rule.get("name") or rid
        action = rule.get("action")
        if action == "block":
            raise RuleBlocked(rule.get("message") or f"Blocked by rule {label!r}")
        if action == "warn":
            warnings.append(rule.get("message") or f"Flagged by rule {label!r}")
        elif action == "set":
            field = rule.get("field")
            if not field:
                continue
            if rule.get("formula"):
                try:
                    val: object = eval_formula(rule["formula"], out)
                except ValueError:
                    continue  # non-numeric data - leave the submitted value alone
            else:
                val = rule.get("value")
            out[field] = _cast_set(field, val, schema)
    return out, warnings


def dry_run(
    rules: list[dict] | None,
    record: dict,
    event: str,
    schema: list[dict],
) -> dict:
    """Same evaluation without raising - for the builder's test button."""
    matches: list[dict] = []
    warnings: list[str] = []
    blocked = False
    out = dict(record)
    for i, rule in enumerate(rules or []):
        if not isinstance(rule, dict):
            continue
        ev = rule.get("event", "always")
        if ev != "always" and ev != event:
            continue
        if not eval_condition(rule.get("when"), out):
            continue
        rid = str(rule.get("id") or f"rule_{i + 1}")
        label = rule.get("name") or rid
        action = rule.get("action")
        entry: dict = {"id": rid, "name": label, "action": action}
        if action == "block":
            entry["message"] = rule.get("message") or f"Blocked by rule {label!r}"
            matches.append(entry)
            blocked = True
            break
        if action == "warn":
            msg = rule.get("message") or f"Flagged by rule {label!r}"
            entry["message"] = msg
            warnings.append(msg)
            matches.append(entry)
        elif action == "set":
            field = rule.get("field")
            before = out.get(field)
            if rule.get("formula"):
                try:
                    out[field] = _cast_set(field, eval_formula(rule["formula"], out), schema)
                    entry["result"] = f"{field} = {out[field]} (formula)"
                except ValueError as exc:
                    entry["result"] = f"formula skipped: {exc}"
            else:
                out[field] = _cast_set(field, rule.get("value"), schema)
                entry["result"] = f"{field} = {out[field]!r}"
            entry["before"] = before
            matches.append(entry)
    return {"matches": matches, "warnings": warnings, "blocked": blocked, "record": out}
