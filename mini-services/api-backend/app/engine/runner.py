"""GraphRunner - executes a validated graph topologically.

Pipeline
========
1. Validate the graph (Pydantic `GraphSpec`) and build the adjacency map.
2. Pick the firing trigger node; every other trigger is marked skipped.
3. `graphlib.TopologicalSorter` yields a dependency-safe execution order
   (raises on cycles - surfaced as a clean 400 by the API layer).
4. Each node: gather active inputs -> resolve parameters (Jinja2 + Pydantic)
   -> execute -> emit events -> record run.
5. A node with incoming edges but **no active input** is skipped; an IF node's
   inactive branch deactivates exactly its own outgoing edges.
6. Final status is `success` if every executed node succeeded, else `error`.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from graphlib import CycleError, TopologicalSorter
from typing import Any, Awaitable, Callable

from .context import ExecutionContext
from .nodes.base import NodeExecutionError
from .nodes.wait import WaitForResumeNode
from .registry import get_node_class
from .schema import EdgeSpec, GraphSpec, NodeSpec
from .templating import TemplateResolutionError

Emitter = Callable[[dict], Awaitable[None]]


class GraphValidationError(ValueError):
    pass


class GraphRunner:
    def __init__(
        self,
        graph: GraphSpec,
        *,
        workflow_id: str,
        workflow_name: str,
        trigger_type: str = "manual",
        trigger_payload: dict | None = None,
        trigger_node_id: str | None = None,
        emit: Emitter | None = None,
        max_output_capture: int = 20_000,
        execution_id: str | None = None,
        depth: int = 0,
        inherit_node_states: dict[str, dict] | None = None,
        resume_state: dict | None = None,
        cancel_event: asyncio.Event | None = None,
        env_vars: dict[str, str] | None = None,
        honor_pinned: bool | None = None,
        respond_channel: Any | None = None,
    ):
        self.graph = graph
        self.workflow_id = workflow_id
        self.workflow_name = workflow_name
        self.trigger_type = trigger_type
        self.trigger_payload = trigger_payload or {}
        self.trigger_node_id = trigger_node_id
        if emit is None:
            async def _noop(event: dict) -> None:
                return None
            emit = _noop
        elif not asyncio.iscoroutinefunction(emit):
            sync_emit = emit

            async def emit(event: dict) -> None:  # type: ignore[misc]
                sync_emit(event)
        self.emit = emit
        self.max_output_capture = max_output_capture
        self.depth = depth  # sub-workflow / loop nesting level (0 = root)
        # Global env vars for {{ env.KEY }} (v15). None = load from the DB on
        # run(); callers that already hold the map (loop batches) pass it in
        # so nested runs never re-query.
        self.env_vars = env_vars
        # v17 pinned data: manual runs + test steps honor node pins (mock
        # output without executing); production triggers never do. None =
        # auto-derive from the trigger type. Loop batches and sub-workflows
        # inherit this runner's decision explicitly.
        self.honor_pinned = (trigger_type == "manual") if honor_pinned is None else honor_pinned
        # v21 Respond to Webhook: the webhook endpoint installs an async
        # callable that the respond_to_webhook node awaits. ONLY the root run
        # receives it - sub-workflows / loop bodies never hijack the caller.
        self.respond_channel = respond_channel
        # Outputs of nodes that already ran in a PARENT run - seeded into the
        # context so loop bodies / sub-runs can reference upstream nodes.
        self.inherit_node_states: dict[str, dict] = inherit_node_states or {}

        self.execution_id: str = execution_id or uuid.uuid4().hex
        self.node_runs: list[dict] = []
        self.status: str = "running"
        self.error: str | None = None
        # Cooperative cancellation: set between nodes by the cancel endpoint.
        self._cancel_event = cancel_event
        self._active_edges: set[str] = set()  # edge ids whose source output is live
        self._node_states: dict[str, dict] = {}  # node_id -> {status, outputs}
        self._loop_bodies: dict[str, set[str]] = {}  # loop node id -> body node ids
        self._body_of: dict[str, str] = {}  # body node id -> owning loop node id

        # ------------------------------------------------------------------
        # Resume mode: rehydrate a suspended run (Wait for Resume node).
        # ``resume_state`` carries the persisted node states + active edges of
        # the first pass, the paused node id and the resume payload that
        # becomes the wait node's output on this second pass.
        # ------------------------------------------------------------------
        self._resuming = False
        self._resume_wait_node: str | None = None
        self._wait_resume_output: Any = None
        if resume_state:
            self._resuming = True
            self._resume_wait_node = resume_state.get("wait_node_id")
            self._wait_resume_output = resume_state.get("wait_output")
            for nid, st in (resume_state.get("node_states") or {}).items():
                self._node_states[nid] = st
            for edge in self.graph.edges:
                st = self._node_states.get(edge.source)
                if st and (st.get("outputs") or {}).get(edge.sourceHandle) is not None:
                    self._active_edges.add(edge.id)
            # Seed the Jinja context so templates can reference pre-wait outputs.
            seeded: dict[str, dict] = {}
            for nid, st in self._node_states.items():
                if st.get("status") == "success":
                    outs = st.get("outputs") or {}
                    seeded[nid] = {
                        "status": "success",
                        "output": outs.get("main") if "main" in outs else outs,
                    }
            self.inherit_node_states = {**seeded, **(inherit_node_states or {})}
            self.node_runs.extend(resume_state.get("prior_node_runs") or [])

    # ------------------------------------------------------------------
    async def run(self) -> dict:
        started = time.monotonic()
        if self.env_vars is None:
            from ..services.env_vars import load_env_map

            self.env_vars = await load_env_map()
        context = ExecutionContext(
            workflow_id=self.workflow_id,
            workflow_name=self.workflow_name,
            execution_id=self.execution_id,
            trigger_type=self.trigger_type,
            trigger_payload=self.trigger_payload,
            depth=self.depth,
            env_vars=self.env_vars or {},
            honor_pinned=self.honor_pinned,
        )
        context.respond_channel = self.respond_channel
        if self.inherit_node_states:
            context.node_states.update(self.inherit_node_states)

        try:
            self._loop_bodies = validate_loops(self.graph)
        except GraphValidationError as exc:
            await self.emit(self._event("execution_started", status="running"))
            self.status = "error"
            self.error = str(exc)
            duration_ms = int((time.monotonic() - started) * 1000)
            await self.emit(
                self._event(
                    "execution_finished",
                    status=self.status,
                    duration_ms=duration_ms,
                    error=self.error,
                    node_runs=self.node_runs,
                )
            )
            return {
                "execution_id": self.execution_id,
                "status": self.status,
                "error": self.error,
                "duration_ms": duration_ms,
                "node_runs": self.node_runs,
                "context": context.snapshot(),
            }
        self._body_of = {
            nid: loop_id for loop_id, body in self._loop_bodies.items() for nid in body
        }
        order = self._topo_order()
        start_node = self._pick_trigger()

        await self.emit(self._event("execution_started", status="running"))

        try:
            for node in order:
                if self._cancel_event is not None and self._cancel_event.is_set():
                    # Stop BEFORE the next node runs; already-completed nodes stay.
                    self.status = "cancelled"
                    self.error = "Cancelled by user"
                    break
                if self._resuming:
                    # Second pass: the paused node completes with the resume
                    # payload; everything already executed stays untouched.
                    if node.id == self._resume_wait_node:
                        context.current_inputs, context.current_input_handles = self._gather_active_inputs(node)
                        context.current_input = next(iter(context.current_inputs.values()), None)
                        await self._record(
                            context, node, "success", {"main": self._wait_resume_output}, 0, None,
                            raw_output=self._wait_resume_output,
                        )
                        continue
                    if node.id in self._node_states:
                        continue
                if node.type.endswith("_trigger") and node.id != start_node.id:
                    await self._record(context, node, "skipped", None, 0, "trigger not fired")
                    continue
                if node.id in self._body_of:
                    # Executed once per batch by the owning Loop node.
                    continue
                inputs, input_handles = self._gather_active_inputs(node)
                has_incoming = bool(self.graph.incoming(node.id))
                if has_incoming and not inputs:
                    await self._record(context, node, "skipped", None, 0, "no active input (upstream skipped or branch inactive)")
                    continue

                context.current_inputs = inputs
                context.current_input_handles = input_handles  # v24: keyed by targetHandle
                context.current_input = next(iter(inputs.values()), None)
                if node.id in self._loop_bodies:
                    await self._run_loop_node(context, node)
                    continue
                if node.disabled:
                    # n8n parity: a disabled node is bypassed - its active input
                    # passes through untouched and downstream keeps flowing.
                    await self._pass_through_disabled(context, node)
                    continue
                if node.pinned_data is not None and self.honor_pinned:
                    # v17 n8n parity: a pinned node returns its pinned output
                    # without executing - mock data for building workflows.
                    # Checked BEFORE the wait-node suspend: pinning a Wait node
                    # replaces the pause with the fake output.
                    await self._run_pinned(context, node)
                    continue
                if getattr(get_node_class(node.type), "pauses_execution", False):
                    return await self._suspend(context, node, started)
                await self._run_node(context, node)
        except CycleError as exc:  # defensive; API validates earlier
            self.status = "error"
            self.error = f"Graph contains a cycle: {exc}"

        duration_ms = int((time.monotonic() - started) * 1000)
        if self.status == "running":
            self.status = "success"

        await self.emit(
            self._event(
                "execution_finished",
                status=self.status,
                duration_ms=duration_ms,
                error=self.error,
                node_runs=self.node_runs,
            )
        )

        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "error": self.error,
            "duration_ms": duration_ms,
            "node_runs": self.node_runs,
            "context": context.snapshot(),
        }

    # ------------------------------------------------------------------
    def _topo_order(self) -> list[NodeSpec]:
        sorter: TopologicalSorter[str] = TopologicalSorter()
        for n in self.graph.nodes:
            deps = {e.source for e in self.graph.incoming(n.id)}
            sorter.add(n.id, *deps)
        try:
            order_ids = list(sorter.static_order())
        except CycleError as exc:
            raise GraphValidationError(f"Workflow graph contains a cycle: {exc}") from exc
        node_map = self.graph.node_map()
        return [node_map[nid] for nid in order_ids]

    def _pick_trigger(self) -> NodeSpec:
        triggers = self.graph.trigger_nodes()
        if not triggers:
            raise GraphValidationError("Workflow has no trigger node (manual/webhook/schedule)")
        enabled = [t for t in triggers if not t.disabled]
        if not enabled:
            raise GraphValidationError("All trigger nodes are disabled - enable one to run")
        triggers = enabled
        if self.trigger_node_id:
            for t in triggers:
                if t.id == self.trigger_node_id:
                    return t
        # prefer a trigger matching the run trigger_type, else first
        for t in triggers:
            expected = {
            "manual": "manual_trigger",
            "webhook": "webhook_trigger",
            "schedule": "schedule_trigger",
            "error": "error_trigger",  # v22: error-handler workflows start from the Error Trigger
            "chat": "chat_trigger",    # v25: chat workflows start from the Chat Trigger
        }.get(self.trigger_type)
            if expected and t.type == expected:
                return t
        return triggers[0]

    # ------------------------------------------------------------------
    # Loop Over Items orchestration
    # ------------------------------------------------------------------
    async def _run_loop_node(self, context: ExecutionContext, node: NodeSpec) -> None:
        """Slice items into batches, then run the loop body once per batch.

        Each batch executes through a nested GraphRunner over the body subgraph,
        seeded with the parent context so body nodes can reference pre-loop
        outputs. Body node runs are appended to this run's log tagged with
        ``batch_index`` and broadcast live (canvas rings update per batch).
        """
        node_map = self.graph.node_map()
        cls = get_node_class(node.type)
        instance = cls(node)
        body_ids = self._loop_bodies.get(node.id, set())

        await self.emit(
            self._event("node_started", node_id=node.id, node_type=node.type, node_name=node.display_name, status="running")
        )
        t0 = time.monotonic()

        try:
            batches, batch_size = instance.prepare(context)
        except Exception as exc:  # noqa: BLE001
            duration_ms = int((time.monotonic() - t0) * 1000)
            await self._record(context, node, "error", None, duration_ms, f"Loop failed: {exc}")
            for nid in sorted(body_ids):
                await self._record(context, node_map[nid], "skipped", None, 0, "loop failed before running the body")
            return

        if not body_ids:
            duration_ms = int((time.monotonic() - t0) * 1000)
            await self._record(
                context, node, "error", None, duration_ms,
                "Loop node has no body - connect at least one node to its loop output",
            )
            return

        body_graph = self._build_body_graph(node, body_ids, node_map)
        total_batches = len(batches)
        results: list[Any] = []
        failure: str | None = None

        for i, batch in enumerate(batches):
            seed = dict(context.node_states)
            seed[node.id] = {
                "status": "success",
                "output": {"items": batch, "batch": {"index": i, "total": total_batches}},
            }
            nested = GraphRunner(
                body_graph,
                workflow_id=self.workflow_id,
                workflow_name=self.workflow_name,
                trigger_type="manual",
                trigger_payload={"items": batch, "batch": {"index": i, "total": total_batches}},
                trigger_node_id="_batch_src",
                execution_id=self.execution_id,
                emit=self._loop_emitter(i),
                max_output_capture=self.max_output_capture,
                depth=self.depth + 1,
                inherit_node_states=seed,
                env_vars=self.env_vars,
                honor_pinned=self.honor_pinned,
            )
            sub = await nested.run()
            self.node_runs.extend({**run, "batch_index": i} for run in sub["node_runs"])
            # Batch result = last successful run of THIS loop's body nodes.
            # Nested loop-node records land after their own body's records and
            # a valid body always terminates in a non-loop node, so loop-type
            # records are excluded from the pick.
            body_runs = [
                r for r in sub["node_runs"]
                if r["node_id"] in body_ids and r["status"] == "success"
                and not getattr(get_node_class(r["node_type"]), "is_loop_node", False)
            ]
            last_ok = body_runs[-1] if body_runs else None
            results.append(last_ok["output"] if last_ok else None)
            if sub["status"] != "success":
                failure = f"batch {i + 1}/{total_batches} failed: {sub.get('error') or 'unknown error'}"
                break

        duration_ms = int((time.monotonic() - t0) * 1000)
        if failure:
            await self._record(context, node, "error", None, duration_ms, failure)
            return

        done_payload = {
            "batches": total_batches,
            "batch_size": batch_size,
            "total_items": sum(len(b) for b in batches),
            "items": results,  # alias so aggregate/filter can consume done output directly
            "results": results,
        }
        await self._record(
            context, node, "success", {"loop": None, "done": done_payload}, duration_ms, None,
            raw_output=done_payload,
        )

    def _loop_emitter(self, batch_index: int) -> Emitter:
        """Forward nested body events to the parent stream, tagged with the batch."""
        parent_emit = self.emit

        async def wrapped(event: dict) -> None:
            # Lifecycle frames belong to the parent run only.
            if event.get("event") in ("execution_started", "execution_finished"):
                return
            await parent_emit({**event, "batch": batch_index})

        return wrapped

    def _build_body_graph(
        self, loop_node: NodeSpec, body_ids: set[str], node_map: dict[str, NodeSpec]
    ) -> GraphSpec:
        """Body subgraph: internal edges stay; edges from the Loop node's loop
        handle are re-sourced from a hidden ``_batch_trigger`` virtual node."""
        nodes: list[NodeSpec] = [NodeSpec(id="_batch_src", type="_batch_trigger", name="Batch")]
        nodes.extend(node_map[nid].model_copy(deep=True) for nid in body_ids)

        edges: list[EdgeSpec] = []
        for e in self.graph.edges:
            if e.source in body_ids and e.target in body_ids:
                edges.append(e.model_copy(deep=True))
            elif e.source == loop_node.id and e.sourceHandle == "loop" and e.target in body_ids:
                edges.append(
                    EdgeSpec(
                        id=f"loopsrc_{e.id}",
                        source="_batch_src",
                        target=e.target,
                        sourceHandle="main",
                        targetHandle=e.targetHandle,
                    )
                )
        return GraphSpec(nodes=nodes, edges=edges)

    def _gather_active_inputs(self, node: NodeSpec) -> tuple[dict[str, Any], dict[str, Any]]:
        """Active incoming payloads, keyed two ways:

        * by **source node id** - the historical ``current_inputs`` contract
        * by **targetHandle** - v24, so multi-input nodes (Compare Datasets'
          "main"/"secondary") can tell their inputs apart; the last edge
          connected to a handle wins, matching visual wiring order.
        """
        inputs: dict[str, Any] = {}
        handles: dict[str, Any] = {}
        for edge in self.graph.incoming(node.id):
            if edge.id not in self._active_edges:
                continue
            state = self._node_states.get(edge.source)
            if not state or state["status"] not in ("success", "error", "skipped"):
                # "error" passes through only for continued-on-fail nodes,
                # which carry an {"error": ...} payload in their outputs;
                # "skipped" passes through only for disabled nodes (which
                # carry their input as the main payload).
                continue
            payload = (state.get("outputs") or {}).get(edge.sourceHandle)
            if payload is None:
                continue
            inputs[edge.source] = payload
            handles[edge.targetHandle] = payload
        return inputs, handles

    async def _pass_through_disabled(self, context: ExecutionContext, node: NodeSpec) -> None:
        """Disabled node: record as skipped, pass the active input through so
        downstream nodes (and ``{{ nodes.<id>.output.* }}`` templates) see the
        same data they would have seen if the node had been removed."""
        payload = next(iter(context.current_inputs.values()), None) if context.current_inputs else None
        outputs = {"main": payload} if payload is not None else {}
        context.register(node.id, "skipped", payload)
        self._node_states[node.id] = {"status": "skipped", "outputs": outputs}
        if payload is not None:
            for edge in self.graph.edges:
                if edge.source == node.id and edge.sourceHandle == "main":
                    self._active_edges.add(edge.id)
        display = self._capture(payload)
        self.node_runs.append(
            {
                "node_id": node.id,
                "node_type": node.type,
                "node_name": node.display_name,
                "status": "skipped",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": 0,
                "output": display,
                "error": "disabled (input passed through)",
            }
        )
        await self.emit(
            self._event(
                "node_finished",
                node_id=node.id,
                node_type=node.type,
                node_name=node.display_name,
                status="skipped",
                duration_ms=0,
                output=display,
                error="disabled",
            )
        )

    async def _run_pinned(self, context: ExecutionContext, node: NodeSpec) -> None:
        """Pinned node (v17): emit the pinned data as its output, unexecuted."""
        pinned = node.pinned_data
        await self.emit(
            self._event("node_started", node_id=node.id, node_type=node.type, node_name=node.display_name, status="running")
        )
        await self._record(
            context, node, "success", {"main": pinned}, 0, None,
            raw_output=pinned, pinned=True,
        )

    async def _run_node(self, context: ExecutionContext, node: NodeSpec) -> None:
        cls = get_node_class(node.type)
        if cls is None:
            await self._record(context, node, "error", None, 0, f"Unknown node type {node.type!r}")
            return

        cfg = node.settings
        attempts = 1 + (cfg.max_retries if cfg.retry_on_fail else 0)
        instance = cls(node)

        await self.emit(self._event("node_started", node_id=node.id, node_type=node.type, node_name=node.display_name, status="running"))

        t0 = time.monotonic()
        last_error: str | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(cfg.retry_wait_ms / 1000)
            try:
                result = await instance.run(context)
                duration_ms = int((time.monotonic() - t0) * 1000)
                await self._record(
                    context, node, "success", result.outputs, duration_ms, None,
                    raw_output=result.raw_output, attempt=attempt + 1,
                )
                return
            except TemplateResolutionError as exc:
                last_error = str(exc)
            except Exception as exc:  # noqa: BLE001
                last_error = f"{exc}"

        duration_ms = int((time.monotonic() - t0) * 1000)
        if cfg.continue_on_fail:
            # n8n parity: surface the error as data and keep the flow alive.
            payload = {"error": last_error, "failed_node": node.display_name}
            await self._record(
                context, node, "error", {"main": payload}, duration_ms, last_error,
                raw_output=payload, continued=True, attempt=attempts,
            )
            return
        await self._record(context, node, "error", None, duration_ms, last_error, attempt=attempts)

    async def _suspend(self, context: ExecutionContext, node: NodeSpec, started: float) -> dict:
        """Pause the run at a Wait for Resume node and persist everything needed
        to continue it later (see ``resume_state`` in the executor + API)."""
        cls = get_node_class(node.type)
        instance = cls(node)
        hint = "POST to the resume URL to continue this workflow"
        pass_through = False
        try:
            params = instance.validate_parameters(context)
            hint = getattr(params, "resume_hint", hint) or hint
            pass_through = bool(getattr(params, "pass_through", False))
        except Exception:  # noqa: BLE001 - never fail the pause on bad params
            pass

        await self.emit(
            self._event("node_started", node_id=node.id, node_type=node.type, node_name=node.display_name, status="running")
        )

        token = uuid.uuid4().hex
        upstream = context.current_input if pass_through else None
        wait_output = WaitForResumeNode.waiting_output(self.execution_id, token, hint, upstream)

        context.register(node.id, "waiting", wait_output)
        self._node_states[node.id] = {"status": "waiting", "outputs": {}}
        self.node_runs.append(
            {
                "node_id": node.id,
                "node_type": node.type,
                "node_name": node.display_name,
                "status": "waiting",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": 0,
                "output": self._capture(wait_output),
                "error": None,
            }
        )
        await self.emit(
            self._event(
                "node_finished",
                node_id=node.id,
                node_type=node.type,
                node_name=node.display_name,
                status="waiting",
                duration_ms=0,
                output=wait_output,
                error=None,
            )
        )

        self.status = "waiting"
        duration_ms = int((time.monotonic() - started) * 1000)
        await self.emit(
            self._event(
                "execution_finished",
                status="waiting",
                duration_ms=duration_ms,
                error=None,
                node_runs=self.node_runs,
            )
        )
        return {
            "execution_id": self.execution_id,
            "status": "waiting",
            "error": None,
            "duration_ms": duration_ms,
            "node_runs": self.node_runs,
            "context": context.snapshot(),
            "resume": {
                "token": token,
                "node_id": node.id,
                "resume_path": f"/api/v1/executions/{self.execution_id}/resume",
            },
            "resume_state": {
                "node_states": dict(self._node_states),
                "active_edges": sorted(self._active_edges),
            },
        }

    async def _record(
        self,
        context: ExecutionContext,
        node: NodeSpec,
        status: str,
        outputs: dict | None,
        duration_ms: int,
        error: str | None,
        raw_output: Any = None,
        continued: bool = False,
        attempt: int = 1,
        pinned: bool = False,
    ) -> None:
        """Persist node run, update context state, activate edges, emit event.

        Output contract (exposed to Jinja as ``nodes.<id>.output``):
        * ``main_output`` = payload of the "main" handle (fallback: full dict
          for branch nodes like IF, so ``nodes.if1.output.true.condition``
          works).
        * ``display``     = compact value persisted in logs/events.
        """
        if status == "success" or continued:
            outputs = outputs or {}
            main_payload = outputs.get("main") if "main" in outputs else outputs
            display = self._capture(raw_output if raw_output is not None else main_payload)
            context.register(node.id, "success" if not continued else "error", main_payload)
            # continued-on-fail nodes keep their edges live so the flow can branch
            # on {{ nodes.x.output.error }} downstream.
            self._node_states[node.id] = {"status": "error" if continued else "success", "outputs": outputs}
            # activate outgoing edges whose handle carries data
            for edge in self.graph.edges:
                if edge.source == node.id and outputs.get(edge.sourceHandle) is not None:
                    self._active_edges.add(edge.id)
        else:
            main_payload = None
            display = None
            context.register(node.id, status, {"error": error} if error else None)
            self._node_states[node.id] = {"status": status, "outputs": {}}
            if status == "error":
                self.status = "error"
                self.error = self.error or f"Node {node.display_name!r} failed: {error}"

        run_record = {
            "node_id": node.id,
            "node_type": node.type,
            "node_name": node.display_name,
            "status": status,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": duration_ms,
            "output": display,
            "error": error,
        }
        # Debugging aid: what this node actually received on its active inputs.
        # Single input -> the payload itself; multiple -> {source_id: payload}.
        if context.current_inputs and len(context.current_inputs) == 1:
            run_record["input"] = self._capture(next(iter(context.current_inputs.values())))
        elif context.current_inputs:
            run_record["input"] = self._capture(context.current_inputs)
        if continued:
            run_record["continued_on_fail"] = True
        if attempt > 1:
            run_record["attempts"] = attempt
        if pinned:
            run_record["pinned"] = True  # v17: output came from pinned data
        self.node_runs.append(run_record)

        await self.emit(
            self._event(
                "node_finished",
                node_id=node.id,
                node_type=node.type,
                node_name=node.display_name,
                status=status,
                duration_ms=duration_ms,
                output=display,
                error=error,
            )
        )

    def _capture(self, value: Any) -> Any:
        """Truncate large outputs before persisting/broadcasting."""
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)[: self.max_output_capture]
        if len(text) <= self.max_output_capture:
            return value
        return {"_truncated": True, "preview": text[: self.max_output_capture]}

    def _event(self, event: str, **extra: Any) -> dict:
        return {
            "event": event,
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            **extra,
        }


def validate_loops(spec: GraphSpec) -> dict[str, set[str]]:
    """Structural validation for Loop Over Items nodes.

    Returns ``{loop_node_id: body_node_ids}``. Raises GraphValidationError on:
    * a body node fed from outside the loop body (would run with stale/missing data)
    * a node connected to both the loop and done outputs of the same Loop node
    * two *sibling* Loop nodes sharing body nodes (nested loops are supported)
    """
    loop_nodes = [n for n in spec.nodes if getattr(get_node_class(n.type), "is_loop_node", False)]
    bodies: dict[str, set[str]] = {}
    for ln in loop_nodes:
        body: set[str] = set()
        stack = [e.target for e in spec.edges if e.source == ln.id and e.sourceHandle == "loop"]
        while stack:
            nid = stack.pop()
            if nid in body or nid == ln.id:
                continue
            body.add(nid)
            stack.extend(e.target for e in spec.edges if e.source == nid)

        for e in spec.edges:
            if e.source == ln.id and e.sourceHandle == "done" and e.target in body:
                raise GraphValidationError(
                    f"Node {e.target!r} is connected to both the loop and done outputs of {ln.id!r} - "
                    "post-loop nodes must hang off done only"
                )
        for nid in body:
            node_cls = get_node_class(spec.node_map()[nid].type)
            if getattr(node_cls, "pauses_execution", False):
                raise GraphValidationError(
                    f"Wait node {nid!r} cannot live inside a loop body - keep it on the main flow"
                )
            for e in spec.edges:
                if e.target == nid and e.source != ln.id and e.source not in body:
                    raise GraphValidationError(
                        f"Loop body node {nid!r} depends on {e.source!r}, which sits outside the loop body. "
                        "Everything the body needs must run before the Loop node (reference it via "
                        "{{ nodes.… }}) or live inside the loop body"
                    )
        bodies[ln.id] = body

    ids = list(bodies)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            if a in bodies[b] or b in bodies[a]:
                continue  # nested loops are supported
            overlap = bodies[a] & bodies[b]
            if overlap:
                raise GraphValidationError(
                    f"Node(s) {sorted(overlap)} sit downstream of two different Loop nodes "
                    f"({a!r}, {b!r}) - restructure so each body belongs to one loop"
                )
    return bodies


def validate_graph_document(graph: dict) -> GraphSpec:
    """Validate a raw graph dict for API saves: schema, node types, cycles."""
    spec = GraphSpec.model_validate(graph)
    for node in spec.nodes:
        if get_node_class(node.type) is None:
            raise GraphValidationError(f"Unknown node type {node.type!r} (node {node.id!r})")
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for n in spec.nodes:
        sorter.add(n.id, *[e.source for e in spec.incoming(n.id)])
    try:
        sorter.prepare()
    except CycleError as exc:
        raise GraphValidationError(f"Workflow graph contains a cycle: {exc}") from exc
    validate_loops(spec)
    return spec
