"""Engine behaviour: parallelism, gates, retries, fallback, rework/rollback, re-planning, safe-stop."""

from __future__ import annotations

import copy
import time

from conftest import BASE_POLICY, FnAgent, produce

from orchestrator.model import AgentResult, NodeSpec, NodeStatus, RetryPolicy, RunStatus


def fast(n: int = 2) -> RetryPolicy:
    return RetryPolicy(max_attempts=n, backoff_seconds=0.01)


def test_parallel_branches_overlap_and_join_waits(make_engine):
    slow = lambda name: FnAgent(name, lambda t, n: (time.sleep(0.3), produce(name))[1])
    a, b = slow("a"), slow("b")
    join = FnAgent("j", lambda t, n: produce("j"))
    nodes = [
        NodeSpec("start", "s", "s0", produces=["s"]),
        NodeSpec("A", "s", "a", deps=["start"]),
        NodeSpec("B", "s", "b", deps=["start"]),
        NodeSpec("J", "s", "j", deps=["A", "B"], consumes=["a", "b"]),
    ]
    eng = make_engine(nodes, {"s0": FnAgent("s0", lambda t, n: produce("s")), "a": a, "b": b, "j": join})
    assert eng.run() == RunStatus.SUCCEEDED
    (a0, a1), (b0, b1) = a.spans[0], b.spans[0]
    assert a0 < b1 and b0 < a1, "A and B should run concurrently"
    assert join.spans[0][0] >= max(a1, b1), "join must wait for both branches"


def test_retry_then_success_records_mttr(make_engine):
    def flaky(t, n):
        if n == 1:
            raise RuntimeError("transient")
        return produce("x")
    eng = make_engine([NodeSpec("X", "s", "f", retry=fast(2))], {"f": FnAgent("f", flaky)})
    assert eng.run() == RunStatus.SUCCEEDED
    m = eng.metrics.summary()
    assert m["retries"] == 1 and m["incidents_recovered"] == 1 and m["mttr_s"] is not None


def test_exit_gate_failure_exhausts_retries_then_safe_stops(make_engine):
    bad = FnAgent("f", lambda t, n: produce("design", {"tasks": [{"id": "T1", "deps": ["T9"]}]}))
    nodes = [NodeSpec("X", "s", "f", exit_gates=["tasks_acyclic"], retry=fast(2)), NodeSpec("Y", "s", "f", deps=["X"])]
    eng = make_engine(nodes, {"f": bad})
    assert eng.run() == RunStatus.HALTED
    assert eng.status["X"] == NodeStatus.FAILED and bad.calls == 2
    assert eng.status["Y"] == NodeStatus.PENDING  # never dispatched, resumable


def test_fallback_agent_used_after_retries(make_engine):
    primary = FnAgent("p", lambda t, n: (_ for _ in ()).throw(RuntimeError("llm down")))
    backup = FnAgent("b", lambda t, n: produce("x"))
    eng = make_engine([NodeSpec("X", "s", "p", retry=fast(2), fallback_agent="b")], {"p": primary, "b": backup})
    assert eng.run() == RunStatus.SUCCEEDED
    assert primary.calls == 2 and backup.calls == 1
    assert eng.metrics.fallbacks == 1
    assert any(d.kind == "fallback" for d in eng.ctx.decisions)


def test_rework_rolls_back_workspace_and_reruns_upstream(make_engine):
    def write(t, n):
        body = "x = 1  # buggy\n" if n == 1 else "x = 2\n"
        return AgentResult(file_changes={"src/app.py": body}, artifacts={"change": n})

    def check(t, n):
        body = t.workspace.read("src/app.py")
        return produce("test_report", {"total": 1, "passed": int("buggy" not in body),
                                       "failed": int("buggy" in body), "errors": 0})
    writer, tester = FnAgent("writer", write), FnAgent("tester", check)
    nodes = [NodeSpec("impl", "s", "writer"),
             NodeSpec("test", "s", "tester", deps=["impl"], exit_gates=["tests_pass"],
                      retry=fast(1), on_failure="rework:impl")]
    eng = make_engine(nodes, {"writer": writer, "tester": tester})
    assert eng.run() == RunStatus.SUCCEEDED
    assert writer.calls == 2 and tester.calls == 2
    assert eng.metrics.rollbacks == 1 and eng.metrics.reworks == 1
    assert eng.workspace.read("src/app.py") == "x = 2\n"
    assert "impl" not in eng.ctx.feedback  # feedback is consumed once the node succeeds


