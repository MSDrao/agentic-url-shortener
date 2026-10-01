"""Human approval checkpoints.

Decisions: approve | reject | revise. `revise` carries feedback and optional
answers routed to an upstream node (e.g. overriding an assumption), which
triggers re-execution and downstream re-planning.

Providers (the mode is always explicit; nothing silently auto-approves):
  InteractiveApprover - a person decides at the terminal (human=True).
  QueuedApprover      - default when no terminal is attached. The run PAUSES at
                        the gate (status awaiting_approval) and persists the exact
                        proposal. A person records a decision with
                        `orchestrator approve <run> <node> ...`, then `resume`
                        commits the stored proposal - the agent is not re-run, so
                        what was approved is exactly what gets committed.
  SimulatedApprover   - opt-in only (`--approvals simulated`) for demos/CI.
                        Decisions come from the scenario script and are recorded
                        as `simulated:<role>` with human=False; reports flag the
                        run as NOT authorized for release. Policy can forbid it.
"""

from __future__ import annotations

import getpass
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .model import content_hash

DECISIONS = ("approve", "reject", "revise")


@dataclass
class ApprovalRequest:
    node: str
    reasons: list[str]
    summary: str
    evidence: dict[str, Any]
    round: int = 1  # how many times this gate has been asked in this run

    @property
    def request_hash(self) -> str:
        """Binds a decision to the exact proposal the human reviewed."""
        return content_hash({"node": self.node, "round": self.round, "reasons": self.reasons,
                             "summary": self.summary, "evidence": self.evidence})


@dataclass
class ApprovalDecision:
    decision: str  # approve | reject | revise
    approver: str
    comment: str = ""
    revise_target: str | None = None
    answers: dict[str, Any] = field(default_factory=dict)
    human: bool = True
    request_hash: str | None = None


class Approver:
    mode = "abstract"
    human = True

    def request(self, req: ApprovalRequest) -> ApprovalDecision | None:  # pragma: no cover - interface
        """Return a decision, or None if no decision is available yet (the run pauses)."""
        raise NotImplementedError


class SimulatedApprover(Approver):
    """Scripted stand-in for a human, for demos and CI only. Never counts as a human sign-off."""

    mode = "simulated"
    human = False

    def __init__(self, role: str, script: dict[str, Any] | None = None, default: str = "approve"):
        self.identity = f"simulated:{role}"
        self.script = script or {}
        self.default = default

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        entries = self.script.get(req.node)
        if isinstance(entries, dict):
            entries = [entries]
        e = entries[req.round - 1] if entries and req.round <= len(entries) else {}
        return ApprovalDecision(
            decision=e.get("decision", self.default),
            approver=self.identity,
            comment="[SIMULATED] " + e.get("comment", "scripted demo decision - not a human sign-off"),
            revise_target=e.get("revise_target"),
            answers=e.get("answers", {}),
            human=False,
            request_hash=req.request_hash,
        )


class InteractiveApprover(Approver):
    mode = "interactive"

    def __init__(self, identity: str | None = None, input_fn=input, print_fn=print):
        self.identity = identity or getpass.getuser()
        self.input = input_fn
        self.print = print_fn

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        p = self.print
        p("\n" + "=" * 72)
        p(f"APPROVAL REQUIRED  node={req.node}  round={req.round}  request={req.request_hash}")
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
                return ApprovalDecision("approve", self.identity, self.input("comment (optional): "),
                                        request_hash=req.request_hash)
            if choice in {"r", "reject"}:
                return ApprovalDecision("reject", self.identity, self.input("reason: "), request_hash=req.request_hash)
            if choice in {"v", "revise"}:
                target = self.input(f"send back to which node? [{req.node}]: ").strip() or req.node
                comment = self.input("feedback for the agent: ")
                raw = self.input("answers as JSON (optional, e.g. {\"Q2\": \"...\"}): ").strip()
                answers = json.loads(raw) if raw else {}
                return ApprovalDecision("revise", self.identity, comment, target, answers,
                                        request_hash=req.request_hash)


# --- queued (asynchronous) human approvals ------------------------------------

def _approvals_dir(run_dir: Path) -> Path:
    return Path(run_dir) / "approvals"


class QueuedApprover(Approver):
    mode = "queue"

    def __init__(self, run_dir: Path):
        self.dir = _approvals_dir(run_dir)

    def request(self, req: ApprovalRequest) -> ApprovalDecision | None:
        self.dir.mkdir(parents=True, exist_ok=True)
        stem = f"{req.node}-r{req.round}"
        decision_file = self.dir / f"{stem}.decision.json"
        if decision_file.exists():
            d = json.loads(decision_file.read_text())
            if d.get("request_hash") != req.request_hash:
                raise ValueError(f"decision {decision_file.name} was recorded for a different proposal "
                                 f"({d.get('request_hash')} != {req.request_hash}); refusing to apply it")
            return ApprovalDecision(d["decision"], d["approver"], d.get("comment", ""), d.get("revise_target"),
                                    d.get("answers", {}), human=True, request_hash=d["request_hash"])
        (self.dir / f"{stem}.request.json").write_text(json.dumps(
            {**asdict(req), "request_hash": req.request_hash, "requested_at": time.time()}, indent=2, default=str))
        return None


def pending_requests(run_dir: Path) -> list[dict[str, Any]]:
    d = _approvals_dir(run_dir)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.request.json")):
        if not f.with_name(f.name.replace(".request.json", ".decision.json")).exists():
            out.append({**json.loads(f.read_text()), "file": str(f)})
    return out


def record_decision(run_dir: Path, node: str, decision: str, approver: str, comment: str = "",
                    revise_target: str | None = None, answers: dict[str, Any] | None = None) -> Path:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    pending = [p for p in pending_requests(run_dir) if p["node"] == node]
    if not pending:
        raise ValueError(f"no pending approval request for node '{node}'")
    req = pending[-1]
    out = Path(req["file"]).with_name(Path(req["file"]).name.replace(".request.json", ".decision.json"))
    out.write_text(json.dumps({
        "node": node, "round": req["round"], "request_hash": req["request_hash"], "decision": decision,
        "approver": approver, "comment": comment, "revise_target": revise_target, "answers": answers or {},
        "decided_at": time.time(),
    }, indent=2))
    return out
