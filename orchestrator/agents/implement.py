"""Implementation agent: turns the design into a concrete, reviewable change set.

The backend returns edit operations; the agent applies them to an in-memory
overlay of the workspace and returns the resulting file contents. Anchored
`replace` edits must match exactly once, so a stale or hallucinated edit fails
loudly (and is retried / falls back) instead of silently corrupting code.
"""

from __future__ import annotations

import fnmatch

from ..model import AgentResult
from .base import Agent, AgentError, AgentTask


class ImplementationAgent(Agent):
    name = "implementer"
    boundary = "may propose writes only within policy write_scopes.implementer"

    def __init__(self, llm=None):
        self.llm = llm

    def run(self, task: AgentTask) -> AgentResult:
        ws = task.workspace
        design = task.inputs["design"]
        files_in_scope = sorted({f for t in design.get("tasks", []) for f in t.get("files", [])})
        current = {f: ws.read(f) for f in files_in_scope if ws.read(f) is not None}
        resp = task.llm.generate(
            "implement",
            prompt="Implement the design. Return edit operations: create {path, content}, "
                   "replace {path, search, replace} (search must match exactly once), append {path, content}.",
            context={"design": design, "test_plan": task.inputs.get("test_plan"), "files": current,
                     "feedback": task.feedback, "answers": task.answers},
            execution=task.execution,
        )
        overlay: dict[str, str | None] = {}

        def read(path: str) -> str | None:
            return overlay[path] if path in overlay else ws.read(path)

        for i, op in enumerate(resp.get("changes", []), 1):
            kind, path = op["op"], op["path"]
            if kind == "create":
                overlay[path] = op["content"]
            elif kind == "create_from_reference":
                src = task.repo_root / op.get("reference_root", "service") / path
                if not src.is_file():
                    raise AgentError(f"edit {i}: reference file not found: {path}")
                overlay[path] = src.read_text()
            elif kind == "append":
                overlay[path] = (read(path) or "") + op["content"]
            elif kind == "replace":
                content = read(path)
                if content is None:
                    raise AgentError(f"edit {i}: cannot patch missing file {path}")
                count = content.count(op["search"])
                if count != 1:
                    raise AgentError(f"edit {i}: anchor in {path} matched {count} times (expected 1)")
                overlay[path] = content.replace(op["search"], op["replace"], 1)
            else:
                raise AgentError(f"edit {i}: unknown op '{kind}'")

        changes = {p: c for p, c in overlay.items() if c != ws.read(p)}
        impact: set[str] = set()
        cc = task.policy.section("change_control")
        for p in changes:
            if any(fnmatch.fnmatch(p, pat) for pat in cc.get("schema_paths", [])):
                impact.add("schema_change")
            if any(fnmatch.fnmatch(p, pat) for pat in cc.get("api_contract_paths", [])):
                impact.add("public_api_change")
            if any(fnmatch.fnmatch(p, pat) for pat in cc.get("dependency_paths", [])):
                impact.add("dependency_change")
        src_files = sum(1 for p in changes if not p.startswith("tests/"))
        return AgentResult(
            file_changes=changes,
            artifacts={"change_set": {"summary": resp.get("summary", ""), "files": sorted(changes),
                                      "operations": len(resp.get("changes", []))}},
            impact=impact,
            summary=f"{len(changes)} files ({src_files} source, {len(changes) - src_files} test): {resp.get('summary', '')}"[:200],
        )
