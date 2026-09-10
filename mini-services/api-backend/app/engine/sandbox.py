"""Sandbox for untrusted user code (code node, python_transform, AI-agent code tool).

The engine executes user-authored Python snippets. A namespace allowlist alone
is NOT a security boundary: CPython object graphs reachable from any value
(``().__class__.__bases__[0].__subclasses__()`` ...) or from a module object
(``json.__loader__``) end in ``os``/``subprocess``. This module closes the
verified escapes with four independent layers:

1. **AST guard** (:func:`guard`) - the snippet is parsed and statically
   rejected unless it only uses the allowed syntax surface:
   * no dunder names anywhere (loads, stores, definitions, attribute access)
     - this kills the entire ``__class__``/``__globals__``/``__subclasses__``
     gadget family, ``__import__`` calls and pickle round-trips;
   * no reflective builtins (``eval``, ``exec``, ``compile``, ``getattr``,
     ``setattr``, ``open``, ``__import__``, ``globals``, ``vars``, ...);
   * imports only from the node's allowlisted module map (no star imports,
     no relative imports, nothing outside the map).
2. **Module proxies** (:class:`ModuleProxy`) - allowed modules are exposed as
   attribute proxies that refuse every underscore-prefixed attribute, so
   ``json.__loader__``/``re.__builtins__`` fail at runtime even if a future
   guard regression let them through.
3. **Deep-copied inputs** (:func:`deepcopy_state`) - values injected into the
   user namespace are deep-copied (when picklable) so a snippet cannot mutate
   engine state (other nodes' outputs) by reference.
4. **Bounded executor** (:func:`run_bounded`) - snippets run on a small
   dedicated daemon-thread pool (never the default executor the server
   itself depends on) with a hard asyncio timeout.

Residual, documented risk: a Python thread cannot be forcibly killed, so a
snippet in a tight infinite loop occupies one pool worker until it finishes
(never, for a true infinite loop) - the asyncio caller still returns on
timeout, but the WORKER is now permanently gone from the pool. This module
tracks that abandonment (:func:`pool_health`) and logs it loudly (a single
timeout logs a warning; enough simultaneous abandonments to threaten the
pool logs an error) rather than degrading silently - see "Observability"
below.

Multi-tenant hardening (task #2 audit, this module's four layers were
already solid; what was missing was what happens when they're not enough):

* **Observability** - :func:`pool_health` reports total/available/abandoned
  worker counts; ``GET /api/v1/health`` surfaces it so "the sandbox pool is
  quietly dying" is visible in a standard health check and to monitoring,
  not just discoverable by noticing every code node has started timing out.
  Every timeout logs a WARNING (snippet label + current pool state); when
  abandoned workers reach ``settings.sandbox_pool_exhaustion_log_threshold``
  a louder ERROR follows, since at that point innocent tenants are starting
  to queue behind dead threads.
* **Configurable pool size** (``PY8N_SANDBOX_POOL_SIZE``, default 4) - a
  fixed small pool means a handful of hung snippets (from ONE tenant, or an
  uncoordinated few) starves every other tenant's code nodes; sizing this to
  the deployment's real concurrency need raises the number of simultaneous
  hangs required to fully exhaust it. This does not fix the underlying
  problem (threads still can't be killed) - it raises the cost of the DoS
  and buys the observability above more time to page someone before 100% of
  slots are gone.
* **Scoped path to real isolation (not yet built - documented here as the
  next step, not implemented this pass):** the fix that actually kills a
  runaway snippet is running it out-of-process. A ``multiprocessing``
  worker pool (not threads) lets a hung worker be ``SIGKILL``ed on timeout
  and the slot reclaimed immediately - at the cost of pickling the
  namespace across the process boundary (this module's
  :func:`deepcopy_state` already assumes picklability for the values it
  copies, so the boundary is not a new constraint) and losing the shared-
  interpreter fast path for the common case (most snippets finish in
  milliseconds; process spawn overhead would dominate for those). The
  scoped design: gate it behind a new setting (``PY8N_SANDBOX_ISOLATION``,
  default ``"thread"``, opt into ``"process"``) so single-tenant/trusted
  deployments keep the fast thread path and multi-tenant/public
  deployments (systems on a custom domain, per the README's whole premise)
  opt into the slower-but-actually-killable one; the AST guard, module
  proxies and deep-copy layers all stay unchanged underneath either
  executor - only :func:`run_bounded`'s implementation would branch.
  Tracked as follow-up work, not silently deferred: this docstring IS the
  scope note.

Defense-in-depth still ends at the process boundary today - run untrusted
multi-tenant workloads in a container regardless of the above.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import logging
import re
import threading
from types import CodeType, ModuleType
from typing import Any, Callable

from ..config import settings

__all__ = [
    "ModuleProxy",
    "SandboxViolation",
    "SandboxTimeout",
    "guard",
    "make_proxies",
    "deepcopy_state",
    "run_bounded",
    "pool_health",
]

_log = logging.getLogger("py8n.sandbox")


class SandboxViolation(ValueError):
    """User code was statically rejected by the sandbox guard."""


class SandboxTimeout(TimeoutError):
    """User code exceeded its wall-clock budget."""


# ---------------------------------------------------------------------------
# Static policy
# ---------------------------------------------------------------------------

# Reflective / escape-hatch names. ``print`` stays available (nodes rely on
# it for stdout capture).
_DENIED_NAMES = frozenset(
    {
        "eval", "exec", "compile", "__import__", "globals", "locals", "vars",
        "getattr", "setattr", "delattr", "open", "input", "breakpoint",
        "help", "exit", "quit", "license", "credits", "copyright",
    }
)

# Attribute names that must never be *accessed*. Any underscore-prefixed
# attribute is refused: dunders are the object-graph gadget family, single
# underscores hide the sandbox's own machinery (and module/private internals),
# and "pickle" covers read_pickle/to_pickle/load_pickle (arbitrary pickle ==
# arbitrary code execution).
_DENIED_ATTR_RE = re.compile(r"^_|pickle")

# Names that may never even be *defined* by a snippet (class/def/arg names).
_DENIED_DEF_RE = re.compile(r"^__")


def guard(
    code: str,
    allowed_modules: dict[str, ModuleType],
    *,
    extra_roots: frozenset[str] | set[str] = frozenset(),
) -> CodeType:
    """Statically validate a snippet and return it compiled and ready to exec.

    ``allowed_modules`` maps names that are (proxy-)injected into the user
    namespace; ``extra_roots`` are additionally importable top-level modules
    resolved by the node's own import hook (e.g. sklearn submodules) - they
    are not proxied. Raises :class:`SandboxViolation` with an end-user-safe
    message when the snippet leaves the allowed surface.
    """
    try:
        tree = ast.parse(code or "", mode="exec")
    except SyntaxError as exc:
        raise SandboxViolation(f"SyntaxError: {exc.msg} (line {exc.lineno})") from exc

    allowed = set(allowed_modules or {}) | set(extra_roots)

    for node in ast.walk(tree):
        # --- imports: only allowlisted top-level modules, no star imports ---
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in allowed:
                    raise SandboxViolation(
                        f"import of {alias.name!r} is not allowed in sandboxed code"
                    )
                if alias.asname and _DENIED_ATTR_RE.search(alias.asname):
                    raise SandboxViolation(
                        f"binding name {alias.asname!r} is not allowed in sandboxed code"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module is None or node.level:  # relative import
                raise SandboxViolation("relative imports are not allowed")
            if node.level or any(alias.name == "*" for alias in node.names):
                raise SandboxViolation("'import *' is not allowed")
            root = node.module.split(".")[0]
            if root not in allowed:
                raise SandboxViolation(
                    f"import of {node.module!r} is not allowed in sandboxed code"
                )
            for alias in node.names:
                # 'from X import __loader__' resolves via getattr on the REAL
                # module (sys.modules), bypassing ModuleProxy - block dunders
                # and pickle surface in from-import bindings outright.
                if alias.name == "*" or _DENIED_ATTR_RE.search(alias.name):
                    raise SandboxViolation(
                        f"importing {alias.name!r} is not allowed in sandboxed code"
                    )
                if alias.asname and _DENIED_ATTR_RE.search(alias.asname):
                    raise SandboxViolation(
                        f"binding name {alias.asname!r} is not allowed in sandboxed code"
                    )

        # --- reflective builtins / escape-hatch names / any dunder name ---
        elif isinstance(node, ast.Name) and (
            node.id in _DENIED_NAMES or node.id.startswith("__")
        ):
            raise SandboxViolation(f"{node.id!r} is not allowed in sandboxed code")

        # --- dunder/pickle attribute access (obj.__class__, pd.read_pickle) ---
        elif isinstance(node, ast.Attribute) and _DENIED_ATTR_RE.search(node.attr):
            raise SandboxViolation(
                f"access to attribute {node.attr!r} is not allowed in sandboxed code"
            )

        # --- dunder definitions (def __init__, class X: __class__ = ...) ---
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _DENIED_DEF_RE.match(node.name):
                raise SandboxViolation(f"defining {node.name!r} is not allowed")
        elif isinstance(node, ast.arg) and _DENIED_DEF_RE.match(node.arg):
            raise SandboxViolation(
                f"argument name {node.arg!r} is not allowed in sandboxed code"
            )
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and any(
            _DENIED_DEF_RE.match(n) for n in node.names
        ):
            raise SandboxViolation("global/nonlocal on dunder names is not allowed")

    try:
        return compile(tree, "<py8n-sandbox>", "exec")
    except (ValueError, TypeError) as exc:  # e.g. null bytes
        raise SandboxViolation(f"code rejected: {exc}") from exc


# ---------------------------------------------------------------------------
# Namespace preparation
# ---------------------------------------------------------------------------


class ModuleProxy:
    """Attribute proxy over an allowed module; hides every underscore name.

    Module objects carry ``__builtins__``/``__loader__``/``__spec__`` which
    are classic sandbox escapes (``json.__loader__.load_module('os')``). The
    proxy only forwards public attributes - enforced in ``__getattribute__``
    so even the proxy's own private slots (``_module``) are unreachable
    through it.
    """

    __slots__ = ("_module", "_name")

    # Dunder protocol attributes the interpreter itself may touch on the
    # proxy; everything else underscore-ish is refused.
    _SAFE_DUNDER = frozenset({"__class__", "__dir__", "__repr__", "__getattr__"})

    def __init__(self, module: ModuleType, name: str):
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_name", name)

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_") and name not in ModuleProxy._SAFE_DUNDER:
            raise AttributeError(
                f"attribute {name!r} is not available in sandboxed code"
            )
        return object.__getattribute__(self, name)

    def __getattr__(self, attr: str) -> Any:
        # Only reached when default lookup already failed (public names not
        # present as slots); refuse underscore names here as well so dynamic
        # resolution can never smuggle private module attributes through.
        if attr.startswith("_") or "pickle" in attr:
            raise AttributeError(
                f"module {object.__getattribute__(self, '_name')!r} attribute "
                f"{attr!r} is not available in sandboxed code"
            )
        return getattr(object.__getattribute__(self, "_module"), attr)

    def __dir__(self) -> list[str]:
        return [a for a in dir(object.__getattribute__(self, "_module")) if not a.startswith("_")]

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<ModuleProxy {object.__getattribute__(self, '_name')!r}>"


def make_proxies(user_globals: dict[str, Any], allowed_modules: dict[str, ModuleType]) -> None:
    """Replace raw module objects in ``user_globals`` with :class:`ModuleProxy`."""
    for key, mod in (allowed_modules or {}).items():
        if isinstance(mod, ModuleType) and user_globals.get(key) is mod:
            user_globals[key] = ModuleProxy(mod, key)


def deepcopy_state(user_globals: dict[str, Any], *, skip: set[str] | frozenset[str]) -> None:
    """Deep-copy injected values so snippets cannot mutate engine state.

    Values that cannot be deep-copied (locks, open handles, huge objects a
    node deliberately shares - e.g. the python_transform DataFrame) are kept
    by reference; ``skip`` names keys that must never be copied.
    """
    for key, value in list(user_globals.items()):
        if key in skip or key == "__builtins__" or isinstance(value, ModuleProxy):
            continue
        try:
            user_globals[key] = copy.deepcopy(value)
        except Exception:  # noqa: BLE001 - keep the reference when uncopyable
            continue


# ---------------------------------------------------------------------------
# Bounded execution
# ---------------------------------------------------------------------------

# Dedicated bounded pool for user snippets (PY8N_SANDBOX_POOL_SIZE, default
# 4 concurrent). anyio's worker threads are DAEMON threads: a snippet stuck
# in a tight loop after its asyncio timeout can never block interpreter
# shutdown (a ThreadPoolExecutor of non-daemon threads would hang the
# process at exit). A hard thread kill does not exist in Python, so a
# runaway snippet occupies a limiter slot until it finishes - documented
# residual risk; container isolation is the backstop for hostile tenants,
# and the module docstring scopes a real subprocess-isolation path.
# Starlette/FastAPI already depend on anyio.
import anyio  # noqa: E402
from anyio import CapacityLimiter  # noqa: E402

_USER_LIMITER = CapacityLimiter(max(1, int(settings.sandbox_pool_size)))

# Observability for the one failure mode the pool can't fix on its own: a
# thread that outlives its timeout is ABANDONED (still holds its limiter
# slot, forever if it never returns). `_abandoned_count` is best-effort
# (a thread that finishes and a timeout firing can race by a few
# instructions) but is exactly right at the scale that matters here: "is
# the pool quietly losing workers" is a trend, not a strict count.
_pool_lock = threading.Lock()
_abandoned_count = 0


def pool_health() -> dict[str, int]:
    """Current user-code pool state, for health checks and monitoring.

    ``total``/``borrowed``/``available`` come straight from the
    CapacityLimiter; ``abandoned`` is this module's own count of workers
    that outlived their timeout and are presumed hung (a subset of
    ``borrowed`` - a slot can be legitimately borrowed by a snippet still
    well within its budget).
    """
    with _pool_lock:
        abandoned = _abandoned_count
    return {
        "total": _USER_LIMITER.total_tokens,
        "borrowed": _USER_LIMITER.borrowed_tokens,
        "available": _USER_LIMITER.available_tokens,
        "abandoned": abandoned,
    }


def _mark_abandoned(label: str) -> None:
    global _abandoned_count
    with _pool_lock:
        _abandoned_count += 1
        count = _abandoned_count
    health = pool_health()
    _log.warning(
        "sandbox: %r timed out - its worker thread cannot be killed and is "
        "now abandoned (keeps running until it finishes, if ever). pool=%s",
        label, health,
    )
    if count >= settings.sandbox_pool_exhaustion_log_threshold:
        _log.error(
            "sandbox: %d abandoned worker thread(s) out of %d total - the "
            "user-code pool is degrading. Other tenants' code nodes will "
            "queue behind these (if all slots end up abandoned, every "
            "future snippet times out too). See engine/sandbox.py's module "
            "docstring for the hardening plan (pool sizing today, scoped "
            "subprocess isolation as the real fix).",
            count, health["total"],
        )


def _mark_recovered() -> None:
    global _abandoned_count
    with _pool_lock:
        if _abandoned_count > 0:
            _abandoned_count -= 1
    _log.info("sandbox: a previously-abandoned worker thread finished on its own - pool slot reclaimed.")


async def run_bounded(
    fn: Callable[[], Any],
    *,
    timeout_seconds: float,
    label: str = "user code",
) -> Any:
    """Run ``fn`` on the bounded user-code pool with a hard timeout.

    On timeout the awaiting task is cancelled and the worker thread is
    abandoned (it keeps running daemonized); the caller receives
    :class:`SandboxTimeout`. The abandonment - and, if the thread does
    eventually finish, its recovery - is tracked via :func:`pool_health`
    and logged, so a degrading pool is visible instead of silently eating
    every tenant's code nodes one timeout at a time.
    """
    state = {"timed_out": False}

    def _tracked() -> Any:
        try:
            return fn()
        finally:
            if state["timed_out"]:
                _mark_recovered()

    try:
        return await asyncio.wait_for(
            anyio.to_thread.run_sync(_tracked, limiter=_USER_LIMITER),
            timeout=timeout_seconds,
        )
    except (asyncio.TimeoutError, TimeoutError):
        state["timed_out"] = True
        _mark_abandoned(label)
        raise SandboxTimeout(f"{label} timed out after {timeout_seconds}s") from None
