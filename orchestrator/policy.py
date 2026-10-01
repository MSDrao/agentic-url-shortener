"""Policy guardrails: autonomy boundaries, security, compliance, change control.

The policy is data (policy/default.yaml) so governance can change without code
changes. The engine consults it before committing any agent output.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .model import AgentResult, NodeSpec
from .scanners import SEVERITY_ORDER, Finding, scan_pii_columns, scan_python, scan_secrets


@dataclass
class Policy:
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Policy":
        return cls(yaml.safe_load(Path(path).read_text()))

    # --- accessors ------------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {}) or {}

    @property
    def budgets(self) -> dict[str, Any]:
        return self.section("budgets")

    @property
    def min_coverage(self) -> float:
        return float(self.section("quality").get("min_coverage", 80))

    @property
    def block_severity(self) -> str:
        return self.section("security").get("block_at_or_above", "high")

    def is_blocking(self, severity: str) -> bool:
        return SEVERITY_ORDER[severity] >= SEVERITY_ORDER[self.block_severity]

    # --- autonomy boundaries + pre-commit checks -----------------------
    def check_changes(self, node: NodeSpec, agent: str, changes: dict[str, str | None]) -> list[Finding]:
        findings: list[Finding] = []
        if not changes:
            return findings
        scopes = self.section("autonomy").get("write_scopes", {})
        allowed = scopes.get(agent)
        if allowed is None:
            findings.append(Finding("autonomy-boundary", "critical", "*", f"agent '{agent}' has no write permission"))
            return findings
        limit = int(self.section("autonomy").get("max_files_per_commit", 50))
        if len(changes) > limit:
            findings.append(Finding("change-size", "critical", "*", f"{len(changes)} files exceeds limit {limit}"))
        sec = self.section("security")
        comp = self.section("compliance")
        for path, content in changes.items():
            if not any(fnmatch.fnmatch(path, pat) for pat in allowed):
                findings.append(Finding("autonomy-boundary", "critical", path, f"outside write scope of '{agent}'"))
                continue
            if content is None:
                if not self.section("change_control").get("allow_deletes", False):
                    findings.append(Finding("file-deletion", "high", path, "agents may not delete files"))
                continue
            findings += scan_secrets(path, content, sec.get("secret_patterns", []))
            if path.endswith(".py"):
                findings += scan_python(path, content, sec.get("forbidden_calls", []))  # tests run too
                if not path.startswith("tests/"):
                    findings += scan_pii_columns(path, content, comp.get("pii_fields", []))
        return findings

    # --- human-in-the-loop ---------------------------------------------
    def approval_reasons(self, node: NodeSpec, result: AgentResult) -> list[str]:
        rules = self.section("approvals")
        reasons: list[str] = []
        if node.id in rules.get("always", []):
            reasons.append(f"'{node.id}' always requires human sign-off")
        for impact in sorted(result.impact):
            if impact in rules.get("on_impact", []):
                reasons.append(f"high-impact change: {impact}")
        protected = self.section("change_control").get("protected_paths", [])
        for path in sorted(result.file_changes):
            if any(fnmatch.fnmatch(path, p) for p in protected):
                reasons.append(f"change to protected path: {path}")
        if rules.get("on_unresolved_ambiguity") and "unresolved_ambiguity" in result.impact:
            reasons.append("requirement contains ambiguities resolved only by assumptions")
        return list(dict.fromkeys(reasons))

    # --- re-planning ----------------------------------------------------
    def allowed_dynamic_node(self, template: str) -> bool:
        return template in self.section("replanning").get("allowed_templates", [])