def test_rework_budget_is_bounded(make_engine):
    writer = FnAgent("writer", lambda t, n: AgentResult(file_changes={"src/app.py": f"# v{n} buggy\n"}))
    tester = FnAgent("tester", lambda t, n: produce("test_report", {"total": 1, "passed": 0, "failed": 1, "errors": 0}))
    nodes = [NodeSpec("impl", "s", "writer"),
             NodeSpec("test", "s", "tester", deps=["impl"], exit_gates=["tests_pass"], retry=fast(1), on_failure="rework:impl")]
    eng = make_engine(nodes, {"writer": writer, "tester": tester})
    assert eng.run() == RunStatus.HALTED
    assert writer.calls == 3  # initial + max_rework_cycles(2)
    assert eng.status["test"] == NodeStatus.FAILED


def test_write_outside_scope_is_critical_and_nothing_is_committed(make_engine):
    rogue = FnAgent("writer", lambda t, n: AgentResult(file_changes={"src/ok.py": "a = 1\n", "deploy.sh": "rm -rf /\n"}))
    eng = make_engine([NodeSpec("impl", "s", "writer")], {"writer": rogue})
    assert eng.run() == RunStatus.HALTED
    assert eng.workspace.read("src/ok.py") is None and eng.workspace.read("deploy.sh") is None
    assert eng.metrics.policy_violations >= 1


def test_forbidden_call_is_retried_with_feedback(make_engine):
    def w(t, n):
        body = "y = eval('1')\n" if n == 1 else "y = 1\n"
        return AgentResult(file_changes={"src/a.py": body})
    agent = FnAgent("writer", w)
    eng = make_engine([NodeSpec("impl", "s", "writer", retry=fast(2))], {"writer": agent})
    assert eng.run() == RunStatus.SUCCEEDED
    assert agent.calls == 2 and eng.workspace.read("src/a.py") == "y = 1\n"


def test_approval_required_for_high_impact_and_reject_stops(make_engine):
    a = FnAgent("a", lambda t, n: produce("design", impact={"schema_change"}))
    nodes = [NodeSpec("design", "s", "a"), NodeSpec("next", "s", "a", deps=["design"])]
    eng = make_engine(nodes, {"a": a}, approvals={"design": {"decision": "reject", "comment": "no"}})
    assert eng.run() == RunStatus.HALTED
    assert eng.metrics.approvals_requested == 1 and eng.metrics.approvals_rejected == 1
    assert eng.ctx.latest("design") is None  # rejected output is never committed


def test_release_rejection_rolls_back_to_baseline(make_engine):
    pol = copy.deepcopy(BASE_POLICY)
    pol["approvals"]["always"] = ["release"]
    w = FnAgent("writer", lambda t, n: AgentResult(file_changes={"src/a.py": "a = 1\n"}))
    r = FnAgent("r", lambda t, n: produce("release_readiness"))
    nodes = [NodeSpec("impl", "s", "writer"), NodeSpec("release", "s", "r", deps=["impl"])]
    eng = make_engine(nodes, {"writer": w, "r": r}, policy=pol, approvals={"release": {"decision": "reject"}})
    assert eng.run() == RunStatus.ROLLED_BACK
    assert eng.workspace.read("src/a.py") is None
    assert eng.workspace.diff() == ""


def test_revise_reruns_upstream_and_invalidates_only_changed_consumers(make_engine):
    """Human revises at 'design' -> 'spec' re-runs with answers; spec changed -> 'plan' is invalidated."""
    spec = FnAgent("spec", lambda t, n: produce("spec", {"answer": (t.inputs.get("clarifications") or {}).get("Q1", "default")}))
    plan = FnAgent("plan", lambda t, n: produce("plan", {"from": t.inputs["spec"]}))
    design = FnAgent("design", lambda t, n: produce("design", impact={"schema_change"}))
    nodes = [NodeSpec("spec", "s", "spec", consumes=["?clarifications"]),
             NodeSpec("plan", "s", "plan", deps=["spec"], consumes=["spec"]),
             NodeSpec("design", "s", "design", deps=["spec", "plan"], consumes=["spec"])]
    approvals = {"design": [{"decision": "revise", "revise_target": "spec", "answers": {"Q1": "yes"}},
                            {"decision": "approve"}]}
    eng = make_engine(nodes, {"spec": spec, "plan": plan, "design": design}, approvals=approvals)
    assert eng.run() == RunStatus.SUCCEEDED
    assert spec.calls == 2 and plan.calls == 2 and design.calls == 2
    assert eng.ctx.get("plan") == {"from": {"answer": "yes"}}
    assert [v.version for v in eng.ctx.artifacts["spec"]] == [1, 2]
    assert eng.metrics.replans >= 2  # revise + artifact-changed invalidation


