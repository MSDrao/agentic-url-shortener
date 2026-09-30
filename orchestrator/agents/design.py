"""Architecture/design, test planning and privacy review agents."""

from __future__ import annotations

import fnmatch

from ..graph import WorkflowGraph
from ..model import AgentResult, NodeSpec
from .base import Agent, AgentTask


class ArchitectAgent(Agent):
    name = "architect"
    boundary = "read-only; produces design (approach, contracts, task DAG, risks, rollback plan)"

    def __init__(self, llm=None):
        self.llm = llm

    def run(self, task: AgentTask) -> AgentResult:
        spec = task.inputs["requirements_spec"]
        impact = task.inputs.get("impact_analysis")
        design = task.llm.generate(
            "design",
            prompt="Produce a design: approach, API/schema changes, alternatives with a decision, "
                   "a task DAG where each task lists deps, files and the AC ids it satisfies, "
                   "risks with mitigations, and a rollback plan.",
            context={"spec": spec, "impact_analysis": impact, "feedback": task.feedback, "answers": spec.get("answers", {})},
            execution=task.execution,
        )
        tasks = design.get("tasks", [])
        # Decomposition -> execution waves (tasks in one wave are independent).
        try:
            g = WorkflowGraph([NodeSpec(id=t["id"], stage="task", agent="-", deps=t.get("deps", [])) for t in tasks])
            design["task_waves"] = g.parallel_levels()
            design["critical_path_length"] = len(design["task_waves"])
        except Exception as exc:  # the exit gate reports the precise problem
            design["task_waves"] = []
            design["decomposition_error"] = str(exc)

        if impact:  # flag files the design touches that analysis did not predict
            predicted = {i["path"] for i in impact.get("impacted", [])}
            touched = {f for t in tasks for f in t.get("files", []) if f.startswith("shortener/")}
            design["unpredicted_files"] = sorted(touched - predicted)

        impact_flags: set[str] = set()
        if design.get("schema_changes"):
            impact_flags.add("schema_change")
        if design.get("api_changes"):
            impact_flags.add("public_api_change")
        if (design.get("data_handling") or {}).get("personal_data"):
            impact_flags.add("pii")
        decisions = [
            {"kind": "design_choice", "summary": f"{a['option']}: {a['decision']}", "rationale": a.get("why", "")}
            for a in design.get("alternatives_considered", [])
        ]
        return AgentResult(
            artifacts={"design": design},
            decisions=decisions,
            impact=impact_flags,
            summary=f"{len(tasks)} tasks in {len(design.get('task_waves', []))} waves; "
                    f"{len(design.get('api_changes', []))} API / {len(design.get('schema_changes', []))} schema changes",
        )


class TestPlannerAgent(Agent):
    name = "test_planner"
    boundary = "read-only; derives test cases from acceptance criteria"

    def run(self, task: AgentTask) -> AgentResult:
        spec = task.inputs["requirements_spec"]
        cases = []
        for ac in spec.get("acceptance_criteria", []):
            for ref in ac.get("verified_by", []):
                # convention: tests named test_http_* or living in test_api.py go through HTTP
                name = ref.split("::")[-1]
                level = "integration" if "test_api.py" in ref or name.startswith("test_http_") else "unit"
                cases.append({
                    "id": f"TC-{len(cases) + 1:02d}",
                    "ac": ac["id"],
                    "test": ref,
                    "level": level,
                    "intent": ac["text"],
                })
        levels = {lv: sum(c["level"] == lv for c in cases) for lv in ("unit", "integration")}
        plan = {
            "strategy": [
                "unit: pure functions and service logic with a fake clock",
                "integration: HTTP API against real SQLite via TestClient",
                "regression: full existing suite must stay green",
                "quality gates: coverage floor, planned tests must pass, security scan",
            ],
            "cases": cases,
            "levels": levels,
        }
        return AgentResult(artifacts={"test_plan": plan}, summary=f"{len(cases)} planned cases ({levels})")


class PrivacyOfficerAgent(Agent):
    """Inserted dynamically when a requirement touches personal data."""

    name = "privacy_officer"
    boundary = "read-only; checks the design against compliance policy"

    def run(self, task: AgentTask) -> AgentResult:
        design = task.inputs["design"]
        comp = task.policy.section("compliance")
        dh = design.get("data_handling") or {}
        pii_cols = [c for sc in design.get("schema_changes", []) for c in [sc.get("column", "")]
                    if any(fnmatch.fnmatch(c.lower(), p) for p in comp.get("pii_fields", []))]
        max_ret = int(comp.get("max_retention_days", 90))
        checks = [
            {"check": "no raw personal identifiers persisted", "passed": dh.get("storage") != "raw" and not pii_cols,
             "detail": f"storage={dh.get('storage')}, pii-named columns={pii_cols}"},
            {"check": "identifiers pseudonymised with a keyed hash", "passed": bool(dh.get("keyed_hash")),
             "detail": dh.get("hashing", "")},
            {"check": f"retention <= {max_ret} days", "passed": int(dh.get("retention_days", 10**6)) <= max_ret,
             "detail": f"{dh.get('retention_days')} days"},
            {"check": "purpose limitation documented", "passed": bool(dh.get("purpose")), "detail": dh.get("purpose", "")},
            {"check": "cross-day linkage prevented", "passed": bool(dh.get("rotation")), "detail": dh.get("rotation", "")},
        ]
        return AgentResult(
            artifacts={"privacy_review": {"checks": checks, "personal_data": dh.get("personal_data", [])}},
            impact={"pii"},
            summary=f"{sum(c['passed'] for c in checks)}/{len(checks)} privacy checks passed",
        )
