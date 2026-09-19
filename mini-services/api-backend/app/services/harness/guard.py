"""Harness guard rails (v109) - the discipline layer around the loop.

The deepseek-harness study (Task 9) distilled three guards worth building
natively; this module is that distillation, wired to the harness turn:

* REPEAT  - the same tool called with byte-identical arguments more than
  ``limit`` times in one turn is a stuck loop, not a strategy: the call is
  blocked and the model is told why (it can vary the call or answer).
* BUDGET  - a turn may take at most ``max_iterations`` model rounds; past
  it the turn ends ``exhausted`` instead of burning the estate forever.
* CLOCK   - a wall-clock deadline per turn (checked between rounds - the
  loop is async and every tool is a local service call, so a between-step
  check is honest for this runtime).

A guard block is NEVER silent: the turn's trace carries a ``guard`` frame
and the model receives the refusal as tool feedback.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


def canonical_args(args: dict) -> str:
    """Byte-stable form of a call's arguments (sort keys, stable scalars)."""
    return json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)


@dataclass
class GuardVerdict:
    ok: bool
    reason: str = ""
    kind: str = ""      # repeat | budget | clock


@dataclass
class TurnGuard:
    """One turn's guard state - created fresh per turn, carried on resume."""

    repeat_limit: int
    max_iterations: int
    timeout_seconds: float
    started_at: float = field(default_factory=time.monotonic)
    _seen: dict[str, int] = field(default_factory=dict)

    def check_repeat(self, tool: str, args: dict) -> GuardVerdict:
        key = f"{tool}:{canonical_args(args)}"
        count = self._seen.get(key, 0) + 1
        self._seen[key] = count
        if count > max(1, int(self.repeat_limit)):
            return GuardVerdict(
                ok=False, kind="repeat",
                reason=f"blocked by guard: {tool!r} was already called "
                       f"{count - 1} time(s) with identical arguments this "
                       "turn - repeating it will not change the result; "
                       "vary the call or answer with what you have")
        return GuardVerdict(ok=True)

    def check_budget(self, iterations: int) -> GuardVerdict:
        if iterations >= max(1, int(self.max_iterations)):
            return GuardVerdict(
                ok=False, kind="budget",
                reason=f"iteration budget exhausted ({self.max_iterations}) "
                       "without a final answer")
        return GuardVerdict(ok=True)

    def check_clock(self) -> GuardVerdict:
        if self.timeout_seconds > 0 and \
                (time.monotonic() - self.started_at) > self.timeout_seconds:
            return GuardVerdict(
                ok=False, kind="clock",
                reason=f"turn exceeded the wall clock ({int(self.timeout_seconds)}s)")
        return GuardVerdict(ok=True)
