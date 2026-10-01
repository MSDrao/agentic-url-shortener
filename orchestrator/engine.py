"""Stateful, governed execution engine for the SDLC dependency graph.

Execution model
  * Nodes become READY when every dependency has SUCCEEDED and entry gates pass.
  * Ready nodes run concurrently in a bounded worker pool (parallel branches);
    a node with several deps is a synchronization point (join).
  * Agents run in worker threads and only *propose* results. The engine (single
    writer, main thread) evaluates exit gates, policy, and approvals, then commits
    files to the workspace and versioned artifacts to the context.
  * Failures: bounded retries with backoff -> fallback agent -> node's on_failure
    policy ("rework:<upstream>" rolls the workspace back and re-runs upstream with
    feedback; "stop" performs a safe-stop).
  * Re-planning: (1) when a re-executed node produces an artifact whose hash
    changed, already-completed consumers are invalidated and re-run; (2) agents may
    request policy-allowlisted nodes to be inserted into the graph at runtime.
  * Every transition is audited (hash chain) and persisted (resumable state).
"""

from __future__ import annotations

import json
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from . import gates as gates_mod
from .agents.base import Agent, AgentTask, InfrastructureError
from .approval import ApprovalRequest, Approver
from .audit import AuditLog
from .context import RunContext
from .graph import GraphError, WorkflowGraph
from .llm import LLMProvider, RecordingLLM
from .metrics import Metrics
from .model import TERMINAL, AgentResult, NodeSpec, NodeStatus, RunStatus, content_hash
from .policy import Policy
from .workspace import Workspace


