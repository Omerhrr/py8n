"""Jinja2 dynamic parameter resolution.

Node parameters may embed expressions that are resolved against the live
execution context at run time, e.g.::

    "https://api.dev/users/{{ nodes.trigger.output.id }}"
    "Hello {{ nodes.http_1.output.body.name | upper }}"

Resolution contract
-------------------
* If a string is *exactly* one ``{{ expression }}``, the expression is
  evaluated and its **native type** is preserved (dicts, lists, ints...).
* Otherwise the template is rendered to a string with partial substitution.
* Dicts / lists are resolved recursively.
* Unknown names raise :class:`TemplateResolutionError` (strict mode) so typos
  fail loudly instead of silently producing ``None``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from jinja2 import StrictUndefined, Undefined
from jinja2.exceptions import SecurityError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment

TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


class TemplateResolutionError(RuntimeError):
    """Raised when an expression cannot be resolved against the context."""


class DictKeyFirstSandbox(SandboxedEnvironment):
    """Sandboxed env where dict **key** access wins over same-named methods.

    Automation data is dict-shaped and the ``items`` convention is everywhere
    (``{{ input.items }}``, ``{{ nodes.lp.output.done.items }}``) — Jinja's
    default ``getattr`` would return the *method* ``dict.items`` for those
    names and blow up with "builtin_function_or_method is not iterable".
    Keys take precedence; methods still resolve when no key matches (so
    ``{% for k, v in plain.items() %}`` keeps working on dicts without an
    ``items`` key).
    """

    def getattr(self, obj: Any, attribute: str) -> Any:
        if isinstance(obj, dict) and attribute in obj:
            return obj[attribute]
        return super().getattr(obj, attribute)


def build_environment() -> SandboxedEnvironment:
    env = DictKeyFirstSandbox(
        undefined=StrictUndefined,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )
    # Handy filters for automation authors
    env.filters["tojson"] = lambda v, indent=None: __import__("json").dumps(v, indent=indent, default=str)
    env.globals["now"] = lambda: datetime.now(timezone.utc)
    return env


_ENV = build_environment()


def _expression_context(context: dict[str, Any]) -> dict[str, Any]:
    """Expose the execution context dict as Jinja root names."""
    ctx = dict(context)
    ctx.setdefault("nodes", {})
    return ctx


def resolve_value(value: Any, context: dict[str, Any]) -> Any:
    """Recursively resolve Jinja2 expressions inside a parameter value."""
    if isinstance(value, str):
        return _resolve_string(value, _expression_context(context))
    if isinstance(value, dict):
        return {k: resolve_value(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_value(v, context) for v in value]
    return value


def _resolve_string(template: str, ctx: dict[str, Any]) -> Any:
    stripped = template.strip()
    # Case 1: whole string is a single expression -> preserve native type.
    match = re.fullmatch(r"\{\{(.*?)\}\}", stripped, re.DOTALL)
    if match:
        return _evaluate_expression(match.group(1), ctx)

    # Case 2: mixed string -> render to text.
    if "{{" in template:
        try:
            return _ENV.from_string(template).render(**ctx)
        except UndefinedError as exc:  # missing variable
            raise TemplateResolutionError(f"Unresolved variable in {template!r}: {exc.message}") from exc
        except SecurityError as exc:
            raise TemplateResolutionError(f"Forbidden expression in {template!r}: {exc}") from exc
        except Exception as exc:  # syntax error etc.
            raise TemplateResolutionError(f"Bad template {template!r}: {exc}") from exc
    return template


def _evaluate_expression(expression: str, ctx: dict[str, Any]) -> Any:
    expr = expression.strip()
    try:
        compiled = _ENV.compile_expression(expr, undefined_to_none=False)
        result = compiled(**ctx)
    except UndefinedError as exc:
        raise TemplateResolutionError(f"Unresolved variable '{{{{ {expr} }}}}': {exc.message}") from exc
    except SecurityError as exc:
        raise TemplateResolutionError(f"Forbidden expression '{{{{ {expr} }}}}': {exc}") from exc
    except TemplateResolutionError:
        raise
    except Exception as exc:
        raise TemplateResolutionError(f"Bad expression '{{{{ {expr} }}}}': {exc}") from exc
    if isinstance(result, Undefined):
        raise TemplateResolutionError(
            f"Unresolved variable '{{{{ {expr} }}}}' — not found in execution context"
        )
    return result
