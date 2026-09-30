"""Human approval checkpoints.

Decisions: approve | reject | revise. `revise` carries feedback and optional
answers that are routed to an upstream node (e.g. overriding an assumption),
which triggers re-execution and downstream re-planning.

Providers:
  InteractiveApprover - prompts on the terminal (a real human decides)
  ScriptedApprover    - non-interactive; decisions come from the scenario file
                        (or approve-all), and are recorded under a named identity
"""

from __future__ import annotations

import getpass
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApprovalRequest:
    node: str
    reasons: list[str]
    summary: str
    evidence: dict[str, Any]
    round: int = 1  # how many times this gate has been asked in this run


@dataclass
class ApprovalDecision:
    decision: str  # approve | reject | revise
    approver: str
    comment: str = ""
    revise_target: str | None = None
    answers: dict[str, Any] = field(default_factory=dict)


class Approver:
    def request(self, req: ApprovalRequest) -> ApprovalDecision:  # pragma: no cover - interface
        raise NotImplementedError


class ScriptedApprover(Approver):
    """Decision script keyed by node id; each entry is a list consumed per round."""

    def __init__(self, identity: str, script: dict[str, Any] | None = None, default: str = "approve"):
        self.identity = identity
        self.script = script or {}
        self.default = default

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        entries = self.script.get(req.node)
        if isinstance(entries, dict):
            entries = [entries]
        if entries and req.round <= len(entries):
            e = entries[req.round - 1]
            return ApprovalDecision(
                decision=e.get("decision", self.default),
                approver=self.identity,
                comment=e.get("comment", "scripted decision"),
                revise_target=e.get("revise_target"),
                answers=e.get("answers", {}),
            )
        return ApprovalDecision(self.default, self.identity, "auto-approved under scripted policy")


class InteractiveApprover(Approver):
    def __init__(self, identity: str | None = None, input_fn=input, print_fn=print):
        self.identity = identity or getpass.getuser()
        self.input = input_fn
        self.print = print_fn

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        p = self.print
        p("\n" + "=" * 72)
        p(f"APPROVAL REQUIRED  node={req.node}  round={req.round}")
        for r in req.reasons:
            p(f"  - {r}")
        p(f"\n{req.summary}\n")
        for k, v in req.evidence.items():
            text = v if isinstance(v, str) else json.dumps(v, indent=2, default=str)
            p(f"--- {k} ---\n{text[:2500]}")
        p("=" * 72)
        while True:
            choice = self.input("[a]pprove / [r]eject / re[v]ise ? ").strip().lower()
            if choice in {"a", "approve"}:
                return ApprovalDecision("approve", self.identity, self.input("comment (optional): "))
            if choice in {"r", "reject"}:
                return ApprovalDecision("reject", self.identity, self.input("reason: "))
            if choice in {"v", "revise"}:
                target = self.input(f"send back to which node? [{req.node}]: ").strip() or req.node
                comment = self.input("feedback for the agent: ")
                raw = self.input("answers as JSON (optional, e.g. {\"Q2\": \"...\"}): ").strip()
                answers = json.loads(raw) if raw else {}
                return ApprovalDecision("revise", self.identity, comment, target, answers)