def test_unchanged_upstream_does_not_invalidate_downstream(make_engine):
    spec = FnAgent("spec", lambda t, n: produce("spec", {"same": True}))
    plan = FnAgent("plan", lambda t, n: produce("plan"))
    design = FnAgent("design", lambda t, n: produce("design", impact={"schema_change"}))
    nodes = [NodeSpec("spec", "s", "spec"), NodeSpec("plan", "s", "plan", deps=["spec"], consumes=["spec"]),
             NodeSpec("design", "s", "design", deps=["plan"])]
    approvals = {"design": [{"decision": "revise", "revise_target": "spec"}, {"decision": "approve"}]}
    eng = make_engine(nodes, {"spec": spec, "plan": plan, "design": design}, approvals=approvals)
    assert eng.run() == RunStatus.SUCCEEDED
    assert spec.calls == 2 and plan.calls == 1  # identical hash -> no wasted re-work


def test_dynamic_node_insertion_is_policy_gated(make_engine):
    templates = {"extra": lambda: NodeSpec("extra", "s", "x", produces=["extra"]),
                 "forbidden": lambda: NodeSpec("forbidden", "s", "x")}
    intake = FnAgent("i", lambda t, n: produce("spec", plan_changes=[
        {"template": "extra", "after": ["intake"], "before": ["impl"], "reason": "needs review"},
        {"template": "forbidden", "after": ["intake"], "before": ["impl"]}]))
    x = FnAgent("x", lambda t, n: produce("extra"))
    impl = FnAgent("impl", lambda t, n: produce("impl"))
    nodes = [NodeSpec("intake", "s", "i"), NodeSpec("impl", "s", "impl", deps=["intake"])]
    eng = make_engine(nodes, {"i": intake, "x": x, "impl": impl}, templates=templates)
    assert eng.run() == RunStatus.SUCCEEDED
    assert "extra" in eng.graph.nodes and "forbidden" not in eng.graph.nodes
    assert "extra" in eng.graph.nodes["impl"].deps
    assert x.spans[0][1] <= impl.spans[0][0]


def test_kill_switch_safe_stops_and_discards_inflight(make_engine):
    eng = None

    def slow(t, n):
        (t.run_dir / "STOP").write_text("stop")
        time.sleep(0.3)
        return produce("a")
    a = FnAgent("a", slow)
    nodes = [NodeSpec("A", "s", "a"), NodeSpec("B", "s", "a", deps=["A"])]
    eng = make_engine(nodes, {"a": a})
    assert eng.run() == RunStatus.HALTED
    assert "kill switch" in eng.stop_reason
    assert eng.ctx.latest("a") is None  # in-flight result discarded
    assert a.calls == 1


def test_attempt_budget_safe_stops(make_engine):
    pol = copy.deepcopy(BASE_POLICY)
    pol["budgets"]["max_total_attempts"] = 2
    a = FnAgent("a", lambda t, n: produce(f"a{n}"))
    nodes = [NodeSpec("A", "s", "a"), NodeSpec("B", "s", "a", deps=["A"]), NodeSpec("C", "s", "a", deps=["B"])]
    eng = make_engine(nodes, {"a": a}, policy=pol)
    assert eng.run() == RunStatus.HALTED
    assert "max_total_attempts" in eng.stop_reason


def test_audit_log_is_complete_and_verifiable(make_engine):
    from orchestrator.audit import AuditLog
    a = FnAgent("a", lambda t, n: produce("a"))
    eng = make_engine([NodeSpec("A", "s", "a")], {"a": a})
    eng.run()
    ok, msg = AuditLog.verify(eng.run_dir / "audit.jsonl")
    assert ok, msg
    events = [l for l in (eng.run_dir / "audit.jsonl").read_text().splitlines()]
    assert '"run_started"' in events[0] and '"run_finished"' in events[-1]
