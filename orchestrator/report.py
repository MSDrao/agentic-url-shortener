"""Per-run engineering summary (Markdown) generated from persisted state and audit log."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _latest(state: dict, name: str) -> Any:
    versions = state["context"]["artifacts"].get(name) or []
    return versions[-1]["content"] if versions else None


def render(run_dir: Path) -> str:
    state = json.loads((run_dir / "state.json").read_text())
    metrics = json.loads((run_dir / "metrics.json").read_text()) if (run_dir / "metrics.json").exists() else {}
    audit = [json.loads(l) for l in (run_dir / "audit.jsonl").read_text().splitlines() if l.strip()]
    sc = state["scenario"]
    spec = _latest(state, "requirements_spec") or {}
    design = _latest(state, "design") or {}
    impact = _latest(state, "impact_analysis")
    tests = _latest(state, "test_report") or {}
    sec = _latest(state, "security_report") or {}
    rr = _latest(state, "release_readiness") or {}
    privacy = _latest(state, "privacy_review")
    L: list[str] = []
    L += [f"# Engineering summary: {sc['title']}", "",
          f"- Run: `{state['run_id']}`  |  scenario: `{sc['id']}` ({sc['type']})",
          f"- Outcome: **{state['run_status'].upper()}**" + (f" (reason: {state['stop_reason']})" if state.get("stop_reason") else ""),
          f"- Release recommendation: **{rr.get('recommendation', 'n/a')}**", ""]

    L += ["## 1. Requirement understanding", "", "> " + sc["requirement"].strip().replace("\n", "\n> "), ""]
    if spec:
        L += [f"**Normalized problem:** {spec.get('problem_statement')}", ""]
        L += ["**Functional requirements**", ""] + [f"- {f}" for f in spec.get("functional", [])] + [""]
        if spec.get("non_functional"):
            L += ["**Non-functional requirements**", ""] + [f"- {f}" for f in spec["non_functional"]] + [""]
        L += ["**Acceptance criteria**", "", _table(["id", "criterion"], [[a["id"], a["text"]] for a in spec.get("acceptance_criteria", [])]), ""]
        if spec.get("ambiguities"):
            L += ["**Ambiguities -> resolution**", "",
                  _table(["id", "term", "question", "resolution", "status"],
                         [[a["id"], a.get("about", ""), a["question"], a["answer"], a["status"]] for a in spec["ambiguities"]]), ""]
        if spec.get("pii_involved"):
            L += [f"Personal-data signals detected: `{spec.get('pii_signals')}` - privacy review inserted into the plan.", ""]
        if spec.get("out_of_scope"):
            L += ["**Out of scope:** " + "; ".join(spec["out_of_scope"]), ""]

    if impact:
        L += ["## 2. Codebase reasoning (impact analysis)", "",
              f"Scanned {impact['modules_scanned']} modules; data flow: `{' -> '.join(impact['data_flow'])}`; risk **{impact['risk']}** ({impact['risk_rationale']}).", "",
              _table(["module", "reason", "matched terms"], [[i["module"], i["reason"], ", ".join(i["matched_terms"])] for i in impact["impacted"]]), "",
              "Routes: " + ", ".join(f"`{r['method']} {r['path']}`" for r in impact["routes"]), "",
              "Tables: " + "; ".join(f"`{t}`({', '.join(c)})" for t, c in impact["tables"].items()), ""]

    if design:
        L += ["## 3. Design and task decomposition", "", design.get("approach", ""), ""]
        if design.get("api_changes"):
            L += ["**API changes:** " + "; ".join(design["api_changes"]), ""]
        if design.get("schema_changes"):
            L += ["**Schema changes:** " + "; ".join(f"{s.get('table')}.{s.get('column', '')} ({s.get('change')})" for s in design["schema_changes"]), ""]
        L += [_table(["task", "title", "deps", "satisfies", "files"],
                     [[t["id"], t["title"], ", ".join(t.get("deps", [])) or "-", ", ".join(t.get("satisfies", [])), ", ".join(t.get("files", []))] for t in design.get("tasks", [])]), ""]
        if design.get("unpredicted_files"):
            L += [f"Files in the design that impact analysis did not predict (review focus): `{', '.join(design['unpredicted_files'])}`", ""]
        L += ["Execution waves (parallelizable): " + " -> ".join("[" + ", ".join(w) + "]" for w in design.get("task_waves", [])), ""]
        if design.get("alternatives_considered"):
            L += ["**Key decisions**", ""] + [f"- **{a['option']}** -> {a['decision']}. {a.get('why', '')}" for a in design["alternatives_considered"]] + [""]

    L += ["## 4. Orchestration trace", "", "```mermaid", (run_dir / "graph.mmd").read_text() if (run_dir / "graph.mmd").exists() else "", "```", ""]
    rows = []
    for e in audit:
        ev, d = e["event"], e["data"]
        if ev == "node_status" and d.get("status") in ("running", "succeeded", "failed", "blocked", "waiting_approval"):
            detail = d.get("summary") or d.get("reason") or "; ".join(d.get("reasons", [])) or (f"agent={d.get('agent')} attempt={d.get('attempt')}" if d.get("agent") else "")
            rows.append([e["seq"], e["node"], d["status"], detail])
        elif ev in ("attempt_failed", "rollback", "fallback", "replan", "replan_denied", "safe_stop", "approval_decision", "stale_result_discarded", "run_resumed"):
            detail = {k: v for k, v in d.items() if k not in ("reasons",)}
            rows.append([e["seq"], e.get("node") or "-", f"**{ev}**", json.dumps(detail, default=str)[:220]])
    L += [_table(["seq", "node", "event", "detail"], rows), ""]

    gl = state.get("gate_log", {})
    L += ["**Gates (last evaluation per node)**", ""]
    grows = []
    for node, entries in gl.items():
        last: dict[str, dict] = {}
        for g in entries:
            last[f"{g['kind']}:{g['gate']}"] = g
        for key, g in last.items():
            grows.append([node, key, "pass" if g["passed"] else "FAIL", g["detail"]])
    L += [_table(["node", "gate", "result", "detail"], grows), ""]

    decisions = state["context"]["decisions"]
    L += ["## 5. Decisions and lineage", "", _table(["id", "node", "kind", "actor", "summary"],
                                                     [[d["id"], d["node"], d["kind"], d["actor"], d["summary"]] for d in decisions]), ""]
    arts = state["context"]["artifacts"]
    L += ["**Artifact versions**", "", _table(["artifact", "version", "hash", "producer", "derived from"],
                                              [[n, v["version"], v["hash"], v["producer"], ", ".join(v["derived_from"]) or "-"] for n, vs in arts.items() for v in vs]), ""]

    L += ["## 6. Validation", ""]
    if tests:
        L += [f"Tests: **{tests.get('passed')}/{tests.get('total')} passed**, coverage **{tests.get('coverage')}%** (`{tests.get('command')}`)", ""]
        if tests.get("failures"):
            L += ["Failures in final run:"] + [f"- {f['test']}: {f['message']}" for f in tests["failures"]] + [""]
    if rr.get("traceability"):
        L += ["**Traceability: acceptance criterion -> tasks -> tests -> result**", "",
              _table(["AC", "tasks", "tests", "verified"], [[m["ac"], ", ".join(m["tasks"]), "<br>".join(m["tests"]), "yes" if m["verified"] else "NO"] for m in rr["traceability"]]), ""]
    if sec:
        L += [f"Security scan: {len(sec.get('files_scanned', []))} files, {len(sec.get('findings', []))} findings (max severity {sec.get('max_severity')}).", ""]
        if sec.get("findings"):
            L += [_table(["rule", "severity", "path", "detail"], [[f["rule"], f["severity"], f"{f['path']}:{f.get('line') or ''}", f["detail"]] for f in sec["findings"]]), ""]
    if privacy:
        L += ["**Privacy review**", "", _table(["check", "passed", "detail"], [[c["check"], c["passed"], c["detail"]] for c in privacy["checks"]]), ""]
    if rr.get("checklist"):
        L += ["**Release checklist**", "", _table(["item", "passed", "evidence"], [[c["item"], c["passed"], c["evidence"]] for c in rr["checklist"]]), ""]

    L += ["## 7. Risks and trade-offs", ""]
    if rr.get("risk_register"):
        L += [_table(["risk", "likelihood", "impact", "mitigation", "source"],
                     [[r["risk"], r.get("likelihood", ""), r.get("impact", ""), r.get("mitigation", ""), r.get("source", "")] for r in rr["risk_register"]]), ""]
    elif design.get("risks"):
        L += [_table(["risk", "mitigation"], [[r["risk"], r.get("mitigation", "")] for r in design["risks"]]), ""]
    if design.get("rollback_plan"):
        L += [f"**Rollback plan:** {design['rollback_plan']}", ""]

    L += ["## 8. Reliability metrics", "", "```json", json.dumps(metrics, indent=2), "```", ""]
    patch = run_dir / "change.patch"
    changed = rr.get("files_changed") or []
    L += ["## 9. Artifacts", "",
          f"- Reviewable change: `change.patch` ({len(patch.read_text().splitlines()) if patch.exists() else 0} lines, {len(changed)} files)",
          "- Workspace with the change applied: `workspace/`", "- Hash-chained audit log: `audit.jsonl` (verify with `python -m orchestrator verify-audit <run_id>`)",
          "- Resumable state: `state.json`; metrics: `metrics.json`; graph: `graph.mmd`; test output: `test-output/`", ""]
    return "\n".join(L)


def write(run_dir: Path) -> Path:
    out = run_dir / "SUMMARY.md"
    out.write_text(render(run_dir))
    return out
