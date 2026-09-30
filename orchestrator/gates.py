"""Named entry/exit gates. Nodes reference gates by name in the graph spec.

Entry gates guard preconditions before an agent is dispatched; exit gates
validate the proposed result before the engine commits it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .context import RunContext
from .graph import GraphError, WorkflowGraph
from .model import AgentResult, GateResult, NodeSpec
from .policy import Policy
from .workspace import Workspace


@dataclass
class GateInput:
    node: NodeSpec
    ctx: RunContext
    policy: Policy
    workspace: Workspace
    result: AgentResult | None = None

    def art(self, name: str):
        if self.result is not None and name in self.result.artifacts:
            return self.result.artifacts[name]
        return self.ctx.get(name)


GateFn = Callable[[GateInput], GateResult]
REGISTRY: dict[str, GateFn] = {}


def gate(name: str):
    def wrap(fn: GateFn) -> GateFn:
        REGISTRY[name] = fn
        return fn
    return wrap


def evaluate(names: list[str], gi: GateInput) -> list[GateResult]:
    out = []
    for n in names:
        fn = REGISTRY.get(n)
        if fn is None:
            out.append(GateResult(n, False, "unknown gate"))
            continue
        try:
            out.append(fn(gi))
        except Exception as exc:  # a crashing gate is a failed gate, never a pass
            out.append(GateResult(n, False, f"gate error: {exc}"))
    return out


# --- entry gates ----------------------------------------------------------

@gate("inputs_available")
def inputs_available(gi: GateInput) -> GateResult:
    missing = [c for c in gi.node.consumes if gi.ctx.latest(c) is None and not c.startswith("?")]
    return GateResult("inputs_available", not missing, f"missing: {missing}" if missing else "all inputs present")


@gate("workspace_has_code")
def workspace_has_code(gi: GateInput) -> GateResult:
    n = len(gi.workspace.files("*.py"))
    return GateResult("workspace_has_code", n > 0, f"{n} python files")


# --- exit gates -----------------------------------------------------------

@gate("spec_complete")
def spec_complete(gi: GateInput) -> GateResult:
    spec = gi.art("requirements_spec") or {}
    missing = [k for k in ("problem_statement", "functional", "acceptance_criteria") if not spec.get(k)]
    ac_ids = [a.get("id") for a in spec.get("acceptance_criteria", [])]
    if len(ac_ids) != len(set(ac_ids)):
        missing.append("unique acceptance criterion ids")
    return GateResult("spec_complete", not missing, f"missing: {missing}" if missing else f"{len(ac_ids)} acceptance criteria")


@gate("impact_identified")
def impact_identified(gi: GateInput) -> GateResult:
    ia = gi.art("impact_analysis") or {}
    n = len(ia.get("impacted", []))
    return GateResult("impact_identified", n > 0, f"{n} impacted modules")


@gate("tasks_acyclic")
def tasks_acyclic(gi: GateInput) -> GateResult:
    tasks = (gi.art("design") or {}).get("tasks", [])
    ids = {t["id"] for t in tasks}
    for t in tasks:
        unknown = set(t.get("deps", [])) - ids
        if unknown:
            return GateResult("tasks_acyclic", False, f"task {t['id']} depends on unknown {sorted(unknown)}")
    try:
        WorkflowGraph([NodeSpec(id=t["id"], stage="task", agent="-", deps=t.get("deps", [])) for t in tasks])
    except GraphError as exc:
        return GateResult("tasks_acyclic", False, str(exc))
    return GateResult("tasks_acyclic", bool(tasks), f"{len(tasks)} tasks, acyclic")


@gate("traceability")
def traceability(gi: GateInput) -> GateResult:
    """Every acceptance criterion must be covered by at least one design task."""
    spec = gi.art("requirements_spec") or {}
    tasks = (gi.art("design") or {}).get("tasks", [])
    covered = {ac for t in tasks for ac in t.get("satisfies", [])}
    missing = [a["id"] for a in spec.get("acceptance_criteria", []) if a["id"] not in covered]
    return GateResult("traceability", not missing, f"uncovered ACs: {missing}" if missing else "all ACs traced to tasks")


@gate("test_plan_covers_acs")
def test_plan_covers_acs(gi: GateInput) -> GateResult:
    spec = gi.art("requirements_spec") or {}
    plan = gi.art("test_plan") or {}
    covered = {c["ac"] for c in plan.get("cases", [])}
    missing = [a["id"] for a in spec.get("acceptance_criteria", []) if a["id"] not in covered]
    return GateResult("test_plan_covers_acs", not missing, f"ACs without tests: {missing}" if missing else "every AC has tests")


@gate("compiles")
def compiles(gi: GateInput) -> GateResult:
    errors = []
    for path, content in (gi.result.file_changes if gi.result else {}).items():
        if content is not None and path.endswith(".py"):
            try:
                compile(content, path, "exec")
            except SyntaxError as exc:
                errors.append(f"{path}:{exc.lineno}: {exc.msg}")
    return GateResult("compiles", not errors, "; ".join(errors) or "all python files compile")


@gate("has_changes")
def has_changes(gi: GateInput) -> GateResult:
    n = len(gi.result.file_changes) if gi.result else 0
    return GateResult("has_changes", n > 0, f"{n} files changed")


@gate("tests_pass")
def tests_pass(gi: GateInput) -> GateResult:
    r = gi.art("test_report") or {}
    ok = r.get("total", 0) > 0 and r.get("failed", 1) == 0 and r.get("errors", 1) == 0
    return GateResult("tests_pass", ok, f"{r.get('passed')}/{r.get('total')} passed, {r.get('failed')} failed, {r.get('errors')} errors")


@gate("coverage_min")
def coverage_min(gi: GateInput) -> GateResult:
    cov = (gi.art("test_report") or {}).get("coverage")
    threshold = gi.policy.min_coverage
    if cov is None:
        return GateResult("coverage_min", False, "coverage not measured")
    return GateResult("coverage_min", cov >= threshold, f"{cov:.1f}% (min {threshold}%)")


@gate("planned_tests_pass")
def planned_tests_pass(gi: GateInput) -> GateResult:
    """Every test the plan committed to must exist and pass (AC -> test -> result)."""
    plan = gi.art("test_plan") or {}
    report = gi.art("test_report") or {}
    passed = set(report.get("passed_tests", []))
    missing = [c["test"] for c in plan.get("cases", []) if c["test"] not in passed]
    return GateResult("planned_tests_pass", not missing, f"planned tests not passing: {missing}" if missing else f"{len(plan.get('cases', []))} planned tests passed")


@gate("no_blocking_findings")
def no_blocking_findings(gi: GateInput) -> GateResult:
    findings = (gi.art("security_report") or {}).get("findings", [])
    blocking = [f for f in findings if gi.policy.is_blocking(f["severity"])]
    return GateResult("no_blocking_findings", not blocking, f"{len(blocking)} blocking of {len(findings)} findings")


@gate("privacy_compliant")
def privacy_compliant(gi: GateInput) -> GateResult:
    pr = gi.art("privacy_review") or {}
    failed = [c["check"] for c in pr.get("checks", []) if not c["passed"]]
    return GateResult("privacy_compliant", bool(pr) and not failed, f"failed: {failed}" if failed else "all privacy checks pass")


@gate("docs_cover_routes")
def docs_cover_routes(gi: GateInput) -> GateResult:
    d = gi.art("docs_report") or {}
    missing = d.get("undocumented_routes", ["<not generated>"])
    return GateResult("docs_cover_routes", not missing, f"undocumented: {missing}" if missing else f"{d.get('routes', 0)} routes documented")


@gate("checklist_green")
def checklist_green(gi: GateInput) -> GateResult:
    items = (gi.art("release_readiness") or {}).get("checklist", [])
    red = [i["item"] for i in items if not i["passed"]]
    return GateResult("checklist_green", bool(items) and not red, f"failing: {red}" if red else f"{len(items)} checks green")
