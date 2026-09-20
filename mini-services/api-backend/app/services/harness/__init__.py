"""The harness (v109) - py8n's own agentic runtime, in tandem with the system.

Package layout (the distilled deepseek-harness architecture, built native):

* ``tools``    - the estate as the model's toolchest (reads + SENSITIVE moves)
* ``guard``    - repeat / budget / clock discipline around the loop
* ``loop``     - the turn state machine (pause at the gate, resume from wire)
* ``service``  - sessions, decisions, the fail-closed sweep, health
"""

from .guard import GuardVerdict, TurnGuard, canonical_args
from .loop import drive, history_messages
from .service import (HarnessError, build_guard, create_session, decide,
                      get_approval, get_session, get_turn, harness_health,
                      list_approvals, list_sessions, list_turns, start_turn,
                      sweep_expired)
from .tools import (HarnessToolError, ToolDef, ToolResult, build_registry,
                    tool_catalogue)

__all__ = [
    "GuardVerdict", "TurnGuard", "canonical_args",
    "drive", "history_messages",
    "HarnessError", "build_guard", "create_session", "decide", "get_approval",
    "get_session", "get_turn", "harness_health", "list_approvals",
    "list_sessions", "list_turns", "start_turn", "sweep_expired",
    "HarnessToolError", "ToolDef", "ToolResult", "build_registry",
    "tool_catalogue",
]
