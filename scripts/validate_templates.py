"""Validate all template graphs + execute the offline-safe ones end-to-end."""
import asyncio
import sys

sys.path.insert(0, "/home/z/my-project/mini-services/api-backend")

from app.engine import GraphRunner  # noqa: E402
from app.engine.nodes.base import BaseNode  # noqa: E402  (registry side-effects)
from app.engine.runner import validate_graph_document  # noqa: E402
from app.services.templates import TEMPLATES  # noqa: E402

# Templates that hit external services, pause, or are webhook-driven: skip direct run.
SKIP_RUN = {"api-poller", "webhook-slack-alert", "approval-gate", "daily-digest", "ai-writer", "lead-router"}


def main() -> None:
    print(f"{len(TEMPLATES)} templates")
    for t in TEMPLATES:
        spec = validate_graph_document(t["graph"])  # raises on bad graph
        print(f"  [validate] {t['id']}: OK ({len(spec.nodes)} nodes)")

    for t in TEMPLATES:
        if t["id"] in SKIP_RUN:
            continue
        graph = validate_graph_document(t["graph"])
        result = asyncio.run(
            GraphRunner(graph, workflow_id="tpl", workflow_name=t["name"]).run()
        )
        assert result["status"] == "success", (t["id"], result["error"])
        last = [r for r in result["node_runs"] if r["status"] == "success"][-1]
        print(f"  [run] {t['id']}: {result['status']} · last output: {str(last.get('output'))[:110]}")

    # ai-writer runs against the LLM bridge; run it too when bridge is up.
    try:
        import urllib.request

        urllib.request.urlopen("http://127.0.0.1:3010/health", timeout=2)
    except Exception:
        print("  [run] ai-writer skipped (bridge down)")
        return
    t = next(t for t in TEMPLATES if t["id"] == "ai-writer")
    graph = validate_graph_document(t["graph"])
    result = asyncio.run(GraphRunner(graph, workflow_id="tpl", workflow_name=t["name"]).run())
    assert result["status"] == "success", result["error"]
    print("  [run] ai-writer: success (LLM output captured)")


if __name__ == "__main__":
    main()