class Engine:
    def __init__(
        self,
        *,
        run_id: str,
        run_dir: Path,
        repo_root: Path,
        scenario: dict[str, Any],
        graph: WorkflowGraph,
        ctx: RunContext,
        agents: dict[str, Agent],
        llm: LLMProvider,
        policy: Policy,
        approver: Approver,
        workspace: Workspace,
        node_templates: dict[str, Any],
        max_parallel: int = 4,
        log=print,
    ):
        self.run_id = run_id
        self.run_dir = run_dir
        self.repo_root = repo_root
        self.scenario = scenario
        self.graph = graph
        self.ctx = ctx
        self.agents = agents
        self.llm = llm
        self.policy = policy
        self.approver = approver
        self.workspace = workspace
        self.node_templates = node_templates
        self.max_parallel = max_parallel
        self.log = log

        self.audit = AuditLog(run_dir / "audit.jsonl", run_id)
        self.metrics = Metrics()
        self.run_status = RunStatus.RUNNING
        self.stop_reason: str | None = None
        self.status: dict[str, NodeStatus] = {k: NodeStatus.PENDING for k in graph.nodes}
        self.attempts: dict[str, int] = {k: 0 for k in graph.nodes}
        self.executions: dict[str, int] = {k: 0 for k in graph.nodes}
        self.epoch: dict[str, int] = {k: 0 for k in graph.nodes}
        self.use_fallback: dict[str, bool] = {k: False for k in graph.nodes}
        self.not_before: dict[str, float] = {}
        self.approval_rounds: dict[str, int] = {}
        self.rework_cycles: dict[str, int] = {}
        self.revisions = 0
        self.gate_log: dict[str, list[dict[str, Any]]] = {}
        self.mutations: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []
        # Human-gate state: proposals awaiting a decision are persisted verbatim so that
        # what the human approved is exactly what gets committed (the agent is not re-run).
        self.paused = False
        self._llm_calls: dict[str, list[dict[str, Any]]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.release_signoff: dict[str, Any] | None = None
        if getattr(approver, "human", True) is False and not self.policy.section("approvals").get(
                "allow_simulated_approvals", False):
            raise ValueError("policy forbids simulated approvals (approvals.allow_simulated_approvals: false)")

    # ------------------------------------------------------------------ run
    def run(self) -> RunStatus:
        self.audit.record(
            "run_started",
            scenario=self.scenario["id"],
            llm=self.llm.name,
            nodes=list(self.graph.nodes),
            waves=self.graph.parallel_levels(),
        )
        self._persist()
        self._session_start = time.time()
        self._session_wait = 0.0
        self._drain_pending()  # on resume: apply decisions recorded while the run was paused
        budgets = self.policy.budgets
        running: dict[Future, tuple[str, int, float]] = {}
        with ThreadPoolExecutor(max_workers=self.max_parallel, thread_name_prefix="agent") as pool:
            while self.run_status == RunStatus.RUNNING:
                if (self.run_dir / "STOP").exists():
                    self._safe_stop("kill switch: STOP file present", running)
                    break
                if self.metrics.attempts >= int(budgets.get("max_total_attempts", 1000)):
                    self._safe_stop("budget exceeded: max_total_attempts", running)
                    break
                if self._active_seconds() > float(budgets.get("max_wall_seconds", 3600)):
                    self._safe_stop("budget exceeded: max_wall_seconds", running)
                    break

                self._propagate_blocked()
                busy = {n for n, _, _ in running.values()}
                for node_id in ([] if self.paused else self._ready(busy)):  # paused: finish in-flight work only
                    if len(running) >= self.max_parallel:
                        break
                    fut = self._dispatch(pool, node_id)
                    if fut is not None:
                        running[fut] = (node_id, self.epoch[node_id], time.time())
                if self.run_status != RunStatus.RUNNING:
                    self._discard(running)
                    break

                if not running:
                    if self.paused:
                        break
                    if any(self.not_before.get(k, 0) > time.time() for k, s in self.status.items() if s == NodeStatus.PENDING):
                        time.sleep(0.05)
                        continue
                    break  # nothing running and nothing ready -> done (or stuck)

                done, _ = wait(list(running), timeout=0.2, return_when=FIRST_COMPLETED)
                for fut in done:
                    node_id, epoch, started = running.pop(fut)
                    self._on_complete(node_id, epoch, started, fut)
                    if self.run_status != RunStatus.RUNNING:
                        self._discard(running)
                        break
        return self._finalize()

    # ------------------------------------------------------------- scheduling
    def _ready(self, busy: set[str]) -> list[str]:
        now = time.time()
        out = []
        for k in self.graph.topological_order():
            if k in busy or self.status[k] != NodeStatus.PENDING:
                continue
            if self.not_before.get(k, 0) > now:
                continue
            if all(self.status[d] == NodeStatus.SUCCEEDED for d in self.graph.nodes[k].deps):
                out.append(k)
        return out

    def _propagate_blocked(self) -> None:
        for k in self.graph.topological_order():
            if self.status[k] != NodeStatus.PENDING:
                continue
            bad = [d for d in self.graph.nodes[k].deps if self.status[d] in (NodeStatus.FAILED, NodeStatus.BLOCKED)]
            if bad:
                self._set(k, NodeStatus.BLOCKED, reason=f"upstream failed: {bad}")

    def _dispatch(self, pool: ThreadPoolExecutor, node_id: str) -> Future | None:
        node = self.graph.nodes[node_id]
        gi = gates_mod.GateInput(node, self.ctx, self.policy, self.workspace)
        entry = gates_mod.evaluate(["inputs_available", *node.entry_gates], gi)
        self._record_gates(node_id, "entry", entry)
        if not all(g.passed for g in entry):
            self._set(node_id, NodeStatus.BLOCKED, reason="entry gate failed")
            self._safe_stop(f"entry gate failed for '{node_id}'", {})
            return None

        self.attempts[node_id] += 1
        self.executions[node_id] += 1
        self.metrics.attempts += 1
        agent_name = node.fallback_agent if self.use_fallback[node_id] and node.fallback_agent else node.agent
        agent = self.agents[agent_name]
        inputs = {c.lstrip("?"): self.ctx.get(c.lstrip("?")) for c in node.consumes}
        sink: list[dict[str, Any]] = []
        self._llm_calls[node_id] = sink
        task = AgentTask(
            node=node,
            attempt=self.attempts[node_id],
            execution=self.executions[node_id],
            inputs=inputs,
            feedback=list(self.ctx.feedback.get(node_id, [])),
            scenario=self.scenario,
            workspace=self.workspace,
            llm=RecordingLLM(getattr(agent, "llm", None) or self.llm, sink),
            policy=self.policy,
            run_dir=self.run_dir,
            repo_root=self.repo_root,
            live_run=self.llm.name != "offline",
        )
        self._set(node_id, NodeStatus.RUNNING, agent=agent_name, attempt=self.attempts[node_id])
        return pool.submit(agent.run, task)

    # ------------------------------------------------------------ completion
    def _on_complete(self, node_id: str, epoch: int, started: float, fut: Future) -> None:
        node = self.graph.nodes[node_id]
        elapsed = time.time() - started
        self.metrics.add_latency(node.stage, elapsed)
        self.timeline.append({"node": node_id, "start": started - self.metrics.started_at, "end": time.time() - self.metrics.started_at, "attempt": self.attempts[node_id]})
        calls = self._llm_calls.pop(node_id, [])
        for call in calls:
            self.audit.record("reasoning_call", node=node_id, **call)
        if epoch != self.epoch[node_id]:
            self.audit.record("stale_result_discarded", node=node_id, reason="node was invalidated while running")
            return
        try:
            result: AgentResult = fut.result()
            result.provenance = calls
        except InfrastructureError as exc:  # environment problem: do not rework correct code
            self.metrics.attempt_failures += 1
            self.metrics.open_incident(node_id)
            self._set(node_id, NodeStatus.FAILED, reason=f"infrastructure: {exc}"[:300])
            self._safe_stop(f"infrastructure failure in '{node_id}': {str(exc)[:200]}", {})
            return
        except Exception as exc:  # agent crash = failed attempt
            self._attempt_failed(node_id, f"{type(exc).__name__}: {exc}")
            return

        # 1) exit gates
        gi = gates_mod.GateInput(node, self.ctx, self.policy, self.workspace, result)
        exit_results = gates_mod.evaluate(node.exit_gates, gi)
        self._record_gates(node_id, "exit", exit_results)
        failed = [g for g in exit_results if not g.passed]
        if failed:
            self._attempt_failed(node_id, "exit gate failed: " + "; ".join(f"{g.gate} ({g.detail})" for g in failed), result)
            return

        # 2) policy guardrails on proposed file changes (autonomy, security, compliance)
        agent_name = node.fallback_agent if self.use_fallback[node_id] and node.fallback_agent else node.agent
        findings = self.policy.check_changes(node, agent_name, result.file_changes)
        blocking = [f for f in findings if self.policy.is_blocking(f.severity)]
        if findings:
            self.audit.record("policy_findings", node=node_id, findings=[f.to_dict() for f in findings])
        if blocking:
            self.metrics.policy_violations += len(blocking)
            if any(f.severity == "critical" for f in blocking):
                self.metrics.attempt_failures += 1
                self.metrics.open_incident(node_id)  # closes when the node later succeeds (e.g. after resume)
                self._set(node_id, NodeStatus.FAILED, reason="critical policy violation")
                self.ctx.decide(node_id, "safe_stop", "critical policy violation - changes not committed", "policy-engine",
                                rationale="; ".join(f"{f.rule}: {f.path} {f.detail}" for f in blocking))
                self._safe_stop(f"critical policy violation in '{node_id}'", {})
                return
            self._attempt_failed(node_id, "policy violation: " + "; ".join(f"{f.rule} {f.path}:{f.line} {f.detail}" for f in blocking))
            return

        # 3) human approval for high-impact actions
        reasons = self.policy.approval_reasons(node, result)
        if reasons and not self._approve(node_id, reasons, result):
            return

        # 4) commit
        self._commit(node_id, result)

    def _approve(self, node_id: str, reasons: list[str], result: AgentResult, rnd: int | None = None) -> bool:
        """Return True to commit. False means: rejected, sent back for revision, or paused pending a human."""
        if rnd is None:
            self._set(node_id, NodeStatus.WAITING_APPROVAL, reasons=reasons)
            rnd = self.approval_rounds.get(node_id, 0) + 1
            self.approval_rounds[node_id] = rnd
            self.metrics.approvals_requested += 1
        evidence: dict[str, Any] = {k: v for k, v in result.artifacts.items()}
        if result.file_changes:
            evidence["files"] = sorted(result.file_changes)
            evidence["diff_sha256"] = content_hash(result.file_changes)
        req = ApprovalRequest(node_id, reasons, result.summary, evidence, rnd)
        waited = time.time()
        try:
            decision = self.approver.request(req)
        except ValueError as exc:  # e.g. a decision recorded for a different proposal
            self._safe_stop(f"approval for '{node_id}' could not be applied: {exc}", {})
            return False
        finally:
            waited_s = time.time() - waited  # time a human spent deciding is not execution time
            self._session_wait += waited_s
            self.metrics.approval_wait_s += waited_s
        if decision is None:  # no human decision yet: pause the run at this gate
            self.pending[node_id] = {"reasons": reasons, "round": rnd, "result": _result_to_dict(result),
                                     "request_hash": req.request_hash}
            self.paused = True
            self.audit.record("approval_pending", node=node_id, round=rnd, request_hash=req.request_hash, reasons=reasons)
            self.log(f"  ⏸ '{node_id}' awaits a human decision (request {req.request_hash}); the run will pause")
            self._persist()
            return False
        if decision.request_hash and decision.request_hash != req.request_hash:
            self._safe_stop(f"approval for '{node_id}' refers to a different proposal", {})
            return False
        if decision.human:
            self.metrics.approvals_human += 1
        else:
            self.metrics.approvals_simulated += 1
        self.audit.record("approval_decision", node=node_id, actor=decision.approver, decision=decision.decision,
                          human=decision.human, mode=getattr(self.approver, "mode", "?"), request_hash=req.request_hash,
                          comment=decision.comment, reasons=reasons, round=rnd, revise_target=decision.revise_target,
                          answers=decision.answers, wait_s=round(time.time() - waited, 3))
        self.ctx.decide(node_id, "approval", f"{decision.decision}: {', '.join(reasons)}", decision.approver,
                        rationale=decision.comment, refs=[f"{k}@pending" for k in result.artifacts])
        if node_id in self.policy.section("approvals").get("always", []) and decision.decision == "approve":
            self.release_signoff = {"node": node_id, "approver": decision.approver, "human": decision.human,
                                    "request_hash": req.request_hash}

        if decision.decision == "approve":
            return True
        if decision.decision == "reject":
            self.metrics.approvals_rejected += 1
            if node_id in self.policy.section("approvals").get("rollback_on_reject", []):
                self._set(node_id, NodeStatus.FAILED, reason="rejected by approver")
                self._rollback_to_baseline(f"'{node_id}' rejected by {decision.approver}: {decision.comment}")
            else:
                self._set(node_id, NodeStatus.FAILED, reason="rejected by approver")
                self._safe_stop(f"'{node_id}' rejected by {decision.approver}", {})
            return False

        # revise: route feedback/answers upstream and re-plan
        self.revisions += 1
        limit = int(self.policy.section("approvals").get("max_revision_rounds", 3))
        if self.revisions > limit:
            self._set(node_id, NodeStatus.FAILED, reason="revision limit reached")
            self._safe_stop("approval revision limit reached", {})
            return False
        target = decision.revise_target or node_id
        if target not in self.graph.nodes or (target != node_id and node_id not in self.graph.descendants(target)):
            target = node_id
        if decision.comment:
            self.ctx.feedback.setdefault(target, []).append(f"[reviewer:{decision.approver}] {decision.comment}")
        if decision.answers:
            prev = self.ctx.get("clarifications") or {}
            merged = {**prev, **decision.answers}
            producer = f"human:{decision.approver}" if decision.human else decision.approver
            self.ctx.put("clarifications", merged, producer=producer, attempt=rnd, derived_from=[])
            self.ctx.decide(node_id, "assumption", f"human clarified {sorted(decision.answers)}", decision.approver,
                            rationale=decision.comment, refs=[self.ctx.ref("clarifications") or ""])
        self.metrics.replans += 1
        self.audit.record("replan", node=node_id, trigger="approval_revise", target=target)
        self.log(f"  ↩ {decision.approver} requested revision at '{node_id}' -> re-running '{target}'"
                 + (f" with answers {decision.answers}" if decision.answers else ""))
        # Re-run the target only; downstream nodes are invalidated later *only if* its output changes.
        self._reset([target], reason=f"revision requested at '{node_id}'")
        if target != node_id:
            self._reset([node_id], reason="awaiting re-run of upstream")
        return False

    def _drain_pending(self) -> None:
        for node_id, p in list(self.pending.items()):
            del self.pending[node_id]
            result = _result_from_dict(p["result"])
            if self._approve(node_id, p["reasons"], result, rnd=p["round"]):
                self._commit(node_id, result)
        self.paused = bool(self.pending)

    def _commit(self, node_id: str, result: AgentResult) -> None:
        node = self.graph.nodes[node_id]
        agent_name = node.fallback_agent if self.use_fallback[node_id] and node.fallback_agent else node.agent
        if result.file_changes:
            self.workspace.snapshot(f"pre-{node_id}")
            self.workspace.apply(result.file_changes)
            self.audit.record("files_committed", node=node_id, files=sorted(result.file_changes))
        changed: list[str] = []
        derived = [r for r in (self.ctx.ref(c.lstrip("?")) for c in node.consumes) if r]
        for name, content in result.artifacts.items():
            prev = self.ctx.latest(name)
            prov = result.provenance or [{"content_origin": "computed", "agent": agent_name}]
            av = self.ctx.put(name, content, producer=node_id, attempt=self.attempts[node_id], derived_from=derived,
                              provenance=prov)
            self.audit.record("artifact_committed", node=node_id, artifact=f"{name}@v{av.version}", hash=av.hash, derived_from=derived)
            if prev is not None and prev.hash != av.hash:
                changed.append(name)
        for d in result.decisions:
            self.ctx.decide(node_id, d.get("kind", "design_choice"), d["summary"], agent_name,
                            rationale=d.get("rationale", ""), refs=[self.ctx.ref(n) or n for n in result.artifacts])
        self.metrics.attempt_successes += 1
        self.metrics.close_incident(node_id)
        self.ctx.feedback.pop(node_id, None)
        self._set(node_id, NodeStatus.SUCCEEDED, summary=result.summary)

        # Re-plan (1): upstream output changed -> invalidate completed consumers.
        for name in changed:
            consumers = [n.id for n in self.graph.nodes.values()
                         if name in [c.lstrip("?") for c in n.consumes] and self.status[n.id] == NodeStatus.SUCCEEDED]
            if consumers:
                self.metrics.replans += 1
                self.audit.record("replan", node=node_id, trigger="artifact_changed", artifact=name, invalidated=consumers)
                self.ctx.decide(node_id, "replan", f"'{name}' changed; invalidating {consumers}", "orchestrator",
                                rationale="downstream work was derived from a superseded artifact version")
                self.log(f"  ⟳ re-plan: '{name}' changed (v{self.ctx.latest(name).version}); invalidated {consumers} and their dependents")
                self._reset(consumers, reason=f"upstream artifact '{name}' changed", cascade=True)

        # Re-plan (2): graph mutations requested by the agent (policy-allowlisted).
        for pc in result.plan_changes:
            self._apply_plan_change(node_id, pc)
        self._persist()

    # -------------------------------------------------------------- failure
    def _attempt_failed(self, node_id: str, error: str, result: AgentResult | None = None) -> None:
        node = self.graph.nodes[node_id]
        self.metrics.attempt_failures += 1
        self.metrics.open_incident(node_id)
        self.ctx.feedback.setdefault(node_id, []).append(error)
        self.audit.record("attempt_failed", node=node_id, attempt=self.attempts[node_id], error=error[:2000])
        self.log(f"  ! {node_id} attempt {self.attempts[node_id]} failed: {error[:200]}")

        if self.attempts[node_id] < node.retry.max_attempts:
            self.metrics.retries += 1
            delay = node.retry.delay(self.attempts[node_id])
            self.not_before[node_id] = time.time() + delay
            self._set(node_id, NodeStatus.PENDING, reason=f"retry in {delay:.2f}s")
            return
        if node.fallback_agent and not self.use_fallback[node_id]:
            self.use_fallback[node_id] = True
            self.attempts[node_id] = node.retry.max_attempts - 1  # fallback gets one attempt
            self.metrics.fallbacks += 1
            self.ctx.decide(node_id, "fallback", f"switching to fallback agent '{node.fallback_agent}'", "orchestrator", rationale=error[:500])
            self.audit.record("fallback", node=node_id, to=node.fallback_agent)
            self._set(node_id, NodeStatus.PENDING, reason="fallback")
            return
        if node.on_failure.startswith("rework:"):
            target = node.on_failure.split(":", 1)[1]
            cycles = self.rework_cycles.get(target, 0)
            if cycles < int(self.policy.budgets.get("max_rework_cycles", 2)):
                self.rework_cycles[target] = cycles + 1
                self._rework(target, node_id, error)
                return
            error = f"rework budget exhausted for '{target}': {error}"
        self._set(node_id, NodeStatus.FAILED, reason=error[:300])
        self._safe_stop(f"'{node_id}' failed after retries/fallback", {})

    def _rework(self, target: str, failed_node: str, error: str) -> None:
        self.metrics.reworks += 1
        self.ctx.feedback.setdefault(target, []).append(f"[{failed_node}] {error}")
        if self.workspace.has_snapshot(f"pre-{target}"):
            self.workspace.restore(f"pre-{target}")
            self.metrics.rollbacks += 1
            self.audit.record("rollback", node=target, to_snapshot=f"pre-{target}", trigger=failed_node)
        self.ctx.decide(failed_node, "rollback", f"rolled back '{target}' and re-running it with failure feedback",
                        "orchestrator", rationale=error[:500])
        self.log(f"  ↺ rework: rolled back '{target}', re-running with feedback from '{failed_node}'")
        self._reset([target], reason=f"rework triggered by '{failed_node}'", cascade=True)

    def _rollback_to_baseline(self, reason: str) -> None:
        self.workspace.restore("baseline")
        self.metrics.rollbacks += 1
        self.audit.record("rollback", to_snapshot="baseline", reason=reason)
        self.ctx.decide("release_readiness", "rollback", "workspace restored to baseline", "orchestrator", rationale=reason)
        self.run_status = RunStatus.ROLLED_BACK
        self.stop_reason = reason

    def _safe_stop(self, reason: str, running: dict) -> None:
        if self.run_status != RunStatus.RUNNING:
            return
        self.run_status = RunStatus.HALTED
        self.stop_reason = reason
        self.audit.record("safe_stop", reason=reason)
        self.ctx.decide("-", "safe_stop", reason, "orchestrator",
                        rationale="no further nodes dispatched; uncommitted results discarded; state persisted for resume")
        self.log(f"  ■ SAFE-STOP: {reason}")
        self._discard(running)

    def _discard(self, running: dict) -> None:
        for fut, (node_id, _, _) in list(running.items()):
            self.epoch[node_id] += 1  # late results will be ignored
            if self.status[node_id] == NodeStatus.RUNNING:
                self.status[node_id] = NodeStatus.PENDING
            fut.cancel()

    # ------------------------------------------------------------- helpers
    def _reset(self, node_ids: list[str], reason: str, cascade: bool = False) -> None:
        targets = set(node_ids)
        if cascade:
            for n in node_ids:
                targets |= self.graph.descendants(n)
        for k in self.graph.topological_order():
            if k in targets:
                self.epoch[k] += 1
                self.attempts[k] = 0
                self.use_fallback[k] = False
                self.not_before.pop(k, None)
                if self.status[k] != NodeStatus.PENDING:
                    self._set(k, NodeStatus.PENDING, reason=reason)

    def _apply_plan_change(self, node_id: str, pc: dict[str, Any]) -> None:
        template = pc.get("template", "")
        if not self.policy.allowed_dynamic_node(template) or template not in self.node_templates:
            self.audit.record("replan_denied", node=node_id, request=pc, reason="template not allowlisted by policy")
            return
        spec: NodeSpec = self.node_templates[template]()
        if spec.id in self.graph.nodes:
            return
        try:
            self.graph.insert_node(spec, after=pc.get("after", []), before=pc.get("before", []))
        except GraphError as exc:
            self.audit.record("replan_denied", node=node_id, request=pc, reason=str(exc))
            return
        for d in (self.status, self.attempts, self.executions, self.epoch):
            d.setdefault(spec.id, 0 if d is not self.status else NodeStatus.PENDING)
        self.use_fallback[spec.id] = False
        self.mutations.append(pc)
        self.metrics.replans += 1
        self.audit.record("replan", node=node_id, trigger="plan_change", inserted=spec.id, after=pc.get("after"), before=pc.get("before"), reason=pc.get("reason"))
        self.ctx.decide(node_id, "replan", f"inserted '{spec.id}' between {pc.get('after')} and {pc.get('before')}", "orchestrator", rationale=pc.get("reason", ""))
        self.log(f"  + re-plan: inserted node '{spec.id}' ({pc.get('reason', '')})")
        # A node inserted before already-completed nodes invalidates them.
        done = [b for b in pc.get("before", []) if self.status.get(b) == NodeStatus.SUCCEEDED]
        if done:
            self._reset(done, reason=f"new upstream node '{spec.id}'", cascade=True)

    def _set(self, node_id: str, status: NodeStatus, **data: Any) -> None:
        self.status[node_id] = status
        self.audit.record("node_status", node=node_id, status=status.value, **data)
        if status in (NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.BLOCKED, NodeStatus.WAITING_APPROVAL):
            mark = {"succeeded": "✓", "failed": "✗", "blocked": "⊘", "waiting_approval": "?"}[status.value]
            extra = data.get("summary") or data.get("reason") or "; ".join(data.get("reasons", []))
            self.log(f"  {mark} {node_id:<20} {status.value:<17} {str(extra)[:110]}")
        elif status == NodeStatus.RUNNING:
            self.log(f"  → {node_id:<20} running (agent={data.get('agent')}, attempt={data.get('attempt')})")

    def _record_gates(self, node_id: str, kind: str, results) -> None:
        entries = [{"kind": kind, "gate": g.gate, "passed": g.passed, "detail": g.detail} for g in results]
        self.gate_log.setdefault(node_id, []).extend(entries)
        self.audit.record("gates", node=node_id, kind=kind, results=entries)

    def _persist(self) -> None:
        state = {
            "run_id": self.run_id,
            "scenario": self.scenario,
            "run_status": self.run_status.value,
            "stop_reason": self.stop_reason,
            "status": {k: v.value for k, v in self.status.items()},
            "attempts": self.attempts,
            "executions": self.executions,
            "approval_rounds": self.approval_rounds,
            "rework_cycles": self.rework_cycles,
            "revisions": self.revisions,
            "mutations": self.mutations,
            "gate_log": self.gate_log,
            "timeline": self.timeline,
            "metrics": self.metrics.to_dict(),
            "audit_head": self.audit.head,
            # Execution configuration is part of the run's identity: resume restores it.
            "config": {"llm": self.llm.name, "approvals": getattr(self.approver, "mode", "?")},
            "pending_approvals": self.pending,
            "release_signoff": self.release_signoff,
            "context": self.ctx.to_dict(),
        }
        tmp = self.run_dir / "state.json.tmp"
        tmp.write_text(json.dumps(state, indent=1, default=str))
        tmp.replace(self.run_dir / "state.json")  # atomic

    def restore_state(self, state: dict[str, Any], reset_failed: bool) -> None:
        for pc in state.get("mutations", []):
            spec = self.node_templates[pc["template"]]()
            if spec.id not in self.graph.nodes:
                self.graph.insert_node(spec, after=pc.get("after", []), before=pc.get("before", []))
                for d in (self.attempts, self.executions, self.epoch):
                    d.setdefault(spec.id, 0)
                self.use_fallback[spec.id] = False
        self.mutations = state.get("mutations", [])
        for k, v in state["status"].items():
            s = NodeStatus(v)
            if s in (NodeStatus.RUNNING, NodeStatus.WAITING_APPROVAL, NodeStatus.SKIPPED):
                s = NodeStatus.PENDING
            if reset_failed and s in (NodeStatus.FAILED, NodeStatus.BLOCKED):
                s = NodeStatus.PENDING
            if k in state.get("pending_approvals", {}):
                s = NodeStatus.WAITING_APPROVAL
            self.status[k] = s
        self.pending = state.get("pending_approvals", {})
        self.release_signoff = state.get("release_signoff")
        self.executions.update(state.get("executions", {}))
        self.approval_rounds = state.get("approval_rounds", {})
        self.rework_cycles = state.get("rework_cycles", {})
        self.revisions = state.get("revisions", 0)
        self.gate_log = state.get("gate_log", {})
        self.timeline = state.get("timeline", [])
        m = Metrics.from_dict(state["metrics"])
        m.finished_at = None
        self.metrics = m
        self.audit.record("run_resumed", reset_failed=reset_failed, previous_stop=state.get("stop_reason"))

    def _active_seconds(self) -> float:
        session = getattr(self, "_session_start", None)
        current = (time.time() - session - self._session_wait) if session else 0.0
        return self.metrics.active_s + current

    def _finalize(self) -> RunStatus:
        if getattr(self, "_session_start", None):
            self.metrics.active_s = self._active_seconds()
            self._session_start = None
        if self.run_status == RunStatus.RUNNING and self.pending:
            self.run_status = RunStatus.AWAITING_APPROVAL
            self.stop_reason = "awaiting human approval: " + ", ".join(sorted(self.pending))
        if self.run_status == RunStatus.RUNNING:
            if all(s == NodeStatus.SUCCEEDED for s in self.status.values()):
                self.run_status = RunStatus.SUCCEEDED
            else:
                self.run_status = RunStatus.FAILED
                self.stop_reason = self.stop_reason or "graph could not complete"
        for k, s in self.status.items():
            if s not in TERMINAL and self.run_status not in (RunStatus.HALTED, RunStatus.AWAITING_APPROVAL):
                self.status[k] = NodeStatus.SKIPPED
        self.metrics.finished_at = time.time()
        (self.run_dir / "change.patch").write_text(self.workspace.diff("baseline"))
        (self.run_dir / "graph.mmd").write_text(self.graph.to_mermaid({k: v.value for k, v in self.status.items()}))
        summary = self.metrics.summary()
        (self.run_dir / "metrics.json").write_text(json.dumps(summary, indent=2))
        self.audit.record("run_finished", status=self.run_status.value, reason=self.stop_reason, metrics=summary)
        self._persist()
        history = self.run_dir.parent / "history.jsonl"
        with history.open("a") as fh:
            # Second, independent copy of the audit head (see audit.py integrity model).
            fh.write(json.dumps({"run_id": self.run_id, "scenario": self.scenario["id"], "status": self.run_status.value,
                                 "metrics": summary, "audit_head": self.audit.head,
                                 "release_signoff": self.release_signoff}) + "\n")
        return self.run_status


def _result_to_dict(r: AgentResult) -> dict[str, Any]:
    return {"artifacts": r.artifacts, "file_changes": r.file_changes, "decisions": r.decisions,
            "plan_changes": r.plan_changes, "impact": sorted(r.impact), "summary": r.summary,
            "provenance": r.provenance}


def _result_from_dict(d: dict[str, Any]) -> AgentResult:
    return AgentResult(artifacts=d["artifacts"], file_changes=d["file_changes"], decisions=d["decisions"],
                       plan_changes=d["plan_changes"], impact=set(d["impact"]), summary=d["summary"],
                       provenance=d.get("provenance", []))
