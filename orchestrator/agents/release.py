"""Release-readiness agent: the join point that aggregates all evidence into a go/no-go."""

from __future__ import annotations

import re

from ..model import AgentResult
from .base import Agent, AgentTask


class ReleaseManagerAgent(Agent):
    name = "release_manager"
    boundary = "read-only; recommends go/no-go - a human makes the release decision"

    def run(self, task: AgentTask) -> AgentResult:
        i = task.inputs
        spec, design = i["requirements_spec"], i["design"]
        plan, tests, sec = i["test_plan"], i["test_report"], i["security_report"]
        docs, privacy = i["docs_report"], i.get("privacy_review")
        policy = task.policy

        passed = set(tests.get("passed_tests", []))
        matrix = []
        for ac in spec.get("acceptance_criteria", []):
            tasks = [t["id"] for t in design.get("tasks", []) if ac["id"] in t.get("satisfies", [])]
            cases = [c["test"] for c in plan.get("cases", []) if c["ac"] == ac["id"]]
            ok = bool(cases) and all(c in passed for c in cases)
            matrix.append({"ac": ac["id"], "text": ac["text"], "tasks": tasks, "tests": cases, "verified": ok})

        diff = task.workspace.diff("baseline", "shortener/db.py")
        added = [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
        removed = [l[1:] for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        destructive = [l.strip() for l in added if re.search(r"\b(DROP|RENAME|DELETE FROM)\b", l, re.I)]
        migrations_safe = not destructive and not any("CREATE TABLE" in l or "ALTER TABLE" in l for l in removed)

        blocking = [f for f in sec.get("findings", []) if policy.is_blocking(f["severity"])]
        checklist = [
            {"item": "full test suite green (pytest exit code 0)",
             "passed": tests.get("failed", 1) == 0 and tests.get("errors", 1) == 0 and tests.get("exit_code") == 0,
             "evidence": f"{tests.get('passed')}/{tests.get('total')}, exit_code={tests.get('exit_code')}"},
            {"item": f"coverage >= {policy.min_coverage}%", "passed": (tests.get("coverage") or 0) >= policy.min_coverage,
             "evidence": f"{tests.get('coverage')}%"},
            {"item": "every acceptance criterion verified by a passing test", "passed": all(m["verified"] for m in matrix),
             "evidence": f"{sum(m['verified'] for m in matrix)}/{len(matrix)} ACs"},
            {"item": "no blocking security findings", "passed": not blocking,
             "evidence": f"{len(sec.get('findings', []))} findings, max={sec.get('max_severity')}"},
            {"item": "migrations forward-only and additive", "passed": migrations_safe,
             "evidence": "; ".join(destructive) or "no destructive statements"},
            {"item": "API reference covers all routes", "passed": not docs.get("undocumented_routes"),
             "evidence": f"{docs.get('routes')} routes"},
            {"item": "rollback plan defined", "passed": bool(design.get("rollback_plan")),
             "evidence": (design.get("rollback_plan") or "")[:120]},
        ]
        if spec.get("pii_involved"):
            ok = bool(privacy) and all(c["passed"] for c in privacy.get("checks", []))
            checklist.append({"item": "privacy review passed", "passed": ok, "evidence": "privacy_review"})

        assumed = [a for a in spec.get("ambiguities", []) if a["status"] == "assumed"]
        risks = [dict(r, source="design") for r in design.get("risks", [])]
        risks += [{"risk": f"assumption {a['id']} unconfirmed: {a['answer']}", "likelihood": "medium",
                   "impact": "medium", "mitigation": "confirm with product owner before GA", "source": "requirements"}
                  for a in assumed]
        risks += [{"risk": f"{f['rule']} in {f['path']}", "likelihood": "low", "impact": f["severity"],
                   "mitigation": "reviewed; below blocking threshold", "source": "security"}
                  for f in sec.get("findings", []) if not policy.is_blocking(f["severity"])]
        go = all(c["passed"] for c in checklist)
        rr = {
            "recommendation": "GO" if go else "NO-GO",
            "checklist": checklist,
            "traceability": matrix,
            "risk_register": risks,
            "rollback_plan": design.get("rollback_plan"),
            "release_notes": design.get("changelog", []),
            "files_changed": task.workspace.changed_files("baseline"),
        }
        return AgentResult(artifacts={"release_readiness": rr},
                           summary=f"{rr['recommendation']}: {sum(c['passed'] for c in checklist)}/{len(checklist)} checks green")
